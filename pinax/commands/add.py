"""
pinax add — mint an item ID and append item.created event.

ADR-003: ID = <prefix>-<short base32 blake2b of (seq, title, actor, worktree_id, nonce)>
with auto-extend on collision against current fold state.

--json prints the created item as JSON (for agents).
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import fold, read_events
from ..ids import mint_item_id


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    hostname = socket.gethostname()
    return f"operator@{hostname}"


def run(
    repo_root: str,
    title: str,
    prefix: str = "pnx",
    actor: str | None = None,
    as_json: bool = False,
    allow_new_prefix: bool = False,
) -> None:
    """
    Execute pinax add in repo_root.

    Mints a new item ID, appends an item.created event to the log,
    and prints the result (plain or --json).

    Refuses an unseen `prefix` in a non-empty tracker so a command cannot
    mix an unrelated item namespace into the selected tracker.  An empty
    tracker is exempt because its first item establishes the prefix.
    `allow_new_prefix` explicitly permits a new prefix in an existing
    tracker.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    # Read current fold state to get seq counter + existing item IDs for collision check.
    events = read_events(log_dir)
    # Next seq = max existing seq + 1, or 0 if empty.
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    # Collect existing item IDs for collision auto-extend.
    state = fold(log_dir)
    existing_items = state.get("items", {})
    existing_ids = set(existing_items.keys())

    # discipline as the missing-.ergon/log check above and pinax priority's
    # unknown-item check) -- nothing is appended before this passes.
    if existing_items and not allow_new_prefix:
        prefix_marker = f"{prefix}-"
        prefix_seen = any(iid.startswith(prefix_marker) for iid in existing_ids)
        if not prefix_seen:
            known_prefixes = sorted({iid.split("-", 1)[0] for iid in existing_ids if "-" in iid})
            print(
                f"pinax add: REJECTED - prefix {prefix!r} has never appeared among "
                f"this tracker's {len(existing_items)} existing item(s) at "
                f"{ergon_dir} (known prefixes: {', '.join(known_prefixes) or 'none'}). "
                "This looks like a tracker mis-bind (wrong repo root resolved from "
                "CWD) rather than a legitimate new prefix -- see 'pinax doctor' and "
                "the --root/PINAX_ROOT pin. If this IS a genuine first use of a new "
                "prefix, pass --allow-new-prefix.",
                file=sys.stderr,
            )
            sys.exit(1)

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    # Determine prev: the most recent event id in this actor's shard.
    # For the simple non-sharded lookup, use the last event in total order.
    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    # Mint the item ID.
    item_id = mint_item_id(
        seq=next_seq,
        title=title,
        actor=_actor,
        prefix=prefix,
        existing_ids=existing_ids,
    )

    # Build and append the event.
    payload = {
        "item_id": item_id,
        "title": title,
        "prefix": prefix,
        "status": "queued",
    }
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="item.created",
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
        "title": title,
        "prefix": prefix,
        "status": "queued",
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible at the command boundary, before the event is appended.
        print(f"pinax: created item {item_id} - \"{title}\" in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq} actor={_actor}")
