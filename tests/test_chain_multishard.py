"""Multi-shard event-chain validation tests."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

import pytest

from pinax.event import mint_event, serialise
from pinax.fold import read_events

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_shard(log_dir: str, shard_name: str, events: list[dict]) -> str:
    """Write events to a named shard file in log_dir."""
    path = os.path.join(log_dir, shard_name)
    with open(path, "wb") as fh:
        for event in events:
            line = serialise(event)
            fh.write(line.encode("utf-8") + b"\n")
    return path


# ---------------------------------------------------------------------------
# 1. Legitimate two-shard union-merge: ZERO false warnings
# ---------------------------------------------------------------------------

def test_two_shard_union_merge_no_false_warnings(caplog):
    """
    Two actors, two shards, each chain valid.  Second actor's first event in shard-B
    carries a non-empty prev (pointing to actor-B's last event before this session).

    Expected: ZERO broken-prev-chain warnings.

    The check groups records by shard and actor, preserving a valid first
    record whose predecessor belongs to another shard.
    """
    # Actor A: shard-a.jsonl — a simple two-event chain starting at prev=''.
    e_a0 = mint_event(
        seq=0, ts="2026-06-29T10:00:00Z",
        actor="actora@host", etype="ergon.created",
        payload={"repo": "test"}, prev="",
    )
    e_a1 = mint_event(
        seq=1, ts="2026-06-29T10:00:01Z",
        actor="actora@host", etype="item.created",
        payload={"item_id": "pnx-aa1", "title": "A item 1", "prefix": "pnx",
                 "status": "queued"},
        prev=e_a0["id"],
    )

    # Actor B: shard-b.jsonl — starts with a non-empty prev.
    # This simulates actor B resuming after a prior session; their first event in
    # this shard legitimately references the last event they wrote in their prior shard.
    # (The "prior shard" is not present in this log_dir — simulating a cross-shard
    prior_b_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"  # synthetic prior id
    e_b0 = mint_event(
        seq=0, ts="2026-06-29T10:00:02Z",
        actor="actorb@host", etype="item.created",
        payload={"item_id": "pnx-bb1", "title": "B item 1", "prefix": "pnx",
                 "status": "queued"},
        prev=prior_b_id,  # non-empty: points to a prior event not in this shard
    )
    e_b1 = mint_event(
        seq=1, ts="2026-06-29T10:00:03Z",
        actor="actorb@host", etype="item.created",
        payload={"item_id": "pnx-bb2", "title": "B item 2", "prefix": "pnx",
                 "status": "queued"},
        prev=e_b0["id"],
    )

    tmpdir = tempfile.mkdtemp()
    try:
        _write_shard(tmpdir, "shard-a.jsonl", [e_a0, e_a1])
        _write_shard(tmpdir, "shard-b.jsonl", [e_b0, e_b1])

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            events = read_events(tmpdir)

        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("broken" in r.message.lower() or "prev" in r.message.lower()
                 or "chain" in r.message.lower())
        ]

        assert chain_warnings == [], (
            f"Expected ZERO false broken-prev-chain warnings for a legitimate "
            f"two-shard union-merge; got:\n"
            + "\n".join(r.message for r in chain_warnings)
            + "\n\nActor-grouping with "
            "expected_prev='' falsely warned on actor B's first event whose "
            "prev was non-empty (a valid cross-shard reference)."
        )

        # Sanity: fold produced all 4 unique events.
        assert len(events) == 4, f"Expected 4 events after dedup+sort, got {len(events)}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Genuine within-shard broken link: WARNING still fires
# ---------------------------------------------------------------------------

def test_within_shard_broken_link_still_warns(caplog):
    """
    A genuine within-shard broken prev-chain (stale id in a prev field) STILL
    produces a warning.

    The tamper-evidence guarantee must not be weakened by the grouping fix.
    """
    e0 = mint_event(
        seq=0, ts="2026-06-29T10:00:00Z",
        actor="actora@host", etype="ergon.created",
        payload={"repo": "test"}, prev="",
    )
    e1 = mint_event(
        seq=1, ts="2026-06-29T10:00:01Z",
        actor="actora@host", etype="item.created",
        payload={"item_id": "pnx-aa1", "title": "A item 1", "prefix": "pnx",
                 "status": "queued"},
        prev=e0["id"],
    )
    # e2 should chain from e1; instead we give it a garbage prev (broken link).
    e2_broken = mint_event(
        seq=2, ts="2026-06-29T10:00:02Z",
        actor="actora@host", etype="item.created",
        payload={"item_id": "pnx-aa2", "title": "A item 2", "prefix": "pnx",
                 "status": "queued"},
        prev="GARBAGE_BROKEN_LINK_FOR_TEST",  # should be e1["id"]
    )

    tmpdir = tempfile.mkdtemp()
    try:
        _write_shard(tmpdir, "shard-a.jsonl", [e0, e1, e2_broken])

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            read_events(tmpdir)

        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("broken" in r.message.lower() or "prev" in r.message.lower()
                 or "chain" in r.message.lower())
        ]

        assert chain_warnings, (
            "Expected a broken-prev-chain WARNING for the within-shard tamper; "
            "got none. The chain check must retain tamper detection."
        )

        # The warning must name the actor and the tampered event.
        warning_text = " ".join(r.message for r in chain_warnings)
        assert "actora@host" in warning_text, (
            f"Expected actor 'actora@host' in chain warning; got: {warning_text}"
        )
        assert "GARBAGE_BROKEN_LINK" in warning_text or e2_broken["id"] in warning_text, (
            f"Expected the broken id or the tampered event id in the warning; "
            f"got: {warning_text}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Two actors, ONE shard (the golden fixture scenario): ZERO false warnings
# ---------------------------------------------------------------------------

def test_two_actors_one_shard_no_false_warnings(caplog):
    """
    Regression: the golden fixture has two actors in one shard.
    Per-shard grouping must not false-positive here either.

    One actor starts a chain (prev=''); a second actor also starts a chain
    (prev='') in the same shard.  The (shard, actor) grouping gives each actor
    their own expected_prev, seeded from their own first event.  No false warnings.
    """
    e_c0 = mint_event(
        seq=0, ts="2026-06-29T10:00:00Z",
        actor="operator@example.test", etype="ergon.created",
        payload={"repo": "test"}, prev="",
    )
    e_c1 = mint_event(
        seq=1, ts="2026-06-29T10:00:01Z",
        actor="operator@example.test", etype="item.created",
        payload={"item_id": "pnx-c1", "title": "C item 1", "prefix": "pnx",
                 "status": "queued"},
        prev=e_c0["id"],
    )

    e_e0 = mint_event(
        seq=0, ts="2026-06-29T10:00:02Z",
        actor="reviewer@example.test", etype="item.created",
        payload={"item_id": "pnx-e1", "title": "E item 1", "prefix": "pnx",
                 "status": "queued"},
        prev="",
    )
    e_e1 = mint_event(
        seq=1, ts="2026-06-29T10:00:03Z",
        actor="reviewer@example.test", etype="item.created",
        payload={"item_id": "pnx-e2", "title": "E item 2", "prefix": "pnx",
                 "status": "queued"},
        prev=e_e0["id"],
    )

    tmpdir = tempfile.mkdtemp()
    try:
        # All four events in one shard — simulating the golden fixture.
        _write_shard(tmpdir, "shared.jsonl", [e_c0, e_e0, e_c1, e_e1])

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            events = read_events(tmpdir)

        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("broken" in r.message.lower() or "prev" in r.message.lower()
                 or "chain" in r.message.lower())
        ]

        assert chain_warnings == [], (
            f"Expected ZERO false broken-prev-chain warnings for two valid chains "
            f"in one shard; got:\n"
            + "\n".join(r.message for r in chain_warnings)
        )

        assert len(events) == 4, f"Expected 4 events, got {len(events)}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
