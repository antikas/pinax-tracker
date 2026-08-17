"""Typed dependency edge fold tests."""

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

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACTOR_A = "operator@example.test"
ACTOR_B = "reviewer@example.test"


def _ts(seq: int) -> str:
    """Stable timestamp string from seq number."""
    return f"2026-06-29T10:00:{seq:02d}Z"


def _append(log_dir: str, seq: int, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    event = mint_event(seq=seq, ts=_ts(seq), actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _item(log_dir: str, seq: int, actor: str, item_id: str,
          title: str, prev: str = "") -> dict:
    return _append(log_dir, seq=seq, actor=actor, etype="item.created",
                   payload={"item_id": item_id, "title": title,
                            "prefix": "pnx", "status": "queued"},
                   prev=prev)


def _dep_add(log_dir: str, seq: int, actor: str, from_id: str, to_id: str,
             edge_type: str, prev: str = "") -> dict:
    return _append(log_dir, seq=seq, actor=actor, etype="dep.added",
                   payload={"from_id": from_id, "to_id": to_id, "type": edge_type},
                   prev=prev)


def _dep_rm(log_dir: str, seq: int, actor: str, from_id: str, to_id: str,
            edge_type: str, prev: str = "") -> dict:
    return _append(log_dir, seq=seq, actor=actor, etype="dep.removed",
                   payload={"from_id": from_id, "to_id": to_id, "type": edge_type},
                   prev=prev)


def _fold_dir(log_dir: str) -> dict:
    events = read_events(log_dir)
    return fold_events(events)


def _shuffle_shards(log_dir: str) -> None:
    """
    Shuffle the line order of every JSONL shard in log_dir in-place.

    This is the production-construct shuffle test: the fold must produce
    identical results regardless of the physical line order on disk.
    (read_events() sorts by total-order key before folding — this proves it.)
    """
    rng = random.Random(42)  # fixed seed for reproducibility
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = [l for l in normalised.split(b"\n") if l]
        rng.shuffle(lines)
        shuffled = b"\n".join(lines) + b"\n"
        with open(fpath, "wb") as fh:
            fh.write(shuffled)


def _duplicate_all_lines(log_dir: str) -> None:
    """
    Duplicate every line in every JSONL shard (simulates union-merge artefact).

    The fold must be idempotent: duplicated lines must be no-ops.
    """
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = [l for l in normalised.split(b"\n") if l]
        doubled = b"\n".join(lines + lines) + b"\n"
        with open(fpath, "wb") as fh:
            fh.write(doubled)


# ---------------------------------------------------------------------------
# Seeded multi-edge log factory
# ---------------------------------------------------------------------------

def _build_typed_edge_log(log_dir: str) -> dict:
    """
    Build a seeded multi-edge log exercising all five edge types.

    Items: alpha, beta, gamma, delta (four items)

    Edge scenario (hand-computed final state):
      blocks:         alpha → beta     (add at seq=5)
      blocks:         alpha → gamma    (add at seq=6, rm at seq=7, add at seq=8)
                      final: alpha → gamma IS in blocks (last op = add, seq=8)
      parent-child:   alpha → beta     (add at seq=9)
      discovered-from: beta → delta    (add at seq=10)
      related:        gamma → delta    (add at seq=11)
      supersedes:     delta → gamma    (add at seq=12)
      related:        alpha → beta     (add at seq=13; same (from,to) as blocks but different type)
      related:        alpha → beta     (rm at seq=14; removes related but leaves blocks intact)

    Hand-computed final state:
      edges["blocks"]          = {(alpha, beta), (alpha, gamma)}
      edges["parent-child"]    = {(alpha, beta)}
      edges["discovered-from"] = {(beta, delta)}
      edges["related"]         = {(gamma, delta)}  — (alpha, beta) related was added then removed
      edges["supersedes"]      = {(delta, gamma)}
      deps                     = {(alpha, beta), (alpha, gamma)}  — alias for blocks
    """
    prev = ""

    e = _append(log_dir, seq=0, actor=ACTOR_A, etype="ergon.created",
                payload={"repo": "typed-edge-test"}, prev=prev)
    prev = e["id"]

    e = _item(log_dir, seq=1, actor=ACTOR_A, item_id="alpha", title="Alpha", prev=prev)
    prev = e["id"]
    e = _item(log_dir, seq=2, actor=ACTOR_A, item_id="beta", title="Beta", prev=prev)
    prev = e["id"]
    e = _item(log_dir, seq=3, actor=ACTOR_A, item_id="gamma", title="Gamma", prev=prev)
    prev = e["id"]
    e = _item(log_dir, seq=4, actor=ACTOR_A, item_id="delta", title="Delta", prev=prev)
    prev = e["id"]

    # blocks: alpha → beta (add, permanent)
    e = _dep_add(log_dir, seq=5, actor=ACTOR_A, from_id="alpha", to_id="beta",
                 edge_type="blocks", prev=prev)
    prev = e["id"]

    # blocks: alpha → gamma (add → rm → add pattern)
    e = _dep_add(log_dir, seq=6, actor=ACTOR_A, from_id="alpha", to_id="gamma",
                 edge_type="blocks", prev=prev)
    prev = e["id"]
    e = _dep_rm(log_dir, seq=7, actor=ACTOR_A, from_id="alpha", to_id="gamma",
                edge_type="blocks", prev=prev)
    prev = e["id"]
    e = _dep_add(log_dir, seq=8, actor=ACTOR_A, from_id="alpha", to_id="gamma",
                 edge_type="blocks", prev=prev)
    prev = e["id"]

    # parent-child: alpha → beta
    e = _dep_add(log_dir, seq=9, actor=ACTOR_A, from_id="alpha", to_id="beta",
                 edge_type="parent-child", prev=prev)
    prev = e["id"]

    # discovered-from: beta → delta
    e = _dep_add(log_dir, seq=10, actor=ACTOR_A, from_id="beta", to_id="delta",
                 edge_type="discovered-from", prev=prev)
    prev = e["id"]

    # related: gamma → delta
    e = _dep_add(log_dir, seq=11, actor=ACTOR_A, from_id="gamma", to_id="delta",
                 edge_type="related", prev=prev)
    prev = e["id"]

    # supersedes: delta → gamma
    e = _dep_add(log_dir, seq=12, actor=ACTOR_A, from_id="delta", to_id="gamma",
                 edge_type="supersedes", prev=prev)
    prev = e["id"]

    # related: alpha → beta (same from,to pair as blocks — cross-type independence test)
    # Add then remove — proves (blocks, alpha, beta) is unaffected by related rm.
    e = _dep_add(log_dir, seq=13, actor=ACTOR_A, from_id="alpha", to_id="beta",
                 edge_type="related", prev=prev)
    prev = e["id"]
    e = _dep_rm(log_dir, seq=14, actor=ACTOR_A, from_id="alpha", to_id="beta",
                edge_type="related", prev=prev)
    prev = e["id"]

    # Return the hand-computed expected final typed-edge state.
    expected_edges = {
        "blocks":          {("alpha", "beta"), ("alpha", "gamma")},
        "parent-child":    {("alpha", "beta")},
        "discovered-from": {("beta", "delta")},
        "related":         {("gamma", "delta")},
        "supersedes":      {("delta", "gamma")},
    }
    return expected_edges


# ---------------------------------------------------------------------------
# (a) + (b) + (c): fold matches expected; order-independent; idempotent
# ---------------------------------------------------------------------------

class TestTypedEdgeFold:
    """Typed edge fold: order-independent, idempotent, cross-type-independent."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmpdir, "log")
        os.makedirs(self.log_dir, exist_ok=True)
        self.expected_edges = _build_typed_edge_log(self.log_dir)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _assert_edges(self, state: dict, label: str = "") -> None:
        """Assert the fold state's edges match the hand-computed expectation."""
        edges = state.get("edges", {})
        for etype, expected_pairs in self.expected_edges.items():
            actual = edges.get(etype, set())
            assert actual == expected_pairs, (
                f"{label}edges[{etype!r}] mismatch.\n"
                f"  Expected: {sorted(expected_pairs)}\n"
                f"  Got:      {sorted(actual)}"
            )
        # state["deps"] must be the alias for blocks.
        assert state.get("deps", set()) == self.expected_edges["blocks"], (
            f"{label}state['deps'] (blocks alias) mismatch.\n"
            f"  Expected: {sorted(self.expected_edges['blocks'])}\n"
            f"  Got:      {sorted(state.get('deps', set()))}"
        )

    def test_fold_matches_hand_computed(self):
        """(a) Folded typed-edge state matches the hand-computed expectation."""
        state = _fold_dir(self.log_dir)
        self._assert_edges(state, label="(a) ")

    def test_order_independent(self):
        """(b) Shuffle the event lines → identical typed-edge state."""
        # Fold before shuffle.
        state_before = _fold_dir(self.log_dir)

        # Shuffle on-disk line order.
        _shuffle_shards(self.log_dir)

        # Fold after shuffle — must be identical.
        state_after = _fold_dir(self.log_dir)

        self._assert_edges(state_before, label="(b) before-shuffle ")
        self._assert_edges(state_after,  label="(b) after-shuffle ")

        # The full edges dicts must match (not just the expected subset).
        assert state_before.get("edges", {}) == state_after.get("edges", {}), (
            "(b) Full edges dict differs after shuffle — fold is NOT order-independent.\n"
            f"Before: {state_before.get('edges', {})}\n"
            f"After:  {state_after.get('edges', {})}"
        )

    def test_idempotent(self):
        """(c) Duplicate any edge event line → identical state (idempotent fold)."""
        # Fold before duplication.
        state_before = _fold_dir(self.log_dir)

        # Duplicate all lines.
        _duplicate_all_lines(self.log_dir)

        # Fold after duplication — must be identical.
        state_after = _fold_dir(self.log_dir)

        self._assert_edges(state_before, label="(c) before-dup ")
        self._assert_edges(state_after,  label="(c) after-dup ")

        assert state_before.get("edges", {}) == state_after.get("edges", {}), (
            "(c) Full edges dict differs after duplication — fold is NOT idempotent.\n"
            f"Before: {state_before.get('edges', {})}\n"
            f"After:  {state_after.get('edges', {})}"
        )


# ---------------------------------------------------------------------------
# (d) Cross-type independence
# ---------------------------------------------------------------------------

class TestCrossTypeIndependence:
    """
    An rm on (related, A, B) leaves (blocks, A, B) intact, and vice versa.

    The fold's
    edge store is keyed by (type, from_id, to_id), so operations on one
    type have NO effect on another type for the same (from, to) pair.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmpdir, "log")
        os.makedirs(self.log_dir, exist_ok=True)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_cross_type_log(self) -> None:
        """
        Build a log with blocks and related both on (A, B), then remove related.

        Expected final state:
          edges["blocks"]  = {("item-a", "item-b")}   -- survives the related rm
          edges["related"] = set()                      -- removed
        """
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-a", title="Item A", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-b", title="Item B", prev=prev)
        prev = e["id"]

        # Add blocks (item-a, item-b).
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="blocks", prev=prev)
        prev = e["id"]

        # Add related (item-a, item-b) — same pair, different type.
        e = _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="related", prev=prev)
        prev = e["id"]

        # Remove related (item-a, item-b) — must NOT remove blocks.
        e = _dep_rm(self.log_dir, seq=4, actor=ACTOR_A,
                    from_id="item-a", to_id="item-b",
                    edge_type="related", prev=prev)
        prev = e["id"]

    def test_rm_related_leaves_blocks_intact(self):
        """(d) rm(related, A, B) must NOT remove (blocks, A, B)."""
        self._build_cross_type_log()
        state = _fold_dir(self.log_dir)
        edges = state.get("edges", {})

        # blocks edge must survive.
        blocks = edges.get("blocks", set())
        assert ("item-a", "item-b") in blocks, (
            "(d) blocks(item-a, item-b) was removed by rm(related, item-a, item-b).\n"
            f"edges['blocks'] = {sorted(blocks)}\n"
            "The fold's (type, from, to) keying must make types independent."
        )

        # related edge must be gone.
        related = edges.get("related", set())
        assert ("item-a", "item-b") not in related, (
            "(d) related(item-a, item-b) was NOT removed despite a dep.removed event.\n"
            f"edges['related'] = {sorted(related)}"
        )

        # state["deps"] alias must reflect blocks (unchanged).
        deps = state.get("deps", set())
        assert ("item-a", "item-b") in deps, (
            "(d) state['deps'] (blocks alias) lost (item-a, item-b) after rm(related).\n"
            f"deps = {sorted(deps)}"
        )

    def test_rm_blocks_leaves_related_intact(self):
        """(d) converse: rm(blocks, A, B) must NOT remove (related, A, B)."""
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-a", title="Item A", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-b", title="Item B", prev=prev)
        prev = e["id"]

        # Add both blocks and related.
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="blocks", prev=prev)
        prev = e["id"]
        e = _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="related", prev=prev)
        prev = e["id"]

        # Remove blocks.
        e = _dep_rm(self.log_dir, seq=4, actor=ACTOR_A,
                    from_id="item-a", to_id="item-b",
                    edge_type="blocks", prev=prev)
        prev = e["id"]

        state = _fold_dir(self.log_dir)
        edges = state.get("edges", {})

        # related must survive.
        related = edges.get("related", set())
        assert ("item-a", "item-b") in related, (
            "(d) related(item-a, item-b) was removed by rm(blocks, item-a, item-b).\n"
            f"edges['related'] = {sorted(related)}"
        )

        # blocks must be gone.
        blocks = edges.get("blocks", set())
        assert ("item-a", "item-b") not in blocks, (
            "(d) blocks(item-a, item-b) was NOT removed despite a dep.removed event.\n"
            f"edges['blocks'] = {sorted(blocks)}"
        )

        # deps alias must be empty.
        deps = state.get("deps", set())
        assert ("item-a", "item-b") not in deps, (
            "(d) state['deps'] still contains (item-a, item-b) after rm(blocks).\n"
            f"deps = {sorted(deps)}"
        )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestReadinessBlocksOnly:
    """
    Non-blocks typed edges must NOT gate readiness.

    compute_ready / compute_next must be identical whether or not parent-child,
    discovered-from, related, or supersedes edges are present.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmpdir, "log")
        os.makedirs(self.log_dir, exist_ok=True)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_non_blocks_edges_do_not_affect_ready(self):
        """parent-child, discovered-from, related, supersedes don't gate readiness."""
        from pinax.fold import compute_ready, compute_next

        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-x", title="X", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-y", title="Y", prev=prev)
        prev = e["id"]

        # Add all four non-blocks edge types from x to y.
        for i, etype in enumerate(
            ["parent-child", "discovered-from", "related", "supersedes"], start=2
        ):
            e = _dep_add(self.log_dir, seq=i, actor=ACTOR_A,
                         from_id="item-x", to_id="item-y",
                         edge_type=etype, prev=prev)
            prev = e["id"]

        state = _fold_dir(self.log_dir)
        ready = compute_ready(state)

        # BOTH items must be ready (no blocks edges → nothing gating either).
        assert "item-x" in ready, (
            "item-x is NOT ready despite having no blocks predecessors.\n"
            "Non-blocks edges must not gate readiness."
        )
        assert "item-y" in ready, (
            "item-y is NOT ready despite having no blocks predecessors.\n"
            "parent-child/discovered-from/related/supersedes must NOT gate readiness.\n"
            f"state['deps'] = {sorted(state.get('deps', set()))}"
        )

    def test_blocks_edge_still_gates_ready(self):
        """blocks edge still prevents to_id from being ready until from_id is done."""
        from pinax.fold import compute_ready

        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-x", title="X", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-y", title="Y", prev=prev)
        prev = e["id"]

        # Add a blocks edge: x blocks y.
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-x", to_id="item-y",
                     edge_type="blocks", prev=prev)
        prev = e["id"]

        state = _fold_dir(self.log_dir)
        ready = compute_ready(state)

        assert "item-x" in ready, "item-x (blocker) should be ready."
        assert "item-y" not in ready, (
            "item-y should NOT be ready because item-x blocks it."
        )

    @pytest.mark.parametrize("seed", [0, 1, 42])
    def test_readiness_seed_independent(self, seed: int):
        """Ready set is identical under different PYTHONHASHSEED values (tested via RNG)."""
        from pinax.fold import compute_ready

        rng = random.Random(seed)
        prev = ""

        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-p", title="P", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-q", title="Q", prev=prev)
        prev = e["id"]

        # Add a blocks: p → q.
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-p", to_id="item-q",
                     edge_type="blocks", prev=prev)
        prev = e["id"]

        # Shuffle the log shards (seed-controlled).
        _shuffle_shards_with_rng(self.log_dir, rng)

        state = _fold_dir(self.log_dir)
        ready = compute_ready(state)

        assert "item-p" in ready, f"(seed={seed}) item-p not ready."
        assert "item-q" not in ready, f"(seed={seed}) item-q should not be ready (blocked by item-p)."


