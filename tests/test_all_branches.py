"""
Tests for board, report, and ready with --all-branches.

These tests cover the fold that includes unmerged local branch records and
the associated read-only command output.

Real-git fixture (same harness shape as tests/test_visibility.py and
tests/test_replay.py): subprocess git init/branch/commit + `python -m pinax`
subprocess calls for the end-to-end CLI surface, plus a direct in-process
import of pinax.all_branches.compute_all_branches_fold for finer-grained
assertions on the attribution side table.

Covers:
  (a) no unmerged branches -> --all-branches output identical to the default
      fold, for board (human + --json), report, and ready.
  (b) one unmerged branch with extra events -> those items appear in
      --all-branches output marked with their source branch, and are ABSENT
      from the plain (non-flagged) fold.
  (c) an item/event present on BOTH the current branch and the other branch
      (identical content-hash id, inherited via shared git history) -> deduped
      (appears exactly once), never marked with a spurious branch label.
  (d) --json is deterministic (repeated calls byte-identical) and ensure_ascii
      (a non-ASCII title never appears as raw UTF-8 bytes in the JSON output).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from pinax.all_branches import compute_all_branches_fold

pytestmark = pytest.mark.deep

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def _git(repo_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
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


def _pinax(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo_root, capture_output=True, text=True, env=_build_env(),
    )


def _init_repo(repo_root: str) -> None:
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "test")
    with open(os.path.join(repo_root, ".gitattributes"), "w", newline="\n") as f:
        f.write(_GITATTRIBUTES)


def _commit_all(repo_root: str, message: str) -> None:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", message)


@pytest.fixture()
def repo(tmp_path):
    """main with .ergon initialised and committed; no side branches yet."""
    root = str(tmp_path)
    _init_repo(root)
    result = _pinax(root, "init")
    assert result.returncode == 0, result.stderr
    _commit_all(root, "init: pinax ergon base")
    return root


# ---------------------------------------------------------------------------
# (a) no unmerged branches -> byte-identical to the default fold
# ---------------------------------------------------------------------------

@requires_git
def test_no_unmerged_branches_output_identical(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    for cmd in (["board"], ["report"], ["ready"]):
        default = _pinax(repo, *cmd)
        all_branches = _pinax(repo, *cmd, "--all-branches")
        assert default.returncode == 0, default.stderr
        assert all_branches.returncode == 0, all_branches.stderr
        assert default.stdout == all_branches.stdout, cmd

    default_json = json.loads(_pinax(repo, "board", "--json").stdout)
    ab_json = json.loads(_pinax(repo, "board", "--all-branches", "--json").stdout)
    assert ab_json["state"] == default_json["state"]
    assert ab_json["all_branches"] is True
    assert ab_json["source_branches"] == []
    assert ab_json["item_sources"] == {}


@requires_git
def test_no_unmerged_branches_ready_json_identical_shape(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    default_json = json.loads(_pinax(repo, "ready", "--json").stdout)
    ab_json = json.loads(_pinax(repo, "ready", "--all-branches", "--json").stdout)
    assert ab_json["ready"] == default_json
    assert ab_json["source_branches"] == []
    assert ab_json["item_sources"] == {}


# ---------------------------------------------------------------------------
# (b) branch-only item: appears in --all-branches, marked, absent from default
# ---------------------------------------------------------------------------

@requires_git
def test_unmerged_branch_item_marked_and_absent_from_default(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    _git(repo, "checkout", "-b", "run/spine")
    r = _pinax(repo, "add", "--title", "branch item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    branch_item_id = json.loads(r.stdout)["item_id"]
    _commit_all(repo, "branch item")
    _git(repo, "checkout", "main")

    # Default fold: branch item invisible everywhere.
    default_board = _pinax(repo, "board")
    default_ready = _pinax(repo, "ready")
    default_board_json = json.loads(_pinax(repo, "board", "--json").stdout)
    assert "branch item" not in default_board.stdout
    assert branch_item_id not in default_ready.stdout
    assert branch_item_id not in default_board_json["state"]["items"]

    # --all-branches: branch item visible, marked with its source branch.
    ab_board = _pinax(repo, "board", "--all-branches")
    assert ab_board.returncode == 0, ab_board.stderr
    assert "branch item" in ab_board.stdout
    assert f"{branch_item_id}" in ab_board.stdout
    assert "[from: run/spine]" in ab_board.stdout

    ab_ready = _pinax(repo, "ready", "--all-branches")
    assert branch_item_id in ab_ready.stdout
    assert "[from: run/spine]" in ab_ready.stdout

    ab_board_json = json.loads(_pinax(repo, "board", "--all-branches", "--json").stdout)
    assert ab_board_json["source_branches"] == ["run/spine"]
    assert ab_board_json["item_sources"][branch_item_id] == ["run/spine"]
    assert branch_item_id in ab_board_json["state"]["items"]

    ab_ready_json = json.loads(_pinax(repo, "ready", "--all-branches", "--json").stdout)
    assert branch_item_id in ab_ready_json["ready"]
    assert ab_ready_json["item_sources"][branch_item_id] == ["run/spine"]


@requires_git
def test_unmerged_branch_parked_item_marked_in_report(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    _git(repo, "checkout", "-b", "run/spine")
    r = _pinax(repo, "add", "--title", "parked branch item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    parked_id = json.loads(r.stdout)["item_id"]
    r = _pinax(repo, "park", parked_id, "--reason", "needs a decision", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "park branch item")
    _git(repo, "checkout", "main")

    default_report = _pinax(repo, "report")
    assert parked_id not in default_report.stdout

    ab_report = _pinax(repo, "report", "--all-branches")
    assert ab_report.returncode == 0, ab_report.stderr
    assert parked_id in ab_report.stdout
    assert "[from: run/spine]" in ab_report.stdout

    ab_report_json = json.loads(_pinax(repo, "report", "--all-branches", "--json").stdout)
    assert any(it["id"] == parked_id for it in ab_report_json["parked"])
    assert ab_report_json["item_sources"][parked_id] == ["run/spine"]


# ---------------------------------------------------------------------------
# (c) shared item (identical content-hash, on both branches) -> deduped, no
#     spurious marker; not double-counted.
# ---------------------------------------------------------------------------

@requires_git
def test_shared_item_deduped_no_spurious_marker(repo):
    r = _pinax(repo, "add", "--title", "shared item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    shared_id = json.loads(r.stdout)["item_id"]
    _commit_all(repo, "shared item")

    # Branch off AFTER the shared item is committed -- the branch tip's tree
    # inherits that same event (identical id) via shared git history, then
    # adds one item of its own.
    _git(repo, "checkout", "-b", "run/spine")
    r = _pinax(repo, "add", "--title", "branch-only item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    branch_only_id = json.loads(r.stdout)["item_id"]
    _commit_all(repo, "branch-only item")
    _git(repo, "checkout", "main")

    result = compute_all_branches_fold(
        repo, os.path.join(repo, ".ergon", "log"),
    )
    items = result["state"]["items"]

    # Both present, each exactly once (dict keys are unique by construction,
    # but also assert the raw union folded to sane counts -- no duplication
    # artefact anywhere in the pipeline).
    assert shared_id in items
    assert branch_only_id in items
    assert len(items) == 2

    # The shared item is NOT attributed to run/spine (it exists on <current>
    # too -- identical content-hash id) and carries no marker at all.
    assert shared_id not in result["item_sources"]
    assert shared_id not in result["event_sources"]

    # The branch-only item IS attributed, and only to run/spine.
    assert result["item_sources"][branch_only_id] == ["run/spine"]

    ab_board = _pinax(repo, "board", "--all-branches")
    board_lines = ab_board.stdout.splitlines()
    shared_line = next(l for l in board_lines if shared_id in l)
    branch_line = next(l for l in board_lines if branch_only_id in l)
    assert "[from:" not in shared_line
    assert "[from: run/spine]" in branch_line


# ---------------------------------------------------------------------------
# (d) --json determinism + ensure_ascii
# ---------------------------------------------------------------------------

@requires_git
def test_all_branches_json_deterministic_and_ascii(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    # Non-ASCII but cp1252-encodable (Windows console default codepage) --
    # a snowman/emoji here would hit the unrelated cp1252-console landmine in
    _git(repo, "checkout", "-b", "run/spine")
    r = _pinax(repo, "add", "--title", "café branch item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "branch item with non-ascii title")
    _git(repo, "checkout", "main")

    r1 = _pinax(repo, "board", "--all-branches", "--json")
    r2 = _pinax(repo, "board", "--all-branches", "--json")
    assert r1.returncode == 0, r1.stderr
    assert r1.stdout == r2.stdout, "repeated --all-branches --json calls must be byte-identical"

    # ensure_ascii: no raw multi-byte UTF-8 sequences in the JSON text itself.
    r1.stdout.encode("ascii")  # raises UnicodeEncodeError if any non-ASCII char leaked through
    payload = json.loads(r1.stdout)
    titles = [it.get("title", "") for it in payload["state"]["items"].values()]
    assert any("café" in t for t in titles)  # decoded back out correctly

    # Same determinism check for report/ready.
    for cmd in (["report", "--all-branches", "--json"], ["ready", "--all-branches", "--json"]):
        a = _pinax(repo, *cmd)
        b = _pinax(repo, *cmd)
        assert a.returncode == 0, a.stderr
        assert a.stdout == b.stdout, cmd
        a.stdout.encode("ascii")


# ---------------------------------------------------------------------------
# Multiple unmerged branches: sorted, deterministic attribution
# ---------------------------------------------------------------------------

@requires_git
def test_multiple_unmerged_branches_sorted_and_attributed(repo):
    r = _pinax(repo, "add", "--title", "main item", "--actor", "t@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "main item")

    _git(repo, "checkout", "-b", "run/zzz")
    r = _pinax(repo, "add", "--title", "zzz item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    zzz_id = json.loads(r.stdout)["item_id"]
    _commit_all(repo, "zzz item")
    _git(repo, "checkout", "main")

    _git(repo, "checkout", "-b", "run/aaa")
    r = _pinax(repo, "add", "--title", "aaa item", "--actor", "t@h", "--json")
    assert r.returncode == 0, r.stderr
    aaa_id = json.loads(r.stdout)["item_id"]
    _commit_all(repo, "aaa item")
    _git(repo, "checkout", "main")

    result = compute_all_branches_fold(repo, os.path.join(repo, ".ergon", "log"))
    assert result["source_branches"] == ["run/aaa", "run/zzz"]
    assert result["item_sources"][zzz_id] == ["run/zzz"]
    assert result["item_sources"][aaa_id] == ["run/aaa"]
    assert zzz_id in result["state"]["items"]
    assert aaa_id in result["state"]["items"]
