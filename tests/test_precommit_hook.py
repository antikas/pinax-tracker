"""Pre-commit hook installation and behavior tests."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import types

import pytest

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOKS_PRE_COMMIT = os.path.join(_PINAX_SRC, "hooks", "pre-commit")

if _PINAX_SRC not in sys.path:
    sys.path.insert(0, _PINAX_SRC)

from pinax.commands.init import _PRE_COMMIT_HOOK_CONTENT, run as pinax_init_run  # noqa: E402


class _ExecNamespace:
    """
    Thin proxy over the exact `globals()` dict a piece of exec'd code ran
    against. Functions defined by `exec(code, ns)` keep `__globals__ = ns`
    BY REFERENCE — so attribute writes here must mutate that same dict
    (not a detached copy, e.g. `types.SimpleNamespace(**ns)`) or a test
    setting `_INSTALL_TIME_PYTHON` afterwards would be invisible to the
    function when it is called.
    """

    def __init__(self, ns: dict):
        object.__setattr__(self, "_ns", ns)

    def __getattr__(self, name):
        try:
            return self._ns[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name, value):
        self._ns[name] = value


def _load_standalone_hook() -> types.ModuleType:
    """Load hooks/pre-commit as a real module. Its verify-or-skip driver is
    guarded by `if __name__ == '__main__'`, so this has no side effects.
    The file has no '.py' extension, so a loader must be given explicitly —
    spec_from_file_location cannot infer one from the filename."""
    loader = importlib.machinery.SourceFileLoader(
        "pinax_precommit_hook_under_test", _HOOKS_PRE_COMMIT
    )
    spec = importlib.util.spec_from_file_location(
        "pinax_precommit_hook_under_test", _HOOKS_PRE_COMMIT, loader=loader
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_rendered_hook_functions(rendered_source: str) -> _ExecNamespace:
    """
    The embedded/generated hook script runs its verify-or-skip driver at
    MODULE SCOPE (it is meant to run as a git hook, invoked directly by
    git — not imported), including a bare `sys.exit(...)`. Executing it
    wholesale would walk cwd looking for a repo root and could exit the
    test process. Extract just the definitions — everything before
    'def _find_repo_root():', which is where _pinax_verify_command and
    _INSTALL_TIME_PYTHON live — and exec THAT in an isolated namespace.
    """
    marker = "def _find_repo_root():"
    assert marker in rendered_source, (
        "hook shape changed — update this test's extraction marker"
    )
    header = rendered_source.split(marker)[0]
    ns: dict = {}
    exec(compile(header, "<rendered-pre-commit-hook>", "exec"), ns)
    return _ExecNamespace(ns)


@pytest.fixture(params=["standalone", "rendered"])
def hook_ns(request):
    """Both shipped hook copies, exposing the same _pinax_verify_command /
    _INSTALL_TIME_PYTHON surface, so every scenario runs against both and
    proves they stay in lockstep."""
    if request.param == "standalone":
        return _load_standalone_hook()
    rendered = _PRE_COMMIT_HOOK_CONTENT.replace("__INSTALL_TIME_PYTHON__", "None")
    return _load_rendered_hook_functions(rendered)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_which_found_is_used_first(hook_ns, monkeypatch):
    monkeypatch.setattr(
        shutil, "which", lambda name: r"C:\tools\pinax.exe" if name == "pinax" else None
    )
    assert hook_ns._pinax_verify_command() == [r"C:\tools\pinax.exe", "verify"]


def test_hook_run_time_interpreter_has_pinax_is_used_second(hook_ns, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    assert hook_ns._pinax_verify_command() == [sys.executable, "-m", "pinax", "verify"]


# ---------------------------------------------------------------------------
# (c): the baked install-time interpreter resolves when
# neither the PATH shim nor the hook-run-time interpreter do.
# ---------------------------------------------------------------------------


def test_baked_interpreter_used_when_nothing_else_resolves(hook_ns, monkeypatch, tmp_path):
    venv_python = tmp_path / "venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")  # only existence is checked, not content
    hook_ns._INSTALL_TIME_PYTHON = str(venv_python)

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = hook_ns._pinax_verify_command()

    assert result == [str(venv_python), "-m", "pinax", "verify"]
    # The candidate is re-verified (not blindly trusted) before use.
    assert calls == [[str(venv_python), "-c", "import pinax"]]


# ---------------------------------------------------------------------------
# (d) / (e): the baked candidate degrades safely instead of being trusted
# blindly — no crash, falls through to the existing warn+skip contract.
# ---------------------------------------------------------------------------


def test_baked_interpreter_stale_path_falls_through_to_none(hook_ns, monkeypatch, tmp_path):
    moved_venv_python = tmp_path / "deleted_venv" / "python.exe"  # never created
    hook_ns._INSTALL_TIME_PYTHON = str(moved_venv_python)

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    assert hook_ns._pinax_verify_command() is None


def test_baked_interpreter_present_but_pinax_not_importable_falls_through(
    hook_ns, monkeypatch, tmp_path
):
    venv_python = tmp_path / "venv_python.exe"
    venv_python.write_text("")
    hook_ns._INSTALL_TIME_PYTHON = str(venv_python)

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )

    assert hook_ns._pinax_verify_command() is None


# ---------------------------------------------------------------------------
# (f): the genuinely-not-installed case is unaffected — still degrades to
# warn+skip, never a crash and never a false-positive resolution.
# ---------------------------------------------------------------------------


def test_nothing_resolves_anywhere_degrades_to_none(hook_ns, monkeypatch):
    hook_ns._INSTALL_TIME_PYTHON = None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert hook_ns._pinax_verify_command() is None


# ---------------------------------------------------------------------------
# Template/lockstep sanity.
# ---------------------------------------------------------------------------


def test_standalone_template_ships_with_no_baked_interpreter():
    """A manually-copied hooks/pre-commit was never through 'pinax init', so
    it must not claim a baked interpreter it doesn't actually have."""
    mod = _load_standalone_hook()
    assert mod._INSTALL_TIME_PYTHON is None


