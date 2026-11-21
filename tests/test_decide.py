"""
pinax decide: one item.decided event, a park in the same commit, the latest
decision on the item page and in the status JSON, a harmless repeat, and the
strict process's decision-actor list.

Every repository here is a temporary one; the command runs as a separate
process with this source tree first on PYTHONPATH, and the pre-commit hook
'pinax init' installs runs this tree through the conftest PATH launcher.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pinax.fold as fold_mod
from pinax.append import append_event
from pinax.event import mint_event, parse_line, verify_id
from pinax.fold import fold, fold_events, read_events, state_to_json_safe
from pinax.projection import render_item

SOURCE_ROOT = Path(__file__).resolve().parent.parent
README = SOURCE_ROOT / "README.md"
DESIGN = SOURCE_ROOT / "DESIGN.md"
FIXTURES = SOURCE_ROOT / "tests" / "fixtures"
PARENT_COMMIT = "a580c82"

OWNER = "owner@desk"
DECIDER = "notifier@desk"
OTHER = "other@desk"
TS = "2026-11-19T10:00:00Z"


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


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


def _add(repo: str, title: str) -> str:
    done = _pinax(repo, "add", "--title", title, "--actor", OWNER, "--offline", "--json")
    assert done.returncode == 0, done.stderr
    return _last_json(done.stdout)["item_id"]


def _decide(repo: str, item_id: str, *, option="A", label="Keep going",
            effect="release", reason="the owner chose this", actor=DECIDER,
            extra=("--offline", "--json")) -> subprocess.CompletedProcess:
    return _pinax(
        repo, "decide", item_id, "--option", option, "--label", label,
        "--effect", effect, "--reason", reason, "--actor", actor, *extra,
    )


def _log_dir(repo: str) -> str:
    return os.path.join(repo, ".ergon", "log")


def _lines(repo: str) -> list[dict]:
    found = []
    log_dir = _log_dir(repo)
    for name in sorted(os.listdir(log_dir)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, name), "rb") as fh:
            for raw in fh.read().split(b"\n"):
                event = parse_line(raw)
                if event is not None:
                    found.append(event)
    return found


def _of_type(repo: str, etype: str) -> list[dict]:
    return [e for e in _lines(repo) if e.get("type") == etype]


def _commit_count(repo: str) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _log_bytes(repo: str) -> dict:
    out = {}
    for name in sorted(os.listdir(_log_dir(repo))):
        with open(os.path.join(_log_dir(repo), name), "rb") as fh:
            out[name] = fh.read()
    return out


@pytest.fixture()
def tracker(tmp_path):
    """A git repository with a tracker and one item blocked with gate scope."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "builder@example.invalid")
    _git(repo, "config", "user.name", "Builder")
    _git(repo, "config", "core.autocrlf", "false")
    done = _pinax(repo, "init", "--actor", OWNER)
    assert done.returncode == 0, done.stderr
    item_id = _add(repo, "Decide me")
    done = _pinax(repo, "block", item_id, "--gate", "scope", "--actor", OWNER, "--offline")
    assert done.returncode == 0, done.stderr
    return repo, item_id


def _state(repo: str) -> dict:
    return fold(_log_dir(repo))


def _item(repo: str, item_id: str) -> dict:
    return _state(repo)["items"][item_id]


def _page(repo: str, item_id: str) -> str:
    return Path(repo, ".ergon", "items", f"{item_id}.md").read_text(encoding="utf-8")


def _status_rows(repo: str) -> list[dict]:
    done = _pinax(repo, "status", "--json")
    assert done.returncode == 0, done.stderr
    payload = _last_json(done.stdout)
    rows = []
    for repo_entry in payload.get("repos") or [payload["repo"]]:
        for key in ("building", "shipped_recent", "parked"):
            rows.extend(repo_entry.get(key, []))
    return rows


# ---------------------------------------------------------------------------
# Replay and identity
# ---------------------------------------------------------------------------

def test_decisions_replay_and_verify(tracker, caplog):
    repo, item_id = tracker
    before = {e["id"] for e in _lines(repo)}
    assert _decide(repo, item_id).returncode == 0
    assert _decide(repo, item_id, option="B", label="Park it", effect="park").returncode == 0

    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        first = state_to_json_safe(fold_events(read_events(_log_dir(repo))))
        second = state_to_json_safe(fold_events(read_events(_log_dir(repo))))
    assert first == second
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Integrity violation" not in messages
    assert "Broken prev-chain" not in messages

    replayed = _pinax(repo, "replay", "--at", "HEAD", "--json")
    assert replayed.returncode == 0, replayed.stderr
    assert _last_json(replayed.stdout)["state"] == first

    verified = _pinax(repo, "verify")
    assert verified.returncode == 0, verified.stdout + verified.stderr

    new = [e for e in _lines(repo) if e["id"] not in before]
    assert [e["type"] for e in new] == ["item.decided", "item.decided", "item.parked"]
    for event in new:
        assert event["v"] == 2
        assert verify_id(event)
    new_ids = {e["id"] for e in new}
    seen = 0
    for raw in b"".join(_log_bytes(repo).values()).split(b"\n"):
        event = parse_line(raw)
        if event is not None and event["id"] in new_ids:
            assert b'"v":2' in raw
            seen += 1
    assert seen == len(new_ids)
    unknown = set(first.get("unknown_events", []))
    assert not unknown & {e["id"] for e in new}


