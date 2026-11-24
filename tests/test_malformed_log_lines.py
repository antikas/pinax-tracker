"""
No line in the event log ends a command in a traceback.

A line that is JSON and not an object, or an event with a damaged envelope,
is named by file and line: verify lists it and exits 1, and every other
command that reads the log refuses with exit 9 before it writes anything. A
payload of any shape is ignored by the fold with a warning.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pinax import remote, sync
from pinax.commands import overview
from pinax.event import DamagedLogLine, envelope_problem, mint_event, verify_id
from pinax.fold import (
    _HANDLERS,
    compute_next,
    compute_ready,
    fold,
    fold_events,
    parse_shard_bytes,
    read_events,
    state_to_json_safe,
)
from pinax.projection import render_board, render_item

SOURCE_ROOT = Path(__file__).resolve().parent.parent
ACTOR = "builder@desk"
SHARD = ".ergon/log/builder-desk.jsonl"

REMOVED = object()


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _pinax(cwd, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SOURCE_ROOT), **env_extra)
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    done = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    assert "Traceback" not in done.stdout + done.stderr, done.stderr
    return done


def _tracker(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "builder@example.invalid")
    _git(path, "config", "user.name", "Builder")
    _git(path, "config", "core.autocrlf", "false")
    hooks = path.parent / (path.name + "-hooks")
    hooks.mkdir()
    _git(path, "config", "core.hooksPath", str(hooks))
    assert _pinax(path, "init", "--actor", ACTOR).returncode == 0
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    done = _pinax(path, "add", "--title", "First", "--actor", ACTOR, "--offline")
    assert done.returncode == 0, done.stderr
    return path


def _plant(repo: Path, line: str | bytes) -> int:
    """Append a line to the shard and return its line number."""
    raw = line.encode("utf-8") if isinstance(line, str) else line
    shard = repo / SHARD
    with open(shard, "ab") as fh:
        fh.write(raw + b"\n")
    return len(shard.read_bytes().splitlines())


def _event(**changes) -> str:
    event = mint_event(
        seq=50, ts="2026-11-23T10:00:00Z", actor=ACTOR, etype="item.created",
        payload={"item_id": "pnx-zzzz", "title": "Planted"}, prev="",
    )
    for key, value in changes.items():
        if value is REMOVED:
            del event[key]
        else:
            event[key] = value
    return json.dumps(event)


NOT_OBJECTS = {
    "array": ("[1, 2, 3]", "the line is a JSON array, not an object"),
    "string": ('"hello"', "the line is a JSON string, not an object"),
    "number": ("42", "the line is a JSON number, not an object"),
    "null": ("null", "the line is JSON null, not an object"),
    "boolean": ("true", "the line is a JSON boolean, not an object"),
}

ENVELOPES = {
    "id wrong": (_event(id=7), "the field id is an integer, not a string"),
    "id empty": (_event(id=""), "the field id is an empty string"),
    "id null": (_event(id=None), "the field id is null, not a string"),
    "seq removed": (_event(seq=REMOVED), "the field seq is missing"),
    "seq wrong": (_event(seq="3"), "the field seq is a string, not an integer"),
    "seq boolean": (_event(seq=True), "the field seq is a boolean, not an integer"),
    "seq past the bound": (_event(seq=2**63), "the field seq is too large"),
    "seq of 4300 digits": (_event(seq=10**4300 - 1), "the field seq is too large"),
    "ts removed": (_event(ts=REMOVED), "the field ts is missing"),
    "ts wrong": (_event(ts=5), "the field ts is an integer, not a string"),
    "actor removed": (_event(actor=REMOVED), "the field actor is missing"),
    "actor wrong": (_event(actor=[]), "the field actor is an array, not a string"),
    "type removed": (_event(type=REMOVED), "the field type is missing"),
    "type wrong": (_event(type={}), "the field type is an object, not a string"),
    "payload removed": (_event(payload=REMOVED), "the field payload is missing"),
    "payload wrong": (_event(payload=[]), "the field payload is an array, not an object"),
    "prev removed": (_event(prev=REMOVED), "the field prev is missing"),
    "prev wrong": (_event(prev=1.5), "the field prev is a number, not a string"),
    "v wrong": (_event(v="2"), "the field v is a string, not the integer 2"),
    "v other": (_event(v=3), "the field v is 3, not the integer 2"),
}

UNREADABLE = {
    "deep": ("[" * 100000, "the line nests values more than 200 levels deep"),
    "deep payload": (
        _event(payload={"k": json.loads("[" * 300 + "]" * 300)}),
        "the line nests values more than 200 levels deep",
    ),
}
if hasattr(sys, "get_int_max_str_digits"):
    UNREADABLE["long number"] = (
        "9" * 5000, "the line holds a number with too many digits to read",
    )

ALL_LINES = {**NOT_OBJECTS, **ENVELOPES, **UNREADABLE}


def test_the_largest_seq_is_usable():
    assert envelope_problem(json.loads(_event(seq=2**63 - 1))) is None


def _damage_message(where: str) -> str:
    return (
        f"pinax: the event log is damaged: {where}. "
        "Run 'pinax verify' to list every damaged line."
    )


@pytest.fixture(scope="module")
def tracker(tmp_path_factory) -> Path:
    return _tracker(tmp_path_factory.mktemp("malformed") / "repo")


@pytest.fixture()
def restored(tracker):
    """The shared tracker, with its shard put back and HEAD unmoved afterwards."""
    shard = (tracker / SHARD).read_bytes()
    head = _git(tracker, "rev-parse", "HEAD")
    yield tracker
    (tracker / SHARD).write_bytes(shard)
    assert _git(tracker, "rev-parse", "HEAD") == head


def _tree(repo: Path) -> dict:
    ergon = repo / ".ergon"
    return {str(p.relative_to(ergon)): p.read_bytes() for p in ergon.rglob("*") if p.is_file()}


def _assert_object(stdout: str, file: str, line: int, message: str) -> None:
    lines = stdout.splitlines()
    assert len(lines) == 1, stdout
    result = json.loads(lines[0])
    assert result["published"] is False
    assert result["sync_status"] == "log_damaged"
    assert result["exit_code"] == 9
    assert result["error"] == message
    assert (result["file"], result["line"]) == (file, line)


@pytest.mark.parametrize("name", sorted(ALL_LINES))
def test_verify_names_the_line_and_every_other_command_refuses(restored, name):
    text, reason = ALL_LINES[name]
    line = _plant(restored, text)
    before, log = _tree(restored), _git(restored, "log", "--format=%H")
    where = f"{SHARD} line {line}: {reason}"
    message = _damage_message(where)

    verify = _pinax(restored, "verify")
    assert verify.returncode == 1
    assert "EVENT LOG INTEGRITY FAILURE" in verify.stderr
    assert where in verify.stderr

    for args in (("ready",), ("status",)):
        done = _pinax(restored, *args)
        assert done.returncode == 9, args
        assert done.stderr.strip() == message
        assert done.stdout == ""

    add = _pinax(restored, "add", "--title", "T", "--actor", ACTOR, "--offline", "--json")
    assert add.returncode == 9
    assert add.stderr.strip() == message
    _assert_object(add.stdout, SHARD, line, message)

    assert _tree(restored) == before
    assert _git(restored, "log", "--format=%H") == log


@pytest.mark.parametrize(
    "args",
    [
        ("ready", "--json"), ("next", "--json"), ("board", "--json"), ("report", "--json"),
        ("metrics", "--json"), ("status", "--json"), ("doctor", "--json"),
        ("registry", "list", "--json"), ("policy", "check", "--json"),
        ("dispatch", "--claim", "--actor", ACTOR, "--json"),
        ("registry", "rm", "--id", "x", "--actor", ACTOR, "--json"),
        ("status", "pnx-zzzz", "done", "--actor", ACTOR, "--json"),
        ("claim", "pnx-zzzz", "--actor", ACTOR, "--json"),
        ("block", "pnx-zzzz", "--gate", "decision", "--actor", ACTOR, "--offline", "--json"),
        ("policy", "process", "--mode", "flow", "--actor", ACTOR, "--offline", "--json"),
        ("annul", "x", "--reason", "r", "--actor", ACTOR, "--offline", "--json"),
        ("init", "--actor", ACTOR), ("board",), ("web",),
    ],
    ids=lambda args: " ".join(args[:2]),
)
def test_a_command_that_reads_the_log_refuses(restored, hub_for, args):
    hub_for(restored)
    line = _plant(restored, "[1, 2, 3]")
    before = _tree(restored)
    message = _damage_message(
        f"{SHARD} line {line}: the line is a JSON array, not an object"
    )
    done = _pinax(restored, *args)
    assert done.returncode == 9, done.stdout + done.stderr
    assert done.stderr.count(message) == 1
    if "--json" in args:
        _assert_object(done.stdout, SHARD, line, message)
    assert _tree(restored) == before


@pytest.fixture()
def hub_for(tmp_path):
    """Give a repository a published origin for one test."""
    wired = []

    def wire(repo: Path) -> Path:
        bare = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True
        )
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "origin", "main")
        wired.append(repo)
        return bare

    yield wire
    for repo in wired:
        _git(repo, "remote", "remove", "origin")


@pytest.mark.parametrize("name", sorted(ALL_LINES))
def test_replay_and_board_refuse(tmp_path, name):
    repo = tmp_path / "repo"
    _init_bare_log(repo)
    text, reason = ALL_LINES[name]
    (repo / SHARD).write_text(text + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "damaged")

    board = _pinax(repo, "board")
    assert board.returncode == 9
    assert board.stderr.strip() == _damage_message(f"{SHARD} line 1: {reason}")

    replay = _pinax(repo, "replay", "--at", "HEAD", "--json")
    assert replay.returncode == 9
    message = _damage_message(f"HEAD:{SHARD} line 1: {reason}")
    assert replay.stderr.strip() == message
    _assert_object(replay.stdout, f"HEAD:{SHARD}", 1, message)


def _init_bare_log(repo: Path) -> None:
    (repo / ".ergon" / "log").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "builder@example.invalid")
    _git(repo, "config", "user.name", "Builder")


def test_verify_lists_every_damaged_line_and_locates_the_events_it_names(restored):
    first = _plant(restored, "42")
    hashless = _plant(restored, _event(payload={"item_id": "other"}))
    last = _plant(restored, _event(seq="3"))
    done = _pinax(restored, "verify")
    assert done.returncode == 1
    assert f"{SHARD} line {first}: the line is a JSON number" in done.stderr
    assert f"{SHARD} line {last}: the field seq is a string" in done.stderr
    assert f"type=item.created at {SHARD} line {hashless}" in done.stderr


def test_verify_locates_an_object_without_an_id(restored):
    line = _plant(restored, _event(id=REMOVED))
    done = _pinax(restored, "verify")
    assert done.returncode == 1
    assert f"id=<missing> seq=50 actor={ACTOR} type=item.created at {SHARD} line {line}" in done.stderr
    assert _pinax(restored, "ready").returncode == 0


def test_an_annulment_does_not_excuse_a_damaged_line(restored):
    damaged = json.loads(_event(seq="3"))
    line = _plant(restored, json.dumps(damaged))
    annulment = mint_event(
        seq=60, ts="2026-11-23T10:00:01Z", actor="reviewer@desk", etype="event.annulled",
        payload={"target_id": damaged["id"], "reason": "junk"}, prev="",
    )
    _plant(restored, json.dumps(annulment))
    verify = _pinax(restored, "verify")
    assert verify.returncode == 1
    assert f"{SHARD} line {line}: the field seq is a string, not an integer" in verify.stderr
    assert _pinax(restored, "ready").returncode == 9


def test_verify_fix_leaves_the_projection_of_a_damaged_log_alone(restored):
    _plant(restored, "true")
    before = _tree(restored)
    assert _pinax(restored, "verify", "--fix").returncode == 1
    assert _tree(restored) == before


# ---------------------------------------------------------------------------
# The remote default branch
# ---------------------------------------------------------------------------

def _damage_remote(tmp_path: Path, bare: Path, text: str) -> int:
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    _git(other, "config", "user.email", "other@example.invalid")
    _git(other, "config", "user.name", "Other")
    line = _plant(other, text)
    _git(other, "commit", "-q", "-am", "damage")
    _git(other, "push", "-q", "origin", "main")
    return line


@pytest.mark.parametrize("name", sorted({**NOT_OBJECTS, **ENVELOPES}))
def test_a_damaged_remote_line_is_refused_before_anything_is_appended(
    restored, hub_for, tmp_path, name
):
    text, reason = ALL_LINES[name]
    line = _damage_remote(tmp_path, hub_for(restored), text)
    before, log = _tree(restored), _git(restored, "log", "--format=%H")
    done = _pinax(restored, "add", "--title", "T", "--actor", ACTOR, "--json")
    assert done.returncode == 9, done.stderr
    file = f"refs/remotes/origin/main:{SHARD}"
    message = _damage_message(f"{file} line {line}: {reason}")
    _assert_object(done.stdout, file, line, message)
    assert done.stderr.strip() == message
    assert _tree(restored) == before
    assert _git(restored, "log", "--format=%H") == log


class _DamageDuringPublish(sync.GitRunner):
    """Damages the remote just before, or just after, this run's first push."""

    def __init__(self, root: str, damage, *, before_push: bool) -> None:
        super().__init__(root)
        self.damage, self.before_push, self.done = damage, before_push, False

    def run(self, *args, env=None):
        if args[0] != "push" or self.done:
            return super().run(*args, env=env)
        self.done = True
        if self.before_push:
            self.damage()
        result = super().run(*args, env=env)
        if not self.before_push:
            self.damage()
        return result


