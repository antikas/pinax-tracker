"""
pinax block <id> --gate <gate> [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
an item.blocked event with the gate type, then fetch (unless offline),
fold, append, commit and push. Gate must be one of: scope, decision,
destructive, proposal. Refuses an actor handle that is not role@host
before minting anything; see pinax.sync for the exit codes and offline
rules.

--owner-gate CLASS records an owner-gate class on the item. Whether it is
required, and whether it is checked, is the project's process
(pinax.owner_gates): under flow, the default, nothing is checked and a
block without the option appends exactly the event it always did; under
strict, a block for the owner without a listed class ends with exit 8 and
nothing minted. The check runs inside the publish sequence on the union
fold, so a process set on another clone already binds.

The fold materialises status='blocked' with the gate, and the class when
one is given, from the latest item.blocked event by total order.
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
    owner_gate: str | None = None,
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

    from .. import owner_gates, sync

    _actor = actor or default_actor()
    payload = {"item_id": item_id, "gate": gate}

    def _policy_check(state: dict) -> str | None:
        refusal = owner_gates.block_refusal(state, gate, owner_gate)
        if refusal is None:
            recorded = owner_gates.recorded_class(state, owner_gate)
            if recorded is not None:
                # Only a block given a class carries the key, so a block
                # without one is the same event it was before classes existed.
                payload["owner_gate"] = recorded
        return refusal

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.blocked",
        payload=payload,
        actor=_actor,
        ts=utc_now_iso(),
        item_id=item_id,
        offline=offline,
        policy_check=_policy_check,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)
    recorded_class = event["payload"].get("owner_gate")

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
    if recorded_class is not None:
        result["owner_gate"] = recorded_class

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        shown = f"gate={gate}" + (
            f", owner gate: {recorded_class}" if recorded_class is not None else ""
        )
        print(f"pinax: item {item_id} blocked ({shown}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
