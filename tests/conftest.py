"""
tests/conftest.py — pytest plugin (auto-loaded by pytest).

Makes normalise_for_comparison available to all test files via helpers.py.
"""

# Re-export for any test that needs it via `from tests.helpers import ...`
# This file is intentionally minimal; helpers.py holds the actual code.


# ---------------------------------------------------------------------------
# Test-lane standard: in-framework verdict emission (scale-down profile).
# Single fast lane; every invocation writes .verdicts/latest.json + a
# Dependency-free by design (stdlib only: json, os, pathlib, subprocess, time).
# ---------------------------------------------------------------------------
import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import time as _time
from datetime import datetime as _datetime
from pathlib import Path as _Path

import pytest as _pytest


# ---------------------------------------------------------------------------
# During a test run the guard in pinax/append.py accepts only logs under the
# temporary directory, and tests rely on tmp_path being inside it. A runner can
# place pytest's base temporary directory elsewhere with --basetemp, so this
# makes that directory the temp tree for the test process and, through the
# variables tempfile reads, for every subprocess a test starts.
# ---------------------------------------------------------------------------

@_pytest.fixture(autouse=True, scope="session")
def _base_temp_is_the_temp_tree(tmp_path_factory: "_pytest.TempPathFactory"):
    base = str(tmp_path_factory.getbasetemp())
    _tempfile.tempdir = base
    with _pytest.MonkeyPatch.context() as patch:
        for name in ("TMPDIR", "TEMP", "TMP"):
            patch.setenv(name, base)
        yield


@_pytest.fixture(autouse=True, scope="session")
def _the_session_is_a_test_run(_base_temp_is_the_temp_tree):
    with _pytest.MonkeyPatch.context() as patch:
        patch.setenv("PINAX_TEST_RUN", "1")
        yield


# ---------------------------------------------------------------------------
# Tests that start the local web view fetch it from 127.0.0.1. A proxy named in
# the environment would take that request and fail it, so local addresses are
# marked as never proxied for the whole session.
# ---------------------------------------------------------------------------

@_pytest.fixture(autouse=True, scope="session")
def _local_addresses_bypass_any_proxy():
    with _pytest.MonkeyPatch.context() as patch:
        for name in ("NO_PROXY", "no_proxy"):
            patch.setenv(name, "127.0.0.1,localhost")
        yield


# ---------------------------------------------------------------------------
# The pre-commit hook 'pinax init' installs runs the first 'pinax' on PATH.
# On a host with an installed release, that release would judge this tree's
# events by its own rules. This puts a launcher for this source tree first on
# PATH for the whole session, so every hook a test repository runs verifies
# with the code under test.
# ---------------------------------------------------------------------------

_SOURCE_ROOT = str(_Path(__file__).resolve().parent.parent)


def _write_pinax_launchers(bin_dir: "_Path") -> None:
    if _os.name == "nt":
        (bin_dir / "pinax.cmd").write_text(
            "@echo off\r\n"
            f'set "PYTHONPATH={_SOURCE_ROOT};%PYTHONPATH%"\r\n'
            f'"{_sys.executable}" -m pinax %*\r\n',
            encoding="utf-8",
        )
    script = bin_dir / "pinax"
    script.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH='{_SOURCE_ROOT}'${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "export PYTHONPATH\n"
        f"exec '{_sys.executable}' -m pinax \"$@\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


@_pytest.fixture(autouse=True, scope="session")
def _source_tree_pinax_first_on_path(tmp_path_factory: "_pytest.TempPathFactory"):
    bin_dir = tmp_path_factory.mktemp("pinax-launcher")
    _write_pinax_launchers(bin_dir)
    with _pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", str(bin_dir) + _os.pathsep + _os.environ.get("PATH", ""))
        yield str(bin_dir)


def pytest_sessionstart(session: "_pytest.Session") -> None:
    session.config._lane_session_start = _time.monotonic()


def pytest_sessionfinish(session: "_pytest.Session", exitstatus: int) -> None:
    config = session.config
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", {}) if reporter else {}

    def _n(key: str) -> int:
        return len(stats.get(key, []))

    counts = {
        "passed": _n("passed"),
        "failed": _n("failed"),
        "errors": _n("error"),
        "skipped": _n("skipped"),
        "deselected": _n("deselected"),
    }
    skips = []
    for report in stats.get("skipped", []):
        longrepr = getattr(report, "longrepr", None)
        reason = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else str(longrepr)
        skips.append({"test": getattr(report, "nodeid", "?"), "reason": reason.removeprefix("Skipped: ")})

    fired = any(
        "from pytest-timeout" in str(getattr(report, "longrepr", ""))
        for key in ("failed", "error")
        for report in stats.get(key, [])
    )
    start = getattr(config, "_lane_session_start", None)

    # Derive the lane label and the not_exercised confession from the
    # marker expression actually in effect for THIS invocation (addopts'
    # default -m "not deep", an explicit -m deep, -m "deep or not deep",
    # or anything else) rather than a hardcoded literal — a hardcoded
    # "fast" would silently lie on any non-default invocation.
    markexpr = (getattr(config.option, "markexpr", "") or "").strip()
    if markexpr == "not deep":
        lane = "fast"
    elif markexpr == "deep":
        lane = "deep"
    elif not markexpr:
        lane = "all"
    else:
        lane = markexpr

    not_exercised = []
    if counts["deselected"] > 0:
        if lane == "fast":
            not_exercised.append({
                "scope": f"deep lane ({counts['deselected']} tests)",
                "reason": "git-subprocess/temp-repo integration tests excluded from "
                          "fast by 'not deep'; run via 'pytest -m deep'",
            })
        else:
            not_exercised.append({
                "scope": f"{counts['deselected']} tests deselected by marker expression '{markexpr}'",
                "reason": "excluded by the active -m filter for this invocation",
            })

    payload = {
        "repo": "pinax",
        "lane": lane,
        "command": " ".join(_sys.argv),
        "duration_s": round(_time.monotonic() - start, 2) if start is not None else 0.0,
        "exit_code": int(exitstatus),
        "counts": counts,
        "skips": skips,
        "not_exercised": not_exercised,
        "timeout": {"per_test_s": config.getoption("timeout", None), "global_s": None, "fired": fired},
        "verdict": "green"
        if (exitstatus == 0 and counts["failed"] == 0 and counts["errors"] == 0 and not fired)
        else "red",
    }
    verdict_dir = _Path(config.rootpath) / ".verdicts"
    verdict_dir.mkdir(parents=True, exist_ok=True)
    body = _json.dumps(payload, indent=2)
    (verdict_dir / "latest.json").write_text(body, encoding="utf-8")
    (verdict_dir / f"{_datetime.now().strftime('%Y%m%dT%H%M%S')}.json").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared deep-lane fixture: a bare hub for a test whose subject needs a
# mutating command driven against a reachable `origin` (claim is the one
# command the publish sequence in pinax/sync.py always requires it for; see
# docs/sharing-and-sync.md). One owner for this
# construction so it is not copied per test file.
#
# Lives entirely under the caller's pytest base temporary directory
# (tmp_path). Its core.hooksPath points at an empty directory alongside it
# so no hook installed on the host machine, outside pinax's own, gates a
# test commit. The hub publishes no content on its own: the caller wires a working repository's `origin` remote at the returned path
# and pushes that repository's base commit before running a command that
# requires the remote.
# ---------------------------------------------------------------------------

@_pytest.fixture()
def bare_hub_origin(tmp_path: "_Path") -> str:
    hub = str(tmp_path / "hub.git")
    hooks_dir = str(tmp_path / "hub-hooks-empty")
    _os.makedirs(hooks_dir, exist_ok=True)
    _subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", hub],
        check=True, capture_output=True, text=True,
    )
    _subprocess.run(
        ["git", "-C", hub, "config", "core.hooksPath", hooks_dir],
        check=True, capture_output=True, text=True,
    )
    return hub


