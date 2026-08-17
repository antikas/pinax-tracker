"""Two-clone convergence tests for the deterministic event log."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Git subprocess helpers (same shape as tests/test_merge_safety.py and
# tests/test_reconcile_roundtrip.py — SSOT: this module does not reinvent
# them, it mirrors the existing precedent's small helper set).
# ---------------------------------------------------------------------------

_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"
_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(repo_root: str, *args: str, check: bool = True,
         env: dict | None = None) -> subprocess.CompletedProcess:
    _env = env if env is not None else _build_env()
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, env=_env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_root}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git not available on PATH")


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def _make_git_repo(path: str) -> str:
    os.makedirs(path)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "test@pinax.test")
    _git(path, "config", "user.name", "Pinax Test")
    _git(path, "config", "core.autocrlf", "false")
    return path


def _init_ergon(repo: str, actor: str = "operator@example.test") -> None:
    env = _build_env()
    ergon_dir = os.path.join(repo, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    ga_path = os.path.join(ergon_dir, ".gitattributes")
    with open(ga_path, "w", newline="\n") as fh:
        fh.write(_GITATTRIBUTES)

    r = subprocess.run(
        [sys.executable, "-m", "pinax", "init", "--actor", actor],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pinax init failed: {r.stderr}")

    _git(repo, "add", ".ergon")
    _git(repo, "commit", "-m", "init: pinax ergon base")


def _pinax(repo: str, *args: str, env=None, check: bool = True) -> subprocess.CompletedProcess:
    _env = env or _build_env()
    r = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, capture_output=True, text=True, env=_env,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo}:\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _commit_all(repo: str, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _fold_repo(repo: str) -> dict:
    from pinax.fold import fold_events, read_events
    log_dir = os.path.join(repo, ".ergon", "log")
    events = read_events(log_dir)
    return fold_events(events)


def _canonical_fold_bytes(state: dict) -> bytes:
    """
    Serialize a fold state to a canonical byte string for byte-identical
    comparison across clones.  Sets (edges/deps/claim_superseded may embed
    tuples/sets) are converted to sorted lists first so json.dumps is
    well-defined and stable; sort_keys=True makes key order irrelevant.
    """
    def _canon(obj):
        if isinstance(obj, dict):
            return {str(k): _canon(v) for k, v in obj.items()}
        if isinstance(obj, (set, frozenset)):
            return sorted(_canon(v) for v in obj)
        if isinstance(obj, (list, tuple)):
            return [_canon(v) for v in obj]
        return obj

    canon = _canon(state)
    return json.dumps(canon, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")


def _resolve_projection_conflict_if_any(repo: str, merge_result: subprocess.CompletedProcess,
                                         commit_message: str) -> None:
    """
    If the merge conflicted, it must ONLY be the projection (board.md /
    items/*.md) -- a JSONL shard conflict would mean the per-actor-session
    shard key failed.  Resolve by accepting ours then regenerating from the
    merged log (never a hand-merged conflict -- ADR-002), same pattern as
    tests/test_reconcile_roundtrip.py and tests/test_merge_safety.py.
    """
    if merge_result.returncode == 0:
        from pinax.projection import regenerate
        regenerate(repo)
        status_r = _git(repo, "status", "--porcelain")
        if status_r.stdout.strip():
            _commit_all(repo, commit_message + " (regenerate)")
        return

    if "CONFLICT" not in merge_result.stdout and "conflict" not in merge_result.stderr.lower():
        raise RuntimeError(
            f"git merge failed:\nstdout: {merge_result.stdout}\nstderr: {merge_result.stderr}"
        )

    jsonl_conflicts = [
        line for line in merge_result.stdout.splitlines()
        if "CONFLICT" in line and ".jsonl" in line
    ]
    assert not jsonl_conflicts, (
        "UNEXPECTED conflict in a JSONL shard -- per-actor-session sharding should "
        f"make this impossible.\n{merge_result.stdout}"
    )

    status_r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, env=_build_env(),
    )
    for sline in status_r.stdout.splitlines():
        if sline.startswith("UU") or sline.startswith("AA"):
            conflict_path = sline[3:].strip()
            if conflict_path.startswith(".ergon/board.md") or conflict_path.startswith(".ergon/items/"):
                _git(repo, "checkout", "--ours", conflict_path)

    from pinax.projection import regenerate
    regenerate(repo)
    _git(repo, "add", ".ergon")
    _git(repo, "commit", "-m", commit_message + " (projection conflict resolved by regeneration)")


# ---------------------------------------------------------------------------
# The two-clone divergence round-trip test
# ---------------------------------------------------------------------------

@requires_git
def test_two_clone_divergence_roundtrip():
    """
    Two disconnected clones each run the real Pinax CLI,
    each claims+completes a different item, push/pull merge, fold
    byte-identical on both sides afterward.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        # --- 1. Seed working tree: pinax init + two items, pushed into a
        #        bare hub so both clones can push back without hitting
        #        git's "refusing to update the current branch" guard. ---
        seed = os.path.join(tmpdir, "seed")
        _make_git_repo(seed)
        _init_ergon(seed, actor="operator@hub")

        add_a = _pinax(seed, "add", "--title", "Item A", "--prefix", "pnx",
                        "--actor", "operator@hub", "--json")
        add_b = _pinax(seed, "add", "--title", "Item B", "--prefix", "pnx",
                        "--actor", "operator@hub", "--json")
        _commit_all(seed, "hub: add items A and B")

        a_id = json.loads(add_a.stdout)["item_id"]
        b_id = json.loads(add_b.stdout)["item_id"]
        assert a_id != b_id

        hub = os.path.join(tmpdir, "hub.git")
        _git(tmpdir, "init", "--bare", "-b", "main", hub)
        _git(seed, "remote", "add", "origin", hub)
        _git(seed, "push", "origin", "main")

        # --- 2. Two REAL, disconnected clones of the hub. ---
        clone1 = os.path.join(tmpdir, "clone-1")
        clone2 = os.path.join(tmpdir, "clone-2")
        _git(tmpdir, "clone", hub, clone1)
        _git(tmpdir, "clone", hub, clone2)
        for c in (clone1, clone2):
            _git(c, "config", "user.email", "clone@pinax.test")
            _git(c, "config", "user.name", "Pinax Clone")
            _git(c, "config", "core.autocrlf", "false")

        # --- 3. clone-1: claim + done item A (real CLI invocations). ---
        _pinax(clone1, "claim", a_id, "--actor", "operator@clone1")
        briefing1 = os.path.join(clone1, "briefing-a.txt")
        with open(briefing1, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Item A shipped from clone-1.\n")
        _pinax(clone1, "done", a_id, "--briefing", briefing1, "--actor", "operator@clone1")
        _commit_all(clone1, "clone-1: claim + done item A")

        # --- 4. clone-2: claim + done item B, fully disconnected from
        #        clone-1 -- it has only ever seen the pre-fork hub state. ---
        _pinax(clone2, "claim", b_id, "--actor", "reviewer@clone2")
        briefing2 = os.path.join(clone2, "briefing-b.txt")
        with open(briefing2, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("Item B shipped from clone-2.\n")
        _pinax(clone2, "done", b_id, "--briefing", briefing2, "--actor", "reviewer@clone2")
        _commit_all(clone2, "clone-2: claim + done item B")

        # --- 5a. clone-1 pushes first: the hub has not moved since the
        #         clone, so this is a fast-forward push. ---
        _git(clone1, "push", "origin", "main")

        # --- 5b. clone-2 fetches the (now-moved) hub and merges: BOTH
        #         sides have a commit the other lacks -> a REAL merge, not
        #         a fast-forward. ---
        _git(clone2, "fetch", "origin")
        merge_result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", "clone-2: merge hub (clone-1's item A landed)",
             "origin/main"],
            cwd=clone2, capture_output=True, text=True, env=_build_env(),
        )
        _resolve_projection_conflict_if_any(
            clone2, merge_result, "clone-2: merge hub (clone-1's item A landed)"
        )

        # clone-2 pushes the merge back to the hub.
        _git(clone2, "push", "origin", "main")

        # --- 5c. clone-1 pulls the merge back: clone-1's own tip is an
        #         ancestor of clone-2's merge commit, so this is a
        #         fast-forward on clone-1's side. ---
        _git(clone1, "fetch", "origin")
        ff_result = subprocess.run(
            ["git", "merge", "--ff-only", "origin/main"],
            cwd=clone1, capture_output=True, text=True, env=_build_env(),
        )
        assert ff_result.returncode == 0, (
            "clone-1's pull of the merged hub state was not a fast-forward "
            f"(expected: clone-1's tip is an ancestor of clone-2's merge commit).\n"
            f"stdout: {ff_result.stdout}\nstderr: {ff_result.stderr}"
        )

        # Both clones must now be at the identical commit.
        head1 = _git(clone1, "rev-parse", "HEAD").stdout.strip()
        head2 = _git(clone2, "rev-parse", "HEAD").stdout.strip()
        assert head1 == head2, f"clone-1 HEAD {head1} != clone-2 HEAD {head2} after round-trip"

        # --- 6a. Both items fold to 'done' with the correct claimant/actor
        #         per clone, on EACH side independently. ---
        for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
            state = _fold_repo(repo)
            items = state["items"]
            assert items[a_id]["status"] == "done", f"{label}: item A not done: {items[a_id]}"
            assert items[a_id]["owner"] == "operator@clone1", f"{label}: item A owner={items[a_id].get('owner')!r}"
            assert items[a_id]["status_changed_by"] == "operator@clone1", (
                f"{label}: item A status_changed_by={items[a_id].get('status_changed_by')!r}"
            )
            assert items[b_id]["status"] == "done", f"{label}: item B not done: {items[b_id]}"
            assert items[b_id]["owner"] == "reviewer@clone2", f"{label}: item B owner={items[b_id].get('owner')!r}"
            assert items[b_id]["status_changed_by"] == "reviewer@clone2", (
                f"{label}: item B status_changed_by={items[b_id].get('status_changed_by')!r}"
            )

        # --- 6b. The fold is byte-identical between the two clones. ---
        state1 = _fold_repo(clone1)
        state2 = _fold_repo(clone2)
        bytes1 = _canonical_fold_bytes(state1)
        bytes2 = _canonical_fold_bytes(state2)
        assert bytes1 == bytes2, (
            "Fold is NOT byte-identical between clone-1 and clone-2 after the "
            "push/pull merge round-trip -- violates the deterministic-fold invariant.\n"
            f"clone-1: {bytes1[:1000]!r}\n"
            f"clone-2: {bytes2[:1000]!r}"
        )

        # --- 6c. Idempotency: re-folding twice on each side is a no-op. ---
        for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
            first = _fold_repo(repo)
            second = _fold_repo(repo)
            assert first == second, f"{label}: fold is not idempotent (differs across two runs)"
            assert _canonical_fold_bytes(first) == _canonical_fold_bytes(second), (
                f"{label}: canonical fold bytes differ across two fold runs"
            )

        # --- 6d. `pinax verify` is clean on both sides post-merge. ---
        for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
            r = subprocess.run(
                [sys.executable, "-m", "pinax", "verify"],
                cwd=repo, capture_output=True, text=True, env=_build_env(),
            )
            assert r.returncode == 0, (
                f"{label}: pinax verify failed post-merge:\nstdout: {r.stdout}\nstderr: {r.stderr}"
            )

        # Working trees are clean on both sides -- no leftover conflict
        # markers or unregenerated projection drift.
        for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
            status_r = _git(repo, "status", "--porcelain")
            assert status_r.stdout.strip() == "", f"{label}: working tree not clean:\n{status_r.stdout}"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
