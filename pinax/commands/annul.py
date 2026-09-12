"""
pinax annul <event-id> --reason <reason> [--actor ...] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint an
event.annulled tombstone for the given target id and reason, then fetch,
fold, append, commit and push through pinax.sync.run_sequence. Refuses an
actor handle that is not role@host and refuses to publish nothing when the
remote cannot be reached, exactly as every other mutating command's run()
does; see pinax.sync for the exit codes.

Formally and auditably retires a junk/tampered event so the fold skips it
silently on every future run instead of warning forever - append-only
preserved: the target event's raw bytes are NEVER rewritten, reordered, or
deleted from its shard. The tombstone is itself a normal, hash-verified,
totally-ordered event (ADR-001) - annulling is audit-trailed, deterministic,
idempotent, and order-independent exactly like every other event in the fold.

The target is addressed by its content-hash `id`, never by `seq` alone: seq
is only unique per-shard-per-actor, not globally across a repo's multiple log
shards (two different shards can legitimately reuse the same seq for unrelated
events), so seq cannot address one specific event on its own.

The fold materialises the annulment two ways:
1. The target event's own ADR-001 tamper-evidence WARNING (id-integrity or
   prev-chain) is suppressed for that SPECIFIC id only - every other,
   not-yet-annulled tampered event still warns exactly as before.
2. The target event's own type handler is no longer applied - its payload
   effects (e.g. "item.completed for unknown item X") are silently skipped.

append_local is the one exception to routing through the sequence.
pinax.sync calls it directly, with no fetch and no push of its own, to
tombstone its OWN just-minted event when a push it required is rejected on
every retry attempt (see pinax.sync._annul_own_event). The sequence has
already tried and failed to publish that event once; running the full
sequence again there would fetch and attempt to push a second time for an
event already known unpublishable. append_local is a plain local append,
the same shape run() had before this file was moved onto the sequence.
"""

from __future__ import annotations

import json
import os
import sys

from ..append import append_event
from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..event import mint_event
from ..fold import read_events

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.
# (pinax.sync's own reach into this file, for append_local, is already a
# deferred import inside _annul_own_event, so there is no reverse cycle.)


def run(
    repo_root: str,
    target_id: str,
    reason: str,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax annul in repo_root.

    Mints the event.annulled event tombstoning target_id with the given
    reason and hands it to the publish sequence, which owns the fetch, the
    union fold, the append, the projection, the commit and the push. Does
    NOT validate that target_id exists in the log - annulling an id that
    never appears is a harmless no-op (nothing to suppress), which keeps
    this command a pure append with no read-then-conditionally-reject step
    that could itself race with a concurrent writer.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    from .. import sync

    _actor = actor or default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="event.annulled",
        payload={"target_id": target_id, "reason": reason},
        actor=_actor,
        ts=utc_now_iso(),
        item_id=target_id,
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = {
        "target_id": target_id,
        "reason": reason,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "event.annulled",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: event {target_id} annulled (reason={reason!r}) by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")


def append_local(
    repo_root: str,
    target_id: str,
    reason: str,
    actor: str,
    as_json: bool = False,
) -> None:
    """
    Append an event.annulled tombstone straight to the local log.

    No publish sequence runs here: no fetch, no commit, no push. This is
    the pre-sequence shape pinax.sync._annul_own_event relies on to
    tombstone its own just-minted event after a required push has already
    been rejected on every attempt - see the module docstring above. Every
    other caller wanting an annulment uses run() and the sequence instead.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0
    ts = utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    payload = {"target_id": target_id, "reason": reason}
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=actor,
        etype="event.annulled",
        payload=payload,
        prev=prev,
    )
    append_event(log_dir, event, actor=actor)

    # Regenerate the projection atomically after the append (ADR-002).
    from ..projection import regenerate
    regenerate(repo_root)

    warn_if_log_ignored(repo_root)

    result = {
        "target_id": target_id,
        "reason": reason,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": actor,
        "ts": ts,
        "type": "event.annulled",
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: event {target_id} annulled (reason={reason!r}) by {actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq}")
