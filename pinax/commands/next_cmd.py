"""
pinax next [--json] [--under ITEM_ID]

Prints the single next item from the ready set, ordered by:
  (phase order, priority tier/rank, -critical_path_depth,
   age = earliest created_at + event_id, id)

Critical-path-depth ordering: within a phase, the ready item on the
longest chain of remaining `blocks`-dependent work is dispatched first.
An explicit item priority (`pinax priority`) outranks
critical-path depth; absent any priority events, ordering is unchanged.  See
pinax.fold.compute_next for the full ordering tuple and semantics.

--under ITEM_ID restricts the candidate set to the transitive
`parent-child` descendants of that item.

--json prints {"item_id": ..., "title": ..., "status": ..., "prefix": ...,
"under": ...} or {"item_id": null, "under": ...} if the ready queue is
empty.
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
    under: str | None = None,
) -> None:
    """
    Execute pinax next in repo_root.

    Folds the log, computes the ready set, and returns the single next
    item. `under`, when given, restricts the candidate set to the
    transitive `parent-child` descendants of that item (see
    pinax.fold.descendants); an id naming no item in the fold ends this
    command with a message on stderr and exit 1.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    state = fold(log_dir)
    try:
        next_id = compute_next(state, under=under)
    except ValueError as exc:
        print(f"pinax: {exc}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        if next_id is None:
            result = {"item_id": None, "under": under}
        else:
            item = state.get("items", {}).get(next_id, {})
            result = {
                "item_id": next_id,
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "prefix": item.get("prefix", ""),
                "under": under,
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
