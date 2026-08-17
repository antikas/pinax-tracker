"""
pinax priority <id> <rank>       -- append item.priority_set with an explicit int rank
pinax priority <id> top          -- append item.priority_set at the front of every
                                     currently-prioritised item
pinax priority <id> bump         -- append item.priority_set one step ahead of the
                                     item's own current rank (or 'top' if the item
                                     has no rank yet)

Appends an item.priority_set event to the log.  compute_next (pinax.fold)
honours the resulting priority ABOVE critical-path depth: lower rank = more
urgent.  An item with no item.priority_set event at all is unaffected and
falls back to today's (phase, -depth, age, id) ordering.

Rank resolution (top/bump) reads the CURRENT LOCAL fold once to compute a
value; the semantics from there on are identical to an explicit numeric
rank -- one item.priority_set event, fold-time last-write-wins, replay-safe.
"bump"/"top" are a CLI convenience over the same event, not a second
mechanism.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import fold, read_events

_BUMP = "bump"
_TOP = "top"


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _min_existing_priority(items: dict) -> int | None:
    """Lowest (most urgent) explicit priority currently held by any item, or None."""
    ranks = [
        item["priority"] for item in items.values()
        if isinstance(item.get("priority"), int) and not isinstance(item.get("priority"), bool)
    ]
    return min(ranks) if ranks else None


def _resolve_rank(rank_arg: str, item_id: str, items: dict) -> int:
    """
    Resolve the CLI rank argument to a concrete integer priority.

    - An integer string: used verbatim (explicit rank, no adjustment).
    - 'top': one below the current minimum prioritised rank in this repo's
      local fold (0 if nothing is prioritised yet) -- strictly ahead of
      every currently-prioritised item.
    - 'bump': one below the item's OWN current rank if it already has one;
      otherwise identical to 'top' (an unranked item has nothing of its own
      to decrement from, so a bump promotes it straight to the front).

    Raises ValueError with a caller-facing message on an invalid rank_arg.
    """
    if rank_arg == _TOP:
        current_min = _min_existing_priority(items)
        return (current_min - 1) if current_min is not None else 0

    if rank_arg == _BUMP:
        own = items.get(item_id, {}).get("priority")
        if isinstance(own, int) and not isinstance(own, bool):
            return own - 1
        current_min = _min_existing_priority(items)
        return (current_min - 1) if current_min is not None else 0

    try:
        return int(rank_arg)
    except (TypeError, ValueError):
        raise ValueError(
            f"pinax priority: invalid rank '{rank_arg}'. "
            f"Must be an integer, or '{_BUMP}'/'{_TOP}'."
        )


def run(
    repo_root: str,
    item_id: str,
    rank_arg: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax priority in repo_root.

    Validates the item exists (validate-before-append, same discipline as
    pinax dep), resolves rank_arg (explicit int / bump / top) against the
    current local fold, then appends item.priority_set with the resolved
    integer.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    state = fold(log_dir)
    items = state.get("items", {})

    if item_id not in items:
        print(
            f"pinax: unknown item '{item_id}'. Known items: {', '.join(sorted(items))}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        priority = _resolve_rank(rank_arg, item_id, items)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    payload = {"item_id": item_id, "priority": priority}
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="item.priority_set",
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
        "priority": priority,
        "rank_arg": rank_arg,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": "item.priority_set",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(
            f"pinax: item {item_id} priority set to {priority} (from {rank_arg!r}) "
            f"by {_actor} in {ergon_dir}"
        )
        print(f"       event_id={event['id'][:12]}... seq={next_seq}")
