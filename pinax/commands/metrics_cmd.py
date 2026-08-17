"""
pinax metrics [--json]

Read-only fold over the event log that emits build-flywheel metrics.

ADR-004 / DESIGN.md: this command never writes any metric value into
~/knowledge/, current-focus.md, sources.toml, or any knowledge-plane file.
It folds and prints; nothing else.

Output (human-readable by default, --json for agents):
- total_items, by_status breakdown
- events_total
- cycle times (items that reached 'done': creation → done elapsed seconds)
- park reasons (parked items and their reason)
- gate counts (item.blocked events by gate type)
- audit verdicts (item.audit_result events by verdict)
- note_added_count
- claim_superseded_count
- ready_queue_size
"""

from __future__ import annotations

import json
import os
import sys

from ..metrics import compute_metrics


def run(
    repo_root: str,
    as_json: bool = False,
) -> None:
    """
    Execute pinax metrics in repo_root.

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

    m = compute_metrics(log_dir)

    if as_json:
        print(json.dumps(m, sort_keys=True, ensure_ascii=True))
        return

    # Human-readable output.
    print(f"pinax metrics")
    print(f"  total_items      : {m['total_items']}")
    print(f"  events_total     : {m['events_total']}")
    print(f"  ready_queue_size : {m['ready_queue_size']}")
    print(f"  items_done       : {m['items_done']}")
    print(f"  items_parked     : {m['items_parked']}")
    print(f"  items_blocked    : {m['items_blocked']}")
    print(f"  note_added_count : {m['note_added_count']}")
    print(f"  claim_superseded : {m['claim_superseded_count']}")

    if m["by_status"]:
        print(f"  by_status:")
        for status, count in sorted(m["by_status"].items()):
            print(f"    {status:20s} {count}")

    if m["gate_counts"]:
        print(f"  gate_counts:")
        for gate, count in sorted(m["gate_counts"].items()):
            print(f"    {gate:20s} {count}")

    if m["audit_verdicts"]:
        print(f"  audit_verdicts:")
        for verdict, count in sorted(m["audit_verdicts"].items()):
            print(f"    {verdict:20s} {count}")

    if m["cycle_times"]:
        print(f"  cycle_times (done items):")
        for ct in m["cycle_times"]:
            print(f"    {ct['item_id']:20s} {ct['elapsed_seconds']:>8}s  "
                  f"({ct['created_at']} -> {ct['done_at']})")

    if m["park_reasons"]:
        print(f"  park_reasons:")
        for pr in m["park_reasons"]:
            reason = pr["reason"] or "(none)"
            print(f"    {pr['item_id']:20s} {reason}")
