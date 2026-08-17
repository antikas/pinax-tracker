"""
pinax note add <item_id> --ref <ref> [--caption <text>] [--actor <actor>] [--json]

Appends a note.added event to the log.

ADR-004 / DESIGN.md enforcement (hard error at CLI write-time, not warning):
- ref MUST match ^(koine://|~/knowledge/|projects/|docs/) — it is a pointer to a
  knowledge-plane document, never knowledge content itself.
- caption is optional; if provided it MUST be <= 200 characters.

Rejection is a hard error: sys.exit(1) with a clear message.  A direct JSONL
append bypasses this check (by construction — the log is append-only); this is
the CLI-enforced boundary.
"""

from __future__ import annotations

import json
import os
import re
import sys

from ..append import append_event
from ..doctor import warn_if_log_ignored
from ..event import mint_event
from ..fold import fold
from ..projection import regenerate

# ADR-004: the typed ref pattern — pointer to a knowledge-plane document.
_REF_PATTERN = re.compile(r"^(koine://|~/knowledge/|projects/|docs/)")

# ADR-004 / DESIGN.md: caption cap.
_CAPTION_MAX = 200


def _default_actor() -> str:
    import socket
    hostname = socket.gethostname()
    return f"operator@{hostname}"


def run(
    repo_root: str,
    item_id: str,
    ref: str,
    caption: str | None,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax note add in repo_root.

    Hard-rejects at the CLI:
    - ref that does not match the typed-ref pattern (ADR-004)
    - caption that exceeds 200 characters (ADR-004 / DESIGN.md)

    On success: appends a note.added event and regenerates the projection.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print(
            "pinax: .ergon/log/ not found - run 'pinax init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- HARD VALIDATION (ADR-004 / DESIGN.md) ---

    if not _REF_PATTERN.match(ref):
        print(
            f"pinax note add: REJECTED - ref must match "
            f"^(koine://|~/knowledge/|projects/|docs/), got: {ref!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    if caption is not None and len(caption) > _CAPTION_MAX:
        print(
            f"pinax note add: REJECTED - caption exceeds {_CAPTION_MAX} characters "
            f"({len(caption)} chars). Truncate or use a ref to a vault document.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- ITEM EXISTENCE CHECK ---

    state = fold(log_dir)
    items = state.get("items", {})
    if item_id not in items:
        print(
            f"pinax note add: REJECTED - item {item_id!r} not found in the fold state.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- EMIT EVENT ---

    _actor = actor or _default_actor()

    # Compute seq: max seq in log + 1 (simple per-actor monotonic counter).
    import glob as _glob
    all_events_seq = [
        e.get("seq", 0)
        for shard in _glob.glob(os.path.join(log_dir, "*.jsonl"))
        for e in _read_shard_seq(shard)
    ]
    seq = max(all_events_seq, default=-1) + 1

    import datetime
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    payload: dict = {"item_id": item_id, "ref": ref}
    if caption is not None:
        payload["caption"] = caption

    # prev: last event id in this actor's shard (or '' if none).
    import re as _re
    shard_name = _actor.replace("@", "-").replace("/", "-").replace("\\", "-").replace(" ", "-")
    shard_path = os.path.join(log_dir, f"{shard_name}.jsonl")
    prev_id = _last_event_id(shard_path)

    event = mint_event(
        seq=seq,
        ts=ts,
        actor=_actor,
        etype="note.added",
        payload=payload,
        prev=prev_id,
    )
    append_event(log_dir, event, actor=_actor)

    # Regenerate projection atomically (ADR-002).
    regenerate(repo_root)

    warn_if_log_ignored(repo_root)

    if as_json:
        print(json.dumps({
            "event_id": event["id"],
            "item_id": item_id,
            "ref": ref,
            "root": ergon_dir,
        }))
    else:
        # visible the moment it happens.
        caption_str = f" ({caption!r})" if caption else ""
        print(f"pinax: note.added on {item_id} -> {ref}{caption_str} in {ergon_dir}")


def _read_shard_seq(shard_path: str) -> list[dict]:
    """Read seq values from a shard file (minimal parse — seq field only needed)."""
    events = []
    try:
        with open(shard_path, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for line in normalised.split(b"\n"):
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
                if isinstance(obj, dict):
                    events.append(obj)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
    except OSError:
        pass
    return events


def _last_event_id(shard_path: str) -> str:
    """Return the id of the last event in a shard, or '' if the shard is empty/absent."""
    events = _read_shard_seq(shard_path)
    if not events:
        return ""
    # Return the last valid id.
    for event in reversed(events):
        eid = event.get("id", "")
        if eid:
            return eid
    return ""
