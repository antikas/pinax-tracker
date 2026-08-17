"""
pinax next [--json]

Prints the single next item from the ready set, ordered by:
  (phase order, priority tier/rank, -critical_path_depth,
   age = earliest created_at + event_id, id)

Critical-path-depth ordering: within a phase, the ready item on the
longest chain of remaining `blocks`-dependent work is dispatched first.
An explicit item priority (`pinax priority`) outranks
critical-path depth; absent any priority events, ordering is unchanged.  See
pinax.fold.compute_next for the full ordering tuple and semantics.

--json prints {"item_id": ..., "title": ..., "status": ..., "phase": ...}
or {"item_id": null} if the ready queue is empty.
"""

from __future__ import annotations

import json
import os
import sys

from ..fold import fold, compute_next


def run(
    repo_root: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax next in repo_root.

    Folds the log, computes the ready set, and returns the single next item.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    state = fold(log_dir)
    next_id = compute_next(state)

    if as_json:
        if next_id is None:
            print(json.dumps({"item_id": None}, ensure_ascii=True))
        else:
            item = state.get("items", {}).get(next_id, {})
            result = {
                "item_id": next_id,
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "prefix": item.get("prefix", ""),
            }
            print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        if next_id is None:
            print("pinax: ready queue is empty - no next item.")
        else:
            item = state.get("items", {}).get(next_id, {})
            title = item.get("title", "")
            status = item.get("status", "")
            print(f"pinax: next -> {next_id}  ({status})  {title}")