def _shuffle_shards_with_rng(log_dir: str, rng: random.Random) -> None:
    """Shuffle shards using the given RNG (for parametrize isolation)."""
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        fpath = os.path.join(log_dir, fname)
        with open(fpath, "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = [l for l in normalised.split(b"\n") if l]
        rng.shuffle(lines)
        shuffled = b"\n".join(lines) + b"\n"
        with open(fpath, "wb") as fh:
            fh.write(shuffled)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestParentChildCycleWarn:
    """
    The fold must warn on a parent-child cycle without hanging and without
    changing readiness or the blocks-cycle behaviour.

    The check protects the parent-child graph walk from a DAG assumption.

    API boundary check:
    - WARN ONLY — do NOT change readiness, do NOT block, do NOT touch other
      edge types.
    - The blocks cycle detector (compute_ready) is byte-identical to before.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_dir = os.path.join(self.tmpdir, "log")
        os.makedirs(self.log_dir, exist_ok=True)
        yield
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _build_pc_cycle(self) -> None:
        """
        Build a log with a parent-child cycle: A → B → A.

        Items: item-a, item-b
        Edges: parent-child(a → b), parent-child(b → a)  ← cycle
        """
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-a", title="A", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-b", title="B", prev=prev)
        prev = e["id"]
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="parent-child", prev=prev)
        prev = e["id"]
        _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                 from_id="item-b", to_id="item-a",
                 edge_type="parent-child", prev=prev)

    def test_parent_child_cycle_warns_without_hang(self):
        """
        A parent-child cycle must surface a warning and NOT hang.

        The fold must complete (no infinite loop in the cycle detector).
        The warning must appear in state["report"]["warnings"].
        """
        self._build_pc_cycle()
        state = _fold_dir(self.log_dir)

        # The fold must have completed (no hang — we got here).
        # Verify the warning was surfaced.
        warnings = state.get("report", {}).get("warnings", [])
        parent_child_warnings = [w for w in warnings if "parent-child cycle" in w]
        assert parent_child_warnings, (
            "Expected at least one 'parent-child cycle' warning in report.warnings, "
            f"got zero.\nAll warnings: {warnings}"
        )

    def test_parent_child_cycle_does_not_change_readiness(self):
        """
        A parent-child cycle must NOT affect readiness (blocks-only gates readiness).

        Both items must remain ready (no blocks edges → nothing gating either).
        """
        from pinax.fold import compute_ready
        self._build_pc_cycle()
        state = _fold_dir(self.log_dir)

        ready = compute_ready(state)
        # Both items are queued with no blocks predecessors → both must be ready.
        assert "item-a" in ready, (
            "item-a is NOT ready despite having no blocks predecessors.\n"
            "Parent-child cycle must NOT gate readiness.\n"
            f"ready={ready}"
        )
        assert "item-b" in ready, (
            "item-b is NOT ready despite having no blocks predecessors.\n"
            "Parent-child cycle must NOT gate readiness.\n"
            f"ready={ready}"
        )

    def test_blocks_cycle_behaviour_unchanged(self):
        """
        The blocks cycle detector behaviour is unchanged by the parent-child detector.

        A blocks cycle must still cause items to be excluded from the ready set.
        Note: the blocks cycle warning is emitted by compute_ready() (not fold_events()),
        so we verify the behaviour via compute_ready() directly.
        """
        from pinax.fold import compute_ready
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-p", title="P", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-q", title="Q", prev=prev)
        prev = e["id"]
        # blocks cycle: P → Q → P
        e = _dep_add(self.log_dir, seq=2, actor=ACTOR_A,
                     from_id="item-p", to_id="item-q",
                     edge_type="blocks", prev=prev)
        prev = e["id"]
        _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                 from_id="item-q", to_id="item-p",
                 edge_type="blocks", prev=prev)

        state = _fold_dir(self.log_dir)

        # compute_ready() emits the blocks cycle warning into state["report"]["warnings"].
        ready = compute_ready(state)

        # Items in a blocks cycle are excluded from ready.
        assert "item-p" not in ready, (
            "item-p should NOT be ready (in a blocks cycle).\n"
            f"ready={ready}"
        )
        assert "item-q" not in ready, (
            "item-q should NOT be ready (in a blocks cycle).\n"
            f"ready={ready}"
        )

        # The blocks cycle warning must be in the report after compute_ready().
        warnings = state.get("report", {}).get("warnings", [])
        blocks_warnings = [w for w in warnings if "dep cycle detected" in w]
        assert blocks_warnings, (
            "Expected 'dep cycle detected' warning in report.warnings after compute_ready(); "
            f"got none.\nAll warnings: {warnings}"
        )

    def test_parent_child_cycle_three_nodes_warns(self):
        """
        A three-node parent-child cycle (A → B → C → A) must surface a warning.
        """
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-a", title="A", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-b", title="B", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=2, actor=ACTOR_A,
                  item_id="item-c", title="C", prev=prev)
        prev = e["id"]
        e = _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="parent-child", prev=prev)
        prev = e["id"]
        e = _dep_add(self.log_dir, seq=4, actor=ACTOR_A,
                     from_id="item-b", to_id="item-c",
                     edge_type="parent-child", prev=prev)
        prev = e["id"]
        _dep_add(self.log_dir, seq=5, actor=ACTOR_A,
                 from_id="item-c", to_id="item-a",
                 edge_type="parent-child", prev=prev)

        state = _fold_dir(self.log_dir)
        warnings = state.get("report", {}).get("warnings", [])
        pc_warnings = [w for w in warnings if "parent-child cycle" in w]
        assert pc_warnings, (
            "Expected 'parent-child cycle' warning for 3-node cycle; got none.\n"
            f"All warnings: {warnings}"
        )

    def test_acyclic_parent_child_no_warning(self):
        """
        An acyclic parent-child graph (A → B → C) must NOT produce any warning.
        """
        prev = ""
        e = _item(self.log_dir, seq=0, actor=ACTOR_A,
                  item_id="item-a", title="A", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=1, actor=ACTOR_A,
                  item_id="item-b", title="B", prev=prev)
        prev = e["id"]
        e = _item(self.log_dir, seq=2, actor=ACTOR_A,
                  item_id="item-c", title="C", prev=prev)
        prev = e["id"]
        e = _dep_add(self.log_dir, seq=3, actor=ACTOR_A,
                     from_id="item-a", to_id="item-b",
                     edge_type="parent-child", prev=prev)
        prev = e["id"]
        _dep_add(self.log_dir, seq=4, actor=ACTOR_A,
                 from_id="item-b", to_id="item-c",
                 edge_type="parent-child", prev=prev)

        state = _fold_dir(self.log_dir)
        warnings = state.get("report", {}).get("warnings", [])
        pc_warnings = [w for w in warnings if "parent-child cycle" in w]
        assert not pc_warnings, (
            "Unexpected 'parent-child cycle' warning for acyclic graph.\n"
            f"Warnings: {pc_warnings}"
        )


# ---------------------------------------------------------------------------
# PYTHONHASHSEED independence (run under seeds 0, 1, random)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [0, 1, 42])
def test_typed_edge_fold_seed_independent(seed: int):
    """
    Typed edge fold is identical under different PYTHONHASHSEED values.

    We test this by seeding the shuffle of the on-disk log lines using a
    known RNG seed — the fold must produce the same result regardless of
    which physical line order is presented (because read_events sorts by
    total-order key).
    """
    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        expected_edges = _build_typed_edge_log(log_dir)

        # Shuffle using the test seed.
        rng = random.Random(seed)
        _shuffle_shards_with_rng(log_dir, rng)

        state = _fold_dir(log_dir)
        edges = state.get("edges", {})

        for etype, expected_pairs in expected_edges.items():
            actual = edges.get(etype, set())
            assert actual == expected_pairs, (
                f"(seed={seed}) edges[{etype!r}] mismatch.\n"
                f"  Expected: {sorted(expected_pairs)}\n"
                f"  Got:      {sorted(actual)}"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
