"""Ready and next command tests."""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import compute_next, compute_ready, fold_events, read_events


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _ts(seq: int) -> str:
    """Stable timestamp string from seq number."""
    return f"2026-06-29T10:00:{seq:02d}Z"


def _append(log_dir: str, seq: int, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    """Mint and append one event; return the event dict."""
    event = mint_event(seq=seq, ts=_ts(seq), actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _fold(log_dir: str) -> dict:
    """Fold a log dir through the production path."""
    return fold_events(read_events(log_dir))


def _all_lines(log_dir: str) -> list[bytes]:
    """Read all event lines from all shards in log_dir."""
    lines: list[bytes] = []
    for fname in sorted(os.listdir(log_dir)):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines.extend(line for line in normalised.split(b"\n") if line)
    return lines


def _shuffle_log(log_dir: str, seed: int) -> tuple[str, str]:
    """
    Create a new temp dir with all shard lines shuffled into one file.

    Returns (new_log_dir, parent) — caller must shutil.rmtree(parent).
    """
    all_lines = _all_lines(log_dir)
    rng = random.Random(seed)
    shuffled = all_lines[:]
    rng.shuffle(shuffled)

    parent = tempfile.mkdtemp()
    new_log_dir = os.path.join(parent, "log")
    os.makedirs(new_log_dir)
    with open(os.path.join(new_log_dir, "shuffled.jsonl"), "wb") as fh:
        for line in shuffled:
            fh.write(line + b"\n")
    return new_log_dir, parent


def _max_seq(log_dir: str) -> int:
    """Return the maximum seq value across all events in log_dir."""
    return max(
        json.loads(line.decode("utf-8"))["seq"]
        for line in _all_lines(log_dir)
    )


def _copy_log(log_dir: str) -> tuple[str, str]:
    """
    Copy all lines from log_dir into a fresh tmp dir (one merged shard).

    Returns (new_log_dir, parent) — caller must shutil.rmtree(parent).
    """
    parent = tempfile.mkdtemp()
    new_log_dir = os.path.join(parent, "log")
    os.makedirs(new_log_dir)
    with open(os.path.join(new_log_dir, "copy.jsonl"), "wb") as fh:
        for line in _all_lines(log_dir):
            fh.write(line + b"\n")
    return new_log_dir, parent


# ---------------------------------------------------------------------------
# Seeded graph fixture
# ---------------------------------------------------------------------------

ACTOR = "operator@example.test"


def _build_base_log() -> tuple[str, str]:
    """
    Build the seeded graph (S0: all queued).

    Returns (log_dir, parent_tmpdir) — caller must shutil.rmtree(parent).

    Layout:
      seq=0  ergon.created
      seq=1  phase.opened (phase-1)
      seq=2  item.created alpha
      seq=3  item.created beta
      seq=4  item.created gamma
      seq=5  item.created delta
      seq=6  item.created epsilon
      seq=7  dep.added (alpha blocks beta)
      seq=8  dep.added (alpha blocks gamma)
      seq=9  dep.added (beta blocks delta)
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    prev = ""

    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "test"}, prev=prev)
    prev = e["id"]

    e = _append(log_dir, seq=1, actor=ACTOR, etype="phase.opened",
                payload={"phase": "phase-1"}, prev=prev)
    prev = e["id"]

    for i, item_id in enumerate(("alpha", "beta", "gamma", "delta", "epsilon")):
        seq = 2 + i
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id.capitalize(),
                             "prefix": "pnx", "status": "queued"},
                    prev=prev)
        prev = e["id"]

    # alpha blocks beta
    e = _append(log_dir, seq=7, actor=ACTOR, etype="dep.added",
                payload={"from_id": "alpha", "to_id": "beta", "type": "blocks"},
                prev=prev)
    prev = e["id"]

    # alpha blocks gamma
    e = _append(log_dir, seq=8, actor=ACTOR, etype="dep.added",
                payload={"from_id": "alpha", "to_id": "gamma", "type": "blocks"},
                prev=prev)
    prev = e["id"]

    # beta blocks delta
    e = _append(log_dir, seq=9, actor=ACTOR, etype="dep.added",
                payload={"from_id": "beta", "to_id": "delta", "type": "blocks"},
                prev=prev)
    # prev = e["id"]  (unused after last append)

    return log_dir, parent


@pytest.fixture
def base_log():
    """Yield (log_dir, parent); clean up after test."""
    log_dir, parent = _build_base_log()
    yield log_dir, parent
    shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# State S0: all queued
# ready = {alpha, epsilon},  next = alpha
# ---------------------------------------------------------------------------

class TestStateS0:
    """S0: all items queued, all edges active."""

    EXPECTED_READY = frozenset({"alpha", "epsilon"})
    EXPECTED_NEXT = "alpha"

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        """Canonical fold of S0 → correct ready/next."""
        log_dir, _ = base_log
        state = _fold(log_dir)
        self._check(state, "S0 canonical")

    @pytest.mark.parametrize("seed", [42, 99, 137, 7, 2026])
    def test_order_independent(self, base_log, seed):
        """Shuffled event lines → identical S0 ready/next."""
        log_dir, _ = base_log
        shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"S0 shuffle seed={seed}")
        finally:
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# State S1: alpha → done
# ready = {beta, gamma, epsilon},  next = beta
# ---------------------------------------------------------------------------

class TestStateS1:
    """S1: alpha is done."""

    EXPECTED_READY = frozenset({"beta", "gamma", "epsilon"})
    EXPECTED_NEXT = "beta"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        """Copy base log + append alpha → done."""
        new_log_dir, parent = _copy_log(base_log_dir)
        seq = _max_seq(new_log_dir) + 1
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                payload={"item_id": "alpha", "status": "done"}, prev="")
        return new_log_dir, parent

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        log_dir, _ = base_log
        s1_log_dir, s1_parent = self._build(log_dir)
        try:
            state = _fold(s1_log_dir)
            self._check(state, "S1 canonical")
        finally:
            shutil.rmtree(s1_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 137])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        s1_log_dir, s1_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(s1_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"S1 shuffle seed={seed}")
        finally:
            shutil.rmtree(s1_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# State S2: alpha + beta → done
# ready = {gamma, delta, epsilon},  next = gamma
# ---------------------------------------------------------------------------

class TestStateS2:
    """S2: alpha and beta are done."""

    EXPECTED_READY = frozenset({"gamma", "delta", "epsilon"})
    EXPECTED_NEXT = "gamma"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        """Copy base log + alpha done + beta done."""
        new_log_dir, parent = _copy_log(base_log_dir)
        seq = _max_seq(new_log_dir) + 1
        e = _append(new_log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                    payload={"item_id": "alpha", "status": "done"}, prev="")
        seq += 1
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                payload={"item_id": "beta", "status": "done"}, prev=e["id"])
        return new_log_dir, parent

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        log_dir, _ = base_log
        s2_log_dir, s2_parent = self._build(log_dir)
        try:
            state = _fold(s2_log_dir)
            self._check(state, "S2 canonical")
        finally:
            shutil.rmtree(s2_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 99])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        s2_log_dir, s2_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(s2_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"S2 shuffle seed={seed}")
        finally:
            shutil.rmtree(s2_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Dep-removal: remove alpha blocks gamma, alpha still queued
# ready = {alpha, gamma, epsilon},  next = alpha
# ---------------------------------------------------------------------------

class TestDepRemoval:
    """dep.removed cancels dep.added; removal is order-independent."""

    EXPECTED_READY = frozenset({"alpha", "gamma", "epsilon"})
    EXPECTED_NEXT = "alpha"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        """Copy base log + dep.removed(alpha blocks gamma)."""
        new_log_dir, parent = _copy_log(base_log_dir)
        seq = _max_seq(new_log_dir) + 1
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.removed",
                payload={"from_id": "alpha", "to_id": "gamma", "type": "blocks"},
                prev="")
        return new_log_dir, parent

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        log_dir, _ = base_log
        rm_log_dir, rm_parent = self._build(log_dir)
        try:
            state = _fold(rm_log_dir)
            self._check(state, "dep.removed canonical")
        finally:
            shutil.rmtree(rm_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 99])
    def test_order_independent(self, base_log, seed):
        """dep.removed is order-independent: shuffled stream cancels the add correctly."""
        log_dir, _ = base_log
        rm_log_dir, rm_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(rm_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"dep.removed shuffle seed={seed}")
        finally:
            shutil.rmtree(rm_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cycle detection: cycA blocks cycB, cycB blocks cycA
# Warning surfaced in state["report"]["warnings"], no hang.
# Uninvolved items still computed correctly.
# ---------------------------------------------------------------------------

def _build_cycle_log() -> tuple[str, str]:
    """
    Build a log with a 2-item cycle (cycA → cycB → cycA) and one free item.

    Returns (log_dir, parent) for cleanup.
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    actor = "operator@example.test"
    prev = ""

    e = _append(log_dir, seq=0, actor=actor, etype="ergon.created",
                payload={"repo": "cycle-test"}, prev=prev)
    prev = e["id"]

    for i, item_id in enumerate(("cycA", "cycB", "free_item")):
        e = _append(log_dir, seq=1 + i, actor=actor, etype="item.created",
                    payload={"item_id": item_id, "title": item_id, "prefix": "pnx",
                             "status": "queued"},
                    prev=prev)
        prev = e["id"]

    # Create the cycle: cycA blocks cycB, cycB blocks cycA.
    e = _append(log_dir, seq=4, actor=actor, etype="dep.added",
                payload={"from_id": "cycA", "to_id": "cycB", "type": "blocks"},
                prev=prev)
    prev = e["id"]

    _append(log_dir, seq=5, actor=actor, etype="dep.added",
            payload={"from_id": "cycB", "to_id": "cycA", "type": "blocks"},
            prev=prev)

    return log_dir, parent


def test_cycle_warning_no_hang():
    """
    A dep cycle surfaces a warning and does NOT hang.

    Cycle: cycA blocks cycB, cycB blocks cycA.
    Both cycA and cycB are excluded from ready (cycle nodes).
    free_item (no blockers, queued) is still in the ready set.
    """
    log_dir, root = _build_cycle_log()
    try:
        state = _fold(log_dir)
        ready = set(compute_ready(state))
        nxt = compute_next(state)

        # Cycle items excluded from ready.
        assert "cycA" not in ready, f"cycA should be excluded (cycle): {ready}"
        assert "cycB" not in ready, f"cycB should be excluded (cycle): {ready}"

        # free_item is ready.
        assert "free_item" in ready, f"free_item should be ready: {ready}"

        # next is free_item (the only ready item).
        assert nxt == "free_item", f"expected next='free_item', got {nxt!r}"

        # Warning was recorded.
        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings), (
            f"Expected 'dep cycle' warning in report.warnings; got: {warnings}"
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("seed", [42, 99, 137])
def test_cycle_order_independent(seed):
    """Cycle detection is order-independent: shuffled lines still warn, never hang."""
    log_dir, root = _build_cycle_log()
    shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
    try:
        state = _fold(shuffled_log_dir)
        ready = set(compute_ready(state))
        assert "cycA" not in ready
        assert "cycB" not in ready
        assert "free_item" in ready
        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings)
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Empty ready queue: compute_next returns None
# ---------------------------------------------------------------------------

def test_empty_ready_returns_none():
    """compute_next returns None when no items are ready."""
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)
    try:
        e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                    payload={"repo": "empty"}, prev="")
        _append(log_dir, seq=1, actor=ACTOR, etype="item.created",
                payload={"item_id": "only", "title": "Only", "prefix": "pnx",
                         "status": "queued"},
                prev=e["id"])
        _append(log_dir, seq=2, actor=ACTOR, etype="item.status_changed",
                payload={"item_id": "only", "status": "done"}, prev="")
        state = _fold(log_dir)
        assert compute_ready(state) == []
        assert compute_next(state) is None
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Idempotent: duplicate dep.added does not double-register the edge
# ---------------------------------------------------------------------------

