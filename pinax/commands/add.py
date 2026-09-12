"""
pinax add --title TEXT [--prefix PREFIX] [--actor ...] [--offline] [--json]

Mints an item ID, then runs the publish sequence (pinax.sync) exactly as
claim and done do: fetch (unless offline), fold the union with the remote
default branch, append the item.created event, regenerate the projection,
commit, and push. Refuses an actor handle that is not role@host before
minting anything; see pinax.sync for the exit codes and offline rules.

ADR-003: ID = <prefix>-<short base32 blake2b of (seq, title, actor, worktree_id, nonce)>
with auto-extend on collision against current fold state. The seq used
here is entropy for the id hash only, read from the local fold before the
sequence runs; it is independent of the seq the sequence itself assigns
the appended event from the union fold.

--json prints the created item as JSON (for agents).
"""

from __future__ import annotations

import json
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..fold import fold, read_events
from ..ids import mint_item_id

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; a top-level "from .. import sync" here is safe today
# (nothing imports this module that early) but would be one more module in
# that chain to break if it ever does, same risk dep.py already carries.
# Deferred to run() below, for the same reason dep.py defers it.


def run(
    repo_root: str,
    title: str,
    prefix: str = "pnx",
    actor: str | None = None,
    as_json: bool = False,
    allow_new_prefix: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax add in repo_root.

    Mints a new item ID, hands an item.created event to the publish
    sequence, and prints the result (plain or --json).

    Refuses an unseen `prefix` in a non-empty tracker so a command cannot
    mix an unrelated item namespace into the selected tracker.  An empty
    tracker is exempt because its first item establishes the prefix.
    `allow_new_prefix` explicitly permits a new prefix in an existing
    tracker.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    # Local fold and log only: the id-mint entropy and the prefix-collision
    # guard are pre-append sanity checks against what this worktree can
    # already see, unrelated to the sequence's own union fold with the
    # remote and the real seq it assigns the appended event below.
    state = fold(log_dir)
    existing_items = state.get("items", {})
    existing_ids = set(existing_items.keys())
    local_events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in local_events) + 1) if local_events else 0

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

    from .. import sync

    _actor = actor or default_actor()

    # An actor that is not role@host is refused before anything is
    # minted: mint_item_id() below would otherwise hand back a real,
    # unique item id for an item that is never actually created, and
    # run_sequence's refusal would go on to report that id as if it were
    # one - a reader could mistake it for a created item. sync.run_sequence
    # refuses the same way and is still the one place the refusal itself
    # is decided and reported; this is only a pre-check so add never mints
    # ahead of it.
    if sync.invalid_actor_message(_actor) is not None:
        item_id = ""
    else:
        # Mint the item ID (id-hash entropy only; the appended event's
        # real seq is assigned by the publish sequence's own union fold
        # below).
        item_id = mint_item_id(
            seq=next_seq,
            title=title,
            actor=_actor,
            prefix=prefix,
            existing_ids=existing_ids,
        )

    payload = {
        "item_id": item_id,
        "title": title,
        "prefix": prefix,
        "status": "queued",
    }

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.created",
        payload=payload,
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
        "title": title,
        "prefix": prefix,
        "status": "queued",
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: created item {item_id} - \"{title}\" in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']} actor={_actor}")
