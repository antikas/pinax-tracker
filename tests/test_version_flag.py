"""Tests for the global --version option."""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

import pinax
import pinax.__main__

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def test_version_subprocess_outside_git_repo(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pinax", "--version"],
        cwd=str(tmp_path),
        env=_build_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"pinax {pinax.__version__}\n"
    assert result.stderr == ""


def test_version_reads_package_version(monkeypatch, capsys):
    monkeypatch.setattr(pinax, "__version__", "9.9.9")
    with pytest.raises(SystemExit) as exc:
        pinax.__main__.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "pinax 9.9.9"


def test_version_matches_pyproject():
    with open(os.path.join(_PINAX_SRC, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match is not None
    assert pinax.__version__ == match.group(1)


def test_no_arguments_still_prints_usage(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "pinax"],
        cwd=str(tmp_path),
        env=_build_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "usage: pinax" in result.stderr
