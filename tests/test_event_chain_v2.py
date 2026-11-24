"""
Version 2 events: each event's hash covers the event before it.

Version 1 events (no "v" field) keep verifying under the old rule, version 2
events (written from now on) hash prev and v, and a log holding both folds,
verifies and replays end to end. Tampering with either kind is caught, and a
deleted or repointed link in a version 2 chain fails pinax verify.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pinax.append import _shard_name_for_actor
from pinax.event import (
    _digest_id,
    event_id,
    event_id_v2,
    mint_event,
    parse_line,
    serialise,
    valid_annulment,
    verify_id,
)
from pinax.fold import fold_events, read_events, state_to_json_safe


ROOT = Path(__file__).resolve().parent.parent
GOLDEN_LOG = ROOT / "tests" / "fixtures" / "golden_log.jsonl"

ACTOR = "builder@alpha"


# ---------------------------------------------------------------------------
# The hashing rule
# ---------------------------------------------------------------------------

def test_every_golden_line_verifies_and_reserialises_byte_for_byte():
    lines = [line for line in GOLDEN_LOG.read_bytes().split(b"\n") if line]
    assert lines
    for line in lines:
        event = parse_line(line)
        assert "v" not in event
        assert verify_id(event), line
        assert serialise(event).encode("utf-8") == line


def test_prev_is_hashed():
    common = dict(seq=3, ts="2026-11-19T10:00:00Z", actor=ACTOR, etype="item.noted", payload={"k": 1})
    first = mint_event(prev="", **common)
    second = mint_event(prev="a" * 52, **common)
    assert first["v"] == 2 and second["v"] == 2
    assert first["id"] != second["id"]
    assert verify_id(first) and verify_id(second)


def test_serialise_writes_v_after_prev():
    event = mint_event(seq=0, ts="2026-11-19T10:00:00Z", actor=ACTOR, etype="x", payload={})
    assert serialise(event).endswith(',"prev":"","v":2}')


def _body(v) -> dict:
    return {
        "seq": 4,
        "ts": "2026-11-19T10:00:00Z",
        "actor": ACTOR,
        "type": "item.noted",
        "payload": {"item_id": "pnx-aaaa"},
        "prev": "b" * 52,
        "v": v,
    }


def _candidate_ids(body: dict) -> dict[str, str]:
    """Every id a check that misreads the version could accept for this body."""
    fields = (body["seq"], body["ts"], body["actor"], body["type"], body["payload"])
    return {
        "version one": event_id(*fields),
        "version two": event_id_v2(*fields, body["prev"]),
        "literal v": _digest_id(body),
    }


def test_the_candidate_ids_are_well_formed():
    body = _body(2)
    ids = _candidate_ids(body)
    assert ids["version two"] == ids["literal v"]
    assert verify_id(dict(body, id=ids["version two"]))
    v1_body = {k: val for k, val in body.items() if k != "v"}
    assert verify_id(dict(v1_body, id=ids["version one"]))


@pytest.mark.parametrize("bad", [1, "2", True, 2.0, 3])
@pytest.mark.parametrize("which", ["version one", "version two", "literal v"])
def test_a_version_other_than_the_integer_two_fails(bad, which):
    body = _body(bad)
    event = dict(body, id=_candidate_ids(body)[which])
    assert not verify_id(event)


def test_version_two_with_a_version_one_id_fails():
    body = _body(2)
    event = dict(
        body,
        id=event_id(body["seq"], body["ts"], body["actor"], body["type"], body["payload"]),
    )
    assert not verify_id(event)
    assert verify_id(dict(event, id=_digest_id(body)))


def test_only_the_event_module_hashes_events():
    allowed = {
        Path("pinax/event.py"),
        Path("pinax/ids.py"),
        Path("pinax/commands/reconcile.py"),
    }
    found = set()
    for path in (ROOT / "pinax").rglob("*.py"):
        if "hashlib.blake2b(" in path.read_text(encoding="utf-8"):
            found.add(path.relative_to(ROOT))
    assert found <= allowed, sorted(str(p) for p in found - allowed)


# ---------------------------------------------------------------------------
# End to end, in temporary repositories
# ---------------------------------------------------------------------------

def _env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    env.pop("PINAX_ROOT", None)
    return env


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=_env())
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {result.stdout}{result.stderr}")
    return result


def _pinax(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "pinax", *args], cwd=root, capture_output=True, text=True, env=_env()
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} exited {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
    return result


def _init_repo(root: str) -> str:
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "builder@pinax.example")
    _git(root, "config", "user.name", "Pinax Builder")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _commit_all(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


def _log_dir(root: str) -> str:
    return os.path.join(root, ".ergon", "log")


def _shard(root: str) -> Path:
    return Path(_log_dir(root)) / _shard_name_for_actor(ACTOR)


def _lines(root: str) -> list[bytes]:
    return [line for line in _shard(root).read_bytes().split(b"\n") if line]


def _write_lines(root: str, lines: list[bytes]) -> None:
    _shard(root).write_bytes(b"".join(line + b"\n" for line in lines))


def _events(root: str) -> list[dict]:
    return [parse_line(line) for line in _lines(root)]


def _as_version_one(events: list[dict]) -> list[dict]:
    """Re-mint a chain under the version 1 rule, the way an earlier Pinax wrote it."""
    out = []
    prev = ""
    for event in events:
        old = {
            "id": event_id(event["seq"], event["ts"], event["actor"], event["type"], event["payload"]),
            "seq": event["seq"],
            "ts": event["ts"],
            "actor": event["actor"],
            "type": event["type"],
            "payload": event["payload"],
            "prev": prev,
        }
        prev = old["id"]
        out.append(old)
    return out


def _add(root: str, title: str) -> str:
    added = _pinax(root, "add", "--title", title, "--prefix", "pnx", "--actor", ACTOR, "--offline", "--json")
    return json.loads(added.stdout.strip().splitlines()[-1])["item_id"]


def _version_one_repo(root: str) -> list[str]:
    """
    A committed repository whose log holds only version 1 events, with a
    real pre-commit hook installed by pinax init. Returns the item ids.
    """
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    items = [_add(root, "Written before the upgrade"), _add(root, "Also written before")]
    _write_lines(root, [serialise(e).encode("utf-8") for e in _as_version_one(_events(root))])
    assert all("v" not in e for e in _events(root))
    _pinax(root, "verify", "--fix")
    _commit_all(root, "log written by an earlier pinax")
    return items


def _append_checked(root: str, *args: str) -> dict:
    """Run a writer and return the one version 2 line it appended."""
    before = _lines(root)
    _pinax(root, *args)
    after = _lines(root)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1, args
    assert b'"v":2' in after[-1]
    event = parse_line(after[-1])
    assert verify_id(event)
    return event


def _mixed_repo(root: str) -> dict:
    v1_items = _version_one_repo(root)
    last_v1 = _events(root)[-1]

    added = _append_checked(
        root, "add", "--title", "Written after the upgrade", "--prefix", "pnx",
        "--actor", ACTOR, "--offline", "--json",
    )
    assert added["prev"] == last_v1["id"]
    new_item = added["payload"]["item_id"]
    noted = _append_checked(
        root, "note", "add", new_item, "--ref", "https://example.org/plan", "--actor", ACTOR, "--offline",
    )
    blocked = _append_checked(
        root, "block", new_item, "--gate", "scope", "--actor", ACTOR, "--offline",
    )
    briefing = os.path.join(root, "..", "briefing.md")
    Path(briefing).write_text("Shipped.\n", encoding="utf-8")
    done = _append_checked(
        root, "done", v1_items[0], "--briefing", briefing, "--actor", ACTOR, "--offline",
    )
    assert _git(root, "status", "--porcelain").stdout.strip() == ""
    return {
        "v1_items": v1_items,
        "new_item": new_item,
        "added": added,
        "noted": noted,
        "blocked": blocked,
        "done": done,
    }


def _fold_warnings(root: str, caplog) -> tuple[dict, list[str]]:
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        state = fold_events(read_events(_log_dir(root)))
    return state, [r.getMessage() for r in caplog.records if r.name == "pinax.fold"]


def _chain_warnings(messages: list[str]) -> list[str]:
    return [m for m in messages if "Integrity violation" in m or "Broken prev-chain" in m]


@pytest.mark.deep
def test_mixed_log_verifies_folds_and_replays(tmp_path, caplog):
    root = str(tmp_path / "repo")
    _mixed_repo(root)
    events = _events(root)
    assert any("v" not in e for e in events) and any(e.get("v") == 2 for e in events)

    assert _pinax(root, "verify", check=False).returncode == 0

    first, messages = _fold_warnings(root, caplog)
    assert _chain_warnings(messages) == []
    second, _ = _fold_warnings(root, caplog)
    replayed = json.loads(_pinax(root, "replay", "--at", "HEAD", "--json").stdout)["state"]
    canonical = json.dumps(state_to_json_safe(first), sort_keys=True)
    assert json.dumps(state_to_json_safe(second), sort_keys=True) == canonical
    assert json.dumps(replayed, sort_keys=True) == canonical


def _tamper_payload(root: str, target_id: str) -> None:
    lines = []
    for line in _lines(root):
        event = parse_line(line)
        if event["id"] == target_id:
            event["payload"] = dict(event["payload"], tampered=True)
            line = serialise(event).encode("utf-8")
        lines.append(line)
    _write_lines(root, lines)


@pytest.mark.deep
@pytest.mark.parametrize("which", ["v1", "v2"])
def test_tampered_payload_is_caught(tmp_path, caplog, which):
    root = str(tmp_path / "repo")
    made = _mixed_repo(root)
    target = _events(root)[2]["id"] if which == "v1" else made["noted"]["id"]
    assert ("v" in next(e for e in _events(root) if e["id"] == target)) == (which == "v2")

    _tamper_payload(root, target)

    result = _pinax(root, "verify", check=False)
    assert result.returncode != 0
    assert f"id={target}" in result.stderr
    _, messages = _fold_warnings(root, caplog)
    assert any("Integrity violation" in m and target in m for m in messages)


def _drop(root: str, event_id_: str, repoint: dict | None = None) -> None:
    lines = []
    for line in _lines(root):
        event = parse_line(line)
        if event["id"] == event_id_:
            continue
        if repoint and event["id"] == repoint["id"]:
            event["prev"] = repoint["prev"]
            line = serialise(event).encode("utf-8")
        lines.append(line)
    _write_lines(root, lines)


@pytest.mark.deep
def test_repointed_version_two_chain_fails_verify(tmp_path):
    root = str(tmp_path / "repo")
    made = _mixed_repo(root)
    a, b, c = made["added"], made["noted"], made["blocked"]
    assert b["prev"] == a["id"] and c["prev"] == b["id"]

    _drop(root, b["id"], repoint={"id": c["id"], "prev": a["id"]})

    result = _pinax(root, "verify", check=False)
    assert result.returncode != 0
    assert f"id={c['id']}" in result.stderr


@pytest.mark.deep
def test_deleted_version_two_link_fails_verify(tmp_path, caplog):
    root = str(tmp_path / "repo")
    made = _mixed_repo(root)
    b, c = made["noted"], made["blocked"]

    _drop(root, b["id"])

    result = _pinax(root, "verify", check=False)
    assert result.returncode != 0
    assert "INTEGRITY FAILURE" in result.stderr
    assert f"id={c['id']}" in result.stderr
    _, messages = _fold_warnings(root, caplog)
    assert any("Broken prev-chain" in m and c["id"] in m for m in messages)


@pytest.mark.deep
def test_a_tombstone_covers_a_deleted_version_two_link(tmp_path, caplog):
    root = str(tmp_path / "repo")
    made = _mixed_repo(root)
    a, b, c = made["added"], made["noted"], made["blocked"]
    assert b["prev"] == a["id"] and c["prev"] == b["id"]

    tombstone = _append_checked(
        root, "annul", c["id"], "--reason", "recorded in error", "--actor", ACTOR, "--offline",
    )
    assert tombstone["type"] == "event.annulled" and valid_annulment(tombstone)
    _drop(root, b["id"])

    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert not any("Broken prev-chain" in m and c["id"] in m for m in messages)

    _drop(root, tombstone["id"])

    result = _pinax(root, "verify", check=False)
    assert result.returncode != 0
    assert f"id={c['id']}" in result.stderr


@pytest.mark.deep
def test_deleted_version_one_link_still_verifies_with_a_warning(tmp_path, caplog):
    root = str(tmp_path / "repo")
    _version_one_repo(root)
    events = _events(root)
    middle, successor = events[-2], events[-1]
    assert successor["prev"] == middle["id"]

    _drop(root, middle["id"])

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert any("Broken prev-chain" in m and successor["id"] in m for m in messages)
    assert not any("Integrity violation" in m for m in messages)


def _assert_verify_names(root: str, caplog, event: dict) -> None:
    """pinax verify fails on an integrity failure naming event; the fold warns for it."""
    _pinax(root, "verify", "--fix", check=False)
    result = _pinax(root, "verify", check=False)
    assert result.returncode != 0
    assert "INTEGRITY FAILURE" in result.stderr
    assert f"id={event['id']}" in result.stderr
    _, messages = _fold_warnings(root, caplog)
    assert any("Broken prev-chain" in m and event["id"] in m for m in messages)


@pytest.mark.deep
def test_oldest_event_moved_after_its_successor_with_a_recomputed_id_fails_verify(tmp_path, caplog):
    """The successor becomes the first event of its chain, and its prev names no event in the log."""
    root = str(tmp_path / "repo")
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    _add(root, "Oldest")
    _add(root, "Next")
    events = _events(root)
    assert all(e.get("v") == 2 for e in events)
    oldest, successor = events[0], events[1]
    assert oldest["prev"] == "" and successor["prev"] == oldest["id"]

    moved = dict(oldest, seq=max(e["seq"] for e in events) + 10)
    moved["id"] = event_id_v2(
        moved["seq"], moved["ts"], moved["actor"], moved["type"], moved["payload"], moved["prev"]
    )
    _write_lines(
        root,
        [serialise(moved if e["id"] == oldest["id"] else e).encode("utf-8") for e in events],
    )

    _assert_verify_names(root, caplog, successor)


@pytest.mark.deep
def test_removing_a_chains_oldest_events_fails_verify(tmp_path, caplog):
    root = str(tmp_path / "repo")
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    for title in ("First", "Second", "Third"):
        _add(root, title)
    lines = _lines(root)
    events = _events(root)
    assert len(events) >= 3 and events[0]["prev"] == ""

    _write_lines(root, lines[2:])

    _assert_verify_names(root, caplog, events[2])


# ---------------------------------------------------------------------------
# A chain's later events moved into another shard file
# ---------------------------------------------------------------------------

OTHER = "reviewer@beta"


def _v2_repo_with_two_actors(root: str) -> list[bytes]:
    """A version 2 log: ACTOR's chain of at least four events and OTHER's shard."""
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    for title in ("One", "Two", "Three", "Four"):
        _add(root, title)
    _pinax(
        root, "add", "--title", "Other actor", "--prefix", "pnx", "--actor", OTHER,
        "--offline", "--json",
    )
    assert all(e.get("v") == 2 for e in _events(root))
    assert (Path(_log_dir(root)) / _shard_name_for_actor(OTHER)).is_file()
    return _lines(root)


def _recompute(event: dict) -> dict:
    edited = dict(event, payload=dict(event["payload"], title="Rewritten"))
    edited["id"] = event_id_v2(
        edited["seq"], edited["ts"], edited["actor"], edited["type"], edited["payload"], edited["prev"]
    )
    return edited


def _move_tail(root: str, keep: list[bytes], moved: list[bytes], destination: str) -> None:
    """Rewrite ACTOR's shard to keep and put moved, byte for byte, into destination."""
    _write_lines(root, keep)
    log = Path(_log_dir(root))
    if destination == "same actor":
        target = log / ("moved-" + _shard_name_for_actor(ACTOR))
        assert not target.exists()
        target.write_bytes(b"".join(line + b"\n" for line in moved))
    else:
        target = log / _shard_name_for_actor(OTHER)
        target.write_bytes(target.read_bytes() + b"".join(line + b"\n" for line in moved))


