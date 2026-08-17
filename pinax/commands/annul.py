"""
pinax annul <event-id> --reason <reason> [--actor …] [--json]

Appends an event.annulled tombstone to the log.

Formally and auditably retires a junk/tampered event so the fold skips it
silently on every future run instead of warning forever — append-only
preserved: the target event's raw bytes are NEVER rewritten, reordered, or
deleted from its shard.  The tombstone is itself a normal, hash-verified,
totally-ordered event (ADR-001) — annulling is audit-trailed, deterministic,
idempotent, and order-independent exactly like every other event in the fold.

The target is addressed by its content-hash `id`, never by `seq` alone: seq
is only unique per-shard-per-actor, not globally across a repo's multiple log
shards (two different shards can legitimately reuse the same seq for unrelated
events), so seq cannot address one specific event on its own.

The fold materialises the annulment two ways:
1. The target event's own ADR-001 tamper-evidence WARNING (id-integrity or
   prev-chain) is suppressed for that SPECIFIC id only — every other,
   not-yet-annulled tampered event still warns exactly as before.
2. The target event's own type handler is no longer applied — its payload
   effects (e.g. "item.completed for unknown item X") are silently skipped.
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
    target_id: str,
    reason: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax annul in repo_root.

    Appends an event.annulled event tombstoning target_id with the given
    reason.  Does NOT validate that target_id exists in the log — annulling
    an id that never appears is a harmless no-op (nothing to suppress), which
    keeps this command a pure append with no read-then-conditionally-reject
    step that could itself race with a concurrent writer.
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

    payload = {"target_id": target_id, "reason": reason}
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="event.annulled",
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
        "target_id": target_id,
        "reason": reason,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": "event.annulled",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: event {target_id} annulled (reason={reason!r}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq}")
