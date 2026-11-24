"""
Every write command's JSON result says whether its event reached the remote.

Each command that runs the publish sequence prints "published" and
"sync_status" on every exit code: on exit 0 in its own result, on any other
exit in the sequence's report. "published" is true only when this run's push
to the remote default branch was accepted, or the remote default branch
already held the event. Every key a command printed before is still there
with the same value type.

Every repository here is a temporary one with a bare repository as origin;
the command runs as a separate process with this source tree first on
PYTHONPATH.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent

OWNER = "owner@desk"
WORKER = "builder@desk"
RIVAL = "rival@desk"

# The keys each command's --json result carried before the two publication
# keys, with each value's type name, as printed by the parent commit.
PARENT_KEYS = {
    "add": {"actor": "str", "event_id": "str", "item_id": "str", "prefix": "str", "root": "str", "seq": "int", "status": "str", "title": "str", "ts": "str"},
    "claim": {"actor": "str", "event_id": "str", "item_id": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "done": {"actor": "str", "briefing_chars": "int", "event_id": "str", "item_id": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "block": {"actor": "str", "event_id": "str", "gate": "str", "item_id": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "park": {"actor": "str", "event_id": "str", "item_id": "str", "reason": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "release": {"actor": "str", "event_id": "str", "item_id": "str", "reason": "str", "released_owner": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "priority": {"actor": "str", "event_id": "str", "item_id": "str", "priority": "int", "rank_arg": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "dep add": {"actor": "str", "dep_type": "str", "event_id": "str", "from_id": "str", "operation": "str", "root": "str", "seq": "int", "to_id": "str", "ts": "str", "type": "str"},
    "dep rm": {"actor": "str", "dep_type": "str", "event_id": "str", "from_id": "str", "operation": "str", "root": "str", "seq": "int", "to_id": "str", "ts": "str", "type": "str"},
    "note add": {"event_id": "str", "item_id": "str", "ref": "str", "root": "str"},
    "annul": {"actor": "str", "event_id": "str", "reason": "str", "root": "str", "seq": "int", "target_id": "str", "ts": "str", "type": "str"},
    "decide release": {"actor": "str", "appended": "bool", "decided_event_id": "str", "effect": "str", "gate": "str", "item_id": "str", "label": "str", "option": "str", "reason": "str", "root": "str", "seq": "int", "status": "str", "ts": "str", "type": "str"},
    "decide park": {"actor": "str", "appended": "bool", "decided_event_id": "str", "effect": "str", "gate": "str", "item_id": "str", "label": "str", "option": "str", "parked_event_id": "str", "reason": "str", "root": "str", "seq": "int", "status": "str", "ts": "str", "type": "str"},
    "policy claim-expiry": {"actor": "str", "event_id": "str", "hours": "float", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "policy process": {"actor": "str", "event_id": "str", "process": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "policy owner-gates": {"actor": "str", "classes": "list", "event_id": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "policy decision-actors": {"actor": "str", "actors": "list", "event_id": "str", "root": "str", "seq": "int", "ts": "str", "type": "str"},
    "policy note-refs": {"actor": "str", "event_id": "str", "prefixes": "list", "root": "str", "seq": "int", "ts": "str", "type": "str"},
}

COMMANDS = list(PARENT_KEYS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: str, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    return env


def _pinax(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, env=_env(), capture_output=True, text=True,
    )


def _ok(repo: str, *args: str) -> dict:
    done = _pinax(repo, *args, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    return _last_json(done.stdout)


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


def _configure(repo: str) -> None:
    _git(repo, "config", "user.email", "builder@example.invalid")
    _git(repo, "config", "user.name", "Builder")
    _git(repo, "config", "core.autocrlf", "false")


def _bare(path: str) -> str:
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", path],
        check=True, capture_output=True, text=True,
    )
    return path


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> dict:
    """
    A published tracker with three items, an edge, a note and a live claim,
    built once and copied for every test.
    """
    root = tmp_path_factory.mktemp("publication-template")
    repo = str(root / "repo")
    bare = _bare(str(root / "origin.git"))
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _configure(repo)
    done = _pinax(repo, "init", "--actor", OWNER)
    assert done.returncode == 0, done.stderr
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", bare)
    _git(repo, "push", "-q", "origin", "main")

    first = _ok(repo, "add", "--title", "First", "--actor", OWNER)["item_id"]
    second = _ok(repo, "add", "--title", "Second", "--actor", OWNER)["item_id"]
    third = _ok(repo, "add", "--title", "Third", "--actor", OWNER)["item_id"]
    _ok(repo, "dep", "add", third, "--blocks", second, "--actor", OWNER)
    note = _ok(repo, "note", "add", first, "--ref", "https://example.org/one", "--actor", OWNER)
    _ok(repo, "claim", third, "--actor", WORKER)
    briefing = root / "briefing.md"
    briefing.write_text("Finished.\n", encoding="utf-8")
    assert _git(repo, "status", "--porcelain", "--", ".ergon").strip() == ""
    return {
        "root": str(root),
        "first": first,
        "second": second,
        "third": third,
        "note_event": note["event_id"],
        "briefing": str(briefing),
    }


@pytest.fixture()
def work(template, tmp_path) -> dict:
    """A private copy of the template, with origin pointing at its own bare copy."""
    root = tmp_path / "work"
    shutil.copytree(template["root"], root, symlinks=True)
    repo = str(root / "repo")
    bare = str(root / "origin.git")
    _git(repo, "remote", "set-url", "origin", bare)
    return dict(template, repo=repo, bare=bare, root=str(root))


def _args(name: str, w: dict) -> list[str]:
    first, second, third = w["first"], w["second"], w["third"]
    table = {
        "add": ["add", "--title", "Another", "--actor", OWNER],
        "claim": ["claim", first, "--actor", WORKER],
        "done": ["done", first, "--briefing", w["briefing"], "--actor", OWNER],
        "block": ["block", first, "--gate", "scope", "--actor", OWNER],
        "park": ["park", first, "--reason", "later", "--actor", OWNER],
        "release": ["release", third, "--reason", "handing over", "--actor", OWNER],
        "priority": ["priority", first, "2", "--actor", OWNER],
        "dep add": ["dep", "add", first, "--blocks", second, "--actor", OWNER],
        "dep rm": ["dep", "rm", third, "--blocks", second, "--actor", OWNER],
        "note add": ["note", "add", first, "--ref", "https://example.org/two", "--actor", OWNER],
        "annul": ["annul", w["note_event"], "--reason", "wrong note", "--actor", OWNER],
        "decide release": [
            "decide", first, "--option", "A", "--label", "Go on", "--effect",
            "release", "--reason", "chosen", "--actor", OWNER,
        ],
        "decide park": [
            "decide", first, "--option", "B", "--label", "Park it", "--effect",
            "park", "--reason", "not now", "--actor", OWNER,
        ],
        "policy claim-expiry": ["policy", "claim-expiry", "--hours", "12", "--actor", OWNER],
        "policy process": ["policy", "process", "--mode", "flow", "--actor", OWNER],
        "policy owner-gates": ["policy", "owner-gates", "--class", "money", "--actor", OWNER],
        "policy decision-actors": [
            "policy", "decision-actors", "--decider", OWNER, "--actor", OWNER,
        ],
        "policy note-refs": ["policy", "note-refs", "--prefix", "wiki://", "--actor", OWNER],
    }
    return table[name]


def _run(name: str, w: dict, *extra: str) -> subprocess.CompletedProcess:
    return _pinax(w["repo"], *_args(name, w), *extra, "--json")


def _type_names(result: dict) -> dict:
    return {key: type(value).__name__ for key, value in result.items()}


def _assert_previous_keys(name: str, result: dict) -> None:
    previous = {
        key: kind for key, kind in _type_names(result).items()
        if key not in ("published", "sync_status")
    }
    assert previous == PARENT_KEYS[name]


# ---------------------------------------------------------------------------
# Every write command reports publication on exit 0
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", COMMANDS)
def test_a_pushed_write_reports_published(work, name):
    done = _run(name, work)
    assert done.returncode == 0, done.stdout + done.stderr
    result = _last_json(done.stdout)
    assert result["published"] is True
    assert result["sync_status"] == "pushed"
    _assert_previous_keys(name, result)
    assert _git(work["bare"], "rev-parse", "main") == _git(work["repo"], "rev-parse", "HEAD")


@pytest.mark.parametrize("name", [n for n in COMMANDS if n != "claim"])
def test_an_offline_write_reports_a_local_commit(work, name):
    remote = _git(work["bare"], "rev-parse", "main")
    done = _run(name, work, "--offline")
    assert done.returncode == 0, done.stdout + done.stderr
    result = _last_json(done.stdout)
    assert result["published"] is False
    assert result["sync_status"] == "committed_local"
    _assert_previous_keys(name, result)
    assert _git(work["bare"], "rev-parse", "main") == remote


@pytest.mark.parametrize("name", COMMANDS)
def test_a_write_with_an_unreachable_origin_is_not_published(work, name):
    missing = os.path.join(work["root"], "no-such-origin.git")
    _git(work["repo"], "remote", "set-url", "origin", missing)
    done = _run(name, work)
    result = _last_json(done.stdout)
    if name == "claim":
        assert done.returncode == 4, done.stdout + done.stderr
    else:
        assert done.returncode == 0, done.stdout + done.stderr
        _assert_previous_keys(name, result)
    assert result["published"] is False
    assert result["sync_status"] == "remote_unreachable"


# ---------------------------------------------------------------------------
# Failure results report publication
# ---------------------------------------------------------------------------

def test_a_rejected_push_is_not_published(work):
    hook = Path(work["bare"], "hooks", "pre-receive")
    hook.write_text("#!/bin/sh\necho rejected by the origin hook\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    done = _run("block", work)
    assert done.returncode == 5, done.stdout + done.stderr
    report = _last_json(done.stdout)
    assert report["published"] is False
    assert report["sync_status"] == "push_rejected"


def test_a_refused_commit_is_not_published(work):
    hook = Path(work["repo"], ".git", "hooks", "pre-commit")
    hook.write_text("#!/bin/sh\necho refused by the commit hook\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    done = _run("note add", work)
    assert done.returncode == 7, done.stdout + done.stderr
    report = _last_json(done.stdout)
    assert report["published"] is False
    assert report["sync_status"] == "commit_refused"


def test_a_claim_on_a_held_item_is_not_published(work):
    other = os.path.join(work["root"], "other")
    _git(work["root"], "clone", "-q", work["bare"], other)
    _configure(other)

    first = _pinax(work["repo"], "claim", work["first"], "--actor", WORKER, "--json")
    assert first.returncode == 0, first.stdout + first.stderr
    assert _last_json(first.stdout)["published"] is True

    second = _pinax(other, "claim", work["first"], "--actor", RIVAL, "--json")
    assert second.returncode == 3, second.stdout + second.stderr
    report = _last_json(second.stdout)
    assert report["published"] is False
    assert report["sync_status"] == "claim_held"


# ---------------------------------------------------------------------------
# One owner
# ---------------------------------------------------------------------------

def test_publication_is_decided_in_sync_alone():
    pattern = re.compile(r"""["'](published|sync_status)["']""")
    sync_text = (SOURCE_ROOT / "pinax" / "sync.py").read_text(encoding="utf-8")
    assert {m.group(1) for m in pattern.finditer(sync_text)} == {"published", "sync_status"}
    for path in sorted((SOURCE_ROOT / "pinax" / "commands").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not pattern.search(text), path.name