# ---------------------------------------------------------------------------
# Park in one commit, release leaves the item alone
# ---------------------------------------------------------------------------

def test_park_decision_lands_both_events_in_one_commit(tracker):
    repo, item_id = tracker
    commits = _commit_count(repo)
    done = _decide(repo, item_id, option="B", label="Park it", effect="park",
                   reason="not this quarter")
    assert done.returncode == 0, done.stderr
    out = _last_json(done.stdout)

    assert _commit_count(repo) == commits + 1
    assert _git(repo, "log", "-1", "--format=%s").strip() == f"pinax: item.decided {item_id}"
    names = _git(repo, "log", "-1", "--name-only", "--format=").split()
    assert ".ergon/log/notifier-desk.jsonl" in names
    diff = _git(repo, "show", "HEAD", "--format=", "--", ".ergon/log/notifier-desk.jsonl")
    added = [json.loads(l[1:]) for l in diff.splitlines() if l.startswith("+{")]
    assert [e["type"] for e in added] == ["item.decided", "item.parked"]
    decided, parked = added
    assert parked["prev"] == decided["id"]
    assert parked["seq"] == decided["seq"] + 1
    assert parked["ts"] == decided["ts"]
    assert decided["payload"] == {
        "item_id": item_id, "option": "B", "label": "Park it",
        "effect": "park", "reason": "not this quarter",
    }
    assert parked["payload"] == {"item_id": item_id, "reason": "not this quarter"}
    assert out["decided_event_id"] == decided["id"]
    assert out["parked_event_id"] == parked["id"]

    item = _item(repo, item_id)
    assert item["status"] == "parked"
    assert item["park_reason"] == "not this quarter"


def test_release_decision_leaves_a_blocked_item_blocked(tracker):
    repo, item_id = tracker
    done = _decide(repo, item_id, extra=("--offline",))
    assert done.returncode == 0, done.stderr
    assert "status unchanged" in done.stdout
    assert "blocked" in done.stdout
    assert "gate scope" in done.stdout

    item = _item(repo, item_id)
    assert item["status"] == "blocked"
    assert item["gate"] == "scope"
    assert len(_of_type(repo, "item.decided")) == 1
    assert _of_type(repo, "item.parked") == []

    [row] = [r for r in _status_rows(repo) if r["id"] == item_id]
    assert row["kind"] == "blocked"
    assert row["reason"] == "scope"

    as_json = _last_json(_decide(repo, item_id, option="C").stdout)
    assert as_json["status"] == "blocked"
    assert as_json["gate"] == "scope"
    assert "parked_event_id" not in as_json


# ---------------------------------------------------------------------------
# Repeat is harmless
# ---------------------------------------------------------------------------

def test_the_same_park_decision_twice_appends_once(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id, effect="park").returncode == 0
    commits = _commit_count(repo)

    again = _decide(repo, item_id, effect="park")
    assert again.returncode == 0, again.stderr
    out = _last_json(again.stdout)
    assert out["appended"] is False
    assert out["status"] == "parked"
    assert _commit_count(repo) == commits
    assert len(_of_type(repo, "item.decided")) == 1
    assert len(_of_type(repo, "item.parked")) == 1


def test_the_same_release_decision_twice_appends_once(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id).returncode == 0
    again = _decide(repo, item_id)
    assert again.returncode == 0
    assert _last_json(again.stdout)["appended"] is False
    assert len(_of_type(repo, "item.decided")) == 1

    text = _decide(repo, item_id, extra=("--offline",))
    assert text.returncode == 0
    assert "nothing appended" in text.stdout
    assert len(_of_type(repo, "item.decided")) == 1


def test_a_different_reason_is_a_new_decision(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id).returncode == 0
    third = _decide(repo, item_id, reason="a different reason")
    assert _last_json(third.stdout)["appended"] is True
    assert len(_of_type(repo, "item.decided")) == 2


