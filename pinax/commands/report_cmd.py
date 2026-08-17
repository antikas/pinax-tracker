"""
pinax report [--json] [--all-branches]

Read-only summary report over the event log.

ADR-004 / DESIGN.md compliance:
- PURE READ FOLD — never writes any value into the knowledge plane.
- Deterministic: same log → same report, independent of PYTHONHASHSEED,
  wall-clock, or locale.

Output sections:
  shipped  — items with status 'done', ordered by done timestamp (status_changed_at)
  parked   — items with status 'parked', with their park reason
  failed   — items with status 'blocked', with their gate qualifier
  next     — the single highest-priority ready item (from compute_next)

Human-readable by default; --json for agents.

--all-branches: folds the union of the current log plus every
unmerged local branch's committed .ergon shards (pinax.all_branches), marking
shipped/parked/failed/next entries whose latest state came only from a
branch. The default (no flag) path below is untouched.
"""

from __future__ import annotations

import json
import os
import sys

from ..fold import fold, compute_next
from ..visibility import warn_unmerged


def _notice_count(state: dict) -> int:
    superseded = state.get("claim_superseded")
    if isinstance(superseded, list):
        return len(superseded)
    warnings = state.get("report", {}).get("warnings", [])
    return sum(1 for w in warnings if isinstance(w, str) and w.startswith("claim.superseded:"))


def _source_suffix(item_id: str, item_sources: dict) -> str:
    branches = item_sources.get(item_id)
    if not branches:
        return ""
    return f"  [from: {', '.join(branches)}]"


def run(
    repo_root: str,
    as_json: bool = False,
    all_branches: bool = False,
) -> None:
    """
    Execute pinax report in repo_root.

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
    items: dict = state.get("items", {})
    notices = _notice_count(state)

    # --- shipped: items with status 'done' ---
    # Ordered by status_changed_at (done timestamp), then item_id for ties.
    shipped = [
        item for item in items.values()
        if item.get("status") == "done"
    ]
    shipped.sort(key=lambda it: (it.get("status_changed_at", ""), it["id"]))

    # --- parked: items with status 'parked' ---
    # Ordered by item_id (deterministic).
    parked = [
        item for item in items.values()
        if item.get("status") == "parked"
    ]
    parked.sort(key=lambda it: it["id"])

    # --- failed/blocked: items with status 'blocked' ---
    # "failed" in the morning-report sense = gate-blocked, needs human.
    # Ordered by item_id (deterministic).
    failed = [
        item for item in items.values()
        if item.get("status") == "blocked"
    ]
    failed.sort(key=lambda it: it["id"])

    # --- next: single highest-priority ready item ---
    next_id = compute_next(state)

    if as_json:
        report = {
            "shipped": [
                {
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "done_at": it.get("status_changed_at", ""),
                    "actor": it.get("status_changed_by", ""),
                }
                for it in shipped
            ],
            "parked": [
                {
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "reason": it.get("park_reason", ""),
                }
                for it in parked
            ],
            "failed": [
                {
                    "id": it["id"],
                    "title": it.get("title", ""),
                    "gate": it.get("gate", ""),
                }
                for it in failed
            ],
            "next": next_id,
            "notices": notices,
        }
        if all_branches:
            report["all_branches"] = True
            report["source_branches"] = source_branches
            report["item_sources"] = item_sources
        print(json.dumps(report, sort_keys=True, ensure_ascii=True))
        return

    # Human-readable output.
    print("pinax report")
    print()

    if notices:
        print(
            f"  notices: {notices} claim-reconciliation notice(s) - "
            "run 'pinax doctor' for detail"
        )
        print()

    print(f"  shipped ({len(shipped)}):")
    if shipped:
        for it in shipped:
            done_at = it.get("status_changed_at", "?")
            src = _source_suffix(it["id"], item_sources)
            print(f"    {it['id']:<24}  {it.get('title', '')[:50]:<50}  done {done_at}{src}")
    else:
        print("    (none)")
    print()

    print(f"  parked ({len(parked)}):")
    if parked:
        for it in parked:
            reason = it.get("park_reason", "") or "(no reason)"
            src = _source_suffix(it["id"], item_sources)
            print(f"    {it['id']:<24}  {it.get('title', '')[:40]:<40}  reason: {reason[:50]}{src}")
    else:
        print("    (none)")
    print()

    print(f"  failed/blocked ({len(failed)}):")
    if failed:
        for it in failed:
            gate = it.get("gate", "") or "(no gate)"
            src = _source_suffix(it["id"], item_sources)
            print(f"    {it['id']:<24}  {it.get('title', '')[:40]:<40}  gate: {gate}{src}")
    else:
        print("    (none)")
    print()

    print(f"  next:")
    if next_id:
        next_item = items.get(next_id, {})
        src = _source_suffix(next_id, item_sources)
        print(f"    {next_id}  {next_item.get('title', '')}{src}")
    else:
        print("    (none - ready queue empty)")