@pytest.mark.deep
@pytest.mark.parametrize("destination", ["same actor", "other actor"])
@pytest.mark.parametrize("change", ["deleted", "recomputed"])
def test_middle_change_with_later_events_moved_to_another_shard_fails_verify(
    tmp_path, caplog, change, destination
):
    root = str(tmp_path / "repo")
    lines = _v2_repo_with_two_actors(root)
    events = [parse_line(line) for line in lines]
    middle, successor = events[1], events[2]
    assert successor["prev"] == middle["id"]

    if change == "deleted":
        keep = lines[:1]
    else:
        keep = lines[:1] + [serialise(_recompute(middle)).encode("utf-8")]
    _move_tail(root, keep, lines[2:], destination)

    _assert_verify_names(root, caplog, successor)


@pytest.mark.deep
def test_a_tombstone_for_the_deleted_event_does_not_excuse_the_moved_successor(tmp_path, caplog):
    """Annulling an event never removes it, so no tombstone excuses a prev naming a missing id."""
    root = str(tmp_path / "repo")
    _v2_repo_with_two_actors(root)
    events = _events(root)
    middle, successor = events[1], events[2]
    tombstone = _append_checked(
        root, "annul", middle["id"], "--reason", "recorded in error", "--actor", ACTOR, "--offline",
    )
    assert valid_annulment(tombstone)
    assert _pinax(root, "verify", check=False).returncode == 0

    lines = _lines(root)
    assert parse_line(lines[1])["id"] == middle["id"]
    _move_tail(root, lines[:1], lines[2:], "same actor")

    _assert_verify_names(root, caplog, successor)


