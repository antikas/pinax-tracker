"""
tests/test_integrity.py — ADR-001 tamper-evidence: id-verification and prev-chain detection.

These tests prove that:
1. A tampered payload (stored id does not match recomputed hash) is DETECTED with a WARNING.
2. A broken prev-chain (prev field points to the wrong id) is DETECTED with a WARNING.

In both cases the fold still applies the events (detection is the guarantee, not rejection).
This exercises the Test path: real JSONL bytes on disk, read through read_events.

ADR-001: "Each event carries the id of the prior event, making the log tamper-evident.
A broken chain link is detectable on replay."
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile

import pytest

from pinax.fold import fold_events, read_events
from pinax.event import mint_event, serialise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN_LOG = os.path.join(FIXTURES_DIR, "golden_log.jsonl")
GOLDEN_STATE = os.path.join(FIXTURES_DIR, "golden_state.json")


def _read_golden_events() -> list[dict]:
    """Return the unique golden fixture events as a list of dicts (deduplicated)."""
    with open(GOLDEN_LOG, "rb") as fh:
        raw = fh.read()
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = [l for l in normalised.split(b"\n") if l]
    seen: set[str] = set()
    events = []
    for line in lines:
        e = json.loads(line.decode("utf-8"))
        eid = e.get("id", "")
        if eid not in seen:
            seen.add(eid)
            events.append(e)
    return events


def _write_events_to_tmpdir(events: list[dict], tmpdir: str, shard: str = "test.jsonl") -> str:
    """Write a list of event dicts as JSONL to a shard file in tmpdir."""
    path = os.path.join(tmpdir, shard)
    with open(path, "wb") as fh:
        for e in events:
            line = serialise(e)
            fh.write(line.encode("utf-8") + b"\n")
    return path


# ---------------------------------------------------------------------------
# 1. Clean golden log: no integrity warnings
# ---------------------------------------------------------------------------

def test_golden_log_no_integrity_warnings(caplog):
    """
    The golden fixture log must pass integrity checks with zero warnings
    (all ids correct, all prev-chains intact).
    """
    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        tmpdir = tempfile.mkdtemp()
        try:
            shutil.copy(GOLDEN_LOG, os.path.join(tmpdir, "golden.jsonl"))
            read_events(tmpdir)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    integrity_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("integrity" in r.message.lower() or "tamper" in r.message.lower()
             or "broken prev" in r.message.lower() or "chain" in r.message.lower())
    ]
    assert integrity_warnings == [], (
        f"Expected zero integrity warnings on clean golden log; got: "
        + "\n".join(r.message for r in integrity_warnings)
    )


# ---------------------------------------------------------------------------
# 2. Tampered payload: id-verification failure is DETECTED
# ---------------------------------------------------------------------------

def test_tampered_payload_detected(caplog):
    """
    A tampered event (payload mutated, stored id now stale) is detected:
    read_events emits an integrity WARNING naming the event id.

    The fold still applies the event (detection, not rejection) — the tampered
    status lands in state.  What matters is that the tamper IS DETECTABLE.

    This is the integrity check: a tampered payload silently accepted by the
    fold is an integrity invariant violation.
    """
    events = _read_golden_events()

    target_idx = None
    for i, e in enumerate(events):
        if e.get("type") == "item.status_changed" and e.get("seq") == 4:
            target_idx = i
            break
    assert target_idx is not None, "Could not find seq=4 item.status_changed in golden fixture"

    # Mutate the payload — status becomes DONE_TAMPERED — but keep the stale id.
    tampered = dict(events[target_idx])
    tampered["payload"] = dict(tampered["payload"])
    tampered["payload"]["status"] = "DONE_TAMPERED"
    # id is intentionally NOT recomputed — simulating a tampered line.

    tampered_events = events[:target_idx] + [tampered] + events[target_idx + 1:]

    tmpdir = tempfile.mkdtemp()
    try:
        _write_events_to_tmpdir(tampered_events, tmpdir)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            state = fold_events(read_events(tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Integrity warning MUST have been emitted naming the tampered event id.
    tampered_id = tampered["id"]
    integrity_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and tampered_id in r.message
    ]
    assert integrity_warnings, (
        f"Expected an integrity WARNING naming id={tampered_id!r} for tampered event; "
        f"got records: {[r.message for r in caplog.records]}"
    )

    # The fold applied the tampered event (detection, not silent drop).
    pnx_aaa1 = state.get("items", {}).get("pnx-aaa1", {})
    assert pnx_aaa1.get("status") == "DONE_TAMPERED", (
        f"Expected tampered status to land in fold state; got status={pnx_aaa1.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# 3. Broken prev-chain: detected with a WARNING
# ---------------------------------------------------------------------------

def test_broken_prev_chain_detected(caplog):
    """
    A broken prev-chain (an event's prev field points to the wrong id) is detected:
    read_events emits a WARNING naming the actor, seq, and expected/actual prev values.

    This is the integrity check: a broken prev link silently accepted by the
    fold is an integrity invariant violation.
    """
    events = _read_golden_events()

    target_idx = None
    for i, e in enumerate(events):
        if e.get("seq") == 3 and e.get("actor") == "operator@example.test":
            target_idx = i
            break
    assert target_idx is not None, "Could not find seq=3 operator@example.test in golden fixture"

    # Mutate the prev field to a garbage value — broken chain.
    broken = dict(events[target_idx])
    broken["prev"] = "GARBAGE_BROKEN_LINK_FOR_INTEGRITY_TEST"

    broken_events = events[:target_idx] + [broken] + events[target_idx + 1:]

    tmpdir = tempfile.mkdtemp()
    try:
        _write_events_to_tmpdir(broken_events, tmpdir)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            read_events(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # A broken-chain WARNING MUST have been emitted.
    chain_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and ("broken" in r.message.lower() or "prev" in r.message.lower() or "chain" in r.message.lower())
    ]
    assert chain_warnings, (
        f"Expected a broken-prev-chain WARNING; got records: "
        f"{[r.message for r in caplog.records]}"
    )

    # Verify the warning specifically names the actor and seq.
    warning_text = " ".join(r.message for r in chain_warnings)
    assert "operator@example.test" in warning_text, (
        f"Expected actor operator@example.test in chain warning; got: {warning_text}"
    )


# ---------------------------------------------------------------------------
# 4. Fold state is still deterministic after id-warning (fold is not aborted)
# ---------------------------------------------------------------------------

def test_id_warning_does_not_abort_fold(caplog):
    """
    Even when an integrity warning fires, the fold completes and returns valid state.
    The tamper is detected (warned), not silently accepted and not a fatal error.
    """
    events = _read_golden_events()

    target_idx = None
    for i, e in enumerate(events):
        if e.get("type") == "ergon.created":
            target_idx = i
            break
    assert target_idx is not None

    tampered = dict(events[target_idx])
    tampered["payload"] = {"repo": "TAMPERED_REPO"}
    # Keep stale id — verify_id will return False.

    tampered_events = events[:target_idx] + [tampered] + events[target_idx + 1:]

    tmpdir = tempfile.mkdtemp()
    try:
        _write_events_to_tmpdir(tampered_events, tmpdir)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            state = fold_events(read_events(tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # Warning was emitted.
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "Expected at least one WARNING from read_events on tampered log"
    )

    # Fold completed; items and other state are still present.
    assert "items" in state, "Fold state missing 'items' after integrity warning"
    assert "pnx-aaa1" in state["items"], "Fold state missing pnx-aaa1 after integrity warning"
