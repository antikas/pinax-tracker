"""
pinax policy claim-expiry --hours N [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as park and release do:
mint a policy.claim_expiry_set event, then fetch (unless offline), fold,
append, commit and push. Refuses an actor handle that is not role@host
before minting anything; see pinax.sync for the exit codes and offline
rules.

The event is repository-level: it names no item, so its commit subject is
the event type alone. It applies to every claim from its own position in
claim order onward (ADR-007): the policy in force for a claim is the last
such event at or before that claim, and twenty-four hours when the log
carries none before it. Setting a policy today therefore never changes how
a claim already folded on any machine.

Refusals, before anything is minted: a value that is not a positive number
of hours. Nothing else is checked here - the fold reads the event and
decides.
"""

from __future__ import annotations

import json
import math
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.


def run_claim_expiry(
    repo_root: str,
    hours: float,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax policy claim-expiry in repo_root.

    Hands a repository-level policy.claim_expiry_set event with the given
    hours to the publish sequence.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if not math.isfinite(hours) or hours <= 0:
        print(
            f"pinax: a claim expiry must be a positive number of hours, got "
            f"{hours!r}.",
            file=sys.stderr,
        )
        sys.exit(1)

    from .. import sync

    _actor = actor or default_actor()
    _hours = float(hours)

    outcome = sync.run_sequence(
        repo_root,
        event_type="policy.claim_expiry_set",
        payload={"hours": _hours},
        actor=_actor,
        ts=utc_now_iso(),
        # Repository-level: this policy is about the tracker, not an item.
        item_id="",
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = {
        "hours": _hours,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "policy.claim_expiry_set",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(
            f"pinax: claim expiry set to {_hours}h by {_actor} in {ergon_dir}"
        )
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