@pytest.mark.deep
def test_a_moved_version_one_anchor_warns_and_passes_verify(tmp_path, caplog):
    """A version 1 prev is outside the version 1 hash: the fold warns and verify passes."""
    root = str(tmp_path / "repo")
    _version_one_repo(root)
    lines = _lines(root)
    events = _events(root)
    assert len(events) >= 3 and all("v" not in e for e in events)
    successor = events[2]
    assert successor["prev"] == events[1]["id"]

    _write_lines(root, lines[:1])
    moved = Path(_log_dir(root)) / ("moved-" + _shard_name_for_actor(ACTOR))
    moved.write_bytes(b"".join(line + b"\n" for line in lines[2:]))

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert any("Broken prev-chain" in m and successor["id"] in m for m in messages)
    assert not any("Integrity violation" in m for m in messages)


# A log written before the shard choice took prev only from the chosen shard:
# the actor's chain continues from a case-variant shard into the exact-name
# shard, so the exact shard's first event names an event in the other file.
_PRE_SHARD_CHOICE_SHARDS = {
    "coordinator-Desk.jsonl": [
        '{"id":"wrxjxbg32dkmq3ciwouabiz6gjq4p26ferimsm2kwhstxaztyzia","seq":0,"ts":"2026-11-10T09:00:00Z","actor":"coordinator@desk","type":"item.created","payload":{"item_id":"pnx-aaaa","title":"First","prefix":"pnx","status":"queued"},"prev":"","v":2}',
        '{"id":"k5os3loos6bj63n7wyqsj6gvg3djxya4p323j7k7zhtpbhnwnp5a","seq":1,"ts":"2026-11-10T09:00:00Z","actor":"coordinator@desk","type":"item.created","payload":{"item_id":"pnx-bbbb","title":"Second","prefix":"pnx","status":"queued"},"prev":"wrxjxbg32dkmq3ciwouabiz6gjq4p26ferimsm2kwhstxaztyzia","v":2}',
    ],
    "coordinator-desk.jsonl": [
        '{"id":"wonakgfiicjmjbiehqd4izccmx4iwclmtngxgxdlvl67ljjugl7a","seq":2,"ts":"2026-11-10T09:00:00Z","actor":"coordinator@desk","type":"item.claimed","payload":{"item_id":"pnx-aaaa","actor":"coordinator@desk"},"prev":"k5os3loos6bj63n7wyqsj6gvg3djxya4p323j7k7zhtpbhnwnp5a","v":2}',
        '{"id":"4mkuurhfqsszyjp4l4spe6rqbxeo3i7uqvvlcrtrwqm2c3hztuvq","seq":3,"ts":"2026-11-10T09:00:00Z","actor":"coordinator@desk","type":"item.created","payload":{"item_id":"pnx-cccc","title":"Third","prefix":"pnx","status":"queued"},"prev":"wonakgfiicjmjbiehqd4izccmx4iwclmtngxgxdlvl67ljjugl7a","v":2}',
    ],
}


