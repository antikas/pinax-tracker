"""
pinax block <id> --gate <gate> [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
an item.blocked event with the gate type, then fetch (unless offline),
fold, append, commit and push. Gate must be one of: scope, decision,
destructive, proposal. Refuses an actor handle that is not role@host
before minting anything; see pinax.sync for the exit codes and offline
rules.

The fold materialises status='blocked' with the gate from the latest
item.blocked event by total order.
"""

from __future__ import annotations

import json
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.
_VALID_GATES = frozenset({"scope", "decision", "destructive", "proposal"})


def run(
    repo_root: str,
    item_id: str,
    gate: str,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax block in repo_root.

    Hands an item.blocked event with the gate type to the publish
    sequence.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if gate not in _VALID_GATES:
        print(
            f"pinax: unknown gate '{gate}'. "
            f"Valid: {', '.join(sorted(_VALID_GATES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    from .. import sync

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.blocked",
        payload={"item_id": item_id, "gate": gate},
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
        "gate": gate,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "item.blocked",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: item {item_id} blocked (gate={gate}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