def test_the_actor_host_case_does_not_make_a_new_decision(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id, effect="park").returncode == 0
    again = _decide(repo, item_id, effect="park", actor="notifier@DESK")
    assert again.returncode == 0
    assert _last_json(again.stdout)["appended"] is False
    assert len(_of_type(repo, "item.decided")) == 1


def test_the_same_park_decision_from_another_actor_appends(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id, effect="park").returncode == 0
    again = _decide(repo, item_id, effect="park", actor=OTHER)
    assert again.returncode == 0, again.stderr
    assert _last_json(again.stdout)["appended"] is True
    decided = _of_type(repo, "item.decided")
    assert len(decided) == 2
    assert decided[-1]["actor"] == OTHER
    assert _item(repo, item_id)["decision"]["actor"] == OTHER


def test_the_same_release_decision_from_another_actor_appends(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id).returncode == 0
    again = _decide(repo, item_id, actor=OTHER)
    assert again.returncode == 0, again.stderr
    assert _last_json(again.stdout)["appended"] is True
    decided = _of_type(repo, "item.decided")
    assert len(decided) == 2
    assert decided[-1]["actor"] == OTHER
    assert _item(repo, item_id)["decision"]["actor"] == OTHER


def test_a_park_decision_on_an_item_no_longer_parked_appends_again(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id, effect="park").returncode == 0
    moved = _pinax(repo, "block", item_id, "--gate", "scope", "--actor", OWNER, "--offline")
    assert moved.returncode == 0, moved.stderr
    assert _item(repo, item_id)["status"] == "blocked"

    again = _decide(repo, item_id, effect="park")
    assert again.returncode == 0
    assert _last_json(again.stdout)["appended"] is True
    assert len(_of_type(repo, "item.decided")) == 2
    assert len(_of_type(repo, "item.parked")) == 2
    assert _item(repo, item_id)["status"] == "parked"