@pytest.mark.deep
def test_a_chain_continued_in_another_shard_before_the_shard_choice_fix_verifies(tmp_path, caplog):
    root = str(tmp_path / "repo")
    _init_repo(root)
    log = Path(_log_dir(root))
    log.mkdir(parents=True)
    for name, lines in _PRE_SHARD_CHOICE_SHARDS.items():
        (log / name).write_bytes("".join(line + "\n" for line in lines).encode("utf-8"))
    if sorted(p.name for p in log.iterdir()) != sorted(_PRE_SHARD_CHOICE_SHARDS):
        pytest.skip("the filesystem cannot hold two names that differ only by case")
    events = [parse_line(line.encode("utf-8")) for lines in _PRE_SHARD_CHOICE_SHARDS.values() for line in lines]
    assert all(verify_id(e) for e in events)
    assert events[2]["prev"] == events[1]["id"]

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert _chain_warnings(messages) == []


# ---------------------------------------------------------------------------
# Documented gaps: tampering that passes verify. These pin the documents'
# not-caught sentences, so a change to the rule shows up here.
# ---------------------------------------------------------------------------

def _forked_repo(root: str) -> list[dict]:
    """Two branches of ACTOR's chain, union-merged by Git, then one more event."""
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    if _git(root, "status", "--porcelain").stdout.strip():
        _commit_all(root, "init")
    _add(root, "Common one")
    _add(root, "Common two")
    _git(root, "checkout", "-q", "-b", "side")
    _add(root, "Side branch")
    _git(root, "checkout", "-q", "main")
    _add(root, "Main branch")
    merged = _git(root, "merge", "-q", "--no-edit", "side", check=False)
    if merged.returncode != 0:
        status = _git(root, "status", "--porcelain").stdout
        conflicted = [line[3:] for line in status.splitlines() if line[:2] in ("UU", "AA")]
        assert conflicted and not any(".ergon/log/" in p for p in conflicted), status
        _git(root, "checkout", "--ours", "--", *conflicted)
        _pinax(root, "verify", "--fix")
        _commit_all(root, "merge side")
    _add(root, "After merge")
    assert _pinax(root, "verify", check=False).returncode == 0
    return _events(root)


