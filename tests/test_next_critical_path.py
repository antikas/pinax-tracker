"""Critical-path next-item selection tests."""

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


ACTOR = "operator@example.test"


# ---------------------------------------------------------------------------
# Low-level helpers (mirrors tests/test_ready_next.py conventions)
# ---------------------------------------------------------------------------

def _ts(seq: int) -> str:
    return f"2026-06-30T10:00:{seq:02d}Z"


def _append(log_dir: str, seq: int, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    event = mint_event(seq=seq, ts=_ts(seq), actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _fold(log_dir: str) -> dict:
    return fold_events(read_events(log_dir))


def _all_lines(log_dir: str) -> list[bytes]:
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
    return max(
        json.loads(line.decode("utf-8"))["seq"]
        for line in _all_lines(log_dir)
    )


def _copy_log(log_dir: str) -> tuple[str, str]:
    parent = tempfile.mkdtemp()
    new_log_dir = os.path.join(parent, "log")
    os.makedirs(new_log_dir)
    with open(os.path.join(new_log_dir, "copy.jsonl"), "wb") as fh:
        for line in _all_lines(log_dir):
            fh.write(line + b"\n")
    return new_log_dir, parent


# ---------------------------------------------------------------------------
# Seeded graph fixture: diamond (A/B/C/D) + chain (E/F/G/H) + shallow (I)
# ---------------------------------------------------------------------------

ITEM_IDS = ("A", "B", "C", "D", "E", "F", "G", "H", "I")


def _build_base_log() -> tuple[str, str]:
    """
    Build the seeded graph (S0: all queued, single phase).

    seq=0   ergon.created
    seq=1   phase.opened (phase-1)
    seq=2..10  item.created A,B,C,D,E,F,G,H,I
    seq=11  dep.added A blocks B
    seq=12  dep.added A blocks C
    seq=13  dep.added B blocks D
    seq=14  dep.added C blocks D
    seq=15  dep.added E blocks F
    seq=16  dep.added F blocks G
    seq=17  dep.added G blocks H

    S0 ready set: {A, E, I}  (none of them are blocked; B/C/D/F/G/H blocked)
    Hand-computed depths: A=2, E=3, I=0  -> deepest is E -> next = E
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

    for i, item_id in enumerate(ITEM_IDS):
        seq = 2 + i
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id,
                             "prefix": "pnx", "status": "queued"},
                    prev=prev)
        prev = e["id"]

    blocks_edges = [
        ("A", "B"), ("A", "C"), ("B", "D"), ("C", "D"),
        ("E", "F"), ("F", "G"), ("G", "H"),
    ]
    seq = 2 + len(ITEM_IDS)
    for from_id, to_id in blocks_edges:
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                    payload={"from_id": from_id, "to_id": to_id, "type": "blocks"},
                    prev=prev)
        prev = e["id"]
        seq += 1

    return log_dir, parent


@pytest.fixture
def base_log():
    log_dir, parent = _build_base_log()
    yield log_dir, parent
    shutil.rmtree(parent, ignore_errors=True)


def _next_seq_and_prev(log_dir: str) -> tuple[int, str]:
    """Return (next_seq, prev_id_of_last_event_in_actor_shard)."""
    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0
    actor_events = [e for e in events if e.get("actor") == ACTOR]
    prev = actor_events[-1]["id"] if actor_events else ""
    return next_seq, prev


# ---------------------------------------------------------------------------
# S0: hand-computed expectation — E is the deepest chain (depth 3) beats
# A (depth 2) and I (depth 0).  A flat/age ordering would pick A (created
# earliest at seq=2) — this is the mutation-sensitive proof.
# ---------------------------------------------------------------------------

class TestS0CriticalPath:
    EXPECTED_READY = frozenset({"A", "E", "I"})
    EXPECTED_NEXT = "E"  # deepest not-done chain: E->F->G->H, depth 3

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
        state = _fold(log_dir)
        self._check(state, "S0 canonical")

    def test_mutation_sensitive_flat_ordering_would_differ(self, base_log):
        """
        Prove the critical-path logic actually bites: a flat (age-only)
        ordering over the SAME ready set would pick A (created at seq=2,
        the earliest of {A, E, I}), not E.  If compute_next degenerated to
        flat/age-only ordering, this assertion would fail — the gate is
        mutation-sensitive, not a call-twice tautology.
        """
        log_dir, _ = base_log
        state = _fold(log_dir)
        ready = compute_ready(state)
        items = state.get("items", {})
        # Hand-compute flat (age, id) ordering for comparison.
        flat_next = min(
            ready,
            key=lambda iid: (items[iid].get("created_at", ""),
                              items[iid].get("event_id", ""), iid),
        )
        assert flat_next == "A", f"sanity: expected flat ordering to pick A, got {flat_next!r}"
        actual_next = compute_next(state)
        assert actual_next == "E", f"expected critical-path next=E, got {actual_next!r}"
        assert actual_next != flat_next, (
            "critical-path ordering must differ from flat ordering on this fixture "
            "-- if they match, the depth logic is not biting"
        )

    @pytest.mark.parametrize("seed", [42, 99, 137, 7, 2026])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"S0 shuffle seed={seed}")
        finally:
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Cross-edge-type invariance: add parent-child + related + discovered-from +
# supersedes edges among the SAME items -> compute_next must be UNCHANGED.
# ---------------------------------------------------------------------------

class TestCrossEdgeTypeInvariance:
    EXPECTED_NEXT = "E"

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        new_log_dir, parent = _copy_log(base_log_dir)
        seq, prev = _next_seq_and_prev(new_log_dir)

        # Non-blocks edges among the SAME items -- must not perturb ordering.
        # Note: parent-child edges are directional hierarchy; pick pairs that
        # don't collide with existing blocks semantics but touch every item.
        extra_edges = [
            ("parent-child", "I", "A"),
            ("related", "H", "D"),
            ("discovered-from", "D", "H"),
            ("supersedes", "I", "E"),
            ("related", "B", "C"),
        ]
        for edge_type, from_id, to_id in extra_edges:
            e = _append(new_log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                        payload={"from_id": from_id, "to_id": to_id, "type": edge_type},
                        prev=prev)
            prev = e["id"]
            seq += 1
        return new_log_dir, parent

    def test_canonical(self, base_log):
        log_dir, _ = base_log
        aug_log_dir, aug_parent = self._build(log_dir)
        try:
            state = _fold(aug_log_dir)
            # Readiness must be identical (blocks-only gate untouched).
            assert set(compute_ready(state)) == {"A", "E", "I"}
            nxt = compute_next(state)
            assert nxt == self.EXPECTED_NEXT, (
                f"non-blocks edges perturbed next: expected {self.EXPECTED_NEXT!r}, got {nxt!r}"
            )
        finally:
            shutil.rmtree(aug_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 137])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        aug_log_dir, aug_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(aug_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            assert set(compute_ready(state)) == {"A", "E", "I"}
            nxt = compute_next(state)
            assert nxt == self.EXPECTED_NEXT
        finally:
            shutil.rmtree(aug_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# done-terminates-the-chain: mark a mid-chain item done -> depth recomputes
# -> expected next changes.
#
# Mark G (mid-chain, blocks H) done.  Chain E->F->G->H: G is done, so from
# F's perspective, F blocks G but G is done -> chain terminates at G.
# F's new depth = 0 (its only successor G is done).  E's new depth = 1
# (E -> F, F is not done, F's depth 0 -> E's depth = 1).
# H is unblocked (its only blocker G is done) -> H becomes ready with depth 0.
# Ready set: {A, E, H, I}  (E depth=1, A depth=2, H depth=0, I depth=0)
# Expected next: A (depth 2; E's chain ends at G).
# ---------------------------------------------------------------------------

class TestDoneTerminatesChain:
    EXPECTED_READY = frozenset({"A", "E", "H", "I"})
    EXPECTED_NEXT = "A"  # A's depth (2) now exceeds E's truncated depth (1)

    def _build(self, base_log_dir: str) -> tuple[str, str]:
        new_log_dir, parent = _copy_log(base_log_dir)
        seq, prev = _next_seq_and_prev(new_log_dir)
        _append(new_log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                payload={"item_id": "G", "status": "done"}, prev=prev)
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
        done_log_dir, done_parent = self._build(log_dir)
        try:
            state = _fold(done_log_dir)
            self._check(state, "G-done canonical")
        finally:
            shutil.rmtree(done_parent, ignore_errors=True)

    def test_before_and_after_differ(self, base_log):
        """The expected next actually CHANGES when G completes (E -> A)."""
        log_dir, _ = base_log
        before_state = _fold(log_dir)
        before_next = compute_next(before_state)
        assert before_next == "E"

        done_log_dir, done_parent = self._build(log_dir)
        try:
            after_state = _fold(done_log_dir)
            after_next = compute_next(after_state)
            assert after_next == "A"
            assert after_next != before_next, (
                "marking a mid-chain item done must change the critical-path "
                "winner -- if it doesn't, depth is not being recomputed"
            )
        finally:
            shutil.rmtree(done_parent, ignore_errors=True)

    @pytest.mark.parametrize("seed", [42, 99])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        done_log_dir, done_parent = self._build(log_dir)
        shuffled_log_dir, shuffled_parent = _shuffle_log(done_log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            self._check(state, f"G-done shuffle seed={seed}")
        finally:
            shutil.rmtree(done_parent, ignore_errors=True)
            shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# depth walk over the rest of the graph still terminates finitely.
# ---------------------------------------------------------------------------

def _build_cycle_with_chain_log() -> tuple[str, str]:
    """
    A 2-item blocks cycle (cycX <-> cycY) alongside the free chain E->F->G->H
    (reusing the chain shape) plus a free single item I, in one log.
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    prev = ""
    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "cycle-critical-path"}, prev=prev)
    prev = e["id"]

    ids = ("cycX", "cycY", "E", "F", "G", "H", "I")
    for i, item_id in enumerate(ids):
        e = _append(log_dir, seq=1 + i, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id, "prefix": "pnx",
                             "status": "queued"},
                    prev=prev)
        prev = e["id"]

    seq = 1 + len(ids)
    cycle_edges = [("cycX", "cycY"), ("cycY", "cycX")]
    chain_edges = [("E", "F"), ("F", "G"), ("G", "H")]
    for from_id, to_id in cycle_edges + chain_edges:
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                    payload={"from_id": from_id, "to_id": to_id, "type": "blocks"},
                    prev=prev)
        prev = e["id"]
        seq += 1

    return log_dir, parent