@pytest.mark.parametrize("before_push", [True, False])
def test_damage_that_arrives_while_publishing_is_reported(tmp_path, hub_for, before_push):
    repo = _tracker(tmp_path / "repo")
    bare = hub_for(repo)
    runner = _DamageDuringPublish(
        str(repo), lambda: _damage_remote(tmp_path, bare, "42"), before_push=before_push
    )
    outcome = sync.run_sequence(
        str(repo), event_type="item.created", payload={"item_id": "pnx-new", "title": "N"},
        actor=ACTOR, ts="2026-11-23T10:00:00Z", item_id="pnx-new", runner=runner,
    )
    assert outcome.status == "log_damaged" and outcome.exit_code == 9
    assert outcome.report["appended"] is True and outcome.report["committed"] is True
    assert outcome.report["published"] is not before_push
    assert outcome.report["file"] == f"refs/remotes/origin/main:{SHARD}"
    assert b"\n42\n" not in (repo / SHARD).read_bytes()


# ---------------------------------------------------------------------------
# Portfolio views
# ---------------------------------------------------------------------------

@pytest.fixture()
def portfolio(tmp_path):
    root = tmp_path / "root"
    return root, _tracker(root / "alpha"), _tracker(root / "beta")


@pytest.mark.parametrize("damaged", ["alpha", "beta"])
def test_overview_and_portfolio_status_go_on_past_a_damaged_repository(portfolio, damaged):
    root, alpha, beta = portfolio
    healthy = "beta" if damaged == "alpha" else "alpha"
    line = _plant(root / damaged, "42")
    reason = "the line is a JSON number, not an object"
    where = f"{SHARD} line {line}: {reason}"

    plain = _pinax(alpha, "overview", "--roots", str(root))
    assert plain.returncode == 0, plain.stderr
    assert f"## {damaged}\n\n- status: damaged · {where}\n" in plain.stdout
    assert f"## {healthy}\n\n- status: ok · 1 items · queued=1\n" in plain.stdout
    assert f"- {damaged} · damaged log: {where}" in plain.stdout

    reports = json.loads(_pinax(alpha, "overview", "--roots", str(root), "--json").stdout)
    by_id = {r["id"]: r for r in reports["repos"]}
    assert by_id[damaged]["damaged"] == {"file": SHARD, "line": line, "reason": reason}
    assert by_id[healthy]["total_items"] == 1

    status = _pinax(alpha, "status", "--portfolio", PINAX_ROOTS=str(root))
    assert status.returncode == 0, status.stderr
    assert f"repo: {damaged} @ main\n  damaged: {where}" in status.stdout
    assert "ready queue: 1 item(s)" in status.stdout

    view = json.loads(
        _pinax(alpha, "status", "--portfolio", "--json", PINAX_ROOTS=str(root)).stdout
    )
    assert {r["id"]: r.get("damaged") for r in view["repos"]}[damaged]["line"] == line


