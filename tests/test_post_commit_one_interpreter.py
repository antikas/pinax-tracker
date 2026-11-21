"""The post-commit hook checks and regenerates the portfolio in one Python child."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys

import pytest

from tests.test_doctor import (
    _PINAX_SRC,
    _commit_all,
    _git,
    _init_repo,
    _pinax,
    requires_git,
)

pytestmark = requires_git

_HOOK_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "post-commit",
)

_REAL_RUN = subprocess.run


def _load_hook():
    loader = importlib.machinery.SourceFileLoader("pinax_post_commit_hook", _HOOK_SRC)
    spec = importlib.util.spec_from_loader("pinax_post_commit_hook", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.child_codes: list[int] = []

    def __call__(self, args, *rest, **kwargs):
        self.calls.append(list(args))
        result = _REAL_RUN(args, *rest, **kwargs)
        if args and args[0] == sys.executable:
            self.child_codes.append(result.returncode)
        return result

    def python_calls(self) -> list[list[str]]:
        return [call for call in self.calls if call and call[0] == sys.executable]

    def git_commits(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["git", "commit"]]


@pytest.fixture()
def repo(tmp_path, tmp_path_factory, monkeypatch):
    root = str(tmp_path)
    _init_repo(root)
    assert _pinax(root, "init").returncode == 0
    assert not os.path.exists(os.path.join(root, ".git", "hooks", "post-commit"))
    with open(os.path.join(root, "PORTFOLIO.md"), "w") as fh:
        fh.write("stale\n")
    _commit_all(root, "init: pinax ergon base")
    monkeypatch.setenv("PYTHONPATH", _PINAX_SRC)
    # The overview otherwise scans ~/src, whose live trackers can change
    # between two runs of it.
    monkeypatch.setenv("PINAX_ROOTS", str(tmp_path_factory.mktemp("no-other-repos")))
    monkeypatch.chdir(root)
    return root


def _run_hook(module, monkeypatch) -> _Recorder:
    recorder = _Recorder()
    with monkeypatch.context() as patch:
        patch.setattr(module.subprocess, "run", recorder)
        module.main()
    return recorder


def _ordinary_commit(root: str) -> None:
    with open(os.path.join(root, "work.txt"), "w") as fh:
        fh.write("work\n")
    _git(root, "add", "work.txt")
    _git(root, "commit", "-q", "-m", "ordinary work", "--", "work.txt")


def _portfolio(root: str) -> str:
    with open(os.path.join(root, "PORTFOLIO.md")) as fh:
        return fh.read()


def _without_generated_time(text: str) -> str:
    return re.sub(r"_Generated: [^_]*_", "_Generated: _", text)


def _assert_regenerated_and_committed(root: str) -> None:
    assert _portfolio(root) != "stale\n"
    assert _git(root, "log", "-1", "--format=%s").stdout.strip() == "pinax: regenerate PORTFOLIO.md"
    assert _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split() == ["PORTFOLIO.md"]
    assert _git(root, "status", "--porcelain", "--", "PORTFOLIO.md").stdout == ""


def test_an_ordinary_commit_starts_one_python_child(repo, monkeypatch):
    _ordinary_commit(repo)

    recorder = _run_hook(_load_hook(), monkeypatch)

    assert len(recorder.python_calls()) == 1, recorder.calls
    _assert_regenerated_and_committed(repo)


def test_the_child_writes_what_the_overview_command_writes(repo, monkeypatch):
    _ordinary_commit(repo)
    _run_hook(_load_hook(), monkeypatch)
    from_hook = _portfolio(repo)
    _git(repo, "reset", "-q", "--hard", "HEAD~1")

    assert _pinax(repo, "overview", "--markdown").returncode == 0

    assert _without_generated_time(_portfolio(repo)) == _without_generated_time(from_hook)


def test_the_child_starts_no_further_python_process():
    with open(_HOOK_SRC) as fh:
        text = fh.read()
    assert '"-m", "pinax"' not in text
    assert "subprocess" not in _load_hook()._CHECK_AND_REGENERATE


def _edit_portfolio(root: str) -> None:
    # A hook that commits whenever PORTFOLIO.md differs from HEAD, whatever
    # the child reported, would commit this edit.
    with open(os.path.join(root, "PORTFOLIO.md"), "w") as fh:
        fh.write("edited\n")


def _shadow_pinax(tmp_path_factory, monkeypatch) -> None:
    shadow = tmp_path_factory.mktemp("shadow")
    os.makedirs(os.path.join(shadow, "pinax"))
    with open(os.path.join(shadow, "pinax", "__init__.py"), "w") as fh:
        fh.write("raise ImportError('shadowed pinax')\n")
    monkeypatch.setenv("PYTHONPATH", str(shadow) + os.pathsep + _PINAX_SRC)


def test_an_unfinished_operation_commits_an_edited_portfolio_nothing(repo, monkeypatch):
    _ordinary_commit(repo)
    _edit_portfolio(repo)
    os.makedirs(os.path.join(repo, ".git", "rebase-merge"))

    hook = _load_hook()
    recorder = _run_hook(hook, monkeypatch)

    assert recorder.child_codes == [hook._UNFINISHED]
    assert recorder.git_commits() == []
    assert _portfolio(repo) == "edited\n"


def test_a_failed_import_commits_an_edited_portfolio_nothing(
    repo, tmp_path_factory, monkeypatch
):
    _ordinary_commit(repo)
    _edit_portfolio(repo)
    _shadow_pinax(tmp_path_factory, monkeypatch)

    hook = _load_hook()
    recorder = _run_hook(hook, monkeypatch)

    assert recorder.child_codes == [hook._FAILED]
    assert recorder.git_commits() == []
    assert _portfolio(repo) == "edited\n"


def test_a_failed_import_exits_failed_without_a_traceback(
    repo, tmp_path_factory, monkeypatch, capfd
):
    _ordinary_commit(repo)
    _shadow_pinax(tmp_path_factory, monkeypatch)
    capfd.readouterr()

    hook = _load_hook()
    recorder = _run_hook(hook, monkeypatch)

    # An uncaught ImportError also exits 1, so only the missing traceback
    # shows that the child caught it.
    assert "Traceback" not in capfd.readouterr().err
    assert recorder.child_codes == [hook._FAILED]
    assert recorder.git_commits() == []
    assert _portfolio(repo) == "stale\n"


def test_an_overview_that_exits_non_zero_commits_nothing(
    repo, tmp_path_factory, monkeypatch, capfd
):
    _ordinary_commit(repo)
    _edit_portfolio(repo)
    # A pinned root other than the repository makes the overview refuse with
    # sys.exit(1) before it writes anything.
    monkeypatch.setenv("PINAX_ROOT", str(tmp_path_factory.mktemp("other-root")))
    capfd.readouterr()

    hook = _load_hook()
    recorder = _run_hook(hook, monkeypatch)

    assert "ROOT MISMATCH" in capfd.readouterr().err
    assert recorder.child_codes == [hook._FAILED]
    assert recorder.git_commits() == []
    assert _portfolio(repo) == "edited\n"


def test_an_ordinary_commit_regenerates_and_commits(repo, monkeypatch):
    _ordinary_commit(repo)

    hook = _load_hook()
    recorder = _run_hook(hook, monkeypatch)

    assert recorder.child_codes == [hook._REGENERATED]
    assert len(recorder.git_commits()) == 1
    _assert_regenerated_and_committed(repo)


def test_a_resolved_cherry_pick_conflict_commits_nothing(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "side")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("side\n")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("main\n")
    _commit_all(repo, "main")
    assert _git(repo, "cherry-pick", "side", check=False).returncode != 0
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("resolved\n")
    _git(repo, "add", "conflict.txt")
    assert os.path.exists(os.path.join(repo, ".git", "CHERRY_PICK_HEAD"))

    recorder = _run_hook(_load_hook(), monkeypatch)

    assert recorder.git_commits() == []
    assert len(recorder.python_calls()) == 1
    assert _portfolio(repo) == "stale\n"