@pytest.mark.deep
@pytest.mark.parametrize("move", [False, True])
def test_deleting_a_fork_tip_passes_verify(tmp_path, caplog, move):
    """An event no later event names can be deleted, though later events of its chain stay."""
    root = str(tmp_path / "repo")
    events = _forked_repo(root)
    named = {e["prev"] for e in events}
    newest = max(events, key=lambda e: (e["seq"], e["ts"], e["actor"], e["id"]))
    tips = [e for e in events if e["id"] not in named and e["id"] != newest["id"]]
    assert len(tips) == 1
    victim = tips[0]
    assert any(e["seq"] > victim["seq"] for e in events)

    lines = [line for line in _lines(root) if parse_line(line)["id"] != victim["id"]]
    if move:
        half = len(lines) // 2
        _write_lines(root, lines[:half])
        moved = Path(_log_dir(root)) / ("moved-" + _shard_name_for_actor(ACTOR))
        moved.write_bytes(b"".join(line + b"\n" for line in lines[half:]))
    else:
        _write_lines(root, lines)

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert _chain_warnings(messages) == []


@pytest.mark.deep
def test_deleting_a_chains_newest_event_passes_verify(tmp_path, caplog):
    """No later event names the newest event, so deleting it leaves no broken link."""
    root = str(tmp_path / "repo")
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    for title in ("First", "Second", "Third"):
        _add(root, title)
    lines = _lines(root)
    events = _events(root)
    assert all(e.get("v") == 2 for e in events)
    newest = max(events, key=lambda e: (e["seq"], e["ts"], e["actor"], e["id"]))
    assert newest["id"] not in {e["prev"] for e in events}

    _write_lines(root, [line for line in lines if parse_line(line)["id"] != newest["id"]])

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert _chain_warnings(messages) == []


