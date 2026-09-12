"""Two-clone convergence tests for the deterministic event log."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Git subprocess helpers (same shape as tests/test_merge_safety.py and
# tests/test_reconcile_roundtrip.py - SSOT: this module does not reinvent
# them, it mirrors the existing precedent's small helper set). The hub, the
# seeded clone and every further clone of it belong to the shared factories
# in tests/conftest.py, which take these helpers as they are.
# ---------------------------------------------------------------------------

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


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


# The line-ending rule has to be in place before git writes any file, so a
# clone carries it from the start rather than being corrected afterwards.
_CHECKOUT_CONFIG = {"core.autocrlf": "false"}


def _configure(repo_root: str) -> None:
    _git(repo_root, "config", "user.email", "clone@pinax.example")
    _git(repo_root, "config", "user.name", "Pinax Clone")
    for key, value in _CHECKOUT_CONFIG.items():
        _git(repo_root, "config", key, value)


def _init_repo(repo_root: str) -> str:
    os.makedirs(repo_root, exist_ok=True)
    _git(repo_root, "init", "-b", "main")
    _configure(repo_root)
    return repo_root


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
    """
    Stage all changes and commit.

    Used where this file makes a change of its own. A mutating pinax
    command commits and publishes its own shard and projection, so a
    scenario never commits again after one of those.
    """
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


def _briefing(tmp_path, name: str, text: str) -> str:
    """
    Write a briefing outside every clone.

    The briefing's content is read into the event payload, not tracked, so
    keeping the file out of the working tree is what leaves the tree clean
    for the assertions at the end.
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


# ---------------------------------------------------------------------------
# The two-clone divergence round-trip test
# ---------------------------------------------------------------------------

@requires_git
def test_two_clone_divergence_roundtrip(tmp_path, clone_wired_to_hub, clone_of_hub):
    """
    Two clones each run the real Pinax CLI, each claims and completes a
    different item, and their work meets on the hub.  Afterwards both
    clones fold byte-identically.

    Every mutating command publishes itself now: it fetches, appends,
    commits and pushes.  The second clone has only ever seen the pre-fork
    hub state, so its pushes are rejected, and its own sequence pulls,
    regenerates the projection from the merged log and pushes again.  The
    real merge is inside the command, not in this file.
    """
    # --- 1. Seed working tree: a clone wired to a bare hub, with two
    #        items, published by the commands that created them. ---
    seed = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor="operator@hub", name="seed",
    )

    add_a = _pinax(seed, "add", "--title", "Item A", "--prefix", "pnx",
                   "--actor", "operator@hub", "--json")
    add_b = _pinax(seed, "add", "--title", "Item B", "--prefix", "pnx",
                   "--actor", "operator@hub", "--json")

    a_id = json.loads(add_a.stdout)["item_id"]
    b_id = json.loads(add_b.stdout)["item_id"]
    assert a_id != b_id

    # --- 2. Two REAL clones of the hub, taken at the same point. ---
    clone1 = clone_of_hub(
        git=_git, name="clone-1", config=_CHECKOUT_CONFIG, configure=_configure
    )
    clone2 = clone_of_hub(
        git=_git, name="clone-2", config=_CHECKOUT_CONFIG, configure=_configure
    )

    # --- 3. clone-1: claim + done item A (real CLI invocations, each one
    #        publishing itself). ---
    _pinax(clone1, "claim", a_id, "--actor", "operator@clone1")
    briefing1 = _briefing(tmp_path, "briefing-a.txt", "Item A shipped from clone-1.\n")
    _pinax(clone1, "done", a_id, "--briefing", briefing1, "--actor", "operator@clone1")

    # --- 4. clone-2: claim + done item B, from a clone that has only ever
    #        seen the pre-fork hub state.  Each command finds its push
    #        rejected, pulls, regenerates the projection from the merged
    #        log and publishes. ---
    _pinax(clone2, "claim", b_id, "--actor", "reviewer@clone2")
    briefing2 = _briefing(tmp_path, "briefing-b.txt", "Item B shipped from clone-2.\n")
    _pinax(clone2, "done", b_id, "--briefing", briefing2, "--actor", "reviewer@clone2")

    # A real merge happened inside clone-2's own commands: both sides had a
    # commit the other lacked, so this was never a fast-forward.
    merges = _git(clone2, "rev-list", "--merges", "--count", "HEAD").stdout.strip()
    assert int(merges) >= 1, (
        "clone-2 published without ever merging the hub, so nothing here "
        "exercised the divergent case"
    )

    # --- 5. clone-1 pulls the merged state back: clone-1's own tip is an
    #        ancestor of clone-2's merge commit, so this is a fast-forward
    #        on clone-1's side. ---
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

    # --- 6d. The projection carries no conflict marker and `pinax verify`
    #         is clean on both sides post-merge. ---
    for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
        board = os.path.join(repo, ".ergon", "board.md")
        with open(board, "r", encoding="utf-8") as fh:
            assert "<<<<<<<" not in fh.read(), f"{label}: a conflict marker survived"
        r = _pinax(repo, "verify", check=False)
        assert r.returncode == 0, (
            f"{label}: pinax verify failed post-merge:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

    # Working trees are clean on both sides -- no leftover conflict
    # markers or unregenerated projection drift.
    for repo, label in ((clone1, "clone-1"), (clone2, "clone-2")):
        status_r = _git(repo, "status", "--porcelain")
        assert status_r.stdout.strip() == "", f"{label}: working tree not clean:\n{status_r.stdout}"
