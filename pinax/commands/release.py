"""
pinax release <id> --reason <reason> [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and park do: mint
an item.claim_released event with a reason, then fetch (unless offline),
fold, append, commit and push. Refuses an actor handle that is not
role@host before minting anything; see pinax.sync for the exit codes and
offline rules.

Any actor may release any live claim (ADR-007). The case a release exists
for is a worker that has stopped, and the actor who can say so is never
the one holding the claim; restricting the release to the owner would
leave that case unserved. The reason is required and the releasing actor
is recorded in the event, so the act is attributable.

The fold clears the item's owner, claim timestamp and claim event id from
the released claim onward and records the release outcome. The item's
status is untouched: a release says the work is free to pick up, not what
state it reached. A later claim by another actor is then the winner.

Refusals, all before anything is minted and each with one reason: an item
the log does not know, an item carrying no live claim, and an empty
reason. The local fold is read first because the live claim's owner is read
from the item; the unknown-item rule itself is pinax.targets', the same one
the publish sequence applies to every command. Reading it decides nothing
about ownership, which stays with the fold alone (ADR-006).
"""

from __future__ import annotations

import json
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..fold import fold
from ..targets import unknown_target_message

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
    Execute pinax release in repo_root.

    Validates the item and its live claim against the current local fold,
    then hands an item.claim_released event with the given reason to the
    publish sequence.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if not reason.strip():
        print(
            "pinax: a release records why a claim ended - give a non-empty "
            "--reason.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = fold(log_dir)
    items = state.get("items", {})

    # The claim's owner has to be read from the item, so release looks the
    # item up itself; the rule and its message stay with pinax.targets.
    unknown = unknown_target_message(
        "item.claim_released", {"item_id": item_id}, items=items
    )
    if unknown is not None:
        print(unknown + ".", file=sys.stderr)
        sys.exit(1)

    owner = items[item_id].get("owner")
    if not owner:
        print(
            f"pinax: item {item_id} carries no live claim, so there is "
            "nothing to release.",
            file=sys.stderr,
        )
        sys.exit(1)

    from .. import sync

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.claim_released",
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
        "released_owner": owner,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "item.claim_released",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(
            f"pinax: claim on item {item_id} by {owner} released "
            f"(reason={reason!r}) by {_actor} in {ergon_dir}"
        )
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