@pytest.mark.deep
def test_a_recomputed_edit_before_a_version_one_successor_warns_and_passes_verify(tmp_path, caplog):
    """The broken link sits on the version 1 event an earlier client wrote, so the fold only warns."""
    root = str(tmp_path / "repo")
    _init_repo(root)
    _pinax(root, "init", "--actor", ACTOR)
    for title in ("One", "Two", "Three", "Four"):
        _add(root, title)
    events = _events(root)
    assert all(e.get("v") == 2 for e in events)
    last = {k: val for k, val in events[-1].items() if k != "v"}
    last["id"] = event_id(last["seq"], last["ts"], last["actor"], last["type"], last["payload"])
    assert last["prev"] == events[-2]["id"] and verify_id(last)

    edited = dict(events[-2], payload=dict(events[-2]["payload"], title="Rewritten"))
    edited["id"] = event_id_v2(
        edited["seq"], edited["ts"], edited["actor"], edited["type"], edited["payload"], edited["prev"]
    )
    assert verify_id(edited) and edited["id"] != events[-2]["id"]
    _write_lines(
        root, [serialise(e).encode("utf-8") for e in events[:-2] + [edited, last]]
    )

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    _, messages = _fold_warnings(root, caplog)
    assert any("Broken prev-chain" in m and last["id"] in m for m in messages)
    assert not any("Integrity violation" in m for m in messages)