# ---------------------------------------------------------------------------
# Shared deep-lane fixture: the working clone wired to that hub.
#
# One owner for the construction every such test needs: a 'clone'
# directory beside the hub, an empty hooks directory of its own so no host
# machine hook gates a test commit, 'pinax init', the base commit, the
# `origin` remote and the first push of the default branch.
#
# A test file keeps its own git and CLI subprocess helpers, which differ in
# what they configure and in how they report a failure, and hands them to
# the factory this fixture returns. Nothing about the construction is
# copied per file, and no file gives up its own conventions.
# ---------------------------------------------------------------------------

@_pytest.fixture()
def clone_wired_to_hub(tmp_path: "_Path", bare_hub_origin: str):
    def build(*, init_repo, git, pinax, commit_all, actor: str, name: str = "clone") -> str:
        """
        Build the clone and return its path.

        init_repo(root), git(root, *args), pinax(root, *args) and
        commit_all(root, message) are the caller's own helpers; actor is
        the role@host handle 'pinax init' records. name places the clone
        beside the hub, so a test that needs more than one working
        repository names them apart.
        """
        root = str(tmp_path / name)
        _os.makedirs(root, exist_ok=True)
        init_repo(root)
        hooks_dir = str(tmp_path / (name + "-hooks-empty"))
        _os.makedirs(hooks_dir, exist_ok=True)
        git(root, "config", "core.hooksPath", hooks_dir)
        result = pinax(root, "init", "--actor", actor)
        assert result.returncode == 0, result.stderr
        commit_all(root, "init: pinax ergon base")
        git(root, "remote", "add", "origin", bare_hub_origin)
        git(root, "push", "origin", "main")
        return root

    return build


# ---------------------------------------------------------------------------
# Shared deep-lane fixture: a second working repository cloned from the same
# hub, for a test that needs two machines rather than one.
#
# The hub is already published by the time this runs (the factory above
# seeds and pushes it), so this is a plain git clone plus the same empty
# hooks directory rule. The caller's own configure(root) callback sets
# whatever else its convention wants, typically the git identity.
# ---------------------------------------------------------------------------

@_pytest.fixture()
def clone_of_hub(tmp_path: "_Path", bare_hub_origin: str):
    def build(*, git, name: str, config: dict | None = None, configure=None) -> str:
        """
        Clone the hub into a directory named 'name' and return its path.

        git(root, *args) is the caller's own helper; configure(root), when
        given, runs once the clone exists. config carries settings the
        clone must already hold when git checks the files out, the
        line-ending rule among them, because a setting applied afterwards
        arrives too late to decide how the working tree was written.
        """
        root = str(tmp_path / name)
        clone_args = ["clone"]
        for key, value in sorted((config or {}).items()):
            clone_args.extend(["-c", f"{key}={value}"])
        clone_args.extend([bare_hub_origin, root])
        git(str(tmp_path), *clone_args)
        hooks_dir = str(tmp_path / (name + "-hooks-empty"))
        _os.makedirs(hooks_dir, exist_ok=True)
        git(root, "config", "core.hooksPath", hooks_dir)
        if configure is not None:
            configure(root)
        return root

    return build