def test_cycle_safe_finite_no_hang():
    """
    A blocks cycle does not hang compute_next; cyclic nodes are excluded from
    ready (as today), and the deepest not-done chain among the rest (E, depth
    3) is still correctly selected.
    """
    log_dir, root = _build_cycle_with_chain_log()
    try:
        state = _fold(log_dir)
        ready = set(compute_ready(state))
        assert "cycX" not in ready
        assert "cycY" not in ready
        assert ready == {"E", "I"}

        nxt = compute_next(state)
        assert nxt == "E", f"expected next='E' (deepest chain), got {nxt!r}"

        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("seed", [42, 99, 137])
def test_cycle_safe_order_independent(seed):
    log_dir, root = _build_cycle_with_chain_log()
    shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
    try:
        state = _fold(shuffled_log_dir)
        ready = set(compute_ready(state))
        assert ready == {"E", "I"}
        nxt = compute_next(state)
        assert nxt == "E"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# A ready item whose ONLY path to depth is a
# non-cyclic path that happens to reach a node ALSO reachable from a cyclic
# region must not have its depth under-counted by a memoised value computed
# reproducer, via the real event path: blocks edges k2->k1, k3->k1, k4->k0,
# k1->k0, k0->k2 (cycle k0->k2->k1->k0).  Ready = {k3, k4}.
#
# Hand-computed depths (edge-count of the longest not-done chain, cyclic
# nodes/edges excluded from the walk, matching compute_ready's exclusion):
#   k1, k0, k2 are ALL on the cycle k0->k2->k1->k0 -> excluded from the walk
#   graph entirely (every edge touching a cycle node is dropped).
#   k3's only edge is k3->k1; k1 is a cycle node -> that edge is dropped ->
#     k3 has NO surviving edges -> depth(k3) = 0.
#   k4's only edge is k4->k0; k0 is a cycle node -> that edge is dropped ->
#     k4 has NO surviving edges -> depth(k4) = 0.
#   Tie at depth 0 -> age/id tie-break decides (k3 was created before k4,
#   at seq=2 vs seq=3 in this fixture) -> next = k3.
#
# The fixture checks that cycle-connected edges are excluded before computing
# depths, so k3 and k4 both receive depth zero regardless of traversal order.
# ---------------------------------------------------------------------------

