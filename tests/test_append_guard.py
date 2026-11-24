"""
Append guard tests: a test run writes only to logs under the temporary directory.

The guard applies only when PINAX_TEST_RUN is 1, whoever writes the event
and whatever the event holds. These tests build their own directories beside
this file, outside the temporary directory, and remove them.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from pinax.append import (
    AppendLeakGuardError,
    _is_isolated_log_dir,
    append_event,
)
from pinax.event import mint_event

SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _make_writable_and_retry(func, path, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


@pytest.fixture()
def outside():
    """A directory that is not under the temporary directory."""
    path = tempfile.mkdtemp(prefix="_scratch_guard_", dir=os.path.dirname(os.path.abspath(__file__)))
    yield path
    shutil.rmtree(path, onerror=_make_writable_and_retry)


def _event(actor: str, payload: dict | None = None, seq: int = 0, prev: str = "") -> dict:
    return mint_event(
        seq=seq, ts="2026-07-08T00:00:00Z", actor=actor,
        etype="ergon.created", payload=payload or {"repo": "pinax"}, prev=prev,
    )


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


def _env(test_run: str | None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    if test_run is None:
        env.pop("PINAX_TEST_RUN", None)
    else:
        env["PINAX_TEST_RUN"] = test_run
    return env


def _pinax(cwd: str, *args: str, test_run: str | None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=cwd, env=_env(test_run), capture_output=True, text=True,
    )


def _snapshot(repo: str) -> dict[str, bytes]:
    root = Path(repo) / ".ergon"
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _refusal_text(log_dir: str) -> str:
    return (
        f"pinax: refusing to write to {log_dir} during a test run (PINAX_TEST_RUN=1): "
        f"a test may write only to a log under {tempfile.gettempdir()}"
    )


@pytest.fixture()
def empty_repo(outside):
    """
    A git repository outside the temporary directory, with no tracker.

    Every command here runs in it: run in a plain directory, a command
    would resolve its root to the repository that holds this file.
    """
    path = os.path.join(outside, "repo")
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "builder@example.invalid")
    _git(path, "config", "user.name", "Builder")
    _git(path, "config", "core.autocrlf", "false")
    return path


@pytest.fixture()
def repo(empty_repo):
    """A committed tracker outside the temporary directory, with one item."""
    path = empty_repo
    done = _pinax(path, "init", "--actor", "owner@desk", test_run=None)
    assert done.returncode == 0, done.stderr
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    done = _pinax(
        path, "add", "--title", "First", "--actor", "owner@desk", test_run=None,
    )
    assert done.returncode == 0, done.stderr
    return path


# ---------------------------------------------------------------------------
# append_event during a test run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("actor", ["operator@host", "test@host"])
def test_a_test_run_refuses_any_writer_outside_the_temporary_directory(outside, actor):
    with pytest.raises(AppendLeakGuardError) as caught:
        append_event(outside, _event(actor), actor=actor)
    assert str(caught.value).startswith(_refusal_text(outside))
    assert os.listdir(outside) == []


def test_a_refused_append_leaves_an_existing_shard_unchanged(outside, monkeypatch):
    first = _event("operator@host")
    with monkeypatch.context() as patch:
        patch.delenv("PINAX_TEST_RUN")
        shard = Path(append_event(outside, first, actor="operator@host"))
    before = shard.read_bytes()
    assert before
    with pytest.raises(AppendLeakGuardError):
        append_event(
            outside, _event("operator@host", seq=1, prev=first["id"]), actor="operator@host",
        )
    assert shard.read_bytes() == before


@pytest.mark.parametrize("actor, payload", [
    ("test@host", {"repo": "test"}),
    ("reviewer@host", {"item_id": "T-001"}),
])
def test_a_test_run_writes_under_the_temporary_directory(tmp_path, actor, payload):
    path = append_event(str(tmp_path), _event(actor, payload), actor=actor)
    assert os.path.exists(path)


# ---------------------------------------------------------------------------
# append_event outside a test run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "0", "true", "11", " 1"])
@pytest.mark.parametrize("actor, payload", [
    ("Test-Runner@host", {"repo": "pinax"}),
    ("reviewer@host", {"item_id": "T-001"}),
])
def test_the_guard_is_off_unless_the_variable_is_exactly_1(
    outside, monkeypatch, value, actor, payload
):
    if value is None:
        monkeypatch.delenv("PINAX_TEST_RUN")
    else:
        monkeypatch.setenv("PINAX_TEST_RUN", value)
    path = append_event(outside, _event(actor, payload), actor=actor)
    assert os.path.exists(path)


@pytest.mark.parametrize("actor, shard", [
    ("tester@laptop", "tester-laptop.jsonl"),
    ("test@laptop", "test-laptop.jsonl"),
])
def test_a_writer_name_that_contains_test_is_accepted(repo, actor, shard):
    done = _pinax(repo, "add", "--title", "T", "--actor", actor, "--json", test_run=None)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Traceback" not in done.stderr
    result = json.loads(done.stdout)
    lines = (Path(repo) / ".ergon" / "log" / shard).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == [result["event_id"]]


# ---------------------------------------------------------------------------
# The command's refusal
# ---------------------------------------------------------------------------

def test_a_write_command_under_json_reports_the_refusal_once(repo):
    before, log = _snapshot(repo), _git(repo, "log", "--format=%H")
    done = _pinax(
        repo, "add", "--title", "T", "--actor", "owner@desk", "--json", test_run="1",
    )
    assert done.returncode == 10, done.stdout + done.stderr
    assert "Traceback" not in done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 1, done.stdout
    result = json.loads(lines[0])
    assert result["published"] is False
    assert result["sync_status"] == "test_run_refused"
    assert result["exit_code"] == 10
    assert result["command"] == "add"
    assert result["error"].startswith(_refusal_text(os.path.join(repo, ".ergon", "log")))
    assert "unset PINAX_TEST_RUN" in result["error"]
    assert done.stderr.splitlines().count(result["error"]) == 1
    assert _snapshot(repo) == before
    assert _git(repo, "log", "--format=%H") == log


@pytest.mark.parametrize("args", [
    ["add", "--title", "T", "--actor", "owner@desk"],
    ["dispatch", "--claim", "--actor", "owner@desk"],
    ["status", "{item}", "building", "--actor", "owner@desk"],
    ["registry", "add", "--id", "here", "--path", ".", "--actor", "owner@desk"],
    ["reconcile", "--actor", "owner@desk"],
], ids=lambda args: args[0])
def test_every_kind_of_append_is_refused_without_a_traceback(repo, args):
    item_id = next(iter(_item_ids(repo)))
    (Path(repo) / "BACKLOG-OFFLINE.md").write_text(
        f"- 2026-07-01T10:00:00Z owner@desk done {item_id} | Finished\n", encoding="utf-8",
    )
    before, log = _snapshot(repo), _git(repo, "log", "--format=%H")
    done = _pinax(repo, *[a.format(item=item_id) for a in args], test_run="1")
    assert done.returncode == 10, done.stdout + done.stderr
    assert done.stdout == ""
    assert done.stderr.count("refusing to write to") == 1
    assert "Traceback" not in done.stderr
    assert _snapshot(repo) == before
    assert _git(repo, "log", "--format=%H") == log


def _item_ids(repo: str) -> set[str]:
    return {
        json.loads(line)["payload"]["item_id"]
        for shard in (Path(repo) / ".ergon" / "log").glob("*.jsonl")
        for line in shard.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["type"] == "item.created"
    }


def test_init_is_refused_without_a_traceback(empty_repo):
    done = _pinax(empty_repo, "init", "--actor", "owner@desk", test_run="1")
    assert done.returncode == 10, done.stdout + done.stderr
    assert done.stderr.startswith("pinax: refusing to write to")
    assert "Traceback" not in done.stderr
    assert list(Path(empty_repo).rglob("*.jsonl")) == []


# ---------------------------------------------------------------------------
# The suite runs as a test run
# ---------------------------------------------------------------------------

def test_the_suite_sets_the_variable():
    assert os.environ["PINAX_TEST_RUN"] == "1"


def test_a_subprocess_of_a_suite_helper_is_in_the_test_run(empty_repo):
    from test_json_every_exit import _env as helper_env, _pinax as helper_pinax

    assert helper_env()["PINAX_TEST_RUN"] == "1"
    done = helper_pinax(empty_repo, "init", "--actor", "owner@desk")
    assert done.returncode == 10, done.stdout + done.stderr


def test_a_subprocess_sees_the_same_temporary_directory():
    done = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        check=True, capture_output=True, text=True,
    )
    assert os.path.realpath(done.stdout.strip()) == os.path.realpath(tempfile.gettempdir())


def test_no_subprocess_environment_is_built_from_scratch():
    """A helper that passes env as a literal would drop the variable."""
    for path in sorted(Path(__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg == "env":
                if isinstance(node.value, ast.Dict):
                    assert None in node.value.keys, f"{path.name}:{node.value.lineno}"


# ---------------------------------------------------------------------------
# The directory check
# ---------------------------------------------------------------------------

def test_is_isolated_log_dir_true_under_temp(tmp_path):
    assert _is_isolated_log_dir(tempfile.gettempdir()) is True
    assert _is_isolated_log_dir(str(tmp_path)) is True


def test_is_isolated_log_dir_false_outside_temp(outside):
    assert _is_isolated_log_dir(outside) is False


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege on Windows")
def test_a_link_out_of_the_temporary_directory_is_refused(tmp_path, outside):
    link = tmp_path / "log"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AppendLeakGuardError):
        append_event(str(link), _event("operator@host"), actor="operator@host")
    assert os.listdir(outside) == []


def test_no_code_looks_for_test_in_a_writer_name():
    for path in sorted((SOURCE_ROOT / "pinax").rglob("*.py")):
        assert '"test" in' not in path.read_text(encoding="utf-8"), path