@pytest.mark.deep
@pytest.mark.parametrize("move", [False, True])
def test_an_annulled_event_replaced_by_a_line_keeping_its_id_passes_verify(tmp_path, caplog, move):
    """Verify checks only that a line carries an annulled id; the annulled event has no fold effect."""
    root = str(tmp_path / "repo")
    _v2_repo_with_two_actors(root)
    events = _events(root)
    target = events[2]
    assert target["type"] == "item.created" and events[3]["prev"] == target["id"]
    _append_checked(
        root, "annul", target["id"], "--reason", "recorded in error", "--actor", ACTOR, "--offline",
    )
    state, _ = _fold_warnings(root, caplog)
    assert target["payload"]["item_id"] not in state["items"]

    stub = {
        "id": target["id"], "seq": target["seq"], "ts": target["ts"], "actor": target["actor"],
        "type": "item.created", "payload": {"item_id": "pnx-zzzz", "title": "Forged body"},
        "prev": "", "v": 2,
    }
    stub_line = json.dumps(stub, separators=(",", ":")).encode("utf-8")
    assert not verify_id(stub)
    lines = _lines(root)
    index = next(i for i, line in enumerate(lines) if parse_line(line)["id"] == target["id"])
    if move:
        _write_lines(root, lines[:index])
        moved = Path(_log_dir(root)) / ("moved-" + _shard_name_for_actor(ACTOR))
        moved.write_bytes(b"".join(line + b"\n" for line in lines[index + 1:]))
        other = Path(_log_dir(root)) / _shard_name_for_actor(OTHER)
        other.write_bytes(other.read_bytes() + stub_line + b"\n")
    else:
        _write_lines(root, lines[:index] + [stub_line] + lines[index + 1:])

    _pinax(root, "verify", "--fix")
    assert _pinax(root, "verify", check=False).returncode == 0
    state, _ = _fold_warnings(root, caplog)
    assert "pnx-zzzz" not in state["items"]
    assert target["payload"]["item_id"] not in state["items"]