def _build_cycle_crossing_path_log() -> tuple[str, str]:
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    prev = ""
    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "cycle-crossing-path"}, prev=prev)
    prev = e["id"]

    ids = ("k0", "k1", "k2", "k3", "k4")
    for i, item_id in enumerate(ids):
        e = _append(log_dir, seq=1 + i, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id, "prefix": "pnx",
                             "status": "queued"},
                    prev=prev)
        prev = e["id"]

    seq = 1 + len(ids)
    # Cycle: k0 -> k2 -> k1 -> k0.  Crossing edges: k2->k1, k3->k1, k4->k0.
    blocks_edges = [
        ("k2", "k1"), ("k3", "k1"), ("k4", "k0"), ("k1", "k0"), ("k0", "k2"),
    ]
    for from_id, to_id in blocks_edges:
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                    payload={"from_id": from_id, "to_id": to_id, "type": "blocks"},
                    prev=prev)
        prev = e["id"]
        seq += 1

    return log_dir, parent


def test_cycle_crossing_path_depth_not_poisoned_by_memo_reuse():
    """
    A depth memoised under one DFS root's per-path cycle guard must not be
    reused (under-counted) for a different root that reaches the same node.
    Both k3 and k4 route into the SAME cyclic region (k0/k1/k2) via
    DIFFERENT single edges — if the bug were present, whichever of k3/k4 is
    walked SECOND would reuse a truncated memo from the first walk's
    path-guard and the two would diverge in a way that depends on walk
    order (non-deterministic-looking / order-fragile).  With the fix, every
    edge touching a cycle node is excluded from the walk graph up front, so
    k3 and k4 both correctly get depth 0 (their sole successor is a cycle
    node, hence excluded) regardless of which one the sorted node-order
    visits first.
    """
    log_dir, root = _build_cycle_crossing_path_log()
    try:
        state = _fold(log_dir)
        ready = set(compute_ready(state))
        assert ready == {"k3", "k4"}, f"expected ready={{k3,k4}}, got {ready}"

        from pinax.fold import _compute_critical_path_depths
        depths = _compute_critical_path_depths(state)
        assert depths.get("k3", 0) == depths.get("k4", 0), (
            f"k3 and k4 must have the SAME depth (both route solely into the "
            f"excluded cyclic region) -- got k3={depths.get('k3', 0)!r}, "
            f"k4={depths.get('k4', 0)!r} (order-dependent divergence == the bug)"
        )
        assert depths.get("k3", 0) == 0
        assert depths.get("k4", 0) == 0

        nxt = compute_next(state)
        # Tie at depth 0 -> age tie-break -> k3 (created first, seq=3 vs seq=4).
        assert nxt == "k3", f"expected next='k3' (age tie-break at equal depth), got {nxt!r}"

        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("seed", [42, 99, 137, 232, 175])
