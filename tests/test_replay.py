"""Git-reference replay tests."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

from pinax.fold import fold_events, read_events, state_to_json_safe
from pinax.replay import ReplayRefError, fold_at_ref, read_events_at_ref

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


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git not available on PATH",
)


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def _make_git_repo(tmpdir: str) -> str:
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@pinax.test")
    _git(repo, "config", "user.name", "Pinax Test")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


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


def _head_sha(repo: str) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _fold_repo(repo: str) -> dict:
    log_dir = os.path.join(repo, ".ergon", "log")
    return fold_events(read_events(log_dir))


def _item_id_by_title(state: dict, title: str) -> str:
    items = state.get("items", {})
    matches = [iid for iid, it in items.items() if it.get("title") == title]
    assert matches, f"{title!r} not found; items={list(items.keys())}"
    return matches[0]


# ---------------------------------------------------------------------------
# The hand-constructed fixture: three checkpoints, tagged, single shard.
# ---------------------------------------------------------------------------

def _build_fixture(tmpdir: str):
    """
    Build the fixture repo:
      replay-c1: item Alpha added (queued).
      replay-c2: item Beta added; Alpha -> building.
      replay-c3 (== HEAD): item Gamma added; Alpha -> done.

    Single actor/shard throughout (operator@example.test) — this fixture deliberately
    does NOT exercise the union merge driver (that is test_merge_safety.py's
    job); it isolates the git-ref time-travel property against the simplest
    possible log shape.
    """
    repo = _make_git_repo(tmpdir)
    _init_ergon(repo, actor="operator@example.test")

    _pinax(repo, "add", "--title", "Alpha", "--prefix", "pnx", "--actor", "operator@example.test")
    _commit_all(repo, "c1: add Alpha")
    _git(repo, "tag", "replay-c1")
    c1_sha = _head_sha(repo)

    state_c1 = _fold_repo(repo)
    alpha_id = _item_id_by_title(state_c1, "Alpha")

    _pinax(repo, "add", "--title", "Beta", "--prefix", "pnx", "--actor", "operator@example.test")
    _pinax(repo, "status", alpha_id, "building", "--actor", "operator@example.test")
    _commit_all(repo, "c2: add Beta, Alpha -> building")
    _git(repo, "tag", "replay-c2")
    c2_sha = _head_sha(repo)

    _pinax(repo, "add", "--title", "Gamma", "--prefix", "pnx", "--actor", "operator@example.test")
    _pinax(repo, "status", alpha_id, "done", "--actor", "operator@example.test")
    _commit_all(repo, "c3: add Gamma, Alpha -> done")
    _git(repo, "tag", "replay-c3")
    c3_sha = _head_sha(repo)

    return repo, alpha_id, c1_sha, c2_sha, c3_sha


# ---------------------------------------------------------------------------
# 1. replay(@ref) reconstructs the EXACT historical state at each checkpoint.
# ---------------------------------------------------------------------------

@requires_git
def test_replay_at_ref_reconstructs_exact_historical_state():
    tmpdir = tempfile.mkdtemp()
    try:
        repo, alpha_id, c1_sha, c2_sha, c3_sha = _build_fixture(tmpdir)

        # --- c1: only Alpha, queued. Beta/Gamma must not exist yet.
        state_c1 = fold_at_ref(repo, "replay-c1")
        items_c1 = state_c1.get("items", {})
        assert set(items_c1.keys()) == {alpha_id}, (
            f"replay@c1 must contain ONLY Alpha; got {list(items_c1.keys())}"
        )
        assert items_c1[alpha_id]["status"] == "queued"

        # --- c2: Alpha building, Beta queued. Gamma must NOT exist yet.
        state_c2 = fold_at_ref(repo, "replay-c2")
        items_c2 = state_c2.get("items", {})
        assert items_c2[alpha_id]["status"] == "building"
        beta_id = _item_id_by_title(state_c2, "Beta")
        assert set(items_c2.keys()) == {alpha_id, beta_id}, (
            f"replay@c2 must contain Alpha+Beta only (no Gamma); got {list(items_c2.keys())}"
        )

        # --- c3 (HEAD): Alpha done, Beta queued, Gamma queued — matches live fold.
        state_c3 = fold_at_ref(repo, "replay-c3")
        live_state = _fold_repo(repo)
        assert state_to_json_safe(state_c3) == state_to_json_safe(live_state), (
            "replay@HEAD-tag must equal the live fold of the current working-tree log"
        )
        assert state_c3["items"][alpha_id]["status"] == "done"
        gamma_id = _item_id_by_title(state_c3, "Gamma")
        assert set(state_c3["items"].keys()) == {alpha_id, beta_id, gamma_id}

        # --- Replay by raw commit SHA (not a symbolic ref) matches replay by tag.
        state_c1_by_sha = fold_at_ref(repo, c1_sha)
        assert state_to_json_safe(state_c1_by_sha) == state_to_json_safe(state_c1), (
            "replay by raw commit SHA must match replay by tag for the same commit"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Determinism: repeated in-process calls AND repeated CLI subprocess calls
#    for the SAME ref are byte-identical, even though the working-tree log has
#    since advanced past that ref.
# ---------------------------------------------------------------------------

@requires_git
def test_replay_determinism_repeated_and_via_cli():
    tmpdir = tempfile.mkdtemp()
    try:
        repo, alpha_id, c1_sha, c2_sha, c3_sha = _build_fixture(tmpdir)

        # In-process: fold_at_ref called 5x for the same ref -> identical.
        results = [state_to_json_safe(fold_at_ref(repo, "replay-c1")) for _ in range(5)]
        assert all(r == results[0] for r in results), (
            "fold_at_ref is not deterministic across repeated in-process calls"
        )

        # Via the CLI (a separate subprocess each time) -> byte-identical stdout.
        outputs = [_pinax(repo, "replay", "--at", "replay-c1", "--json").stdout
                   for _ in range(3)]
        assert all(o == outputs[0] for o in outputs), (
            "pinax replay --at <ref> --json is not byte-identical across "
            "repeated CLI invocations"
        )

        # The CLI JSON payload's state must match the in-process fold_at_ref result.
        payload = json.loads(outputs[0])
        assert payload["at"] == "replay-c1"
        assert payload["state"]["items"][alpha_id]["status"] == "queued"
        assert len(payload["state"]["items"]) == 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Order-independence of the git-ref-sourced path: shuffling the total-ordered
#    event list sourced from a historical ref still folds to the same state
#    (the same property test_fold_determinism.py proves for the filesystem path).
# ---------------------------------------------------------------------------

@requires_git
def test_replay_at_ref_order_independent():
    tmpdir = tempfile.mkdtemp()
    try:
        repo, alpha_id, c1_sha, c2_sha, c3_sha = _build_fixture(tmpdir)

        events_c2 = read_events_at_ref(repo, "replay-c2")
        expected = state_to_json_safe(fold_events(events_c2))

        for seed in (1, 2, 3):
            shuffled = events_c2[:]
            random.Random(seed).shuffle(shuffled)
            shuffled_state = state_to_json_safe(fold_events(shuffled))
            assert shuffled_state == expected, (
                f"fold over shuffled git-ref-sourced events (seed={seed}) != "
                f"fold over the total-ordered read (both sourced from replay-c2)"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Error handling: an unknown ref fails clearly (exit 1, no traceback, no
#    partial/garbage state) — it must not silently fall back to HEAD.
# ---------------------------------------------------------------------------

@requires_git
def test_replay_unknown_ref_fails_clearly():
    tmpdir = tempfile.mkdtemp()
    try:
        repo, *_ = _build_fixture(tmpdir)

        r = subprocess.run(
            [sys.executable, "-m", "pinax", "replay", "--at", "does-not-exist-xyz", "--json"],
            cwd=repo, capture_output=True, text=True, env=_build_env(),
        )
        assert r.returncode == 1, (
            f"pinax replay --at <bad-ref> must exit 1; got {r.returncode}\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        assert "Traceback" not in r.stderr, f"Unhandled exception leaked to stderr:\n{r.stderr}"
        assert r.stdout.strip() == "", f"No state should print on a ref error; got: {r.stdout!r}"

        # Same contract at the library level.
        with pytest.raises(ReplayRefError):
            fold_at_ref(repo, "does-not-exist-xyz")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. Replay never mutates the working tree, the log, or the projection.
# ---------------------------------------------------------------------------

@requires_git
def test_replay_is_read_only():
    tmpdir = tempfile.mkdtemp()
    try:
        repo, *_ = _build_fixture(tmpdir)

        status_before = _git(repo, "status", "--porcelain").stdout
        _pinax(repo, "replay", "--at", "replay-c1", "--json")
        _pinax(repo, "replay", "--at", "replay-c2")
        status_after = _git(repo, "status", "--porcelain").stdout

        assert status_before == status_after == "", (
            f"pinax replay must not write to the working tree.\n"
            f"before: {status_before!r}\nafter: {status_after!r}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. A ref that predates 'pinax init' (no .ergon/log at all) folds to the
#    empty base state rather than erroring — a valid outcome, not a crash.
# ---------------------------------------------------------------------------

@requires_git
def test_replay_at_ref_before_ergon_log_exists():
    tmpdir = tempfile.mkdtemp()
    try:
        repo = _make_git_repo(tmpdir)
        # A commit that has nothing to do with .ergon/ at all.
        readme_path = os.path.join(repo, "README.md")
        with open(readme_path, "w", encoding="utf-8") as fh:
            fh.write("pre-ergon commit\n")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "pre-ergon: no .ergon/log yet")
        _git(repo, "tag", "pre-ergon")

        state = fold_at_ref(repo, "pre-ergon")
        assert state.get("items", {}) == {}, (
            f"Expected empty items at a ref predating 'pinax init'; got {state.get('items')}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Canonical fixture replay through two Git references.
# ---------------------------------------------------------------------------

GOLDEN_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN_LOG_PATH = os.path.join(GOLDEN_FIXTURES_DIR, "golden_log.jsonl")
GOLDEN_STATE_PATH = os.path.join(GOLDEN_FIXTURES_DIR, "golden_state.json")
GOLDEN_STATE_K3_PATH = os.path.join(GOLDEN_FIXTURES_DIR, "golden_state_k3.json")


def _load_golden_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _read_golden_log_lines() -> list[bytes]:
    """Read the canonical golden fixture, return non-empty LF-normalised lines."""
    with open(GOLDEN_LOG_PATH, "rb") as fh:
        raw = fh.read()
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return [line for line in normalised.split(b"\n") if line]


@requires_git
def test_replay_at_ref_matches_canonical_golden_fixture():
    """Replay canonical fixture prefixes from Git references."""
    lines = _read_golden_log_lines()
    k3_lines = [lines[0], lines[2], lines[3]]

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _make_git_repo(tmpdir)
        log_dir = os.path.join(repo, ".ergon", "log")
        os.makedirs(log_dir, exist_ok=True)
        shard_path = os.path.join(log_dir, "golden.jsonl")

        # --- Checkpoint 1: the k3 prefix only.
        with open(shard_path, "wb") as fh:
            for line in k3_lines:
                fh.write(line + b"\n")
        _commit_all(repo, "checkpoint: golden fixture k3 prefix")
        _git(repo, "tag", "golden-k3")

        # --- Checkpoint 2: the complete, unmodified fixture.
        with open(shard_path, "wb") as fh:
            for line in lines:
                fh.write(line + b"\n")
        _commit_all(repo, "checkpoint: golden fixture complete")
        _git(repo, "tag", "golden-full")

        golden_state_k3 = _load_golden_json(GOLDEN_STATE_K3_PATH)
        golden_state_full = _load_golden_json(GOLDEN_STATE_PATH)

        # 1. fold(fixture) == golden — filesystem path, on the exact bytes now
        #    committed to git (not a temp-dir copy of the fixture module).
        fs_state = fold_events(read_events(log_dir))
        assert state_to_json_safe(fs_state) == golden_state_full, (
            "fold(golden fixture) != golden_state, sourced from the git-committed "
            "shard file"
        )

        # 2. Replay at each named ref matches its corresponding fixture state.
        replay_k3 = fold_at_ref(repo, "golden-k3")
        assert state_to_json_safe(replay_k3) == golden_state_k3, (
            "replay(@golden-k3) != golden_state_k3\n"
            f"got: {json.dumps(state_to_json_safe(replay_k3), sort_keys=True, indent=2)}"
        )

        replay_full = fold_at_ref(repo, "golden-full")
        assert state_to_json_safe(replay_full) == golden_state_full, (
            "replay(@golden-full) != golden_state (the canonical golden fixture's fold)\n"
            f"got: {json.dumps(state_to_json_safe(replay_full), sort_keys=True, indent=2)}"
        )

        # Sanity: the two refs are genuinely distinct historical snapshots —
        # replay@golden-k3 must NOT equal the full golden state.
        assert state_to_json_safe(replay_k3) != golden_state_full, (
            "replay(@golden-k3) unexpectedly matches the FULL golden state — "
            "the k3 checkpoint did not isolate a distinct historical snapshot"
        )

        # And via the shipped CLI, not just the library call.
        cli_k3 = _pinax(repo, "replay", "--at", "golden-k3", "--json")
        cli_full = _pinax(repo, "replay", "--at", "golden-full", "--json")
        assert json.loads(cli_k3.stdout)["state"] == golden_state_k3
        assert json.loads(cli_full.stdout)["state"] == golden_state_full
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
