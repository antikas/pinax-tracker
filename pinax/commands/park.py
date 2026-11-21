"""
pinax park <id> --reason <reason> [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
an item.parked event with a reason, then fetch (unless offline), fold,
append, commit and push. Refuses an actor handle that is not role@host
before minting anything; see pinax.sync for the exit codes and offline
rules.

The fold materialises status='parked' with the reason from the latest
item.parked event by total order.
"""

from __future__ import annotations

import json
import os
import sys

from .. import refusal
from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.


def run(
    repo_root: str,
    item_id: str,
    reason: str,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax park in repo_root.

    Hands an item.parked event with the given reason to the publish
    sequence.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    refusal.require_tracker(log_dir)

    from .. import sync

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.parked",
        payload={"item_id": item_id, "reason": reason},
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
        "reason": reason,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "item.parked",
        "root": ergon_dir,
    }

    result.update(outcome.publication())
    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: item {item_id} parked (reason={reason!r}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