def test_dep_added_idempotent(base_log):
    """
    Duplicating a dep.added line (union-merge artefact) is a no-op.

    The deps set uses set semantics; the same (from_id, to_id) pair
    written twice produces the same state as writing it once.
    """
    log_dir, _ = base_log
    all_lines = _all_lines(log_dir)

    # Find dep.added(alpha blocks beta).
    target_line = None
    for line in all_lines:
        try:
            evt = json.loads(line.decode("utf-8"))
            if (evt.get("type") == "dep.added"
                    and evt.get("payload", {}).get("from_id") == "alpha"
                    and evt.get("payload", {}).get("to_id") == "beta"):
                target_line = line
                break
        except (ValueError, KeyError):
            pass
    assert target_line is not None, "dep.added(alpha blocks beta) not found"

    # Build a log with the line duplicated.
    parent = tempfile.mkdtemp()
    dup_log_dir = os.path.join(parent, "log")
    os.makedirs(dup_log_dir)
    try:
        with open(os.path.join(dup_log_dir, "dup.jsonl"), "wb") as fh:
            for line in all_lines:
                fh.write(line + b"\n")
            fh.write(target_line + b"\n")  # duplicate

        state = _fold(dup_log_dir)
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == {"alpha", "epsilon"}, (
            f"idempotent dup: expected {{alpha, epsilon}}, got {ready}"
        )
        assert nxt == "alpha", f"idempotent dup: expected next=alpha, got {nxt!r}"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-add: dep.added → dep.removed → dep.added
