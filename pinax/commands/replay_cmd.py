"""
pinax replay --at <ref> [--json]

Time-travel fold: reconstruct Pinax state exactly as it existed at
any git reference (a branch, tag, or commit sha) — NOT the current working
tree, which may have advanced past <ref>.

ADR-001 / DESIGN.md: "Replay = fold the event log up to a git ref; no
secondary state store is needed."  The shard bytes are sourced from the git
object store as committed at <ref>, then folded with the identical
determinism layer (pinax.fold.finalise_events / fold_events) every other
command uses — see pinax.replay for the sourcing half.

PURE READ — never writes to the log, the projection, the index, or the
working tree.  A bad <ref> fails clearly (exit 1, message on stderr) rather
than silently falling back to HEAD or printing partial state.
"""

from __future__ import annotations

import json
import sys

from ..fold import compute_next, state_to_json_safe
from ..replay import ReplayRefError, fold_at_ref


def run(
    repo_root: str,
    ref: str,
    as_json: bool = False,
) -> None:
    """
    Execute pinax replay --at <ref> in repo_root.

    Read-only fold sourced from git history — no filesystem writes.
    """
    try:
        state = fold_at_ref(repo_root, ref)
    except ReplayRefError as exc:
        print(f"pinax replay: {exc}", file=sys.stderr)
        sys.exit(1)

    items: dict = state.get("items", {})

    if as_json:
        payload = {
            "at": ref,
            "state": state_to_json_safe(state),
        }
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return

    # Human-readable output.
    print(f"pinax replay --at {ref}")
    print()

    if not items:
        print("  (no items exist at this ref)")
        return

    print(f"  items ({len(items)}):")
    for item_id in sorted(items.keys()):
        item = items[item_id]
        status = item.get("status", "?")
        title = item.get("title", "")[:50]
        print(f"    {item_id:<24}  {status:<12}  {title}")
    print()

    next_id = compute_next(state)
    print("  next (as of this ref):")
    if next_id:
        next_item = items.get(next_id, {})
        print(f"    {next_id}  {next_item.get('title', '')}")
    else:
        print("    (none - ready queue empty at this ref)")