def test_a_damaged_hub_still_reports_its_registered_repositories(tmp_path):
    hub, other = _tracker(tmp_path / "hub"), _tracker(tmp_path / "elsewhere" / "far")
    done = _pinax(hub, "registry", "add", "--id", "far", "--path", str(other), "--actor", ACTOR)
    assert done.returncode == 0, done.stderr
    _plant(hub, "null")
    done = _pinax(hub, "overview", "--json", "--roots", "")
    assert done.returncode == 0, done.stderr
    by_id = {r["id"]: r for r in json.loads(done.stdout)["repos"]}
    assert "damaged" in by_id["hub"] and by_id["far"]["total_items"] == 1


def test_overview_remote_reports_a_damaged_remote(tmp_path, hub_for):
    repo = _tracker(tmp_path / "repo")
    bare = hub_for(repo)
    line = _damage_remote(tmp_path, bare, '"hello"')
    report = overview._summarise_remote("far", str(bare), str(tmp_path / "scratch"))
    assert report["damaged"]["file"] == f"refs/heads/main:{SHARD}"
    assert report["damaged"]["line"] == line


def test_the_github_transport_names_the_branch_and_path():
    answers = {
        "https://api.github.com/repos/o/r": {"default_branch": "main"},
        "https://api.github.com/repos/o/r/commits/main": {"sha": "abc"},
        "https://api.github.com/repos/o/r/contents/.ergon/log?ref=abc": [
            {"name": "a.jsonl", "download_url": "https://raw.invalid/a"}
        ],
    }

    def get(url):
        return 200, json.dumps(answers[url]).encode() if url in answers else b"\n42\n"

    with pytest.raises(DamagedLogLine) as caught:
        remote.fetch_remote_github_api("https://github.com/o/r", http_get=get)
    assert (caught.value.file, caught.value.line) == ("refs/heads/main:.ergon/log/a.jsonl", 2)


