"""
Tests for deterministic concurrent double-claim reconciliation.

1. Owner is the earliest (ts, actor, id) — ADR-003 claim order.
2. The loser yields a claim.superseded outcome in state["claim_superseded"].
3. A report warning is surfaced in state["report"]["warnings"].
4. The result is identical under several seeded file-line shuffles (order-independent).
5. Duplicating a claim line is a no-op (idempotent).

Two cases are covered:
- Different-ts: actor_a claims at ts=T1, actor_b at ts=T2 (T2 > T1).
  Winner = actor_a by ts.
- Same-ts tie: both actors claim at the same second.
  Winner = lexicographically earlier actor string (then id as final tiebreak).

Test path: claims are appended via the real append_event() to a real
shard file; the fold is done via the real read_events() + fold_events() path,
round-tripping through the filesystem (write → read → normalise → parse → fold).
Not in-memory object shuffles — real file-line shuffles.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold_events, read_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_log_dir() -> str:
    """Create a temp log dir; caller must clean up."""
    tmpdir = tempfile.mkdtemp()
    log_dir = os.path.join(tmpdir, "log")
    os.makedirs(log_dir)
    return log_dir


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    """Mint and append one event; return the event dict."""
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _fold(log_dir: str) -> dict:
    """Fold the log dir through the production path."""
    return fold_events(read_events(log_dir))


def _shuffle_log_dir(src_log_dir: str, seed: int) -> str:
    """
    Create a new temp log dir with the shard lines shuffled using `seed`.

    Reads all lines from all shards in src_log_dir, shuffles them together
    into one shard in a new temp dir, and returns that new temp dir.
    This exercises the order-independence guarantee: the fold must produce
    the same result regardless of file-line order.
    """
    all_lines: list[bytes] = []
    for fname in sorted(os.listdir(src_log_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(src_log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        all_lines.extend(line for line in normalised.split(b"\n") if line)

    rng = random.Random(seed)
    shuffled = all_lines[:]
    rng.shuffle(shuffled)

    tmpdir = tempfile.mkdtemp()
    log_dir = os.path.join(tmpdir, "log")
    os.makedirs(log_dir)
    out_path = os.path.join(log_dir, "shuffled.jsonl")
    with open(out_path, "wb") as fh:
        for line in shuffled:
            fh.write(line + b"\n")
    return log_dir


def _duplicate_claim_line(src_log_dir: str, claim_event_id: str) -> str:
    """
    Create a new temp log dir with one specific event line duplicated.

    Finds the shard containing the event with `claim_event_id` and writes
    a new shard to a fresh temp log dir with that line duplicated.  All other
    lines are copied verbatim.
    """
    all_lines: list[bytes] = []
    for fname in sorted(os.listdir(src_log_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(src_log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        all_lines.extend(line for line in normalised.split(b"\n") if line)

    # Find the line to duplicate.
    target_line: bytes | None = None
    for line in all_lines:
        try:
            event = json.loads(line.decode("utf-8"))
            if event.get("id") == claim_event_id:
                target_line = line
                break
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    assert target_line is not None, (
        f"Claim event {claim_event_id} not found in log dir {src_log_dir}"
    )

    duplicated = all_lines + [target_line]

    tmpdir = tempfile.mkdtemp()
    log_dir = os.path.join(tmpdir, "log")
    os.makedirs(log_dir)
    out_path = os.path.join(log_dir, "with_dup.jsonl")
    with open(out_path, "wb") as fh:
        for line in duplicated:
            fh.write(line + b"\n")
    return log_dir


# ---------------------------------------------------------------------------
# Fixture: a repo with two items and a double-claim scenario
# ---------------------------------------------------------------------------

class DoubleClaimFixture:
    """
    A constructed log with:
    - ergon.created
    - item.created for ''
    - item.claimed by actor_a at ts_a
    - item.claimed by actor_b at ts_b (may be same as ts_a for same-ts test)

    The fixture keeps track of all created temp dirs for cleanup.
    """

    def __init__(
        self,
        item_id: str,
        actor_a: str,
        ts_a: str,
        actor_b: str,
        ts_b: str,
    ):
        self.tmpdirs: list[str] = []
        self.item_id = item_id
        self.actor_a = actor_a
        self.ts_a = ts_a
        self.actor_b = actor_b
        self.ts_b = ts_b

        self.log_dir = self._build()

    def _build(self) -> str:
        root_tmpdir = tempfile.mkdtemp()
        self.tmpdirs.append(root_tmpdir)
        log_dir = os.path.join(root_tmpdir, "log")
        os.makedirs(log_dir)

        e0 = _append(
            log_dir, seq=0, ts="2026-06-29T10:00:00Z",
            actor="operator@example.test", etype="ergon.created",
            payload={"repo": "test-repo"}, prev="",
        )

        # seq=1: item.created
        e1 = _append(
            log_dir, seq=1, ts="2026-06-29T10:00:01Z",
            actor="operator@example.test", etype="item.created",
            payload={
                "item_id": self.item_id,
                "title": "Double-claim test item",
                "prefix": "pnx",
                "status": "queued",
            },
            prev=e0["id"],
        )

        # seq=2: item.claimed by actor_a
        e2 = _append(
            log_dir, seq=2, ts=self.ts_a,
            actor=self.actor_a, etype="item.claimed",
            payload={"item_id": self.item_id},
            prev="",
        )
        self.claim_a_event = e2

        # seq=3: item.claimed by actor_b (same item — the double-claim)
        e3 = _append(
            log_dir, seq=3, ts=self.ts_b,
            actor=self.actor_b, etype="item.claimed",
            payload={"item_id": self.item_id},
            prev="",
        )
        self.claim_b_event = e3

        return log_dir

    def cleanup(self) -> None:
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 1: Different-ts double-claim
# actor_a claims at T1 (earlier), actor_b at T2 (later).
# Winner: actor_a (earlier ts).
# ---------------------------------------------------------------------------

class TestDoubleClaimDifferentTs:
    """Different timestamps: actor_a wins by earlier ts."""

    ITEM_ID = "pnx-alpha"
    ACTOR_A = "operator@example.test"   # claims at T1
    ACTOR_B = "reviewer@example.test"   # claims at T2 > T1
    TS_A = "2026-06-29T10:00:02Z"
    TS_B = "2026-06-29T10:00:03Z"

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.fixture = DoubleClaimFixture(
            item_id=self.ITEM_ID,
            actor_a=self.ACTOR_A, ts_a=self.TS_A,
            actor_b=self.ACTOR_B, ts_b=self.TS_B,
        )
        yield
        self.fixture.cleanup()

    def _assert_correct_winner(self, state: dict, context: str = "") -> None:
        items = state.get("items", {})
        assert self.ITEM_ID in items, (
            f"{context}: item {self.ITEM_ID!r} missing from fold state"
        )
        item = items[self.ITEM_ID]

        # (1) Owner is the earliest (ts, actor, id) winner.
        assert item.get("owner") == self.ACTOR_A, (
            f"{context}: expected owner={self.ACTOR_A!r} (earlier ts={self.TS_A}), "
            f"got owner={item.get('owner')!r}"
        )
        assert item.get("claimed_at") == self.TS_A, (
            f"{context}: expected claimed_at={self.TS_A!r}, got {item.get('claimed_at')!r}"
        )

        # (2) Loser yields a claim.superseded outcome.
        superseded = state.get("claim_superseded", [])
        assert len(superseded) == 1, (
            f"{context}: expected 1 claim.superseded entry, got {len(superseded)}: {superseded}"
        )
        sup = superseded[0]
        assert sup["item_id"] == self.ITEM_ID, (
            f"{context}: claim.superseded item_id mismatch: {sup}"
        )
        assert sup["superseded_actor"] == self.ACTOR_B, (
            f"{context}: expected superseded_actor={self.ACTOR_B!r}, got {sup['superseded_actor']!r}"
        )
        assert sup["winner_actor"] == self.ACTOR_A, (
            f"{context}: expected winner_actor={self.ACTOR_A!r}, got {sup['winner_actor']!r}"
        )

        # (3) Report warning is surfaced.
        warnings = state.get("report", {}).get("warnings", [])
        assert len(warnings) >= 1, (
            f"{context}: expected at least 1 warning in report.warnings, got {warnings!r}"
        )
        # The warning must mention claim.superseded and the item id.
        warning_text = " ".join(warnings)
        assert "claim.superseded" in warning_text, (
            f"{context}: 'claim.superseded' not in warnings: {warnings!r}"
        )
        assert self.ITEM_ID in warning_text, (
            f"{context}: item id {self.ITEM_ID!r} not in warnings: {warnings!r}"
        )

    def test_canonical_fold(self):
        """Assertion 1-3: canonical fold gives the correct winner + superseded + warning."""
        state = _fold(self.fixture.log_dir)
        self._assert_correct_winner(state, context="canonical")

    @pytest.mark.parametrize("seed", [42, 99, 137, 7, 2026])
    def test_order_independent_shuffle(self, seed: int):
        """Assertion 4: result is identical under seeded file-line shuffles."""
        shuffled_log_dir = _shuffle_log_dir(self.fixture.log_dir, seed)
        parent_dir = os.path.dirname(shuffled_log_dir)
        try:
            state = _fold(shuffled_log_dir)
            self._assert_correct_winner(state, context=f"shuffle(seed={seed})")
        finally:
            shutil.rmtree(parent_dir, ignore_errors=True)

    def test_idempotent_duplicate_winner_claim(self):
        """Assertion 5: duplicating the winner's claim line is a no-op (idempotent)."""
        dup_log_dir = _duplicate_claim_line(
            self.fixture.log_dir, self.fixture.claim_a_event["id"]
        )
        parent_dir = os.path.dirname(dup_log_dir)
        try:
            state = _fold(dup_log_dir)
            self._assert_correct_winner(state, context="dup_winner_claim")
        finally:
            shutil.rmtree(parent_dir, ignore_errors=True)

    def test_idempotent_duplicate_loser_claim(self):
        """Assertion 5b: duplicating the loser's claim line is also a no-op."""
        dup_log_dir = _duplicate_claim_line(
            self.fixture.log_dir, self.fixture.claim_b_event["id"]
        )
        parent_dir = os.path.dirname(dup_log_dir)
        try:
            state = _fold(dup_log_dir)
            self._assert_correct_winner(state, context="dup_loser_claim")
        finally:
            shutil.rmtree(parent_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Case 2: Same-ts double-claim (tiebreak by actor, then id)
# actor_a = "aaaa@host" (lexically earlier), actor_b = "zzzz@host" (later).
# Both claim at identical ts.
# Winner: aaaa@host (lexically earlier actor string).
# ---------------------------------------------------------------------------

class TestDoubleClaimSameTs:
    """Same timestamp: winner determined by actor string (lexicographic), then id."""

    ITEM_ID = "pnx-beta"
    ACTOR_A = "aaaa@host"   # lexically earlier → wins same-ts tie
    ACTOR_B = "zzzz@host"   # lexically later → superseded
    TS = "2026-06-29T10:00:02Z"  # same ts for both

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.fixture = DoubleClaimFixture(
            item_id=self.ITEM_ID,
            actor_a=self.ACTOR_A, ts_a=self.TS,
            actor_b=self.ACTOR_B, ts_b=self.TS,
        )
        yield
        self.fixture.cleanup()

    def _expected_winner(self) -> str:
        """
        Compute the expected winner from the claim events' (ts, actor, id) keys.

        Both ts are equal.  The tiebreaker is actor (lexicographic), then id.
        actor_a='aaaa@host' < actor_b='zzzz@host' → actor_a wins.
        """
        key_a = (self.TS, self.ACTOR_A, self.fixture.claim_a_event["id"])
        key_b = (self.TS, self.ACTOR_B, self.fixture.claim_b_event["id"])
        return self.ACTOR_A if key_a < key_b else self.ACTOR_B

    def _assert_correct_winner(self, state: dict, context: str = "") -> None:
        items = state.get("items", {})
        assert self.ITEM_ID in items, (
            f"{context}: item {self.ITEM_ID!r} missing from fold state"
        )
        item = items[self.ITEM_ID]

        expected_winner = self._expected_winner()
        expected_loser = self.ACTOR_B if expected_winner == self.ACTOR_A else self.ACTOR_A

        # (1) Owner is the earliest (ts, actor, id) winner.
        assert item.get("owner") == expected_winner, (
            f"{context}: expected owner={expected_winner!r} (same-ts, earlier actor), "
            f"got owner={item.get('owner')!r}"
        )

        # (2) Loser yields a claim.superseded outcome.
        superseded = state.get("claim_superseded", [])
        assert len(superseded) == 1, (
            f"{context}: expected 1 claim.superseded entry, got {len(superseded)}: {superseded}"
        )
        sup = superseded[0]
        assert sup["superseded_actor"] == expected_loser, (
            f"{context}: expected superseded_actor={expected_loser!r}, got {sup['superseded_actor']!r}"
        )
        assert sup["winner_actor"] == expected_winner, (
            f"{context}: expected winner_actor={expected_winner!r}, got {sup['winner_actor']!r}"
        )

        # (3) Report warning is surfaced.
        warnings = state.get("report", {}).get("warnings", [])
        assert len(warnings) >= 1, (
            f"{context}: expected at least 1 warning in report.warnings"
        )
        warning_text = " ".join(warnings)
        assert "claim.superseded" in warning_text, (
            f"{context}: 'claim.superseded' not in warnings: {warnings!r}"
        )

    def test_canonical_fold(self):
        """Same-ts case: winner determined by actor tiebreaker, not ts."""
        state = _fold(self.fixture.log_dir)
        self._assert_correct_winner(state, context="same_ts_canonical")

    @pytest.mark.parametrize("seed", [42, 99, 137, 7, 2026])
    def test_order_independent_shuffle(self, seed: int):
        """Same-ts: result is identical under seeded file-line shuffles."""
        shuffled_log_dir = _shuffle_log_dir(self.fixture.log_dir, seed)
        parent_dir = os.path.dirname(shuffled_log_dir)
        try:
            state = _fold(shuffled_log_dir)
            self._assert_correct_winner(state, context=f"same_ts_shuffle(seed={seed})")
        finally:
            shutil.rmtree(parent_dir, ignore_errors=True)

    def test_idempotent_duplicate_claim(self):
        """Same-ts: duplicating either claim line is a no-op."""
        for label, claim_event in [
            ("winner_claim", self.fixture.claim_a_event),
            ("loser_claim", self.fixture.claim_b_event),
        ]:
            dup_log_dir = _duplicate_claim_line(
                self.fixture.log_dir, claim_event["id"]
            )
            parent_dir = os.path.dirname(dup_log_dir)
            try:
                state = _fold(dup_log_dir)
                self._assert_correct_winner(
                    state, context=f"same_ts_dup_{label}"
                )
            finally:
                shutil.rmtree(parent_dir, ignore_errors=True)
