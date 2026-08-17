"""
pinax.append — locked, LF-normalised append to a JSONL shard.

ADR-001 / ADR-002 compliance:
- Append a single line + LF under an OS file lock.
- msvcrt.locking on Windows (os.name == 'nt'); fcntl.flock on POSIX — both stdlib.
- LF-normalised: the stored line always uses LF, regardless of platform autocrlf.
- Torn-trailing-line tolerance: the fold (pinax.fold) handles this; the appender
  never needs to inspect the existing content.

Shard key:
  The default shard is named after the actor handle, for example
  actor@host becomes actor-host.jsonl. The shard is actor-scoped, which keeps
  concurrent writers on separate files while the fold remains shard-agnostic.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

from .event import serialise


class AppendLeakGuardError(RuntimeError):
    """
    Raised by append_event when an event looks like a test/fixture or
    foreign-project leak being appended into a real, non-isolated log
    directory. The guard rejects clear fixture markers only in persistent
    logs. Isolated temporary directories remain available for hermetic tests.
    """


# Pinax IDs are always <lowercase-prefix>-<lowercase base32 hash>. By contrast,
# an external ticket tracker's scheme uses an uppercase prefix, a dash, then
# plain digits (e.g. "T-001", "CASE-156") — is definitionally foreign to
_FOREIGN_ID_RE = re.compile(r"^[A-Z]{1,8}-\d+$")

# Payload keys across the type handlers (pinax/fold.py) that carry a
# reference to another event/item's id.
_ID_PAYLOAD_KEYS = (
    "item_id", "target_id", "parent_id", "child_id",
    "from_id", "to_id", "repo_id",
)


def _is_isolated_log_dir(log_dir: str) -> bool:
    """
    True if log_dir resolves inside the OS temp tree. Temporary directories
    are isolated from persistent tracker logs, so fixture records are allowed
    there without affecting a repository.

    Anything NOT under the OS temp tree is treated as "a real, persistent
    log directory" for guard purposes — this covers both the actual
    working repo and an linked worktree clone alike, without hardcoding
    any particular repo path.
    """
    try:
        real_log_dir = os.path.realpath(log_dir)
        real_tmp = os.path.realpath(tempfile.gettempdir())
    except OSError:
        return False
    try:
        return os.path.commonpath([real_log_dir, real_tmp]) == real_tmp
    except ValueError:
        # commonpath raises on e.g. mixed Windows drives — different
        # drives can never be "under" the same temp tree.
        return False


def _test_leak_reason(event: dict, actor: str) -> str | None:
    """
    Return a short human-readable reason if `event`/`actor` matches an
    obvious test-fixture or foreign-project marker pattern; None if clean.

    Two independent signals, either sufficient on its own:
      1. actor contains "test" (case-insensitive) — the general
         test-fixture marker.
      2. a payload id field (see _ID_PAYLOAD_KEYS) has the foreign
         <UPPER>-<digits> ticket shape (see _FOREIGN_ID_RE) — the exact
         shape of an unrelated ticket identifier such as "T-001".
    """
    if "test" in (actor or "").lower():
        return f"actor {actor!r} contains a test marker"

    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in _ID_PAYLOAD_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and _FOREIGN_ID_RE.match(value):
                return (
                    f"payload.{key}={value!r} has a foreign ticket-id shape "
                    "(UPPER-prefix + digits), not this repo's own id scheme "
                    "(lowercase-prefix + base32 hash, ADR-003)"
                )
    return None


def _shard_name_for_actor(actor: str) -> str:
    """
    Derive a JSONL shard filename from an actor string.

    'actor@host' → 'actor-host.jsonl'
    '@' replaced with '-'; other filesystem-unsafe chars replaced with '-'.
    """
    safe = actor.replace("@", "-").replace("/", "-").replace("\\", "-").replace(" ", "-")
    return f"{safe}.jsonl"


def _append_locked_posix(path: str, line: bytes) -> None:
    """POSIX: open/create in append mode, flock for exclusive write, append, unlock."""
    import fcntl  # type: ignore[import]

    with open(path, "ab") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _append_locked_windows(path: str, line: bytes) -> None:
    """
    Windows: acquire the OS lock FIRST, then seek to EOF, write, unlock.

    msvcrt.locking locks a byte range starting at the file's current position.

    The lock MUST be acquired before seeking to EOF.
    Acquiring the lock before seeking makes the end-of-file position exclusive:
      1. Open 'r+b' at position 0.
      2. Lock a block at position 0 large enough to serialise writers (len(line)).
      3. Under the lock, seek to the true current EOF.
      4. Write there; flush; fsync.
      5. Seek back to 0; unlock.

    Only one writer at a time determines the current EOF.

    If the file does not exist, create it first then reopen for locking.
    """
    import msvcrt  # type: ignore[import]

    # Ensure file exists.
    if not os.path.exists(path):
        open(path, "ab").close()

    lock_size = len(line)

    # Use 'r+b' so we can seek to an explicit position for msvcrt.locking.
    with open(path, "r+b") as fh:
        # Step 1: position at 0 for the lock range.
        fh.seek(0)
        # Step 2: acquire the lock BEFORE reading EOF — blocks until the lock is free.
        # LK_NBLCK raises OSError immediately if busy; LK_LOCK retries.
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, lock_size)
        except OSError:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, lock_size)
        try:
            # Step 3: under the lock, seek to the true current EOF.
            write_offset = fh.seek(0, 2)
            # Step 4: write at EOF; flush; fsync.
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            # Step 5: seek back to lock start (0) before unlocking.
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, lock_size)
            except OSError:
                pass  # Best-effort unlock.


def append_event(log_dir: str, event: dict, actor: str | None = None) -> str:
    """
    Serialise event and append it to the appropriate shard under an OS file lock.

    Returns the path of the shard file written.

    The shard is selected by actor (defaulting to event['actor']).
    The line is always LF-terminated regardless of platform settings.
    """
    _actor = actor if actor is not None else event.get("actor", "default")

    # foreign-project-looking event into a real, non-isolated log
    # directory.  Scoped off entirely inside the OS temp tree, so every
    # hermetic test in this suite (all of which write to
    # tempfile.mkdtemp()/tmp_path) is unaffected.
    if not _is_isolated_log_dir(log_dir):
        leak_reason = _test_leak_reason(event, _actor)
        if leak_reason is not None:
            raise AppendLeakGuardError(
                "pinax: refusing to append a test/foreign-looking event into "
                f"a live log directory ({log_dir}): {leak_reason}. If this is "
                "genuinely intentional, write it into a directory under the "
                "OS temp tree instead (tempfile.mkdtemp()/tmp_path) — that is "
                "the isolated sandbox this guard exempts."
            )

    shard_name = _shard_name_for_actor(_actor)
    shard_path = os.path.join(log_dir, shard_name)

    line_str = serialise(event)
    # LF-normalise: ensure no CRLF in the serialised line (should be none from
    # json.dumps, but guard against any future change), then append LF.
    line_bytes = line_str.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8") + b"\n"

    if os.name == "nt":
        _append_locked_windows(shard_path, line_bytes)
    else:
        _append_locked_posix(shard_path, line_bytes)

    return shard_path
