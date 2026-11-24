"""
Which references a note may carry: a repository-relative path, an http or
https address, or a prefix the repository's note-refs policy lists.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold, read_events

SOURCE_ROOT = Path(__file__).resolve().parent.parent

OWNER = "owner@desk"
OTHER = "other@laptop"
# Split so that this file does not itself hold a name it searches for.
PRIVATE_PREFIXES = ("koine" + "://", "~/" + "knowledge/")


def _git(root: str, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    )
    return done.stdout


def _pinax(root: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=root, env=env, capture_output=True, text=True,
    )


def _configure(root: str) -> None:
    _git(root, "config", "user.email", "builder@example.invalid")
    _git(root, "config", "user.name", "Builder")
    _git(root, "config", "core.autocrlf", "false")


def _init_repo(root: str) -> None:
    _git(root, "init", "-q", "-b", "main")
    _configure(root)


def _commit_all(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


def _add(root: str) -> str:
    done = _pinax(root, "add", "--title", "Carry a note", "--actor", OWNER, "--json")
    assert done.returncode == 0, done.stderr
    return _last_json(done.stdout)["item_id"]


def _log_dir(root: str) -> str:
    return os.path.join(root, ".ergon", "log")


def _log_bytes(root: str) -> dict:
    return {p.name: p.read_bytes() for p in sorted(Path(_log_dir(root)).iterdir())}


def _state(root: str) -> dict:
    return fold(_log_dir(root))


def _seed_event(root: str, etype: str, payload: dict) -> dict:
    """Append an event directly to the log, as a foreign writer would."""
    events = read_events(_log_dir(root))
    event = mint_event(
        max(e["seq"] for e in events) + 1, "2026-11-23T10:00:00Z", OTHER, etype,
        payload, max(events, key=lambda e: e["seq"])["id"],
    )
    append_event(_log_dir(root), event)
    return event


def _note(root: str, item_id: str, ref: str, *extra: str) -> subprocess.CompletedProcess:
    return _pinax(root, "note", "add", item_id, f"--ref={ref}", "--actor", OWNER, *extra)


def _policy(root: str, *prefixes: str, extra=("--json",)) -> subprocess.CompletedProcess:
    return _pinax(
        root, "policy", "note-refs", *[f"--prefix={p}" for p in prefixes],
        "--actor", OWNER, *extra,
    )


def _message(ref: str, listed: str = "none set") -> str:
    return (
        f"pinax note add: the reference '{ref}' is not a repository-relative path, "
        "not an http or https address, and does not start with a prefix the "
        f"repository accepts ({listed}; see 'pinax policy note-refs')"
    )


def _assert_refused(root: str, item_id: str, ref: str, listed: str = "none set") -> None:
    before = _log_bytes(root)
    head = _git(root, "rev-parse", "HEAD")
    done = _note(root, item_id, ref, "--json")
    assert done.returncode == 8, f"{ref!r}: {done.stdout}{done.stderr}"
    assert _message(ref, listed) in done.stderr.split("\n")
    assert "Traceback" not in done.stderr
    report = _last_json(done.stdout)
    assert report["sync_status"] == "policy_refused"
    assert report["published"] is False
    assert _log_bytes(root) == before
    assert _git(root, "rev-parse", "HEAD") == head


@pytest.fixture()
def tracker(tmp_path):
    """A tracker with one item, notes/brief.md committed, and a file beside the root."""
    root = str(tmp_path / "repo")
    os.makedirs(os.path.join(root, "notes"))
    _init_repo(root)
    done = _pinax(root, "init", "--actor", OWNER)
    assert done.returncode == 0, done.stderr
    Path(root, "notes", "brief.md").write_text("brief\n", encoding="utf-8")
    (tmp_path / "outside.md").write_text("outside\n", encoding="utf-8")
    _commit_all(root, "base")
    return root, _add(root)


# ---------------------------------------------------------------------------
# The default rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ref", [
    "notes/brief.md",
    "./notes/brief.md",
    "notes",
    "notes/",
    "notes/missing.md",
    "docs/campaigns/run-1.md",
    "briefs/item-7.md",
    "no-such-folder/below/x.md",
    "projects/",
    "docs/a b/.hidden..md#part",
    "https://example.invalid/page",
    "https://example.org/brief",
    "HTTP://example.org:8080/brief?x=1#y",
])
def test_a_relative_path_or_a_web_address_is_accepted(tracker, ref):
    root, item_id = tracker
    done = _note(root, item_id, ref, "--caption", "C", "--json")
    assert done.returncode == 0, done.stderr
    assert _last_json(done.stdout)["ref"] == ref
    assert [n["ref"] for n in _state(root)["notes"]] == [ref]


@pytest.mark.parametrize("ref", [
    "ftp://example.org/x",
    "https://",
    "https:///path",
    "https://:80/x",
    "https://example.org:port/x",
    "https://exa mple.org/x",
    " https://example.org/x",
    "ftp://example.invalid/x",
    "..",
    "../outside.md",
    "./../outside.md",
    "notes/../notes/brief.md",
    "notes/..",
    "C:\\x\\y.md",
    "C:/x/y.md",
    "C:x.md",
    "\\\\host\\share\\x.md",
    "//host/share/x.md",
    "\\x.md",
    "docs/what?.md",
    "docs/a:b.md",
    "docs/tab\t.md",
    "docs/ends-in-dot.",
    "docs/ends-in-space /x.md",
    ".",
    "./",
    "/",
    "notes/../../outside.md",
    "../repo/notes/brief.md",
    "/etc/passwd",
    "notes\\brief.md",
    "",
    "wiki://page",
    PRIVATE_PREFIXES[0] + "page",
])
def test_any_other_reference_is_refused_and_appends_nothing(tracker, ref):
    root, item_id = tracker
    _assert_refused(root, item_id, ref)


def test_an_absolute_path_inside_the_repository_is_refused(tracker):
    root, item_id = tracker
    _assert_refused(root, item_id, os.path.join(root, "notes", "brief.md"))


@pytest.mark.parametrize("process", ["flow", "strict"])
def test_the_rule_is_the_same_under_both_processes(tracker, process):
    root, item_id = tracker
    done = _pinax(root, "policy", "process", "--mode", process, "--actor", OWNER)
    assert done.returncode == 0, done.stderr
    _assert_refused(root, item_id, "../outside.md")
    assert _note(root, item_id, "notes/missing.md").returncode == 0


def test_the_caption_limit_is_refused_before_the_reference_is_read(tracker):
    root, item_id = tracker
    before = _log_bytes(root)
    done = _note(root, item_id, "../outside.md", "--caption", "x" * 201, "--json")
    assert done.returncode == 1
    assert _last_json(done.stdout)["sync_status"] == "invalid_argument"
    assert _log_bytes(root) == before


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def test_a_listed_prefix_is_accepted_whether_or_not_a_file_exists(tracker):
    root, item_id = tracker
    done = _policy(root, "wiki://")
    assert done.returncode == 0, done.stderr
    [line] = done.stdout.strip().splitlines()
    printed = json.loads(line)
    assert printed["prefixes"] == ["wiki://"]
    assert printed["type"] == "policy.note_ref_prefixes_set"

    assert _note(root, item_id, "wiki://page").returncode == 0
    assert _state(root)["note_ref_policy"] == {
        "prefixes": ["wiki://"], "set_at": printed["ts"], "set_by": OWNER,
        "event_id": printed["event_id"],
    }
    [event] = [e for e in read_events(_log_dir(root)) if e["id"] == printed["event_id"]]
    assert event["payload"] == {"prefixes": ["wiki://"]}


def test_the_latest_list_replaces_the_earlier_one(tracker):
    root, item_id = tracker
    assert _policy(root, "wiki://").returncode == 0
    assert _policy(root, "vault://", "shelf:").returncode == 0
    _assert_refused(root, item_id, "wiki://page", "vault://, shelf:")
    assert _note(root, item_id, "vault://page.md").returncode == 0
    assert _note(root, item_id, "shelf:3").returncode == 0
    assert _note(root, item_id, "notes/brief.md").returncode == 0


def test_a_scheme_the_policy_lists_is_accepted(tracker):
    root, item_id = tracker
    _assert_refused(root, item_id, "ftp://example.invalid/x")
    assert _policy(root, "ftp://").returncode == 0
    assert _note(root, item_id, "ftp://example.invalid/x").returncode == 0


@pytest.mark.parametrize("given", [
    (), ("",), ("   ",), ("a b",), ("a\tb",), ("wiki://", "wiki://"), ("wiki://", ""),
])
def test_a_malformed_list_is_refused_before_anything_is_minted(tracker, given):
    root, _ = tracker
    before = _log_bytes(root)
    done = _policy(root, *given)
    assert done.returncode == 1, done.stdout + done.stderr
    assert _last_json(done.stdout)["sync_status"] == "invalid_argument"
    assert _log_bytes(root) == before


@pytest.mark.parametrize("payload", [
    {}, {"prefixes": []}, {"prefixes": "vault/"}, {"prefixes": ["vault/", 7]},
    {"prefixes": [""]}, {"prefixes": ["a b"]}, {"prefixes": ["vault/", "vault/"]},
    {"prefixes": [["vault/"]]},
])
def test_a_malformed_event_sets_nothing_and_is_warned_about(tracker, caplog, payload):
    root, item_id = tracker
    assert _policy(root, "wiki://").returncode == 0
    in_force = _state(root)["note_ref_policy"]
    event = _seed_event(root, "policy.note_ref_prefixes_set", payload)
    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        state = _state(root)
    assert state["note_ref_policy"] == in_force
    assert [
        r for r in caplog.records
        if "policy.note_ref_prefixes_set event without a list of prefixes" in r.getMessage()
        and event["id"] in r.getMessage()
    ]


def test_a_log_without_the_event_holds_no_policy_key(tracker):
    root, _ = tracker
    assert "note_ref_policy" not in _state(root)


def test_a_list_set_on_one_clone_binds_on_the_other_without_a_pull(
    clone_wired_to_hub, clone_of_hub
):
    first = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all, actor=OWNER,
    )
    item_id = _add(first)
    second = clone_of_hub(
        git=_git, name="second", config={"core.autocrlf": "false"}, configure=_configure,
    )
    _assert_refused(second, item_id, "wiki://page")

    done = _policy(first, "wiki://")
    assert done.returncode == 0, done.stderr
    assert _last_json(done.stdout)["published"] is True
    assert "note_ref_policy" not in _state(second)

    done = _pinax(
        second, "note", "add", item_id, "--ref", "wiki://page", "--actor", OTHER,
    )
    assert done.returncode == 0, done.stdout + done.stderr


# ---------------------------------------------------------------------------
# Notes already in the log, and the private prefixes
# ---------------------------------------------------------------------------

def test_an_existing_note_stays_valid_whatever_its_reference(tracker):
    root, item_id = tracker
    legacy = PRIVATE_PREFIXES[0] + "legacy"
    _seed_event(root, "note.added", {"item_id": item_id, "ref": legacy})
    assert _pinax(root, "verify", "--fix").returncode == 0
    done = _pinax(root, "verify")
    assert done.returncode == 0, done.stdout + done.stderr
    board = json.loads(_pinax(root, "board", "--json").stdout)
    assert [n["ref"] for n in board["state"]["notes"]] == [legacy]
    assert "note_ref_policy" not in board["state"]


def test_the_package_and_its_tests_hold_no_private_prefix():
    found = [
        f"{path.relative_to(SOURCE_ROOT)}: {prefix}"
        for folder in ("pinax", "tests")
        for path in sorted((SOURCE_ROOT / folder).rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
        for prefix in PRIVATE_PREFIXES
        if prefix.encode() in path.read_bytes()
    ]
    assert found == []


def test_the_help_text_states_the_accepted_references(tmp_path):
    done = _pinax(str(tmp_path), "note", "add", "--help")
    text = " ".join(done.stdout.split())
    assert "repository-relative path, an http or https address" in text
    assert "exist" not in text
    assert "pinax policy note-refs" in text
    for prefix in PRIVATE_PREFIXES:
        assert prefix not in text
