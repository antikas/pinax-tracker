"""
Tests for branch-scoped truth warnings.

The failure this guards against: `pinax board` run
from main answered "queue empty" while an in-flight run's 19 items were
tracked wholly in .ergon commits on an unmerged run branch checked out in a
separate worktree. board/report/ready must now warn on stderr whenever any
local branch carries tracker events not reachable from HEAD — and stdout
(human or --json) must stay byte-identical to the warning-free case.

Real-git fixture (same harness shape as tests/test_replay.py): subprocess
git init/branch/commit; the pure detector is exercised in-process, the
shipped CLI via `python -m pinax` subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from pinax.visibility import unmerged_tracker_refs, warn_unmerged

pytestmark = pytest.mark.deep

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
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


def _append_shard_line(repo_root: str, line: str) -> None:
    log_dir = os.path.join(repo_root, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "t.jsonl"), "a", newline="\n") as f:
        f.write(line + "\n")


@pytest.fixture()
def repo(tmp_path):
    """main with one committed shard line; no side branches yet."""
    root = str(tmp_path)
    _init_repo(root)
    _append_shard_line(root, '{"id": "e1"}')
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")
    return root


@requires_git
def test_no_side_branches_is_silent(repo):
    assert unmerged_tracker_refs(repo) == []


@requires_git
def test_unmerged_branch_detected_with_event_count(repo):
    _git(repo, "checkout", "-b", "run/spine")
    _append_shard_line(repo, '{"id": "e2"}')
    _append_shard_line(repo, '{"id": "e3"}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "run events")
    _git(repo, "checkout", "main")

    assert unmerged_tracker_refs(repo) == [("run/spine", 2)]


@requires_git
def test_merged_branch_is_silent(repo):
    _git(repo, "checkout", "-b", "run/spine")
    _append_shard_line(repo, '{"id": "e2"}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "run events")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "run/spine")

    assert unmerged_tracker_refs(repo) == []


@requires_git
def test_branch_without_tracker_commits_is_silent(repo):
    _git(repo, "checkout", "-b", "feat/other")
    with open(os.path.join(repo, "code.py"), "w", newline="\n") as f:
        f.write("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "non-tracker work")
    _git(repo, "checkout", "main")

    assert unmerged_tracker_refs(repo) == []


def test_non_git_dir_is_silent_not_fatal():
    with tempfile.TemporaryDirectory() as plain_dir:
        assert unmerged_tracker_refs(plain_dir) == []
        assert warn_unmerged(plain_dir) == []


@requires_git
def test_warn_unmerged_message_shape(repo, capsys):
    _git(repo, "checkout", "-b", "run/spine")
    _append_shard_line(repo, '{"id": "e2"}')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "run events")
    _git(repo, "checkout", "main")

    refs = warn_unmerged(repo)
    err = capsys.readouterr().err
    assert refs == [("run/spine", 1)]
    assert "WARNING" in err
    assert "run/spine" in err
    assert "+1 event" in err
    assert "branch-scoped" in err


# ---------------------------------------------------------------------------
# End-to-end: the shipped CLI warns on stderr; stdout stays clean.
# ---------------------------------------------------------------------------

@requires_git
@pytest.mark.parametrize("command", [["board"], ["board", "--json"],
                                     ["ready"], ["report"]])
def test_cli_warns_on_stderr_stdout_clean(tmp_path, command):
    root = str(tmp_path)
    _init_repo(root)
    result = _pinax(root, "init")
    assert result.returncode == 0, result.stderr
    result = _pinax(root, "add", "--title", "seed item", "--actor", "t@h")
    assert result.returncode == 0, result.stderr
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "seed")

    _git(root, "checkout", "-b", "run/spine")
    result = _pinax(root, "add", "--title", "run-branch item", "--actor", "t@h")
    assert result.returncode == 0, result.stderr
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "run-branch event")
    _git(root, "checkout", "main")

    result = _pinax(root, *command)
    assert result.returncode == 0, result.stderr
    assert "WARNING" in result.stderr
    assert "run/spine" in result.stderr
    assert "WARNING" not in result.stdout
    if "--json" in command:
        json.loads(result.stdout)  # stdout must remain pure JSON
    else:
        # the run-branch item must NOT appear in main's fold
        assert "run-branch item" not in result.stdout