#
# Scenario: alpha blocks gamma is added, then removed, then re-added.
# Final state: the edge exists (re-add wins because it is latest by total order).
# ready = {alpha, epsilon}   (gamma is still blocked by alpha)
# next  = alpha
#
# The sequence confirms that the later add operation restores the edge.
# ---------------------------------------------------------------------------

class TestDepReAdd:
    """add → rm → add: the re-add is latest by total order and must win."""

    EXPECTED_READY = frozenset({"alpha", "epsilon"})
    EXPECTED_NEXT = "alpha"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        """Copy base log, remove alpha→gamma edge, then re-add it."""
        new_log_dir, parent = _copy_log(base_log_dir)
        seq = _max_seq(new_log_dir) + 1

        # dep.removed at seq N
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.removed",
                payload={"from_id": "alpha", "to_id": "gamma", "type": "blocks"},
                prev="")
        seq += 1

        # dep.added at seq N+1 — this is the LATEST event for this pair
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                payload={"from_id": "alpha", "to_id": "gamma", "type": "blocks"},
                prev="")
        return new_log_dir, parent

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        """add→rm→add: re-add wins (latest by seq); gamma still blocked."""
        log_dir, _ = base_log
        re_log_dir, re_parent = self._build(log_dir)
        try:
            state = _fold(re_log_dir)
            self._check(state, "re-add canonical")
        finally:
            shutil.rmtree(re_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 99])
    def test_order_independent(self, base_log, seed):
        """Re-add is order-independent: shuffled stream still has the edge."""
        log_dir, _ = base_log
        re_log_dir, re_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(re_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"re-add shuffle seed={seed}")
        finally:
            shutil.rmtree(re_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Remove-then-add: dep.removed arrives before dep.added in log order,
# but the dep.added has a higher seq (latest by total order) → edge exists.
#
# This ordering verifies that a later add wins over an earlier removal.
# ---------------------------------------------------------------------------

class TestDepRemoveThenAdd:
    """rm(seq N) followed by add(seq N+1): add is latest, edge must exist."""

    EXPECTED_READY = frozenset({"alpha", "epsilon"})
    EXPECTED_NEXT = "alpha"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        """
        Build a log where dep.removed for alpha→gamma appears FIRST in seq,
        then dep.added for the same pair appears at a HIGHER seq.

        The base log already has alpha→gamma at seq=8.  We remove it (seq N)
        then re-add it (seq N+1); both events have higher seq than the original
        add, so the re-add is definitively the last event by total order.
        """
        new_log_dir, parent = _copy_log(base_log_dir)
        seq = _max_seq(new_log_dir) + 1

        # dep.removed at seq N (lower seq of the new pair)
        rm_event = _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.removed",
                           payload={"from_id": "alpha", "to_id": "gamma",
                                    "type": "blocks"},
                           prev="")
        seq += 1

        # dep.added at seq N+1 (higher seq — last-write-wins → edge exists)
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                payload={"from_id": "alpha", "to_id": "gamma", "type": "blocks"},
                prev=rm_event["id"])
        return new_log_dir, parent

    def _check(self, state: dict, context: str = "") -> None:
        ready = set(compute_ready(state))
        nxt = compute_next(state)
        assert ready == self.EXPECTED_READY, (
            f"[{context}] ready: expected {set(self.EXPECTED_READY)}, got {ready}"
        )
        assert nxt == self.EXPECTED_NEXT, (
            f"[{context}] next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
        )

    def test_canonical(self, base_log):
        """rm then add (add is latest): edge exists, gamma still blocked."""
        log_dir, _ = base_log
        new_log_dir, parent = self._build(log_dir)
        try:
            state = _fold(new_log_dir)
            self._check(state, "rm-then-add canonical")
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 99])
    def test_order_independent(self, base_log, seed):
        """rm-then-add is order-independent: shuffled stream still has the edge."""
        log_dir, _ = base_log
        new_log_dir, parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(new_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"rm-then-add shuffle seed={seed}")
        finally:
            shutil.rmtree(parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)