def test_a_line_damaged_while_the_web_view_runs_is_shown(restored, capfd):
    from pinax.commands.web_cmd import create_server

    server = create_server(str(restored), 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _plant(restored, "42")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/")
        assert caught.value.code == 500
        assert "the line is a JSON number, not an object" in caught.value.read().decode()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert "Traceback" not in capfd.readouterr().err


# ---------------------------------------------------------------------------
# Lines that are not damage
# ---------------------------------------------------------------------------

def test_a_line_that_is_not_json_is_skipped_as_before(restored):
    shard = restored / SHARD
    intact = fold(str(shard.parent))
    lines = shard.read_bytes().splitlines(keepends=True)
    torn = b'{"id":"abc","seq":'
    shard.write_bytes(lines[0] + b"not json {\n" + b"".join(lines[1:]) + torn)
    assert fold(str(shard.parent)) == intact
    verify = _pinax(restored, "verify")
    assert verify.returncode == 0, verify.stderr


def test_an_object_without_an_id_is_dropped_outside_verify(caplog):
    with caplog.at_level(logging.WARNING):
        for text in (_event(id=REMOVED), "{}", '{"payload": [1], "seq": "x"}'):
            assert [e.get("id") for e in parse_shard_bytes(text.encode(), "s")] == [None]


def test_verify_id_and_the_reader_share_the_envelope_rule():
    for text, reason in ENVELOPES.values():
        assert envelope_problem(json.loads(text)) == reason
        assert not verify_id(json.loads(text))
    assert envelope_problem(json.loads(_event())) is None


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

PAYLOADS = {
    "ergon.created": {"repo": "r"},
    "phase.opened": {"phase": "init"},
    "item.created": {"item_id": "a", "title": "T", "prefix": "p", "status": "queued"},
    "item.status_changed": {"item_id": "a", "status": "building"},
    "item.claimed": {"item_id": "a", "actor": ACTOR},
    "item.claim_released": {"item_id": "a", "reason": "r"},
    "policy.claim_expiry_set": {"hours": 4},
    "policy.process_set": {"process": "strict"},
    "policy.owner_gate_classes_set": {"classes": ["money"]},
    "item.blocked": {"item_id": "a", "gate": "human", "owner_gate": "money"},
    "item.completed": {"item_id": "a", "briefing": "b"},
    "item.parked": {"item_id": "a", "reason": "r"},
    "item.priority_set": {"item_id": "a", "priority": 1},
    "item.decided": {
        "item_id": "a", "option": "o", "label": "l", "effect": "park", "reason": "r",
    },
    "policy.decision_actors_set": {"actors": [ACTOR]},
    "policy.note_ref_prefixes_set": {"prefixes": ["wiki://"]},
    "dep.added": {"from_id": "a", "to_id": "b", "type": "parent-child"},
    "dep.removed": {"from_id": "a", "to_id": "b", "type": "parent-child"},
    "note.added": {"item_id": "a", "ref": "docs/x", "caption": "c"},
    "registry.repo_added": {"repo_id": "r", "path": "/p", "url": "u"},
    "registry.repo_removed": {"repo_id": "r"},
    "event.annulled": {"target_id": "x", "reason": "r"},
}

WRONG = {
    "list": [1], "empty list": [], "list in list": [[1]], "object": {"k": 1},
    "number": 7, "float": 1e308, "huge number": 10 ** 400, "null": None, "boolean": True,
}


def test_every_handled_type_has_a_payload_here():
    assert set(PAYLOADS) == set(_HANDLERS)


def _base(seq: int, etype: str, payload: dict) -> dict:
    return mint_event(
        seq=seq, ts=f"2026-11-23T10:00:{seq:02d}Z", actor=ACTOR, etype=etype,
        payload=payload, prev="",
    )


def _surrounding() -> list[dict]:
    return [
        _base(0, "item.created", PAYLOADS["item.created"]),
        _base(1, "item.created", dict(PAYLOADS["item.created"], item_id="b")),
        _base(2, "item.claimed", {"item_id": "a"}),
        _base(3, "dep.added", {"from_id": "a", "to_id": "b", "type": "blocks"}),
        _base(4, "dep.added", {"from_id": "a", "to_id": "b", "type": "parent-child"}),
        _base(5, "registry.repo_added", {"repo_id": "r", "path": "/p"}),
    ]


@pytest.mark.parametrize("etype", sorted(PAYLOADS))
def test_no_payload_shape_raises_in_the_fold_or_the_views(etype):
    for key in PAYLOADS[etype]:
        for value in WRONG.values():
            planted = _base(6, etype, dict(PAYLOADS[etype], **{key: value}))
            assert verify_id(planted)
            later = _base(7, "item.claimed", {"item_id": "b"})
            state = fold_events([*_surrounding(), planted, later])
            render_board(state)
            for item_id, item in state["items"].items():
                render_item(item_id, item, state)
            compute_ready(state)
            compute_next(state)
            json.dumps(state_to_json_safe(state), sort_keys=True)


IGNORED = [
    (etype, key)
    for etype, keys in {
        "phase.opened": ("phase",),
        "item.created": ("item_id", "title", "prefix", "status"),
        "item.status_changed": ("item_id", "status"),
        "item.claimed": ("item_id",),
        "item.blocked": ("item_id", "gate"),
        "item.completed": ("item_id", "briefing"),
        "item.parked": ("item_id", "reason"),
        "item.priority_set": ("item_id", "priority"),
        "dep.added": ("from_id", "to_id", "type"),
        "dep.removed": ("from_id", "to_id", "type"),
        "note.added": ("item_id", "ref", "caption"),
        "registry.repo_added": ("repo_id", "path", "url"),
        "registry.repo_removed": ("repo_id",),
    }.items()
    for key in keys
]


@pytest.mark.parametrize("etype,key", IGNORED)
def test_a_wrong_typed_field_makes_the_event_ignored_with_a_warning(etype, key, caplog):
    without = fold_events(_surrounding())
    for value in ([1], {"k": 1}, 1.5):
        caplog.clear()
        planted = _base(6, etype, dict(PAYLOADS[etype], **{key: value}))
        with caplog.at_level(logging.WARNING):
            state = fold_events([*_surrounding(), planted])
        for empty in ("notes", "phases"):
            state.pop(empty, None) if not state.get(empty) else None
        assert state == without
        assert any(etype in record.getMessage() or key in record.getMessage()
                   for record in caplog.records)


def test_a_payload_that_is_not_an_object_is_ignored_by_fold_events():
    for etype in _HANDLERS:
        event = dict(_base(6, etype, {}), payload=[1])
        fold_events([*_surrounding(), event])


def test_a_claim_at_the_last_date_does_not_raise():
    claim = mint_event(
        seq=6, ts="9999-12-31T23:59:59Z", actor=ACTOR, etype="item.claimed",
        payload={"item_id": "b"}, prev="",
    )
    assert fold_events([*_surrounding(), claim])["items"]["b"]["owner"] == ACTOR


@pytest.mark.parametrize("key", ["gate", "item_id"])
def test_a_wrong_typed_payload_does_not_end_a_command_in_a_traceback(restored, key):
    payload = {"item_id": "pnx-zzzz", "gate": "human", key: [1], "source_line_hash": {}}
    event = mint_event(
        seq=50, ts="2026-11-23T10:00:00Z", actor=ACTOR, etype="item.blocked",
        payload=payload, prev="",
    )
    _plant(restored, json.dumps(event))
    actions = restored.parent / "actions.md"
    actions.write_text("", encoding="utf-8")
    for args in (
        ("verify",), ("ready",), ("board",), ("metrics",), ("report",), ("status",),
        ("reconcile", "--file", str(actions), "--dry-run"), ("ready", "--all-branches"),
    ):
        assert _pinax(restored, *args).returncode == 0, args


def test_read_events_never_raises_anything_else(tmp_path):
    for index, (text, _reason) in enumerate(ALL_LINES.values()):
        log = tmp_path / str(index) / ".ergon" / "log"
        log.mkdir(parents=True)
        (log / "s.jsonl").write_text("\n" + text + "\n", encoding="utf-8")
        with pytest.raises(DamagedLogLine) as caught:
            read_events(str(log))
        assert (caught.value.file, caught.value.line) == (".ergon/log/s.jsonl", 2)