def test_embedded_literal_has_exactly_one_substitution_placeholder():
    assert _PRE_COMMIT_HOOK_CONTENT.count("__INSTALL_TIME_PYTHON__") == 1


# ---------------------------------------------------------------------------
# End-to-end: 'pinax init' actually bakes ITS OWN running interpreter into
# the installed hook, and idempotent re-init / non-Pinax-hook preservation
# behave as documented. No real git subprocess needed — the installer only
# checks for a '.git/hooks' directory shape.
# ---------------------------------------------------------------------------


def _seed_fake_git_repo(root) -> None:
    os.makedirs(os.path.join(str(root), ".git", "hooks"))


def test_init_bakes_the_running_interpreter_into_the_generated_hook(tmp_path):
    _seed_fake_git_repo(tmp_path)
    pinax_init_run(str(tmp_path), actor="reviewer@example.test")

    hook_path = os.path.join(str(tmp_path), ".git", "hooks", "pre-commit")
    content = open(hook_path, encoding="utf-8").read()

    assert "__INSTALL_TIME_PYTHON__" not in content
    assert f"_INSTALL_TIME_PYTHON = {sys.executable!r}" in content
    assert "Pinax drift lint" in content  # ownership marker for idempotent overwrite


def test_init_idempotent_rerun_refreshes_baked_path_and_stays_pinax_owned(tmp_path):
    _seed_fake_git_repo(tmp_path)
    pinax_init_run(str(tmp_path), actor="reviewer@example.test")
    pinax_init_run(str(tmp_path), actor="reviewer@example.test")  # idempotent re-init

    hook_path = os.path.join(str(tmp_path), ".git", "hooks", "pre-commit")
    content = open(hook_path, encoding="utf-8").read()

    assert f"_INSTALL_TIME_PYTHON = {sys.executable!r}" in content
    assert "Pinax drift lint" in content


def test_init_does_not_overwrite_a_non_pinax_hook(tmp_path):
    _seed_fake_git_repo(tmp_path)
    hook_path = os.path.join(str(tmp_path), ".git", "hooks", "pre-commit")
    foreign_content = "#!/bin/sh\necho 'not pinax'\n"
    with open(hook_path, "w", encoding="utf-8") as fh:
        fh.write(foreign_content)

    pinax_init_run(str(tmp_path), actor="reviewer@example.test")

    assert open(hook_path, encoding="utf-8").read() == foreign_content


def test_generated_hook_uses_install_time_interpreter(tmp_path, monkeypatch):
    """Use the recorded interpreter when other Pinax lookup paths are unavailable."""
    _seed_fake_git_repo(tmp_path)
    pinax_init_run(str(tmp_path), actor="reviewer@example.test")
    hook_path = os.path.join(str(tmp_path), ".git", "hooks", "pre-commit")
    rendered = open(hook_path, encoding="utf-8").read()
    ns = _load_rendered_hook_functions(rendered)

    assert ns._INSTALL_TIME_PYTHON == sys.executable

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    result = ns._pinax_verify_command()

    assert result == [sys.executable, "-m", "pinax", "verify"]
