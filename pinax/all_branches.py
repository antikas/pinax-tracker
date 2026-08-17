"""
pinax.all_branches — repo-wide truth-view fold across all local branches.

The companion to pinax.visibility's unmerged-branch warning: that warning
is stderr-only advisory — it tells the caller the fold is partial but never
changes what board/report/ready render.  `--all-branches` is the other half:
instead of warning about the blind spot, it FOLDS the union of the current
working-tree log plus every unmerged local branch's committed .ergon shards at
its tip, rendering the whole repo-wide picture in one pass.

Sourcing (ADR-001 / pinax.replay's documented pattern — "one fold
implementation, a second byte source"): this module adds a THIRD byte source
alongside the filesystem (pinax.fold.read_raw_events) and the single-ref
git-blob reader (pinax.replay.read_raw_events_at_ref) — a UNION of the
filesystem pool with one git-blob pool per unmerged branch tip. All raw pools
are combined and handed to the SAME determinism layer (pinax.fold.finalise_events
then pinax.fold.fold_events) every other command uses. No new sorting, dedup,
or per-type handler logic lives here.

Attribution is a pure, deterministic side computation — it never mutates the
event dicts that flow into the fold:
  - Before union, each raw event's id is recorded against the source label(s)
    that supplied a copy of it ("<current>" for the working tree, or the
    branch name for a git-blob-sourced copy) in a side table keyed by event id.
  - An event is "branch-only" when its id was NEVER supplied by "<current>" —
    i.e. it only exists because of one or more unmerged branches. An event
    present on BOTH the current branch and another branch (identical
    content-hash id) is, by construction, not branch-only: the dedupe in
    finalise_events collapses it to one entry either way, and the presence
    check here excludes it from ever being marked.
  - An item is attributed to the branch(es) that supplied its branch-only
    item.* events (all of which carry payload.item_id) — this covers both a
    wholly-new item (absent from the plain current-branch fold entirely) and
    an existing item whose LATEST state was changed only by a branch-sourced
    event (e.g. a status change that happened only on the run branch).

Deterministic: branch enumeration comes from
pinax.visibility.unmerged_tracker_refs (already sorted by branch name); a
branch whose git read fails between enumeration and read (deleted mid-run,
corrupt ref) is skipped — fail-safe, matching visibility.py's philosophy that
advisory/whole-picture context must never break the fold it decorates.
"""

from __future__ import annotations

from .fold import finalise_events, fold_events, read_raw_events
from .replay import ReplayRefError, read_raw_events_at_ref
from .visibility import unmerged_tracker_refs

_DEFAULT_LOG_SUBPATH = ".ergon/log"


def compute_all_branches_fold(
    repo_root: str,
    log_dir: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> dict:
    """
    Compute the repo-wide truth-view fold: union of the current working-tree
    log plus every unmerged local branch's committed .ergon shards at its tip.

    Returns:
      {
        "state": <union fold state — same shape as fold.fold_events output>,
        "default_state": <plain current-branch-only fold state, for reference>,
        "source_branches": [branch names actually folded in, sorted],
        "item_sources": {item_id: [branch names, sorted], ...}
            — items touched by at least one branch-only event,
        "event_sources": {event_id: [branch names, sorted], ...}
            — events that exist only on unmerged branches (never on current),
      }

    Order-independent / idempotent: branch names are enumerated in sorted
    order (visibility.unmerged_tracker_refs); the union event stream is
    produced by the same finalise_events sort+dedupe regardless of the order
    branches were unioned in (dedupe by id is order-independent by
    construction — see fold._dedupe_by_id). No wall-clock, no RNG, no
    PYTHONHASHSEED dependence.
    """
    current_raw = read_raw_events(log_dir)
    current_ids = {e["id"] for e in current_raw if e.get("id")}

    unmerged = unmerged_tracker_refs(repo_root, log_subpath)
    branch_names = sorted(name for name, _added in unmerged)

    combined_raw: list[dict] = list(current_raw)
    # event id -> list of source labels ("<current>" and/or branch names) that
    # supplied a raw copy of that id. Side table only — never merged into the
    # event dicts themselves.
    id_sources: dict[str, list[str]] = {}
    for eid in current_ids:
        id_sources.setdefault(eid, []).append("<current>")

    contributing_branches: list[str] = []
    for branch in branch_names:
        try:
            branch_raw = read_raw_events_at_ref(repo_root, branch, log_subpath)
        except ReplayRefError:
            # Fail-safe: a branch that vanished/broke between enumeration and
            # read is skipped, never breaks the whole-picture fold.
            continue
        contributing_branches.append(branch)
        for event in branch_raw:
            eid = event.get("id")
            if eid is None:
                continue
            id_sources.setdefault(eid, []).append(branch)
            combined_raw.append(event)

    union_events = finalise_events(combined_raw)
    union_state = fold_events(union_events)

    default_events = finalise_events(list(current_raw))
    default_state = fold_events(default_events)

    event_sources: dict[str, list[str]] = {}
    item_sources: dict[str, set[str]] = {}
    for event in union_events:
        eid = event["id"]
        if eid in current_ids:
            # Present on the current branch too (deduped, identical content
            # hash) — never branch-only, never marked.
            continue
        branches = sorted(set(id_sources.get(eid, [])) - {"<current>"})
        if not branches:
            continue
        event_sources[eid] = branches
        item_id = event.get("payload", {}).get("item_id")
        if item_id:
            item_sources.setdefault(item_id, set()).update(branches)

    return {
        "state": union_state,
        "default_state": default_state,
        "source_branches": sorted(contributing_branches),
        "item_sources": {iid: sorted(b) for iid, b in item_sources.items()},
        "event_sources": event_sources,
    }
