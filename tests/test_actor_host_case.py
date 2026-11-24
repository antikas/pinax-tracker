"""Host-name case in actors and log shard file names.

New events carry the host part of the actor in lower case, and an append
never creates a shard whose name differs from an existing one only by case.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import subprocess

import pytest

import pinax.append as append_mod
from pinax.__main__ import main
from pinax.append import append_event
from pinax.event import mint_event, parse_line, serialise
from pinax.projection import regenerate


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git not available on PATH")

_TS = "2026-11-01T10:00:00Z"


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _new_repo(tmp_path) -> str:
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "builder@example.invalid")
    _git(repo, "config", "user.name", "Builder")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def _run_main(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue()


def _read_shard(path: str) -> list[dict]:
    events = []
    with open(path, "rb") as fh:
        for line in fh.read().split(b"\n"):
            if line:
                ev = parse_line(line)
                if ev is not None:
                    events.append(ev)
    return events


def _all_events(log_dir: str) -> list[tuple[str, dict]]:
    found = []
    for name in sorted(os.listdir(log_dir)):
        if name.endswith(".jsonl"):
            for ev in _read_shard(os.path.join(log_dir, name)):
                found.append((name, ev))
    return found


def _write_shard(log_dir: str, name: str, events: list[dict]) -> None:
    with open(os.path.join(log_dir, name), "w", newline="\n", encoding="utf-8") as fh:
        for ev in events:
            fh.write(serialise(ev) + "\n")


def _ev(seq: int, actor: str, etype: str, payload: dict, prev: str = "") -> dict:
    return mint_event(seq=seq, ts=_TS, actor=actor, etype=etype, payload=payload, prev=prev)


def _created(seq: int, actor: str, item_id: str, title: str, prev: str = "") -> dict:
    return _ev(seq, actor, "item.created",
               {"item_id": item_id, "title": title, "prefix": "pnx", "status": "queued"}, prev)


# ---------------------------------------------------------------------------
# The normaliser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("coordinator@Desk", "coordinator@desk"),
    ("Alex@Desk", "Alex@desk"),
    ("coordinator@desk-wsl", "coordinator@desk-wsl"),
    ("builder", "builder"),
])
def test_normalise_actor(given, expected):
    assert append_mod.normalise_actor(given) == expected


# ---------------------------------------------------------------------------
# Default actors
# ---------------------------------------------------------------------------

def test_every_default_actor_is_lower_case_host(monkeypatch):
    from pinax import doctor
    from pinax.commands import dispatch, done, init, ready, reconcile, registry_cmd, status_cmd

    monkeypatch.setattr(socket, "gethostname", lambda: "Desk")
    assert doctor.default_actor() == "operator@desk"
    for mod in (status_cmd, done, init, ready, registry_cmd, reconcile, dispatch):
        assert mod._default_actor() == "operator@desk", mod.__name__


@requires_git
def test_add_without_actor_writes_lower_case_host(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(socket, "gethostname", lambda: "Desk")
    assert _run_main("--root", repo, "init")[0] == 0
    assert _run_main("--root", repo, "add", "--title", "Default actor item")[0] == 0

    log_dir = os.path.join(repo, ".ergon", "log")
    created = [(name, ev) for name, ev in _all_events(log_dir) if ev["type"] == "item.created"]
    assert len(created) == 1
    name, ev = created[0]
    assert ev["actor"] == "operator@desk"
    assert name == "operator-desk.jsonl"


@requires_git
def test_add_with_mixed_case_actor_is_normalised_and_verifies(tmp_path, monkeypatch):
    repo = _new_repo(tmp_path)
    monkeypatch.chdir(repo)
    assert _run_main("--root", repo, "init", "--actor", "operator@example")[0] == 0
    assert _run_main("--root", repo, "add", "--title", "Mixed", "--actor", "coordinator@Desk")[0] == 0

    log_dir = os.path.join(repo, ".ergon", "log")
    created = [ev for _, ev in _all_events(log_dir) if ev["type"] == "item.created"]
    assert [ev["actor"] for ev in created] == ["coordinator@desk"]
    code, out = _run_main("--root", repo, "verify")
    assert code == 0, out
    assert "OK" in out


# ---------------------------------------------------------------------------
# Shard choice
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("actor", ["operator@Desk", "operator@desk"])
def test_append_reuses_shard_that_differs_only_by_case(tmp_path, actor):
    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    first = _ev(0, "operator@Desk", "ergon.created", {"repo": "fixture"})
    _write_shard(log_dir, "operator-Desk.jsonl", [first])
    before = set(os.listdir(log_dir))

    event = _ev(1, actor, "phase.opened", {"phase": "init"})
    path = append_event(log_dir, event, actor=actor)

    assert set(os.listdir(log_dir)) == before
    assert os.path.basename(path) == "operator-Desk.jsonl"
    assert [ev["id"] for ev in _read_shard(path)] == [first["id"], event["id"]]


def test_shard_choice_ignores_case_insensitive_exists(tmp_path, monkeypatch):
    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    _write_shard(log_dir, "operator-Desk.jsonl",
                 [_ev(0, "operator@Desk", "ergon.created", {"repo": "fixture"})])

    real_listdir = os.listdir

    def exists_ignoring_case(path):
        parent, name = os.path.split(os.fspath(path))
        try:
            return name.lower() in {n.lower() for n in real_listdir(parent or ".")}
        except OSError:
            return False

    monkeypatch.setattr(os.path, "exists", exists_ignoring_case)
    path = append_mod.shard_path_for_actor(log_dir, "operator@desk")
    assert os.path.basename(path) == "operator-Desk.jsonl"


def test_exact_name_wins_over_case_variant(tmp_path):
    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    _write_shard(log_dir, "coordinator-Desk.jsonl",
                 [_created(0, "coordinator@Desk", "pnx-aaaa", "A")])
    _write_shard(log_dir, "coordinator-desk.jsonl",
                 [_created(1, "coordinator@desk", "pnx-bbbb", "B")])

    event = _ev(2, "coordinator@Desk", "phase.opened", {"phase": "x"})
    path = append_event(log_dir, event, actor="coordinator@Desk")
    assert os.path.basename(path) == "coordinator-desk.jsonl"
    assert sorted(os.listdir(log_dir)) == ["coordinator-Desk.jsonl", "coordinator-desk.jsonl"]


# ---------------------------------------------------------------------------
# dispatch --claim
# ---------------------------------------------------------------------------

def _dispatch_repo(tmp_path, shard_events: list[dict]) -> tuple[str, str]:
    repo = _new_repo(tmp_path)
    log_dir = os.path.join(repo, ".ergon", "log")
    os.makedirs(log_dir)
    _write_shard(log_dir, "operator-Desk.jsonl", shard_events)
    regenerate(repo)
    return repo, log_dir


@requires_git
def test_dispatch_claim_uses_existing_case_variant_shard(tmp_path, monkeypatch):
    a = _ev(0, "operator@Desk", "ergon.created", {"repo": "fixture"})
    b = _created(1, "operator@Desk", "pnx-aaaa", "First", a["id"])
    repo, log_dir = _dispatch_repo(tmp_path, [a, b])
    monkeypatch.chdir(repo)

    code, out = _run_main("--root", repo, "dispatch", "--claim", "--actor", "operator@Desk")
    assert code == 0, out

    assert sorted(n for n in os.listdir(log_dir)) == ["operator-Desk.jsonl"]
    events = _read_shard(os.path.join(log_dir, "operator-Desk.jsonl"))
    claims = [ev for ev in events if ev["type"] == "item.claimed"]
    assert len(claims) == 1
    assert claims[0]["actor"] == "operator@desk"


@requires_git
def test_dispatch_claim_prev_follows_own_actor_not_last_line(tmp_path, monkeypatch):
    a = _ev(0, "operator@desk", "ergon.created", {"repo": "fixture"})
    b = _created(1, "operator@desk", "pnx-aaaa", "First", a["id"])
    c = _created(2, "operator@Desk", "pnx-bbbb", "Second")
    repo, log_dir = _dispatch_repo(tmp_path, [a, b, c])
    monkeypatch.chdir(repo)

    code, out = _run_main("--root", repo, "dispatch", "--claim", "--max", "1",
                          "--actor", "operator@desk")
    assert code == 0, out

    events = _read_shard(os.path.join(log_dir, "operator-Desk.jsonl"))
    claims = [ev for ev in events if ev["type"] == "item.claimed"]
    assert len(claims) == 1
    assert claims[0]["prev"] == b["id"]
    code, out = _run_main("--root", repo, "verify")
    assert code == 0, out


# ---------------------------------------------------------------------------
# prev comes from the shard the append goes to
# ---------------------------------------------------------------------------

_ACTOR = "coordinator@desk"
_VARIANT = "coordinator-Desk.jsonl"
_EXACT = "coordinator-desk.jsonl"


def _require_case_distinct_names(log_dir: str) -> None:
    probe = os.path.join(log_dir, "probe-case")
    os.makedirs(probe)
    try:
        for name in ("a-Desk.jsonl", "a-desk.jsonl"):
            open(os.path.join(probe, name), "w").close()
        distinct = len(os.listdir(probe)) == 2
    finally:
        for name in os.listdir(probe):
            os.remove(os.path.join(probe, name))
        os.rmdir(probe)
    if not distinct:
        pytest.skip("file system cannot hold names that differ only by case")


def _switched_repo(tmp_path, exact_events=None) -> tuple[str, str, dict]:
    """
    A committed repository whose actor's events sit in the case-variant
    shard, after which an exact-name shard arrives holding earlier events.
    """
    repo = _new_repo(tmp_path)
    log_dir = os.path.join(repo, ".ergon", "log")
    os.makedirs(log_dir)
    _require_case_distinct_names(log_dir)

    a = _ev(0, _ACTOR, "ergon.created", {"repo": "fixture"})
    b = _created(1, _ACTOR, "pnx-aaaa", "First", a["id"])
    c = _created(2, _ACTOR, "pnx-cccc", "Third", b["id"])
    newest = _ev(4, _ACTOR, "item.claimed", {"item_id": "pnx-aaaa", "actor": _ACTOR}, c["id"])
    _write_shard(log_dir, _VARIANT, [a, b, c, newest])
    if exact_events is None:
        exact_events = [_created(3, _ACTOR, "pnx-bbbb", "Second")]
    _write_shard(log_dir, _EXACT, exact_events)
    assert sorted(os.listdir(log_dir)) == sorted([_VARIANT, _EXACT])

    regenerate(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture log")
    return repo, log_dir, {"newest": newest, "exact": exact_events}


def _new_events(log_dir: str, before: set[str]) -> list[tuple[str, dict]]:
    return [(name, ev) for name, ev in _all_events(log_dir) if ev["id"] not in before]


def _assert_links_in_exact_shard(repo: str, log_dir: str, before: set[str], expected_prev: str):
    added = _new_events(log_dir, before)
    assert added, "no event was appended"
    assert {name for name, _ in added} == {_EXACT}
    code, out = _run_main("--root", repo, "verify")
    assert code == 0, out
    first = added[0][1]
    assert first["actor"] == _ACTOR
    assert first["prev"] == expected_prev
    for (_, earlier), (_, later) in zip(added, added[1:]):
        assert later["prev"] == earlier["id"]


def _ids(log_dir: str) -> set[str]:
    return {ev["id"] for _, ev in _all_events(log_dir)}


@requires_git
def test_block_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    code, out = _run_main("--root", repo, "block", "pnx-bbbb", "--gate", "scope",
                          "--offline", "--actor", _ACTOR)
    assert code == 0, out
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_annul_local_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    from pinax.commands import annul

    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    with contextlib.redirect_stdout(io.StringIO()):
        annul.append_local(repo, fx["newest"]["id"], "superseded", _ACTOR)
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_registry_add_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    code, out = _run_main("--root", repo, "registry", "add", "--id", "fixture-repo",
                          "--path", repo, "--actor", _ACTOR)
    assert code == 0, out
    # registry add leaves the projection to the next state-changing command.
    regenerate(repo)
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_reconcile_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)
    offline = os.path.join(repo, "BACKLOG-OFFLINE.md")
    with open(offline, "w", newline="\n", encoding="utf-8") as fh:
        fh.write(f"- 2026-11-01T11:00:00Z {_ACTOR} done pnx-aaaa | Finished\n"
                 f"- 2026-11-01T11:05:00Z {_ACTOR} park pnx-cccc | Waiting\n")

    code, out = _run_main("--root", repo, "reconcile", "--actor", _ACTOR)
    assert code == 0, out
    assert len(_new_events(log_dir, before)) == 2
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_doctor_stale_claim_repair_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    from pinax.commands import doctor_cmd

    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)
    answers = iter(["r", "handing over"])
    monkeypatch.setattr(doctor_cmd, "_prompt", lambda text: next(answers, None))

    code, out = _run_main("--root", repo, "doctor", "--reconcile", "--now",
                          "2026-11-10T10:00:00Z", "--actor", _ACTOR)
    assert code == 0, out
    added = _new_events(log_dir, before)
    assert [ev["type"] for _, ev in added] == ["item.claim_released"]
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_dispatch_claim_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    code, out = _run_main("--root", repo, "dispatch", "--claim", "--actor", _ACTOR)
    assert code == 0, out
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_status_setter_after_shard_switch_links_into_chosen_shard(tmp_path, monkeypatch):
    repo, log_dir, fx = _switched_repo(tmp_path)
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    code, out = _run_main("--root", repo, "status", "pnx-bbbb", "ready", "--actor", _ACTOR)
    assert code == 0, out
    _assert_links_in_exact_shard(repo, log_dir, before, fx["exact"][-1]["id"])


@requires_git
def test_block_into_exact_shard_without_own_events_has_empty_prev(tmp_path, monkeypatch):
    other = _created(3, "operator@desk", "pnx-bbbb", "Second")
    repo, log_dir, _ = _switched_repo(tmp_path, exact_events=[other])
    monkeypatch.chdir(repo)
    before = _ids(log_dir)

    code, out = _run_main("--root", repo, "block", "pnx-bbbb", "--gate", "scope",
                          "--offline", "--actor", _ACTOR)
    assert code == 0, out
    _assert_links_in_exact_shard(repo, log_dir, before, "")


def test_prev_for_actor_reads_only_the_chosen_shard(tmp_path):
    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    _require_case_distinct_names(log_dir)
    a = _ev(0, _ACTOR, "ergon.created", {"repo": "fixture"})
    newest = _created(5, _ACTOR, "pnx-aaaa", "First", a["id"])
    _write_shard(log_dir, _VARIANT, [a, newest])
    assert append_mod.prev_for_actor(log_dir, _ACTOR) == newest["id"]

    own = _created(1, _ACTOR, "pnx-bbbb", "Second")
    other = _created(2, "coordinator@Desk", "pnx-cccc", "Third")
    _write_shard(log_dir, _EXACT, [own, other])
    assert append_mod.prev_for_actor(log_dir, _ACTOR) == own["id"]
    assert append_mod.prev_for_actor(log_dir, "coordinator@Desk") == other["id"]
    assert append_mod.prev_for_actor(log_dir, "operator@desk") == ""


def test_prev_for_actor_follows_file_order(tmp_path):
    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    later_seq = _created(7, _ACTOR, "pnx-aaaa", "First")
    earlier_seq = _created(3, _ACTOR, "pnx-bbbb", "Second")
    _write_shard(log_dir, _EXACT, [later_seq, earlier_seq])
    assert append_mod.prev_for_actor(log_dir, _ACTOR) == earlier_seq["id"]


# ---------------------------------------------------------------------------
# Only .jsonl names are chosen
# ---------------------------------------------------------------------------

def test_upper_case_suffix_shard_is_never_chosen(tmp_path):
    from pinax.fold import read_events

    log_dir = str(tmp_path / "log")
    os.makedirs(log_dir)
    first = _ev(0, "operator@desk", "ergon.created", {"repo": "fixture"})
    _write_shard(log_dir, "operator-desk.JSONL", [first])
    upper = os.path.join(log_dir, "operator-desk.JSONL")
    with open(upper, "rb") as fh:
        upper_bytes = fh.read()

    event = _ev(1, "operator@desk", "phase.opened", {"phase": "init"})
    path = append_event(log_dir, event, actor="operator@desk")

    assert os.path.basename(path) == "operator-desk.jsonl"
    assert event["id"] in {ev["id"] for ev in read_events(log_dir)}
    if os.name == "posix" and os.uname().sysname == "Linux":
        with open(upper, "rb") as fh:
            assert fh.read() == upper_bytes


# ---------------------------------------------------------------------------
# Replay of an existing mixed-case log
# ---------------------------------------------------------------------------

_FIXTURE_SHARDS = {
    "operator-Desk.jsonl": [
        '{"id":"5ldhfxfzzjizrsy6uk3cirehqyz2sygvmrto22d3iiwwhot2p7uq","seq":0,"ts":"2026-11-01T10:00:00Z","actor":"operator@Desk","type":"ergon.created","payload":{"repo":"fixture"},"prev":"","v":2}',
        '{"id":"76uoruj4mgbe5uyhutrf5dtwfmmzkjxqlzmgewlhbquivnuss6gq","seq":1,"ts":"2026-11-01T10:00:00Z","actor":"operator@Desk","type":"item.created","payload":{"item_id":"pnx-aaaa","title":"First","prefix":"pnx","status":"queued"},"prev":"5ldhfxfzzjizrsy6uk3cirehqyz2sygvmrto22d3iiwwhot2p7uq","v":2}',
    ],
    "coordinator-Desk.jsonl": [
        '{"id":"3yqcbzrzkd554nvijceiyr3u4yvtrslfhtby74cu2nahms2snnuq","seq":2,"ts":"2026-11-01T10:00:00Z","actor":"coordinator@Desk","type":"item.created","payload":{"item_id":"pnx-bbbb","title":"Second","prefix":"pnx","status":"queued"},"prev":"","v":2}',
        '{"id":"hshsccmgmlimfsxhrohemvo67jcumpqstrqaxsa7srns76t4etda","seq":4,"ts":"2026-11-01T10:00:00Z","actor":"coordinator@Desk","type":"item.claimed","payload":{"item_id":"pnx-bbbb","actor":"coordinator@Desk"},"prev":"3yqcbzrzkd554nvijceiyr3u4yvtrslfhtby74cu2nahms2snnuq","v":2}',
    ],
    "coordinator-desk.jsonl": [
        '{"id":"yisy3unr3645nfbngmprchfgh6yby3cutremfdjwt33v3vwowbwa","seq":3,"ts":"2026-11-01T10:00:00Z","actor":"coordinator@desk","type":"item.claimed","payload":{"item_id":"pnx-aaaa","actor":"coordinator@desk"},"prev":"","v":2}',
        '{"id":"a4psyaduhi5gglipnqhrvdzb2itsb4kz6id6eohm33ruacyb5k6a","seq":5,"ts":"2026-11-01T10:00:00Z","actor":"coordinator@desk","type":"item.created","payload":{"item_id":"pnx-cccc","title":"Third","prefix":"pnx","status":"queued"},"prev":"yisy3unr3645nfbngmprchfgh6yby3cutremfdjwt33v3vwowbwa","v":2}',
    ],
}

_EXPECTED_BOARD = (
    "# Board\n\n## Queued\n\n"
    "- pnx-cccc \u00b7 Third \u00b7 queued [next] \u00b7 coordinator@desk \u00b7 \u2014\n\n"
    "## Building\n\n"
    "- pnx-bbbb \u00b7 Second \u00b7 building \u00b7 coordinator@Desk \u00b7 \u2014\n"
    "- pnx-aaaa \u00b7 First \u00b7 building \u00b7 coordinator@desk \u00b7 \u2014\n"
)


def test_replay_fixture_ids_verify():
    from pinax.event import verify_id

    for lines in _FIXTURE_SHARDS.values():
        for line in lines:
            assert verify_id(parse_line(line.encode("utf-8"))), line


def test_replay_of_mixed_case_log_is_unchanged(tmp_path):
    root = str(tmp_path / "fixture")
    log_dir = os.path.join(root, ".ergon", "log")
    os.makedirs(log_dir)
    for name, lines in _FIXTURE_SHARDS.items():
        with open(os.path.join(log_dir, name), "w", newline="\n", encoding="utf-8") as fh:
            fh.write("".join(line + "\n" for line in lines))

    regenerate(root)
    with open(os.path.join(root, ".ergon", "board.md"), encoding="utf-8", newline="") as fh:
        assert fh.read() == _EXPECTED_BOARD

    assert sorted(os.listdir(log_dir)) == sorted(_FIXTURE_SHARDS)
    for name, lines in _FIXTURE_SHARDS.items():
        with open(os.path.join(log_dir, name), encoding="utf-8", newline="") as fh:
            assert fh.read() == "".join(line + "\n" for line in lines)


# ---------------------------------------------------------------------------
# No new command
# ---------------------------------------------------------------------------

_SUBCOMMANDS = {
    "init", "add", "claim", "status", "done", "block", "park", "decide", "release", "policy",
    "priority", "annul", "dep", "ready", "next", "verify", "note", "metrics", "report",
    "dispatch", "replay", "board", "web", "overview", "registry", "reconcile", "doctor",
}


def test_subcommand_set_is_unchanged():
    code, out = _run_main("--help")
    assert code == 0
    match = re.search(r"\{([a-z,\-]+)\}", out)
    assert match is not None, out
    assert set(match.group(1).split(",")) == _SUBCOMMANDS
