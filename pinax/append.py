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
  actor@host becomes actor-host.jsonl, with the host in lower case. An
  existing shard whose name differs only by case is reused, so the log never
  gains a pair of names that a case-insensitive file system cannot tell
  apart. The shard is actor-scoped, which keeps concurrent writers on
  separate files while the fold remains shard-agnostic.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

from .event import parse_line, serialise


class AppendLeakGuardError(RuntimeError):
    """
    Raised by append_event when an event looks like a test/fixture or
    foreign-project leak being appended into a real, non-isolated log
    directory. The guard rejects clear fixture markers only in persistent
    logs. Isolated temporary directories remain available for hermetic tests.
    """


# Pinax IDs are always <lowercase-prefix>-<lowercase base32 hash>. By contrast,
# an external ticket tracker's scheme uses an uppercase prefix, a dash, then
# plain digits (e.g. "T-001", "CASE-156"). An id of that shape is foreign to
# Pinax, so an event that carries one is a leak from another project's tracker.
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


def normalise_actor(actor: str) -> str:
    """
    Lower-case the host part of 'role@host'; the role part is kept exactly.

    'coordinator@Laptop' -> 'coordinator@laptop'. An actor with no '@' is
    returned unchanged.
    """
    role, sep, host = actor.partition("@")
    if not sep:
        return actor
    return f"{role}@{host.lower()}"


def shard_path_for_actor(log_dir: str, actor: str) -> str:
    """
    The shard file an append for actor goes to, with its name as on disk.

    The candidate is the shard name of the normalised actor. A listed file of
    exactly that name wins; otherwise the first, in sorted order, of the
    listed names ending in exactly .jsonl that equal it ignoring case;
    otherwise a new file of the candidate name.
    """
    candidate = _shard_name_for_actor(normalise_actor(actor))
    # os.path.exists ignores case on Windows, so only the directory listing
    # can tell which spelling is on disk; git stages that exact spelling.
    try:
        listed = os.listdir(log_dir)
    except OSError:
        listed = []
    if candidate in listed:
        return os.path.join(log_dir, candidate)
    folded = candidate.casefold()
    # The fold reads only *.jsonl, which matches case-sensitively on POSIX,
    # so a name such as actor-host.JSONL would never be read back.
    matches = sorted(
        name
        for name in listed
        if name.endswith(".jsonl") and name.casefold() == folded
    )
    return os.path.join(log_dir, matches[0] if matches else candidate)


def prev_for_actor(log_dir: str, actor: str) -> str:
    """
    The prev for the next event whose actor field is actor.

    That is the id of the last event, in file order, whose actor field equals
    actor exactly, in the shard shard_path_for_actor picks for actor; or the
    empty string when that shard holds none. The predecessor check works per
    shard and actor, so a prev taken from any other shard would be a broken
    link in the shard the event is written to.
    """
    shard_path = shard_path_for_actor(log_dir, actor)
    try:
        with open(shard_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    prev = ""
    for line in normalised.split(b"\n"):
        if not line:
            continue
        event = parse_line(line)
        if not isinstance(event, dict):
            continue
        eid = event.get("id")
        if isinstance(eid, str) and eid and event.get("actor") == actor:
            prev = eid
    return prev


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

    shard_path = shard_path_for_actor(log_dir, _actor)

    line_str = serialise(event)
    # LF-normalise: ensure no CRLF in the serialised line (should be none from
    # json.dumps, but guard against any future change), then append LF.
    line_bytes = line_str.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8") + b"\n"

    if os.name == "nt":
        _append_locked_windows(shard_path, line_bytes)
    else:
        _append_locked_posix(shard_path, line_bytes)

    return shard_path