def test_cycle_crossing_path_order_independent(seed):
    log_dir, root = _build_cycle_crossing_path_log()
    shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
    try:
        state = _fold(shuffled_log_dir)
        ready = set(compute_ready(state))
        assert ready == {"k3", "k4"}
        nxt = compute_next(state)
        assert nxt == "k3"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(shuffled_parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Shallow single item alone: depth 0, no crash, matches expectation trivially.
# ---------------------------------------------------------------------------

def test_shallow_single_item_depth_zero():
    """A lone item with no blocks edges at all has depth 0 and is next when
    it's the only ready item — the degenerate base case."""
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)
    try:
        e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                    payload={"repo": "shallow"}, prev="")
        _append(log_dir, seq=1, actor=ACTOR, etype="item.created",
                payload={"item_id": "solo", "title": "Solo", "prefix": "pnx",
                         "status": "queued"},
                prev=e["id"])
        state = _fold(log_dir)
        assert compute_ready(state) == ["solo"]
        assert compute_next(state) == "solo"
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# LARGER than the single back-edge cycle _detect_dep_cycles' one-DFS-per-root
# existence check happens to record must be excluded from the depth walk in
# full, not just the reported back-edge subset.
#
# {n5->n1, n3->n0, n3->n2, n0->n4, n0->n3, n4->n3, n1->n4} plus an unrelated
# m->k.  The true SCC is {n0, n3, n4} (n0->n3->n4->n0 via n4->n3 is NOT a
# direct edge back to n0, but n0->n4->n3->n0 IS a cycle, and n0->n3->n0 is
# also a cycle — n4 sits on a cycle through n0/n3 via the extra chord edges
# n0->n4 and n4->n3).  _detect_dep_cycles' single DFS from n0 records only
# the back-edge cycle n0->n3->n0 (cycle_nodes={n0, n3}) and MISSES n4.
#
# The SCC is {n0, n3, n4}. Its connected edges are excluded from the depth
# walk, leaving n5 at depth one and m as the next item.
# ---------------------------------------------------------------------------

