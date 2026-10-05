"""
pinax priority <id> <rank>       -- append item.priority_set with an explicit int rank
pinax priority <id> top          -- append item.priority_set at the front of every
                                     currently-prioritised item
pinax priority <id> bump         -- append item.priority_set one step ahead of the
                                     item's own current rank (or 'top' if the item
                                     has no rank yet)
[--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
an item.priority_set event with the resolved integer, then fetch (unless
offline), fold, append, commit and push. compute_next (pinax.fold) honours
the resulting priority ABOVE critical-path depth: lower rank = more
urgent. An item with no item.priority_set event at all is unaffected and
falls back to today's (phase, -depth, age, id) ordering. Refuses an actor
handle that is not role@host before minting anything; see pinax.sync for
the exit codes and offline rules.

Rank resolution (top/bump) reads the CURRENT LOCAL fold once to compute a
value; the semantics from there on are identical to an explicit numeric
rank -- one item.priority_set event, fold-time last-write-wins, replay-safe.
"bump"/"top" are a CLI convenience over the same event, not a second
mechanism.
"""

from __future__ import annotations

import json
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..fold import fold

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.

_BUMP = "bump"
_TOP = "top"


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
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax priority in repo_root.

    Resolves rank_arg (explicit int / bump / top) against the current local
    fold, then hands an item.priority_set event with the resolved integer
    to the publish sequence, which refuses an item that does not exist
    before appending anything (pinax.targets owns that rule).

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    state = fold(log_dir)
    items = state.get("items", {})

    try:
        priority = _resolve_rank(rank_arg, item_id, items)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    from .. import sync

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.priority_set",
        payload={"item_id": item_id, "priority": priority},
        actor=_actor,
        ts=utc_now_iso(),
        item_id=item_id,
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = {
        "item_id": item_id,
        "priority": priority,
        "rank_arg": rank_arg,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
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
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
