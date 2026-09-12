"""
pinax claim <id> [--actor <actor>] [--json]

Records an item.claimed event through the publish sequence (pinax.sync):
fetch, fold the union with the remote default branch, append, commit and
push.  The fold materialises claim ownership via fold-time reconciliation
(ADR-003): if two item.claimed events exist for the same item, the earliest
(ts, actor, id) wins; the loser folds to claim.superseded + a report warning.
No cross-worktree lock needed.

A claim exists so the other machines see the item as taken, so this command
requires the remote: it fails when the sequence published nothing.

It is also the one command that asks the sequence to guard the clock: a
claim whose own timestamp sits behind the newest published event by more
than the tolerance is refused with exit 6 before anything is minted, and a
claim the fold reports superseded after its push ends with exit 3. Both
rules live in pinax.sync; this module only asks for the first and reports
what the sequence decided.
"""

from __future__ import annotations

import json
import os
import sys

from .. import sync


def run(
    repo_root: str,
    item_id: str,
    actor: str | None = None,
    as_json: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax claim in repo_root.

    Mints the item.claimed event and hands it to the publish sequence, which
    owns the fetch, the union fold, the append, the projection, the commit
    and the push, and which ends the command on any outcome that published
    nothing.  Does NOT resolve the claim here - the fold is the single
    source of truth.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    # pinax.doctor owns the actor and timestamp helpers every mutating
    # command shares; the import is deferred here for the same reason the
    # log-ignored probe below is, to stay clear of the projection import
    # chain this module already sits on.
    from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.claimed",
        payload={"item_id": item_id},
        actor=_actor,
        ts=utc_now_iso(),
        item_id=item_id,
        requires_remote=True,
        guard_clock=True,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event

    warn_if_log_ignored(repo_root)

    result = {
        "item_id": item_id,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "item.claimed",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: claimed item {item_id} by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")