def _build_scc_chord_log() -> tuple[str, str]:
    """
    blocks: n5->n1, n3->n0, n3->n2, n0->n4, n0->n3, n4->n3, n1->n4, m->k,
    k->j.  n0, n3 are marked done (so the SIMPLE n0<->n3 back-edge cycle is
    "spent" from a readiness point of view, but n4 remains not-done and
    blocks-graph-cyclic via the chord edges n0->n4/n4->n3).  Ready set after
    n0/n3 done: {m, n2, n5} (n4 is still blocked by not-done predecessors and
    additionally sits on the residual cycle through n4->n3, so it is
    excluded from ready either way).

    m's chain is made STRICTLY deeper than n5's (m->k->j, depth 2) than the
    CORRECT depth of n5 (n5->n1 only, depth 1, once n4's chord-SCC
    membership is honoured) so the divergence is unambiguous on ordering
    alone, not an age-tie-break artefact: a depth(n5)=2
    (via the surviving edge n1->n4) would tie or
    beat m's depth-2 chain depending on age — this fixture's depths (m=2,
    correct n5=1 / buggy n5=2) make BOTH the correctness failure (wrong
    n5 depth) and its consequence (wrong dispatch, m should always win
    outright at depth 2 > 1) independently checkable.
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    prev = ""
    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "scc-chord"}, prev=prev)
    prev = e["id"]

    ids = ("n0", "n1", "n2", "n3", "n4", "n5", "m", "k", "j")
    for i, item_id in enumerate(ids):
        e = _append(log_dir, seq=1 + i, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id, "prefix": "pnx",
                             "status": "queued"},
                    prev=prev)
        prev = e["id"]

    seq = 1 + len(ids)
    blocks_edges = [
        ("n5", "n1"), ("n3", "n0"), ("n3", "n2"),
        ("n0", "n4"), ("n0", "n3"), ("n4", "n3"), ("n1", "n4"),
        ("m", "k"), ("k", "j"),
    ]
    for from_id, to_id in blocks_edges:
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="dep.added",
                    payload={"from_id": from_id, "to_id": to_id, "type": "blocks"},
                    prev=prev)
        prev = e["id"]
        seq += 1

    # Mark n0 and n3 done so n2/n5 become candidates for ready (n4 remains
    # not-done and still sits on a residual cycle via n4->n3->n0->n4).
    for done_id in ("n0", "n3"):
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                    payload={"item_id": done_id, "status": "done"}, prev=prev)
        prev = e["id"]
        seq += 1

    return log_dir, parent


def test_scc_chord_not_missed_by_single_dfs_detector():
    """
    A true SCC larger than the one back-edge cycle the single-DFS existence
    check records must be excluded from the depth walk in full.  Without the
    fix, n4 (a genuine SCC member missed by _detect_dep_cycles) survives into
    the depth walk, n5's depth is over-counted to 2, and compute_next
    dispatches n5 instead of the correct winner m.
    """
    log_dir, root = _build_scc_chord_log()
    try:
        state = _fold(log_dir)

        from pinax.fold import _compute_critical_path_depths, _strongly_connected_cycle_nodes
        deps = state.get("deps", set())
        cyclic = _strongly_connected_cycle_nodes(deps)
        assert cyclic == {"n0", "n3", "n4"}, (
            f"full SCC must include n4 (chord-connected) alongside the "
            f"back-edge pair n0/n3 -- got {cyclic}"
        )

        ready = set(compute_ready(state))
        assert ready == {"m", "n2", "n5"}, f"expected ready={{m,n2,n5}}, got {ready}"

        depths = _compute_critical_path_depths(state)
        # n5's only surviving edge is n5->n1 (n1->n4 is dropped: n4 is cyclic).
        assert depths.get("n5", 0) == 1, (
            f"n5's depth must be 1 (n5->n1 only; n1->n4 excluded because n4 "
            f"is a true SCC member) -- got {depths.get('n5', 0)} "
            f"(2 would mean the chord-connected SCC member n4 leaked in)"
        )
        assert depths.get("m", 0) == 2  # m -> k -> j

        nxt = compute_next(state)
        assert nxt == "m", (
            f"expected next='m' (correct winner once n4's chord-SCC "
            f"membership is honoured) -- got {nxt!r}"
        )

        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize("seed", [7, 23, 101, 256, 4242])
def test_scc_chord_order_independent(seed):
    log_dir, root = _build_scc_chord_log()
    shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
    try:
        state = _fold(shuffled_log_dir)
        ready = set(compute_ready(state))
        assert ready == {"m", "n2", "n5"}
        nxt = compute_next(state)
        assert nxt == "m"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(shuffled_parent, ignore_errors=True)
