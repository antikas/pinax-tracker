"""
An earlier Pinax, one that writes version 2 events and has no decisions,
reading a log that holds a park decision.

The earlier build is the real pinax/ package of the commit before decisions,
extracted with git archive. Its verify reports the decided item's page as
drift and nothing else, with no integrity violation; its own block commits
through its own pre-commit hook; this tree then reports the same page as
drift, and verify --fix repairs it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent
EARLIER_COMMIT = "a580c82"
OWNER = "owner@desk"
DECIDER = "notifier@desk"

pytestmark = pytest.mark.deep


def _git(repo: str, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _env(tree: Path, path_first: str | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(tree)
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    if path_first is not None:
        env["PATH"] = path_first + os.pathsep + env.get("PATH", "")
    return env


def _run(tree: Path, repo: str, *args: str, path_first: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, env=_env(tree, path_first), capture_output=True, text=True,
    )


def _extract_earlier(tmp_path: Path) -> Path:
    try:
        archive = subprocess.run(
            ["git", "archive", EARLIER_COMMIT, "pinax"],
            cwd=SOURCE_ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"commit {EARLIER_COMMIT} is not reachable")
    tree = tmp_path / "earlier"
    tree.mkdir()
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    return tree


def _launcher(tmp_path: Path, tree: Path) -> str:
    bin_dir = tmp_path / "earlier-bin"
    bin_dir.mkdir()
    script = bin_dir / "pinax"
    script.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH='{tree}'\n"
        "export PYTHONPATH\n"
        f"exec '{sys.executable}' -m pinax \"$@\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(bin_dir)


def _item_id(done) -> str:
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])["item_id"]


@pytest.mark.skipif(os.name == "nt", reason="the launcher is a POSIX shell script")
def test_an_earlier_build_reads_a_park_decision(tmp_path):
    earlier = _extract_earlier(tmp_path)
    current = SOURCE_ROOT
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "builder@example.invalid")
    _git(repo, "config", "user.name", "Builder")
    _git(repo, "config", "core.autocrlf", "false")
    assert _run(current, repo, "init", "--actor", OWNER).returncode == 0
    decided = _item_id(_run(current, repo, "add", "--title", "Decided",
                            "--actor", OWNER, "--offline", "--json"))
    other = _item_id(_run(current, repo, "add", "--title", "Other",
                          "--actor", OWNER, "--offline", "--json"))
    done = _run(current, repo, "decide", decided, "--option", "B", "--label", "Park it",
                "--effect", "park", "--reason", "not now", "--actor", DECIDER,
                "--offline", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    page = f".ergon/items/{decided}.md"

    verified = _run(earlier, repo, "verify")
    report = verified.stdout + verified.stderr
    assert verified.returncode != 0
    assert "DRIFT" in report
    drifted = [line.strip() for line in report.splitlines() if line.strip().startswith(".ergon/")]
    assert drifted == [page]
    assert "ntegrity" not in report
    assert "rev-chain" not in report

    commits = int(_git(repo, "rev-list", "--count", "HEAD"))
    blocked = _run(earlier, repo, "block", other, "--gate", "scope", "--actor", OWNER,
                   "--offline", path_first=_launcher(tmp_path, earlier))
    assert blocked.returncode == 0, blocked.stdout + blocked.stderr
    assert int(_git(repo, "rev-list", "--count", "HEAD")) == commits + 1
    assert "## Decision" not in Path(repo, page).read_text(encoding="utf-8")

    after = _run(current, repo, "verify")
    lines = [line.strip() for line in (after.stdout + after.stderr).splitlines()
             if line.strip().startswith(".ergon/")]
    assert after.returncode != 0
    assert lines == [page]

    fixed = _run(current, repo, "verify", "--fix")
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    assert "## Decision" in Path(repo, page).read_text(encoding="utf-8")
    assert _run(current, repo, "verify").returncode == 0
