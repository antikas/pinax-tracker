"""Deterministic, read-only metrics derived from the event log.

Metrics are computed from the folded log for the current invocation. They do
not write tracker data into knowledge-plane files.
"""
from __future__ import annotations

import os
from typing import Any

from .fold import fold, compute_ready
from .event import parse_line


def _count_raw_events(log_dir: str) -> int:
    """
    Count the total number of valid event lines across all shards.

    Uses the same LF-normalisation as the fold, so the count matches fold input.
    Torn/unparseable lines are not counted (consistent with fold behaviour).
    """
    import glob
    total = 0
    for shard_path in sorted(glob.glob(os.path.join(log_dir, "*.jsonl"))):
        try:
            with open(shard_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for line in normalised.split(b"\n"):
            if not line:
                continue
            if parse_line(line) is not None:
                total += 1
    return total


def _count_event_types(log_dir: str) -> dict[str, int]:
    """
    Count raw events per type across all shards.

    Used for note_added_count, gate_counts, audit_verdicts — these are
    derived from raw type counts, not fold state, so the count is event-granular.
    """
    import glob
    counts: dict[str, int] = {}
    gate_counts: dict[str, int] = {}
    audit_verdicts: dict[str, int] = {}

    for shard_path in sorted(glob.glob(os.path.join(log_dir, "*.jsonl"))):
        try:
            with open(shard_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for line in normalised.split(b"\n"):
            if not line:
                continue
            event = parse_line(line)
            if event is None:
                continue
            etype = event.get("type", "")
            counts[etype] = counts.get(etype, 0) + 1
            # Gate counts (from item.blocked events).
            if etype == "item.blocked":
                gate = event.get("payload", {}).get("gate", "unknown")
                gate_counts[gate] = gate_counts.get(gate, 0) + 1
            if etype == "item.audit_result":
                verdict = event.get("payload", {}).get("verdict", "unknown")
                audit_verdicts[verdict] = audit_verdicts.get(verdict, 0) + 1

    return {
        "by_type": counts,
        "gate_counts": gate_counts,
        "audit_verdicts": audit_verdicts,
    }


def compute_metrics(log_dir: str) -> dict[str, Any]:
    """
    Compute all derivable metrics from the event log at log_dir.

    READ ONLY — no filesystem writes.
    Deterministic: same log → identical output, PYTHONHASHSEED-independent.

    Returns a plain dict with:
        total_items, by_status, items_done, items_parked, items_blocked,
        events_total, claim_superseded_count, note_added_count,
        cycle_times, park_reasons, gate_counts, audit_verdicts,
        ready_queue_size.
    """
    # --- Fold ---
    state = fold(log_dir)
    items: dict = state.get("items", {})

    # --- Item status counts ---
    by_status: dict[str, int] = {}
    for item in items.values():
        s = item.get("status", "queued")
        by_status[s] = by_status.get(s, 0) + 1

    items_done = by_status.get("done", 0)
    items_parked = by_status.get("parked", 0)
    items_blocked = by_status.get("blocked", 0)

    # --- Cycle times (creation → done, in seconds) ---
    # ISO-8601 fixed-precision timestamps are directly comparable as strings
    # and parseable.  Use datetime for delta.
    import datetime
    cycle_times: list[dict[str, Any]] = []
    for item_id, item in sorted(items.items()):  # sorted for determinism
        if item.get("status") != "done":
            continue
        created_at = item.get("created_at", "")
        done_at = item.get("status_changed_at", "")
        if created_at and done_at:
            try:
                t_create = datetime.datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
                t_done = datetime.datetime.strptime(done_at, "%Y-%m-%dT%H:%M:%SZ")
                delta_seconds = int((t_done - t_create).total_seconds())
                cycle_times.append({
                    "item_id": item_id,
                    "created_at": created_at,
                    "done_at": done_at,
                    "elapsed_seconds": delta_seconds,
                })
            except (ValueError, TypeError):
                pass

    # --- Park reasons ---
    park_reasons: list[dict[str, str]] = []
    for item_id, item in sorted(items.items()):  # sorted for determinism
        if item.get("status") == "parked":
            park_reasons.append({
                "item_id": item_id,
                "reason": item.get("park_reason", ""),
            })

    # --- Claim superseded count ---
    claim_superseded_count = len(state.get("claim_superseded", []))

    # --- Ready queue size (current) ---
    ready_queue_size = len(compute_ready(state))

    events_total = _count_raw_events(log_dir)
    raw_counts = _count_event_types(log_dir)
    note_added_count = raw_counts["by_type"].get("note.added", 0)
    gate_counts = raw_counts["gate_counts"]
    audit_verdicts = raw_counts["audit_verdicts"]

    return {
        "total_items": len(items),
        "by_status": by_status,
        "items_done": items_done,
        "items_parked": items_parked,
        "items_blocked": items_blocked,
        "events_total": events_total,
        "claim_superseded_count": claim_superseded_count,
        "note_added_count": note_added_count,
        "cycle_times": cycle_times,
        "park_reasons": park_reasons,
        "gate_counts": gate_counts,
        "audit_verdicts": audit_verdicts,
        "ready_queue_size": ready_queue_size,
    }
