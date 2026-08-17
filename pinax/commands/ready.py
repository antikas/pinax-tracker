"""Print the deterministic set of items eligible for dispatch.

An item is ready when it is queued or ready, all `blocks` predecessors are
done, and it is not in a dependency cycle. `--all-branches` evaluates the
union of the current log and unmerged local branches.
"""
from __future__ import annotations

import json
import os
import sys

from ..fold import fold, compute_ready
from ..visibility import warn_unmerged


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def run(
    repo_root: str,
    actor: str | None = None,
    as_json: bool = False,
    all_branches: bool = False,
) -> None:
    """
    Execute pinax ready in repo_root.

    Folds the log and computes the ready set.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    item_sources: dict = {}
    source_branches: list = []

    if all_branches:
        from ..all_branches import compute_all_branches_fold

        result = compute_all_branches_fold(repo_root, log_dir)
        state = result["state"]
        item_sources = result["item_sources"]
        source_branches = result["source_branches"]
    else:
        warn_unmerged(repo_root)
        state = fold(log_dir)

    ready_ids = compute_ready(state)

    if as_json:
        if all_branches:
            payload = {
                "ready": ready_ids,
                "all_branches": True,
                "source_branches": source_branches,
                "item_sources": {
                    iid: item_sources[iid] for iid in ready_ids if iid in item_sources
                },
            }
            print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        else:
            print(json.dumps(ready_ids, ensure_ascii=True))
    else:
        if ready_ids:
            for item_id in ready_ids:
                item = state.get("items", {}).get(item_id, {})
                title = item.get("title", "")
                status = item.get("status", "")
                branches = item_sources.get(item_id)
                src = f"  [from: {', '.join(branches)}]" if branches else ""
                print(f"  {item_id}  {status}  {title}{src}")
        else:
            print("pinax: ready queue is empty.")
