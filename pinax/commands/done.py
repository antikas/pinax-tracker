"""
pinax done <id> --briefing <file> [--actor …] [--json]

Appends an item.completed event carrying the briefing content as a
work-record in the log payload.

The briefing is operational provenance — it is NOT knowledge-plane content.
It is stored in the log/fold state only.  Durable knowledge is projected to
the vault via 'capability-project' at a separate step (Discipline 12).

The briefing file content is read verbatim and stored in the payload.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import read_events


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def run(
    repo_root: str,
    item_id: str,
    briefing_path: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax done in repo_root.

    Reads the briefing file, appends item.completed event with briefing
    as a work-record in the payload.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(briefing_path):
        print(
            f"pinax: briefing file not found: {briefing_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(briefing_path, "r", encoding="utf-8") as fh:
        briefing_content = fh.read()

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    payload = {
        "item_id": item_id,
        "briefing": briefing_content,
    }
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="item.completed",
        payload=payload,
        prev=prev,
    )
    append_event(log_dir, event, actor=_actor)

    # Regenerate the projection atomically after the append (ADR-002).
    from ..projection import regenerate
    regenerate(repo_root)

    from ..doctor import warn_if_log_ignored
    warn_if_log_ignored(repo_root)

    result = {
        "item_id": item_id,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": "item.completed",
        "briefing_chars": len(briefing_content),
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: item {item_id} marked done by {_actor} in {ergon_dir}")
        print(
            f"       briefing={len(briefing_content)} chars "
            f"event_id={event['id'][:12]}... seq={next_seq}"
        )
