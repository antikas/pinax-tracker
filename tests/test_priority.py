"""Priority command and next-item ordering tests."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import compute_next, compute_ready, fold_events, read_events
from pinax.replay import fold_at_ref

pytestmark = pytest.mark.deep


ACTOR = "operator@example.test"


# ---------------------------------------------------------------------------
# Part 1-3 helpers: raw filesystem event log (mirrors test_next_critical_path.py)
# ---------------------------------------------------------------------------

def _ts(seq: int) -> str:
    return f"2026-07-06T10:00:{seq:02d}Z"


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


def _duplicate_log(log_dir: str, duplicate_last_n: int) -> tuple[str, str]:
    """Copy the log, appending a duplicate of its last N lines (idempotency probe)."""
    all_lines = _all_lines(log_dir)
    dup = all_lines + all_lines[-duplicate_last_n:]

    parent = tempfile.mkdtemp()
    new_log_dir = os.path.join(parent, "log")
    os.makedirs(new_log_dir)
    with open(os.path.join(new_log_dir, "dup.jsonl"), "wb") as fh:
        for line in dup:
            fh.write(line + b"\n")
    return new_log_dir, parent


def _next_seq_and_prev(log_dir: str) -> tuple[int, str]:
    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0
    actor_events = [e for e in events if e.get("actor") == ACTOR]
    prev = actor_events[-1]["id"] if actor_events else ""
    return next_seq, prev


def _build_base_log() -> tuple[str, str]:
    """
    Seeded ready set with THREE distinct depths so priority overriding depth
    is unambiguous, not a tie-break artefact:

      X0 -> X1 -> X2 -> X3   (blocks chain; depth(X0) = 3, the deepest)
      A0 -> A1               (blocks chain; depth(A0) = 1)
      I                      (no edges; depth(I) = 0)

    S0 ready set: {X0, A0, I}.  Baseline (no priority events at all):
    compute_next picks X0 because the deepest chain wins.
    """
    parent = tempfile.mkdtemp()
    log_dir = os.path.join(parent, "log")
    os.makedirs(log_dir)

    prev = ""
    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "priority-test"}, prev=prev)
    prev = e["id"]
    e = _append(log_dir, seq=1, actor=ACTOR, etype="phase.opened",
                payload={"phase": "phase-1"}, prev=prev)
    prev = e["id"]

    ids = ("X0", "X1", "X2", "X3", "A0", "A1", "I")
    for i, item_id in enumerate(ids):
        e = _append(log_dir, seq=2 + i, actor=ACTOR, etype="item.created",
                    payload={"item_id": item_id, "title": item_id,
                             "prefix": "pnx", "status": "queued"},
                    prev=prev)
        prev = e["id"]

    blocks_edges = [("X0", "X1"), ("X1", "X2"), ("X2", "X3"), ("A0", "A1")]
    seq = 2 + len(ids)
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


# ---------------------------------------------------------------------------
# 1. Priority overrides depth ordering
# ---------------------------------------------------------------------------

class TestPriorityOverridesDepth:
    def test_baseline_no_priority_deepest_wins(self, base_log):
        """Sanity:  depth ordering, unchanged, before any priority exists."""
        log_dir, _ = base_log
        state = _fold(log_dir)
        assert set(compute_ready(state)) == {"X0", "A0", "I"}
        assert compute_next(state) == "X0"

    def test_single_priority_beats_deepest_unprioritised(self, base_log):
        """
        Giving the SHALLOWEST item (I, depth 0) an explicit priority makes it
        beat X0 (depth 3) — priority is spliced ABOVE critical-path depth.
        """
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "I", "priority": 5}, prev=prev)

        state = _fold(log_dir)
        assert set(compute_ready(state)) == {"X0", "A0", "I"}, "readiness must be unchanged"
        assert compute_next(state) == "I", "explicit priority must beat unprioritised depth"

    def test_lower_rank_wins_among_prioritised(self, base_log):
        """Among two explicitly-prioritised items, the lower rank value wins."""
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                    payload={"item_id": "I", "priority": 5}, prev=prev)
        seq += 1
        prev = e["id"]
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "A0", "priority": 3}, prev=prev)

        state = _fold(log_dir)
        assert compute_next(state) == "A0", "rank 3 must beat rank 5, regardless of depth"

    def test_latest_priority_set_wins_for_same_item(self, base_log):
        """
        Two item.priority_set events on the SAME item: latest-by-total-order
        wins (same discipline as item.status_changed) — a later, more urgent
        rank supersedes an earlier one.
        """
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                    payload={"item_id": "I", "priority": 5}, prev=prev)
        seq += 1
        prev = e["id"]
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                    payload={"item_id": "A0", "priority": 3}, prev=prev)
        seq += 1
        prev = e["id"]
        # I's priority is REVISED to -10 (more urgent than A0's 3) by a second event.
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "I", "priority": -10}, prev=prev)

        state = _fold(log_dir)
        assert state["items"]["I"]["priority"] == -10
        assert compute_next(state) == "I", "the LATEST priority_set for I must win"

    def test_unprioritised_items_still_ordered_by_depth(self, base_log):
        """
        Give ONLY A0 a priority; X0 and I remain unprioritised and must still
        be ordered relative to EACH OTHER by depth if A0 is later marked done
        (removing it from the ready set) — tier-1 ordering among the
        remaining un-prioritised items is untouched by the presence of a
        priority event elsewhere in the log.
        """
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                    payload={"item_id": "A0", "priority": 0}, prev=prev)
        seq += 1
        prev = e["id"]
        # Mark A0 done so it leaves the ready set entirely.
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.status_changed",
                payload={"item_id": "A0", "status": "done"}, prev=prev)

        state = _fold(log_dir)
        ready = set(compute_ready(state))
        assert "A0" not in ready
        assert ready == {"X0", "I", "A1"}, f"unexpected ready set: {ready}"
        # X0 (depth 3) and I (depth 0) are both unprioritised -> depth decides.
        assert compute_next(state) == "X0"


# ---------------------------------------------------------------------------
# 2. Absence of priority is a no-op
# ---------------------------------------------------------------------------

def test_no_priority_events_anywhere_matches_pre_feature_tuple(base_log):
    """
    With zero item.priority_set events anywhere in the log, compute_next's
    winner must be IDENTICAL to the hand-computed pre-existing
    (phase, -depth, age, id) tuple — the priority_tier/priority_rank pair is
    a uniform constant (1, 0) for every item, so it cannot perturb the
    result. Mutation-sensitive: if priority_tier/rank leaked a non-constant
    default (e.g. per-item id-derived), this would likely diverge.
    """
    log_dir, _ = base_log
    state = _fold(log_dir)
    items = state.get("items", {})
    ready = compute_ready(state)

    # Compute the baseline tuple directly, independent of compute_next.
    from pinax.fold import _compute_critical_path_depths
    depths = _compute_critical_path_depths(state)
    old_style_next = min(
        ready,
        key=lambda iid: (
            -depths.get(iid, 0),
            items[iid].get("created_at", ""),
            items[iid].get("event_id", ""),
            iid,
        ),
    )
    assert old_style_next == "X0"
    assert compute_next(state) == old_style_next, (
        "compute_next must match the pre-ua3b ordering exactly when no "
        "priority events exist"
    )
    # No item carries a 'priority' key at all.
    assert all("priority" not in item for item in items.values())


# ---------------------------------------------------------------------------
# 3. Fold-determinism / replay of item.priority_set
# ---------------------------------------------------------------------------

class TestFoldDeterminism:
    @pytest.mark.parametrize("seed", [1, 42, 2026])
    def test_order_independent(self, base_log, seed):
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        e = _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                    payload={"item_id": "I", "priority": 5}, prev=prev)
        seq += 1
        prev = e["id"]
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "A0", "priority": 3}, prev=prev)

        shuffled_log_dir, shuffled_parent = _shuffle_log(log_dir, seed)
        try:
            state = _fold(shuffled_log_dir)
            assert state["items"]["I"]["priority"] == 5
            assert state["items"]["A0"]["priority"] == 3
            assert compute_next(state) == "A0"
        finally:
            shutil.rmtree(shuffled_parent, ignore_errors=True)

    def test_idempotent_duplicate_priority_event(self, base_log):
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "I", "priority": 5}, prev=prev)

        canonical = _fold(log_dir)
        dup_log_dir, dup_parent = _duplicate_log(log_dir, duplicate_last_n=1)
        try:
            dup_state = _fold(dup_log_dir)
            assert dup_state["items"]["I"]["priority"] == canonical["items"]["I"]["priority"]
            assert compute_next(dup_state) == compute_next(canonical)
        finally:
            shutil.rmtree(dup_parent, ignore_errors=True)

    def test_unknown_item_priority_set_ignored(self, base_log):
        """item.priority_set for a never-created item_id is ignored (warn, no crash)."""
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "ghost", "priority": -100}, prev=prev)

        state = _fold(log_dir)
        assert "ghost" not in state.get("items", {})
        assert compute_next(state) == "X0", "unaffected -- baseline ordering must hold"

    def test_non_integer_priority_ignored(self, base_log):
        """A malformed payload (non-int priority) is ignored, not applied."""
        log_dir, _ = base_log
        seq, prev = _next_seq_and_prev(log_dir)
        _append(log_dir, seq=seq, actor=ACTOR, etype="item.priority_set",
                payload={"item_id": "I", "priority": "not-a-number"}, prev=prev)

        state = _fold(log_dir)
        assert "priority" not in state["items"]["I"]
        assert compute_next(state) == "X0"


# ---------------------------------------------------------------------------
# Git subprocess helpers for parts 3b (real replay) / 4 / 5.
# ---------------------------------------------------------------------------

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"


def _build_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing if existing else "")
    return env


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=_build_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git not available on PATH",
)


def _pinax(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, capture_output=True, text=True, env=_build_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _init_repo(repo: str) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@pinax.test")
    _git(repo, "config", "user.name", "Pinax Test")
    _git(repo, "config", "core.autocrlf", "false")
    with open(os.path.join(repo, ".gitattributes"), "w", newline="\n") as fh:
        fh.write(_GITATTRIBUTES)


def _commit_all(repo: str, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _head_sha(repo: str) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _count_log_lines(repo: str) -> int:
    log_dir = os.path.join(repo, ".ergon", "log")
    total = 0
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, fname), "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        total += len([l for l in normalised.split(b"\n") if l])
    return total


@pytest.fixture()
def cli_repo(tmp_path):
    """A committed pinax repo with one item ('Item X'), ready for CLI tests."""
    root = str(tmp_path)
    _init_repo(root)
    r = _pinax(root, "init", "--actor", ACTOR)
    assert r.returncode == 0, r.stderr
    _commit_all(root, "init: pinax ergon base")

    r = _pinax(root, "add", "--title", "Item X", "--prefix", "pnx", "--actor", ACTOR, "--json")
    assert r.returncode == 0, r.stderr
    item_id = json.loads(r.stdout)["item_id"]
    _commit_all(root, "add Item X")
    return root, item_id


# ---------------------------------------------------------------------------
# 3b. Real git-ref replay round-trip for item.priority_set
# ---------------------------------------------------------------------------

@requires_git
def test_replay_at_ref_reconstructs_priority_history(cli_repo):
    """
    fold_at_ref must reconstruct the exact historical priority state at each
    tagged checkpoint — before the priority event existed, and after — the
    same "time travel over the committed log" guarantee every other event
    type already has (test_replay.py), now proven for item.priority_set.
    """
    root, item_id = cli_repo

    _git(root, "tag", "prio-c1")
    c1_sha = _head_sha(root)

    r = _pinax(root, "priority", item_id, "7", "--actor", ACTOR, "--json")
    assert r.returncode == 0, r.stderr
    _commit_all(root, "set priority 7")
    _git(root, "tag", "prio-c2")
    c2_sha = _head_sha(root)

    state_c1 = fold_at_ref(root, "prio-c1")
    assert "priority" not in state_c1["items"][item_id], (
        "priority must NOT exist yet at the pre-priority checkpoint"
    )

    state_c2 = fold_at_ref(root, "prio-c2")
    assert state_c2["items"][item_id]["priority"] == 7

    # Replay by raw SHA matches replay by tag.
    state_c2_by_sha = fold_at_ref(root, c2_sha)
    assert state_c2_by_sha["items"][item_id]["priority"] == 7

    # Replay@c1 by sha also confirms no leakage backwards.
    state_c1_by_sha = fold_at_ref(root, c1_sha)
    assert "priority" not in state_c1_by_sha["items"][item_id]


# ---------------------------------------------------------------------------
# 4. CLI happy-path + invalid-rank / invalid-id error handling
# ---------------------------------------------------------------------------

@requires_git
def test_cli_explicit_rank_happy_path(cli_repo):
    root, item_id = cli_repo
    r = _pinax(root, "priority", item_id, "5", "--actor", ACTOR, "--json")
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout)
    assert result["item_id"] == item_id
    assert result["priority"] == 5
    assert result["type"] == "item.priority_set"


@requires_git
def test_cli_explicit_negative_rank_happy_path(cli_repo):
    root, item_id = cli_repo
    r = _pinax(root, "priority", item_id, "-3", "--actor", ACTOR, "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["priority"] == -3


@requires_git
def test_cli_top_and_bump(cli_repo):
    root, item_id = cli_repo
    r = _pinax(root, "add", "--title", "Item Y", "--prefix", "pnx", "--actor", ACTOR, "--json")
    item_y = json.loads(r.stdout)["item_id"]

    r = _pinax(root, "priority", item_id, "5", "--actor", ACTOR, "--json")
    assert json.loads(r.stdout)["priority"] == 5

    # 'top' on Item Y -> one below the current minimum (5) -> 4.
    r = _pinax(root, "priority", item_y, "top", "--actor", ACTOR, "--json")
    assert json.loads(r.stdout)["priority"] == 4

    # 'bump' on item_id (currently 5) -> one below its OWN current rank -> 4... but
    # Item Y already holds 4; bump only decrements item_id's own prior value (5 -> 4)
    # independent of Item Y's rank -- ties are a legitimate, deterministic outcome
    # resolved by compute_next's age/id tie-break, not by this command.
    r = _pinax(root, "priority", item_id, "bump", "--actor", ACTOR, "--json")
    assert json.loads(r.stdout)["priority"] == 4

    # 'bump' on an item with NO existing priority falls back to 'top' semantics:
    # one below the current minimum (now 4, held by both) -> 3.
    r = _pinax(root, "add", "--title", "Item Z", "--prefix", "pnx", "--actor", ACTOR, "--json")
    item_z = json.loads(r.stdout)["item_id"]
    r = _pinax(root, "priority", item_z, "bump", "--actor", ACTOR, "--json")
    assert json.loads(r.stdout)["priority"] == 3


@requires_git
def test_cli_invalid_rank_rejected_nothing_appended(cli_repo):
    root, item_id = cli_repo
    lines_before = _count_log_lines(root)
    r = _pinax(root, "priority", item_id, "not-a-number", "--actor", ACTOR, check=False)
    assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
    assert r.stderr.strip(), "expected an error message on stderr"
    assert _count_log_lines(root) == lines_before, (
        "validate-before-append: an invalid rank must not append anything"
    )


@requires_git
def test_cli_invalid_id_rejected_nothing_appended(cli_repo):
    root, item_id = cli_repo
    lines_before = _count_log_lines(root)
    r = _pinax(root, "priority", "pnx-doesnotexist", "5", "--actor", ACTOR, check=False)
    assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
    assert r.stderr.strip(), "expected an error message on stderr"
    assert _count_log_lines(root) == lines_before, (
        "validate-before-append: an unknown item id must not append anything"
    )


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

@requires_git
def test_all_branches_honours_branch_only_priority_over_depth():
    """
    A priority event that exists ONLY on an unmerged branch:
    - is invisible to the plain (current-branch) fold -> depth still decides.
    - is honoured under --all-branches -> priority overrides depth, exactly
      as it would if the event were local.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        root = tmpdir
        _init_repo(root)
        r = _pinax(root, "init", "--actor", ACTOR)
        assert r.returncode == 0, r.stderr
        _commit_all(root, "init: pinax ergon base")

        # DEEP0 -> DEEP1 blocks chain (depth(DEEP0) = 1); SHALLOW has no edges (depth 0).
        r = _pinax(root, "add", "--title", "DEEP0", "--prefix", "pnx", "--actor", ACTOR, "--json")
        deep0 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "add", "--title", "DEEP1", "--prefix", "pnx", "--actor", ACTOR, "--json")
        deep1 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "add", "--title", "SHALLOW", "--prefix", "pnx", "--actor", ACTOR, "--json")
        shallow = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "dep", "add", deep0, "--blocks", deep1, "--actor", ACTOR)
        assert r.returncode == 0, r.stderr
        _commit_all(root, "add DEEP0/DEEP1/SHALLOW + blocks edge")

        # Baseline (no priority anywhere yet): DEEP0 wins (depth 1 > 0).
        r = _pinax(root, "next", "--json")
        assert json.loads(r.stdout)["item_id"] == deep0

        # Branch-only: prioritise SHALLOW, committed on an unmerged branch only.
        _git(root, "checkout", "-b", "run/spine")
        r = _pinax(root, "priority", shallow, "0", "--actor", ACTOR, "--json")
        assert r.returncode == 0, r.stderr
        _commit_all(root, "prioritise SHALLOW")
        _git(root, "checkout", "main")

        # Plain fold: branch-only priority is invisible -> depth still decides.
        r = _pinax(root, "next", "--json")
        assert json.loads(r.stdout)["item_id"] == deep0, (
            "branch-only priority must NOT leak into the plain (current-branch) fold"
        )

        # --all-branches: the branch-only priority is honoured -> SHALLOW wins.
        # 'next' does not itself expose --all-branches (only ready/report/board do;
        # see docs/portfolio-views.md) -- confirm readiness is unaffected via 'ready
        # --all-branches', then prove the priority-aware "next" winner end-to-end
        # via the real 'report --all-branches --json' CLI surface (its "next"
        # field is exactly compute_next(union_state)).
        r = _pinax(root, "ready", "--all-branches", "--json")
        assert r.returncode == 0, r.stderr
        payload = json.loads(r.stdout)
        assert set(payload["ready"]) == {deep0, shallow}

        r = _pinax(root, "report", "--all-branches", "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["next"] == shallow, (
            "--all-branches report must honour the branch-only priority over "
            "the deeper unprioritised item"
        )

        # And re-confirm the plain (non-all-branches) report is untouched.
        r = _pinax(root, "report", "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["next"] == deep0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