@pytest.mark.deep
def test_version_two_tombstones_retire_version_one_and_version_two_targets(tmp_path, caplog):
    root = str(tmp_path / "repo")
    made = _mixed_repo(root)
    v1_target = next(
        e for e in _events(root)
        if "v" not in e and e["type"] == "item.created" and e["payload"]["item_id"] == made["v1_items"][1]
    )
    v2_target = made["noted"]

    tombstones = []
    for target in (v1_target, v2_target):
        tombstone = _append_checked(
            root, "annul", target["id"], "--reason", "recorded in error",
            "--actor", ACTOR, "--offline",
        )
        assert tombstone["type"] == "event.annulled"
        assert valid_annulment(tombstone)
        tombstones.append(tombstone)

    assert _pinax(root, "verify", check=False).returncode == 0
    state, messages = _fold_warnings(root, caplog)
    assert _chain_warnings(messages) == []
    assert made["v1_items"][1] not in state["items"]
    notes = json.dumps(state_to_json_safe(state["items"][made["new_item"]]))
    assert "https://example.org/plan" not in notes


@pytest.mark.deep
def test_claim_through_a_hub_writes_version_two(clone_wired_to_hub):
    def pinax(root, *args):
        return _pinax(root, *args, check=False)

    root = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=pinax, commit_all=_commit_all, actor=ACTOR,
    )
    added = _pinax(root, "add", "--title", "Claimed", "--prefix", "pnx", "--actor", ACTOR, "--json")
    item_id = json.loads(added.stdout.strip().splitlines()[-1])["item_id"]
    claimed = _append_checked(root, "claim", item_id, "--actor", ACTOR, "--json")
    assert claimed["type"] == "item.claimed"
    assert _pinax(root, "verify", check=False).returncode == 0


def _install_pinax_hook(root: str, hook_source: str, hooks_dir: str) -> None:
    """Gate the repository's commits with the pre-commit hook pinax init installed."""
    os.makedirs(hooks_dir, exist_ok=True)
    target = os.path.join(hooks_dir, "pre-commit")
    Path(target).write_bytes(Path(hook_source).read_bytes())
    os.chmod(target, 0o755)
    _git(root, "config", "core.hooksPath", hooks_dir)


@pytest.mark.deep
def test_a_stale_clone_of_the_same_actor_writes_through_its_own_hook(
    tmp_path, clone_wired_to_hub, clone_of_hub
):
    def pinax(root, *args):
        return _pinax(root, *args, check=False)

    first = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=pinax, commit_all=_commit_all, actor=ACTOR,
    )
    hook = _git(first, "rev-parse", "--git-path", "hooks/pre-commit").stdout.strip()
    hook = os.path.join(first, hook) if not os.path.isabs(hook) else hook
    assert "Pinax drift lint" in Path(hook).read_text(encoding="utf-8")

    def configure(root):
        _git(root, "config", "user.email", "builder@pinax.example")
        _git(root, "config", "user.name", "Pinax Builder")

    second = clone_of_hub(git=_git, name="second", config={"core.autocrlf": "false"}, configure=configure)
    _install_pinax_hook(second, hook, str(tmp_path / "second-hooks-pinax"))

    _pinax(first, "add", "--title", "Published by the first clone", "--prefix", "pnx", "--actor", ACTOR, "--json")
    published = _events(first)[-1]

    local_before = {e["id"] for e in _events(second)}
    assert published["id"] not in local_before
    written = _pinax(
        second, "add", "--title", "Written by the stale clone", "--prefix", "pnx",
        "--actor", ACTOR, "--json", check=False,
    )
    assert written.returncode == 0, written.stdout + written.stderr

    events = _events(second)
    stale = next(e for e in events if e.get("payload", {}).get("title") == "Written by the stale clone")
    assert stale.get("v") == 2 and verify_id(stale)
    assert stale["prev"] in local_before
    assert published["id"] in {e["id"] for e in events}
    assert _pinax(second, "verify", check=False).returncode == 0
    assert _git(second, "status", "--porcelain").stdout.strip() == ""
