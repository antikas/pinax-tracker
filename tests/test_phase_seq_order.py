"""Phase sequence ordering tests."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import compute_next, compute_ready, fold_events, read_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAME_TS = "2026-06-29T12:00:00Z"
ACTOR = "operator@example.test"


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    """Mint and append one event; return the event dict."""
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _fold(log_dir: str) -> dict:
    """Fold a log dir through the production path."""
    return fold_events(read_events(log_dir))


# ---------------------------------------------------------------------------
# Test: phases opened at the same second resolve by seq, not ts
# ---------------------------------------------------------------------------

def test_same_second_phases_resolve_by_seq():
    """
    Two phases opened at the same clock-second must be ordered by seq.

    phase-B is opened at seq=3 (LOWER seq, therefore EARLIER in total order).
    phase-A is opened at seq=5 (HIGHER seq, therefore LATER in total order).
    Both have the same ts (SAME_TS).

    Expected phase order: phase-B (seq=3) BEFORE phase-A (seq=5).

    If compute_next uses opened_at as the primary sort key, same-second phases
    produce a non-deterministic order (dict insertion order / stable-sort luck).
    Seq-first ordering makes it deterministic.

    To exercise this: create one item per phase, both queued and unblocked.
    compute_next should return the item from phase-B (earlier by seq).
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)
    try:
        # seq=0: ergon.created
        e = _append(log_dir, seq=0, ts="2026-06-29T11:00:00Z", actor=ACTOR,
                    etype="ergon.created", payload={"repo": "test"}, prev="")
        prev = e["id"]

        # seq=1: item in phase-A (prefix='phase-A')
        e = _append(log_dir, seq=1, ts="2026-06-29T11:00:01Z", actor=ACTOR,
                    etype="item.created",
                    payload={"item_id": "item-a", "title": "Item A",
                             "prefix": "phase-A", "status": "queued"},
                    prev=prev)
        prev = e["id"]

        # seq=2: item in phase-B (prefix='phase-B')
        e = _append(log_dir, seq=2, ts="2026-06-29T11:00:02Z", actor=ACTOR,
                    etype="item.created",
                    payload={"item_id": "item-b", "title": "Item B",
                             "prefix": "phase-B", "status": "queued"},
                    prev=prev)
        prev = e["id"]

        # seq=3: phase-B opened (EARLIER in total order by seq)
        # ts = SAME_TS as phase-A below
        e = _append(log_dir, seq=3, ts=SAME_TS, actor=ACTOR,
                    etype="phase.opened",
                    payload={"phase": "phase-B"},
                    prev=prev)
        prev = e["id"]

        # seq=4: some unrelated event (not a phase)
        e = _append(log_dir, seq=4, ts=SAME_TS, actor=ACTOR,
                    etype="ergon.created",  # reuse ergon.created as a no-op filler
                    payload={"repo": "test"},
                    prev=prev)
        prev = e["id"]

        # seq=5: phase-A opened (LATER in total order by seq)
        # ts = SAME_TS as phase-B above — same clock-second
        _append(log_dir, seq=5, ts=SAME_TS, actor=ACTOR,
                etype="phase.opened",
                payload={"phase": "phase-A"},
                prev=prev)

        state = _fold(log_dir)

        # Verify phases were recorded with their opened_seq.
        phases = state.get("phases", {})
        assert "phase-A" in phases, f"phase-A missing from fold state; phases={list(phases.keys())}"
        assert "phase-B" in phases, f"phase-B missing from fold state; phases={list(phases.keys())}"

        assert phases["phase-B"]["opened_seq"] == 3, (
            f"phase-B opened_seq: expected 3, got {phases['phase-B'].get('opened_seq')}"
        )
        assert phases["phase-A"]["opened_seq"] == 5, (
            f"phase-A opened_seq: expected 5, got {phases['phase-A'].get('opened_seq')}"
        )
        assert phases["phase-A"]["opened_at"] == SAME_TS, "phase-A opened_at mismatch"
        assert phases["phase-B"]["opened_at"] == SAME_TS, "phase-B opened_at mismatch"

        # Both items are ready (no blockers).
        ready = set(compute_ready(state))
        assert "item-a" in ready, f"item-a not ready: {ready}"
        assert "item-b" in ready, f"item-b not ready: {ready}"

        # compute_next must return item-b (phase-B, seq=3 BEFORE phase-A seq=5).
        # If compute_next incorrectly used opened_at, the result would be
        # non-deterministic (same ts — stable-sort artefact).
        nxt = compute_next(state)
        assert nxt == "item-b", (
            f"Expected compute_next='item-b' (phase-B has seq=3 < seq=5 for phase-A "
            f"and both have the same ts={SAME_TS!r}). "
            f"Got: {nxt!r}. "
            f"If compute_next uses opened_at as the primary key, same-second phases "
            f"produce non-deterministic ordering — the seq-first invariant requires it."
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_phase_seq_order_opposite_ts_order():
    """
    When ts-order and seq-order disagree, seq wins.

    phase-X: seq=10, ts=2026-06-29T12:00:01Z  (later ts)
    phase-Y: seq=2,  ts=2026-06-29T12:00:02Z  (even later ts, but EARLIER seq)

    Correct order by seq: phase-Y (seq=2) BEFORE phase-X (seq=10).
    Wrong order by ts: phase-X (ts=:01) BEFORE phase-Y (ts=:02).

    compute_next must return the item from phase-Y (earlier by seq).
    This is the clearest violation of "timestamps are metadata, never the sort key".
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)
    try:
        # seq=0: ergon.created
        e = _append(log_dir, seq=0, ts="2026-06-29T11:00:00Z", actor=ACTOR,
                    etype="ergon.created", payload={"repo": "test"}, prev="")
        prev = e["id"]

        # seq=1: item in phase-X
        e = _append(log_dir, seq=1, ts="2026-06-29T11:00:01Z", actor=ACTOR,
                    etype="item.created",
                    payload={"item_id": "item-x", "title": "Item X",
                             "prefix": "phase-X", "status": "queued"},
                    prev=prev)
        prev = e["id"]

        # seq=2: phase-Y opened (EARLIER seq, but LATER ts)
        e = _append(log_dir, seq=2, ts="2026-06-29T12:00:02Z", actor=ACTOR,
                    etype="phase.opened",
                    payload={"phase": "phase-Y"},
                    prev=prev)
        prev = e["id"]

        # seq=3: item in phase-Y
        e = _append(log_dir, seq=3, ts="2026-06-29T11:00:03Z", actor=ACTOR,
                    etype="item.created",
                    payload={"item_id": "item-y", "title": "Item Y",
                             "prefix": "phase-Y", "status": "queued"},
                    prev=prev)
        prev = e["id"]

        # seq=10: phase-X opened (LATER seq, but EARLIER ts)
        _append(log_dir, seq=10, ts="2026-06-29T12:00:01Z", actor=ACTOR,
                etype="phase.opened",
                payload={"phase": "phase-X"},
                prev=prev)

        state = _fold(log_dir)
        phases = state.get("phases", {})

        assert phases.get("phase-X", {}).get("opened_seq") == 10
        assert phases.get("phase-Y", {}).get("opened_seq") == 2

        # phase-Y (seq=2) comes before phase-X (seq=10) by total order.
        # item-y belongs to phase-Y → should be chosen as next.
        nxt = compute_next(state)
        assert nxt == "item-y", (
            f"Expected compute_next='item-y' (phase-Y seq=2 < phase-X seq=10). "
            f"Got: {nxt!r}. If compute_next uses ts-order, phase-X (ts=:01) would "
            f"come first, returning item-x — wrong (violates ADR-001)."
        )
    finally:
        shutil.rmtree(parent, ignore_errors=True)