def test_a_retry_after_a_refused_commit_appends_and_commits(tracker):
    repo, item_id = tracker
    hook = Path(repo, ".git", "hooks", "pre-commit")
    original = hook.read_bytes() if hook.exists() else None
    hook.write_text("#!/bin/sh\necho refused by the test hook\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    commits = _commit_count(repo)

    refused = _decide(repo, item_id, effect="park")
    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert _commit_count(repo) == commits

    if original is None:
        hook.unlink()
    else:
        hook.write_bytes(original)
    retry = _decide(repo, item_id, effect="park")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert _last_json(retry.stdout)["appended"] is False
    assert _commit_count(repo) == commits + 1
    assert _git(repo, "status", "--porcelain", "--", ".ergon/log", ".ergon/items").strip() == ""
    assert len(_of_type(repo, "item.decided")) == 1
    assert len(_of_type(repo, "item.parked")) == 1


# ---------------------------------------------------------------------------
# A repeat delivers the existing record to the remote
# ---------------------------------------------------------------------------

PARK = dict(option="B", label="Park it", effect="park", reason="not this quarter")


@pytest.fixture()
def published(tracker, tmp_path):
    """The tracker with everything committed and main pushed to a bare origin."""
    repo, item_id = tracker
    bare = str(tmp_path / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", bare],
        check=True, capture_output=True, text=True,
    )
    _git(repo, "add", "-A")
    if _git(repo, "status", "--porcelain").strip():
        _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", bare)
    _git(repo, "push", "-q", "origin", "main")
    return repo, item_id, bare


def _online(repo: str, item_id: str) -> subprocess.CompletedProcess:
    return _decide(repo, item_id, extra=("--json",), **PARK)


def _reject_pushes(bare: str) -> Path:
    hook = Path(bare, "hooks", "pre-receive")
    hook.write_text("#!/bin/sh\necho rejected by the origin hook\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _bare_events(bare: str, etype: str) -> list[dict]:
    from pinax.replay import read_raw_events_at_ref

    return [e for e in read_raw_events_at_ref(bare, "main") if e.get("type") == etype]


def _bare_head(bare: str) -> str:
    return _git(bare, "rev-parse", "main").strip()


def test_a_retry_after_a_rejected_push_publishes_the_existing_decision(published):
    repo, item_id, bare = published
    hook = _reject_pushes(bare)
    rejected = _online(repo, item_id)
    assert rejected.returncode == 5, rejected.stdout + rejected.stderr
    assert _last_json(rejected.stdout)["published"] is False
    hook.unlink()

    retry = _online(repo, item_id)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    out = _last_json(retry.stdout)
    assert out["appended"] is False
    assert out["published"] is True
    assert out["sync_status"] == "pushed"
    subjects = _git(bare, "log", "--format=%s", "main").splitlines()
    assert f"pinax: item.decided {item_id}" in subjects
    decided = [e for e in _bare_events(bare, "item.decided")
               if e["payload"]["item_id"] == item_id]
    parked = [e for e in _bare_events(bare, "item.parked")
              if e["payload"]["item_id"] == item_id]
    assert len(decided) == 1 and len(parked) == 1
    assert [e["id"] for e in _of_type(repo, "item.decided")] == [decided[0]["id"]]
    assert [e["id"] for e in _of_type(repo, "item.parked")] == [parked[0]["id"]]


def test_a_retry_after_a_refused_commit_commits_and_publishes_once(published):
    repo, item_id, bare = published
    hook = Path(repo, ".git", "hooks", "pre-commit")
    original = hook.read_bytes() if hook.exists() else None
    hook.write_text("#!/bin/sh\necho refused by the commit hook\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    commits = _commit_count(repo)
    refused = _online(repo, item_id)
    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert _last_json(refused.stdout)["published"] is False
    if original is None:
        hook.unlink()
    else:
        hook.write_bytes(original)

    retry = _online(repo, item_id)
    assert retry.returncode == 0, retry.stdout + retry.stderr
    out = _last_json(retry.stdout)
    assert out["appended"] is False
    assert out["published"] is True
    assert _commit_count(repo) == commits + 1
    names = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert ".ergon/log/notifier-desk.jsonl" in names
    assert f".ergon/items/{item_id}.md" in names
    porcelain = _git(repo, "status", "--porcelain", "--",
                     ".ergon/log", ".ergon/items", ".ergon/board.md")
    assert porcelain.strip() == ""
    assert len(_of_type(repo, "item.decided")) == 1
    assert len(_of_type(repo, "item.parked")) == 1
    assert _bare_head(bare) == _git(repo, "rev-parse", "HEAD").strip()


def test_a_repeat_of_a_published_decision_does_nothing(published):
    repo, item_id, bare = published
    first = _online(repo, item_id)
    assert first.returncode == 0, first.stderr
    assert _last_json(first.stdout)["published"] is True
    commits = _commit_count(repo)
    remote = _bare_head(bare)

    again = _online(repo, item_id)
    assert again.returncode == 0, again.stdout + again.stderr
    out = _last_json(again.stdout)
    assert out["appended"] is False
    assert out["published"] is True
    assert out["sync_status"] == "already_published"
    assert _commit_count(repo) == commits
    assert _bare_head(bare) == remote


def test_an_offline_repeat_reports_a_local_commit(published):
    repo, item_id, bare = published
    first = _decide(repo, item_id, **PARK)
    assert first.returncode == 0, first.stderr
    commits = _commit_count(repo)

    again = _decide(repo, item_id, **PARK)
    assert again.returncode == 0, again.stdout + again.stderr
    out = _last_json(again.stdout)
    assert out["appended"] is False
    assert out["published"] is False
    assert out["sync_status"] == "committed_local"
    assert _commit_count(repo) == commits


def test_a_repeat_on_another_branch_pushes_nothing(published):
    repo, item_id, bare = published
    first = _decide(repo, item_id, **PARK)
    assert first.returncode == 0, first.stderr
    remote = _bare_head(bare)
    _git(repo, "checkout", "-q", "-b", "side")

    again = _online(repo, item_id)
    assert again.returncode == 0, again.stdout + again.stderr
    out = _last_json(again.stdout)
    assert out["appended"] is False
    assert out["published"] is False
    assert out["sync_status"] == "committed_local"
    assert _bare_head(bare) == remote
    assert _git(bare, "branch", "--list", "side").strip() == ""


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "overrides",
    [{"reason": "   "}, {"label": ""}, {"effect": "maybe"}, {"option": " "}],
)
def test_a_blank_value_or_unknown_effect_is_refused_with_exit_2(tracker, overrides):
    repo, item_id = tracker
    before = _log_bytes(repo)
    done = _decide(repo, item_id, **overrides)
    assert done.returncode == 2, done.stdout + done.stderr
    assert _log_bytes(repo) == before


def test_a_missing_flag_is_refused_with_exit_2(tracker):
    repo, item_id = tracker
    before = _log_bytes(repo)
    done = _pinax(repo, "decide", item_id, "--option", "A", "--effect", "park",
                  "--reason", "r", "--actor", DECIDER, "--offline")
    assert done.returncode == 2
    assert _log_bytes(repo) == before


@pytest.mark.parametrize("effect", ["release", "park"])
def test_an_unknown_item_is_refused_with_exit_1(tracker, effect):
    repo, _item_id = tracker
    before = _log_bytes(repo)
    done = _decide(repo, "pnx-zzzz", effect=effect)
    assert done.returncode == 1, done.stdout + done.stderr
    assert _log_bytes(repo) == before


# ---------------------------------------------------------------------------
# Shown on the item page and in the status JSON
# ---------------------------------------------------------------------------

def test_the_latest_decision_shows_on_the_page_and_in_status(tracker):
    repo, item_id = tracker
    assert _decide(repo, item_id, option="A", label="Keep going",
                   reason="first reason").returncode == 0
    page = _page(repo, item_id)
    assert "## Decision" in page
    assert page.index("## Briefing") < page.index("## Decision") < page.index("## Edges")
    assert "option A (Keep going), effect release, by notifier@desk" in page
    assert "first reason" in page

    assert _decide(repo, item_id, option="B", label="Park it", effect="park",
                   reason="second reason").returncode == 0
    page = _page(repo, item_id)
    assert page.count("## Decision") == 1
    assert "option B (Park it), effect park, by notifier@desk" in page
    assert "second reason" in page
    assert "first reason" not in page

    [row] = [r for r in _status_rows(repo) if r["id"] == item_id]
    decision = row["decision"]
    assert decision["option"] == "B"
    assert decision["label"] == "Park it"
    assert decision["effect"] == "park"
    assert decision["reason"] == "second reason"
    assert decision["actor"] == DECIDER
    assert decision["event_id"] == _of_type(repo, "item.decided")[-1]["id"]


def test_an_item_with_no_decision_has_no_section_and_no_key(tracker):
    repo, item_id = tracker
    other = _add(repo, "Undecided")
    done = _pinax(repo, "park", other, "--reason", "later", "--actor", OWNER, "--offline")
    assert done.returncode == 0
    assert _decide(repo, item_id).returncode == 0
    assert "## Decision" not in _page(repo, other)
    [row] = [r for r in _status_rows(repo) if r["id"] == other]
    assert "decision" not in row
    assert "decision" not in _item(repo, other)


def test_a_claimed_queued_item_shows_its_decision_on_its_building_row(published):
    repo, _blocked, _bare = published
    claimed_id = _add(repo, "Claimed while queued")
    idle_id = _add(repo, "Queued without a claim")
    claimed = _pinax(repo, "claim", claimed_id, "--actor", OWNER, "--json")
    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    assert _decide(repo, claimed_id).returncode == 0
    assert _decide(repo, idle_id).returncode == 0
    assert _item(repo, claimed_id)["status"] == "queued"
    assert _item(repo, claimed_id)["owner"] == OWNER

    done = _pinax(repo, "status", "--json")
    assert done.returncode == 0, done.stderr
    payload = _last_json(done.stdout)
    [entry] = payload.get("repos") or [payload["repo"]]
    [row] = [r for r in entry["building"] if r["id"] == claimed_id]
    assert row["stage"] == "queued"
    assert row["owner"] == OWNER
    assert row["decision"]["option"] == "A"

    assert not [r for r in _status_rows(repo) if r["id"] == idle_id]
    assert _item(repo, idle_id)["decision"]["option"] == "A"
    assert "## Decision" in _page(repo, idle_id)


def test_pages_without_decisions_match_the_parent_commit_rendering(tmp_path):
    try:
        archive = subprocess.run(
            ["git", "archive", PARENT_COMMIT, "pinax"],
            cwd=SOURCE_ROOT, capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"commit {PARENT_COMMIT} is not reachable")
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    subprocess.run(["tar", "-x", "-C", str(earlier)], input=archive, check=True)

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    (log_dir / "golden.jsonl").write_bytes((FIXTURES / "golden_log.jsonl").read_bytes())
    script = (
        "import json, sys\n"
        "from pinax.fold import fold\n"
        "from pinax.projection import render_item\n"
        "state = fold(sys.argv[1])\n"
        "print(json.dumps({i: render_item(i, it, state) "
        "for i, it in state.get('items', {}).items()}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(earlier))
    done = subprocess.run(
        [sys.executable, "-c", script, str(log_dir)],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, check=True,
    )
    expected = json.loads(done.stdout)
    state = fold(str(log_dir))
    current = {i: render_item(i, it, state) for i, it in state.get("items", {}).items()}
    assert expected and current == expected


# ---------------------------------------------------------------------------
# Fold rules, in process
# ---------------------------------------------------------------------------

ITEM = "pnx-a1b2"


def _seeded(tmp_path) -> str:
    log_dir = str(tmp_path / ".ergon" / "log")
    os.makedirs(log_dir)
    _put(log_dir, 0, "item.created", {"item_id": ITEM, "title": "T", "prefix": "pnx"}, OWNER)
    _put(log_dir, 1, "item.blocked", {"item_id": ITEM, "gate": "scope"}, OWNER)
    return log_dir


_PREV: dict = {}


def _put(log_dir: str, seq: int, etype: str, payload: dict, actor: str = DECIDER) -> dict:
    key = (log_dir, actor)
    event = mint_event(seq=seq, ts=f"2026-11-19T10:00:{seq:02d}Z", actor=actor,
                       etype=etype, payload=payload, prev=_PREV.get(key, ""))
    _PREV[key] = event["id"]
    append_event(log_dir, event, actor=actor)
    return event


def _decision(option: str, effect: str = "release", reason: str = "why") -> dict:
    return {"item_id": ITEM, "option": option, "label": f"label {option}",
            "effect": effect, "reason": reason}


def test_annulling_the_latest_decision_shows_the_previous_one_or_none(tmp_path):
    log_dir = _seeded(tmp_path)
    first = _put(log_dir, 2, "item.decided", _decision("A"))
    second = _put(log_dir, 3, "item.decided", _decision("B"))
    assert fold(log_dir)["items"][ITEM]["decision"]["event_id"] == second["id"]

    _put(log_dir, 4, "event.annulled", {"target_id": second["id"], "reason": "wrong"})
    shown = fold(log_dir)["items"][ITEM]["decision"]
    assert shown["event_id"] == first["id"]
    assert shown["option"] == "A"

    _put(log_dir, 5, "event.annulled", {"target_id": first["id"], "reason": "wrong too"})
    assert "decision" not in fold(log_dir)["items"][ITEM]


@pytest.mark.parametrize(
    "payload",
    [
        {"item_id": ITEM, "label": "L", "effect": "park", "reason": "why"},
        {"item_id": ITEM, "option": "A", "label": "L", "effect": "park", "reason": "  "},
        {"item_id": ITEM, "option": "A", "label": "L", "effect": "maybe", "reason": "why"},
    ],
)
def test_a_malformed_decision_records_nothing(tmp_path, payload):
    log_dir = _seeded(tmp_path)
    event = _put(log_dir, 2, "item.decided", payload)
    assert verify_id(event)
    item = fold(log_dir)["items"][ITEM]
    assert "decision" not in item
    assert item["status"] == "blocked"
    assert item["gate"] == "scope"


def test_a_decision_releases_no_claim_and_one_past_the_deadline_expires_it(tmp_path):
    log_dir = str(tmp_path / ".ergon" / "log")
    os.makedirs(log_dir)
    prev: dict = {}

    def put(seq: int, ts: str, etype: str, payload: dict, actor: str) -> dict:
        event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype,
                           payload=payload, prev=prev.get(actor, ""))
        prev[actor] = event["id"]
        append_event(log_dir, event, actor=actor)
        return event

    put(0, "2026-11-19T10:00:00Z", "item.created",
        {"item_id": ITEM, "title": "T", "prefix": "pnx"}, OWNER)
    claim = put(1, "2026-11-19T10:00:01Z", "item.claimed", {"item_id": ITEM}, OWNER)
    before = fold(log_dir)
    status = before["items"][ITEM]["status"]

    put(2, "2026-11-19T11:00:00Z", "item.decided", _decision("A"), DECIDER)
    held = fold(log_dir)
    assert held["items"][ITEM]["owner"] == OWNER
    assert held["items"][ITEM]["claim_event_id"] == claim["id"]
    assert held["items"][ITEM]["status"] == status
    assert not held.get("claim_expired") and not held.get("claim_released")

    # The default expiry is twenty-four hours; this decision is at the deadline.
    late = put(3, "2026-11-20T10:00:01Z", "item.decided", _decision("B"), DECIDER)
    ended = fold(log_dir)
    item = ended["items"][ITEM]
    assert "owner" not in item
    assert item["status"] == status
    assert item["decision"]["event_id"] == late["id"]
    [expired] = ended["claim_expired"]
    assert expired["claim_event_id"] == claim["id"]
    assert expired["expiring_event_id"] == late["id"]
    assert not ended.get("claim_released")


def test_a_decision_is_never_a_note(tracker):
    repo, item_id = tracker
    done = _pinax(repo, "note", "add", item_id, "--ref", "docs/brief.md",
                  "--actor", OWNER, "--offline")
    assert done.returncode == 0, done.stderr
    before = _state(repo).get("notes", [])
    assert before
    assert _decide(repo, item_id, effect="park").returncode == 0
    assert _state(repo).get("notes", []) == before
    assert _of_type(repo, "note.added") and len(_of_type(repo, "note.added")) == 1


def test_an_earlier_fold_without_the_handler_still_parks(tmp_path, monkeypatch, caplog):
    log_dir = _seeded(tmp_path)
    decided = _put(log_dir, 2, "item.decided", _decision("B", "park", "not now"))
    _put(log_dir, 3, "item.parked", {"item_id": ITEM, "reason": "not now"})
    current_state = fold(log_dir)
    current_page = render_item(ITEM, current_state["items"][ITEM], current_state)

    handlers = dict(fold_mod._HANDLERS)
    handlers.pop("item.decided")
    handlers.pop("policy.decision_actors_set")
    monkeypatch.setattr(fold_mod, "_HANDLERS", handlers)
    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        earlier = fold(log_dir)
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "Integrity violation" not in messages
    assert "Broken prev-chain" not in messages
    assert decided["id"] in earlier["unknown_events"]
    item = earlier["items"][ITEM]
    assert item["status"] == "parked"
    assert item["park_reason"] == "not now"

    earlier_page = render_item(ITEM, item, earlier)
    start = current_page.index("## Decision")
    end = current_page.index("## Edges")
    assert current_page[:start] + current_page[end:] == earlier_page


# ---------------------------------------------------------------------------
# Strict: only listed deciders decide
# ---------------------------------------------------------------------------

def _strict(repo: str) -> None:
    done = _pinax(repo, "policy", "process", "--mode", "strict", "--actor", OWNER, "--offline")
    assert done.returncode == 0, done.stderr


def test_strict_with_no_list_refuses_every_decision(tracker):
    repo, item_id = tracker
    _strict(repo)
    before = _log_bytes(repo)
    done = _decide(repo, item_id, effect="park")
    assert done.returncode == 8, done.stdout + done.stderr
    assert "pinax policy decision-actors" in done.stdout + done.stderr
    assert _log_bytes(repo) == before


def test_strict_accepts_a_listed_decider_only(tracker):
    repo, item_id = tracker
    _strict(repo)
    listed = _pinax(repo, "policy", "decision-actors", "--decider", DECIDER,
                    "--actor", OWNER, "--offline", "--json")
    assert listed.returncode == 0, listed.stderr
    [event] = _of_type(repo, "policy.decision_actors_set")
    assert event["payload"] == {"actors": [DECIDER]}
    assert event["actor"] == OWNER

    assert _decide(repo, item_id).returncode == 0
    before = _log_bytes(repo)
    refused = _decide(repo, item_id, option="B", actor=OTHER)
    assert refused.returncode == 8
    assert DECIDER in refused.stdout + refused.stderr
    assert _log_bytes(repo) == before


def test_flow_lets_any_actor_decide(tracker):
    repo, item_id = tracker
    listed = _pinax(repo, "policy", "decision-actors", "--decider", DECIDER,
                    "--actor", OWNER, "--offline")
    assert listed.returncode == 0
    assert _decide(repo, item_id, actor=OTHER).returncode == 0


def test_an_invalid_decider_is_refused_with_exit_2(tracker):
    repo, _item_id = tracker
    before = _log_bytes(repo)
    done = _pinax(repo, "policy", "decision-actors", "--decider", "Notifier Bot",
                  "--actor", OWNER, "--offline")
    assert done.returncode == 2
    assert "Notifier Bot" in done.stderr
    assert OWNER not in done.stderr
    assert _log_bytes(repo) == before


def test_a_decider_listed_with_an_upper_case_host_matches(tracker):
    repo, item_id = tracker
    _strict(repo)
    listed = _pinax(repo, "policy", "decision-actors", "--decider", "notifier@Desk",
                    "--actor", OWNER, "--offline")
    assert listed.returncode == 0
    [event] = _of_type(repo, "policy.decision_actors_set")
    assert event["payload"] == {"actors": [DECIDER]}
    assert _decide(repo, item_id).returncode == 0


# ---------------------------------------------------------------------------
# The caller's exact form
# ---------------------------------------------------------------------------

def test_the_callers_argument_list_parks_and_prints_one_json_line(tracker):
    repo, item_id = tracker
    done = subprocess.run(
        [sys.executable, "-m", "pinax", "--root", repo, "decide", item_id,
         "--option", "B", "--label", "Park it", "--effect", "park",
         "--reason", "Owner answer: park until the review lands", "--actor", DECIDER,
         "--json"],
        cwd=repo, env=_env(), capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    [line] = done.stdout.strip().splitlines()
    out = json.loads(line)
    assert out["item_id"] == item_id
    assert out["effect"] == "park"
    assert _item(repo, item_id)["status"] == "parked"


def test_a_reason_that_looks_like_a_flag_is_stored_as_text(tracker):
    repo, item_id = tracker
    done = subprocess.run(
        [sys.executable, "-m", "pinax", "--root", repo, "decide", item_id,
         "--option", "B", "--label", "Park it", "--effect", "park",
         "--reason", "--offline", "--actor", DECIDER, "--json"],
        cwd=repo, env=_env(), capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    [decided] = _of_type(repo, "item.decided")
    assert decided["payload"]["reason"] == "--offline"
    assert _item(repo, item_id)["park_reason"] == "--offline"


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_readme_states_the_upgrade_order_beside_the_version_2_paragraph():
    text = _flat(README)
    phrase = "must run this version before the first decision is written"
    assert phrase in text
    v2 = text.index("before the first version 2 event is written")
    assert 0 < text.index(phrase, v2) - v2 < 400


def test_readme_lists_the_two_commands():
    text = README.read_text(encoding="utf-8")
    assert (
        "pinax decide ITEM_ID --option KEY --label TEXT --effect release|park --reason TEXT"
        in text
    )
    assert "pinax policy decision-actors --decider ROLE@HOST [--decider ROLE@HOST ...]" in text


EARLIER_RELEASES = (
    "Every released Pinax before 0.4.0 predates version 2 events and reports "
    "each version 2 event that no tombstone names as an integrity violation. "
    "Decision events are version 2 events and are reported the same way."
)
OLD_EARLIER_RELEASES = (
    "already reports every version 2 event",
    "the decision events add nothing to that",
)


def test_design_has_a_decisions_section():
    text = DESIGN.read_text(encoding="utf-8")
    assert "\n## Decisions\n" in text
    section = " ".join(text.split("\n## Decisions\n", 1)[1].split("\n## ", 1)[0].split())
    assert EARLIER_RELEASES in section
    for phrase in OLD_EARLIER_RELEASES:
        assert phrase not in section, phrase


def test_no_document_describes_an_unreleased_client():
    # No public release wrote version 2 events without decisions.
    for path in (README, DESIGN):
        flat = " ".join(path.read_text(encoding="utf-8").split())
        assert "writes version 2 events but" not in flat, path.name
    readme = _section(README, "Decisions")
    assert EARLIER_RELEASES in readme
    for phrase in OLD_EARLIER_RELEASES:
        assert phrase not in readme, phrase


README_RELEASE = (
    "The item's status, gate and owner-gate class stay as they were, and the "
    "command says so"
)
README_CLAIM = (
    "A decision does not release a claim; like any event, one recorded past a "
    "claim's expiry deadline marks that claim expired."
)
DESIGN_RELEASE = (
    "With `--effect release` only the decision is appended, and the item's "
    "status, gate and owner-gate class are unchanged; the claim follows the "
    "expiry rule above, as for any event."
)
README_NO_ROW = (
    "`pinax status --json` prints no row for a queued or ready item without a "
    "live claim, so such an item's decision shows on its item page and in the "
    "fold state only."
)
NO_DECISION = (
    "Decisions change nothing in how an item with no decision renders or "
    "serialises."
)


def test_documents_say_a_release_decision_leaves_the_claim_to_the_expiry_rule():
    readme = _section(README, "Decisions")
    design = _section(DESIGN, "Decisions")
    assert README_RELEASE in readme
    assert README_CLAIM in readme
    assert DESIGN_RELEASE in design
    for section in (readme, design):
        assert "owner-gate class and claim" not in section


def test_readme_says_which_items_status_json_prints_no_row_for():
    readme = _section(README, "Decisions")
    assert README_NO_ROW in readme
    assert "prints no row for a queued item," not in readme


def test_documents_say_decisions_change_nothing_for_an_undecided_item():
    readme = _section(README, "Decisions")
    design = _section(DESIGN, "Decisions")
    for section in (readme, design):
        assert NO_DECISION in section
    assert "looks exactly as it did before" not in readme
    assert "renders and serialises exactly as before" not in design


def _section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    assert f"\n## {heading}\n" in text
    return " ".join(text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0].split())


def test_design_states_that_a_retry_delivers_the_decision():
    section = _section(DESIGN, "Decisions")
    assert "A matching decision that is only in the working tree" not in section
    assert (
        "a retry after a rejected push publishes the existing decision "
        "without appending" in section
    )
    assert "A repeat never adds a second `item.decided` or `item.parked`." in section


def test_readme_states_that_a_retry_delivers_the_decision():
    section = _section(README, "Decisions")
    assert "does not count, so running the command again commits it" not in section
    assert (
        "a retry after a rejected push publishes the existing decision "
        "without appending a second copy" in section
    )


@pytest.mark.parametrize("path", [README, DESIGN], ids=["readme", "design"])
def test_publishing_sections_name_published_and_sync_status(path):
    section = _section(path, "Publishing a state change")
    assert "JSON result carries `published` and `sync_status`" in section
    assert (
        "`published` is true only when this run's push to the remote default "
        "branch was accepted" in section
    )
    assert "reads `published`" in section
