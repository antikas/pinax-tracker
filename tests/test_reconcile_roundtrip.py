"""Offline action reconciliation round-trip tests."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Git subprocess helpers (same shape as tests/test_merge_safety.py)
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


def _pinax(repo: str, *args: str, env=None) -> subprocess.CompletedProcess:
    _env = env or _build_env()
    r = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, capture_output=True, text=True, env=_env,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo}:\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _commit_all(repo: str, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _commit_if_dirty(repo: str, message: str) -> None:
    _git(repo, "add", "-A")
    status_r = _git(repo, "status", "--porcelain")
    if status_r.stdout.strip():
        _git(repo, "commit", "-m", message)


def _fold_repo(repo: str) -> dict:
    from pinax.fold import fold_events, read_events
    log_dir = os.path.join(repo, ".ergon", "log")
    events = read_events(log_dir)
    return fold_events(events)


def _resolve_projection_conflict_if_any(repo: str, merge_result: subprocess.CompletedProcess,
                                         commit_message: str) -> None:
    """
    If the merge conflicted, it must ONLY be the projection (board.md /
    items/*.md) -- a JSONL shard conflict would mean the per-actor-session
    shard key failed.  Resolve by accepting ours then regenerating from the
    merged log (never a hand-edited conflict -- ADR-002), same pattern as
    tests/test_merge_safety.py.
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
            f"git pull/merge failed:\nstdout: {merge_result.stdout}\nstderr: {merge_result.stderr}"
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
# The round-trip test
# ---------------------------------------------------------------------------

@requires_git
def test_offline_reconcile_two_clone_roundtrip():
    """Reconcile offline actions from a merged clone without duplicate events."""
    tmpdir = tempfile.mkdtemp()
    try:
        # --- 1. repo_a: the equipped hub ---
        repo_a = os.path.join(tmpdir, "repo-a")
        _make_git_repo(repo_a)
        _init_ergon(repo_a, actor="operator@example.test")

        _pinax(repo_a, "add", "--title", "Item X", "--prefix", "pnx", "--actor", "operator@example.test")
        _pinax(repo_a, "add", "--title", "Item Y", "--prefix", "pnx", "--actor", "operator@example.test")
        _commit_all(repo_a, "hub: add items X and Y")

        state = _fold_repo(repo_a)
        items = state.get("items", {})
        x_id = next(iid for iid, it in items.items() if "Item X" in it.get("title", ""))
        y_id = next(iid for iid, it in items.items() if "Item Y" in it.get("title", ""))

        # --- 2. repo_b: a REAL clone playing the CLI-less machine ---
        repo_b = os.path.join(tmpdir, "repo-b-laptop")
        _git(tmpdir, "clone", repo_a, repo_b)
        _git(repo_b, "config", "user.email", "laptop@pinax.test")
        _git(repo_b, "config", "user.name", "Laptop (no CLI)")

        offline_path = os.path.join(repo_b, "BACKLOG-OFFLINE.md")
        with open(offline_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"- 2026-07-01T09:00:00Z operator@laptop done {x_id} | shipped from the laptop\n")
            fh.write(f"- 2026-07-01T09:05:00Z operator@laptop park {y_id} | blocked, needs review\n")
        _commit_all(repo_b, "laptop (no CLI): log offline completions in BACKLOG-OFFLINE.md")

        # --- 3. repo_a diverges (a real merge, not a fast-forward) ---
        _pinax(repo_a, "add", "--title", "Item Z", "--prefix", "pnx", "--actor", "operator@example.test")
        _commit_all(repo_a, "hub: add item Z (diverges from the laptop clone)")

        # --- 4. repo_a pulls repo_b's commit: a real two-clone merge ---
        # (fetch + merge rather than `git pull -m` -- pull's CLI does not take
        # a -m message flag; this is otherwise a real fetch-then-merge pull.)
        _git(repo_a, "fetch", repo_b, "main:refs/remotes/laptop/main")
        merge_result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", "merge: laptop offline batch",
             "refs/remotes/laptop/main"],
            cwd=repo_a, capture_output=True, text=True, env=_build_env(),
        )
        _resolve_projection_conflict_if_any(repo_a, merge_result, "merge: laptop offline batch")

        # BACKLOG-OFFLINE.md must have arrived in repo_a via the merge.
        assert os.path.isfile(os.path.join(repo_a, "BACKLOG-OFFLINE.md")), (
            "BACKLOG-OFFLINE.md was not merged into repo_a from the laptop clone"
        )

        # --- 5. repo_a reconciles ---
        _pinax(repo_a, "reconcile", "--actor", "reviewer@example.test")
        _commit_all(repo_a, "hub: reconcile laptop offline batch")

        # --- 6a. Fold reflects the completions ---
        state = _fold_repo(repo_a)
        items = state.get("items", {})
        assert items[x_id]["status"] == "done", items[x_id]
        assert items[x_id]["status_changed_by"] == "operator@laptop"
        assert items[y_id]["status"] == "parked", items[y_id]
        assert items[y_id]["park_reason"] == "blocked, needs review"
        z_id = next(iid for iid, it in items.items() if "Item Z" in it.get("title", ""))
        assert items[z_id]["status"] == "queued"

        # The imported events must be attributed to the offline author, never
        # the reconciler, and carry reconciliation provenance in the payload.
        from pinax.fold import read_events
        all_events = read_events(os.path.join(repo_a, ".ergon", "log"))
        completed = [e for e in all_events if e["type"] == "item.completed" and e["payload"]["item_id"] == x_id]
        assert len(completed) == 1
        assert completed[0]["actor"] == "operator@laptop"
        assert completed[0]["payload"]["imported_by"] == "reviewer@example.test"
        assert completed[0]["payload"]["source"] == "BACKLOG-OFFLINE.md"
        assert completed[0]["payload"]["source_line_hash"]

        # --- 6b. pinax verify is clean ---
        r = subprocess.run(
            [sys.executable, "-m", "pinax", "verify"],
            cwd=repo_a, capture_output=True, text=True, env=_build_env(),
        )
        assert r.returncode == 0, f"pinax verify failed:\nstdout: {r.stdout}\nstderr: {r.stderr}"

        # --- 6c. Determinism: fold twice ---
        state_1 = _fold_repo(repo_a)
        state_2 = _fold_repo(repo_a)
        assert state_1 == state_2

        # --- 6d. Determinism: shuffled physical line order ---
        log_dir = os.path.join(repo_a, ".ergon", "log")
        for fname in os.listdir(log_dir):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(log_dir, fname)
            with open(fpath, "rb") as fh:
                raw = fh.read()
            normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            lines = [l for l in normalised.split(b"\n") if l]
            random.Random(2026).shuffle(lines)
            with open(fpath, "wb") as fh:
                fh.write(b"\n".join(lines) + b"\n")

        from pinax.fold import fold_events, read_events as _read_events
        state_shuffled = fold_events(_read_events(log_dir))
        assert state_shuffled["items"][x_id]["status"] == "done"
        assert state_shuffled["items"][y_id]["status"] == "parked"
        # Restore the un-shuffled shards for the rest of the test (git checkout).
        _git(repo_a, "checkout", "--", ".ergon/log")

        # --- 6e. Idempotency: re-reconcile is a zero-duplicate no-op ---
        events_before = read_events(os.path.join(repo_a, ".ergon", "log"))
        _pinax(repo_a, "reconcile", "--actor", "reviewer@example.test")
        events_after = read_events(os.path.join(repo_a, ".ergon", "log"))
        assert events_before == events_after, (
            "Re-reconciling with no new offline lines appended duplicate events"
        )

        # --- 6f. Idempotency: a resurrected raw line (simulated merge
        #     resurrection) reconciles to a no-op, zero duplicate events ---
        offline_path_hub = os.path.join(repo_a, "BACKLOG-OFFLINE.md")
        with open(offline_path_hub, "r", encoding="utf-8") as fh:
            rewritten = fh.read()
        resurrected_line = f"- 2026-07-01T09:00:00Z operator@laptop done {x_id} | shipped from the laptop"
        with open(offline_path_hub, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(resurrected_line + "\n\n" + rewritten)

        _pinax(repo_a, "reconcile", "--actor", "reviewer@example.test")
        events_after_resurrect = read_events(os.path.join(repo_a, ".ergon", "log"))
        completed_after = [
            e for e in events_after_resurrect
            if e["type"] == "item.completed" and e["payload"]["item_id"] == x_id
        ]
        assert len(completed_after) == 1, (
            f"Resurrected raw line caused a duplicate item.completed event: {completed_after}"
        )
        assert completed_after[0]["id"] == completed[0]["id"]

        _commit_if_dirty(repo_a, "hub: reconcile no-op after resurrected raw line")

        # Working tree is clean after the final commit.
        status_r = _git(repo_a, "status", "--porcelain")
        assert status_r.stdout.strip() == "", f"Working tree not clean:\n{status_r.stdout}"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
