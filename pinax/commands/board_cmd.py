"""Render the current repository board from the deterministic event-log fold.

`pinax board` is read-only and prints the same Markdown representation used by
the committed board projection. `--all-branches` folds the current log together
with committed shards from unmerged local branches and marks branch-only items.
"""
from __future__ import annotations

import json
import os
import sys

from ..fold import fold, state_to_json_safe
from ..projection import render_board
from ..visibility import warn_unmerged


def run(
    repo_root: str,
    as_json: bool = False,
    all_branches: bool = False,
) -> None:
    """
    Execute pinax board in repo_root.

    Read-only fold — no filesystem writes.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print(
            "pinax: .ergon/log/ not found - run 'pinax init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    if all_branches:
        from ..all_branches import compute_all_branches_fold

        result = compute_all_branches_fold(repo_root, log_dir)
        state = result["state"]
        item_sources = result["item_sources"]

        if as_json:
            payload = {
                "repo": os.path.basename(os.path.normpath(repo_root)),
                "state": state_to_json_safe(state),
                "all_branches": True,
                "source_branches": result["source_branches"],
                "item_sources": item_sources,
            }
            print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
            return

        sys.stdout.write(render_board(state, item_sources=item_sources))
        return

    warn_unmerged(repo_root)
    state = fold(log_dir)

    if as_json:
        payload = {
            "repo": os.path.basename(os.path.normpath(repo_root)),
            "state": state_to_json_safe(state),
        }
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
        return

    # Human-readable: byte-identical to the committed .ergon/board.md content
    # for the same log state (same renderer, same fold).
    sys.stdout.write(render_board(state))
