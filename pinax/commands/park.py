"""
pinax park <id> --reason <reason> [--actor …] [--json]

Appends an item.parked event to the log with a reason.

The fold materialises status='parked' with the reason from the latest
item.parked event by total order.
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
    reason: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax park in repo_root.

    Appends item.parked event with the given reason.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    payload = {"item_id": item_id, "reason": reason}
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="item.parked",
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
        "reason": reason,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": "item.parked",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: item {item_id} parked (reason={reason!r}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq}")
