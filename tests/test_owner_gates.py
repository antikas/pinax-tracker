"""
The owner-gate class rule: a project process of flow (the default) or strict,
a class recorded on a block for the owner, and an on-demand check.

Every rule here can fail a wrong answer: an invented class is planted and must
be refused under strict and accepted under flow, a log with no policy must run
on flow, and the check must exit non-zero under strict only.
"""

from __future__ import annotations

import json
import os

import pytest

from pinax import owner_gates, sync
from pinax.commands.block import run as block_run
from pinax.commands.policy import run_check, run_owner_gates, run_process
from pinax.event import mint_event
from pinax.fold import fold
from pinax.projection import render_item

from test_sync_sequence import FakeGit, _log_dir, _seed, _shard_events

ACTOR = "coordinator@host-a"
OWNER = "worker@alpha"
ITEM = "pnx-a1b2"
OTHER = "pnx-c3d4"
INVENTED = "because the moon is full"
DEFAULT_CLASS = "production deployment"


@pytest.fixture()
def repo(tmp_path):
    """A tracker log holding two items and no policy."""
    root = str(tmp_path)
    os.makedirs(_log_dir(root))
    _seed(root, 0, "2026-09-01T09:00:00Z", OWNER, "item.created",
          {"item_id": ITEM, "title": "First", "prefix": "pnx"})
    _seed(root, 1, "2026-09-01T09:01:00Z", OWNER, "item.created",
          {"item_id": OTHER, "title": "Second", "prefix": "pnx"})
    return root


def _strict(root, seq=2):
    _seed(root, seq, "2026-09-01T09:02:00Z", OWNER, "policy.process_set",
          {"process": "strict"})


def _log_bytes(root):
    out = {}
    for name in sorted(os.listdir(_log_dir(root))):
        with open(os.path.join(_log_dir(root), name), "rb") as fh:
            out[name] = fh.read()
    return out


def _state(root):
    return fold(_log_dir(root))


def _blocked_events(root):
    return [e for e in _shard_events(root, ACTOR) if e["type"] == "item.blocked"]


# ---------------------------------------------------------------------------
# The default is flow
# ---------------------------------------------------------------------------

def test_the_default_process_is_flow(repo):
    assert owner_gates.DEFAULT_PROCESS == "flow"
    assert owner_gates.process_mode({}) == "flow"
    assert owner_gates.process_mode(_state(repo)) == "flow"
    assert "process_policy" not in _state(repo)


def test_the_default_list_is_the_built_in_classes(repo):
    assert owner_gates.classes(_state(repo)) == (
        "production deployment",
        "publication",
        "real personal data",
        "irreversible or destructive act",
        "material change of scope, cost or architecture",
        "login-bound act or owner-held input",
        "start signal for a material plan",
    )


# ---------------------------------------------------------------------------
# Flow: nothing changes
# ---------------------------------------------------------------------------

def test_flow_block_without_a_class_appends_the_event_it_always_did(repo, capsys):
    block_run(repo, ITEM, "decision", actor=ACTOR, as_json=True, runner=FakeGit(remote_events=[]))

    [event] = _blocked_events(repo)
    assert event["payload"] == {"item_id": ITEM, "gate": "decision"}
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "owner_gate" not in printed
    item = _state(repo)["items"][ITEM]
    assert item["status"] == "blocked"
    assert "owner_gate" not in item


def test_flow_accepts_a_planted_invented_class_and_records_it(repo):
    block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate=INVENTED,
              runner=FakeGit(remote_events=[]))

    [event] = _blocked_events(repo)
    assert event["payload"]["owner_gate"] == INVENTED
    assert _state(repo)["items"][ITEM]["owner_gate"] == INVENTED


# ---------------------------------------------------------------------------
# Strict: a block for the owner must name a listed class
# ---------------------------------------------------------------------------

def test_strict_refuses_a_planted_invented_class(repo, capsys):
    _strict(repo)
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exc:
        block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate=INVENTED,
                  runner=FakeGit(remote_events=[]))

    assert exc.value.code == sync.EXIT_POLICY_REFUSED == 8
    assert _log_bytes(repo) == before
    err = capsys.readouterr().err
    listed = "; ".join(owner_gates.DEFAULT_CLASSES)
    assert err.strip() == (
        f"pinax: strict process: '{INVENTED}' is not an owner-gate class. "
        f"Give one of these with --owner-gate: {listed}. Nothing was appended."
    )


@pytest.mark.parametrize("gate", ["decision", "destructive"])
def test_strict_refuses_a_block_for_the_owner_with_no_class(repo, capsys, gate):
    _strict(repo)
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exc:
        block_run(repo, ITEM, gate, actor=ACTOR, runner=FakeGit(remote_events=[]))

    assert exc.value.code == 8
    assert _log_bytes(repo) == before
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["status"] == sync.POLICY_REFUSED
    assert report["appended"] is False


def test_the_refusal_is_one_plain_line_that_shows_the_list(repo, capsys):
    _strict(repo)
    with pytest.raises(SystemExit):
        block_run(repo, ITEM, "decision", actor=ACTOR, runner=FakeGit(remote_events=[]))

    err = [line for line in capsys.readouterr().err.splitlines() if line.strip()]
    assert len(err) == 1
    listed = "; ".join(owner_gates.DEFAULT_CLASSES)
    assert err[0] == (
        "pinax: strict process: a block with gate decision needs an owner-gate class. "
        f"Give one of these with --owner-gate: {listed}. Nothing was appended."
    )


def test_strict_accepts_a_listed_class_however_it_is_typed(repo):
    _strict(repo)
    block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate="  Production   DEPLOYMENT ",
              runner=FakeGit(remote_events=[]))

    [event] = _blocked_events(repo)
    assert event["payload"]["owner_gate"] == DEFAULT_CLASS
    assert _state(repo)["items"][ITEM]["owner_gate"] == DEFAULT_CLASS


@pytest.mark.parametrize("gate", ["scope", "proposal"])
def test_strict_does_not_require_a_class_on_a_gate_that_is_not_for_the_owner(repo, gate):
    _strict(repo)
    block_run(repo, ITEM, gate, actor=ACTOR, runner=FakeGit(remote_events=[]))

    [event] = _blocked_events(repo)
    assert event["payload"] == {"item_id": ITEM, "gate": gate}


def test_strict_checks_a_class_given_on_any_gate(repo):
    _strict(repo)
    with pytest.raises(SystemExit) as exc:
        block_run(repo, ITEM, "scope", actor=ACTOR, owner_gate=INVENTED,
                  runner=FakeGit(remote_events=[]))
    assert exc.value.code == 8


def test_a_strict_process_set_only_on_the_remote_already_binds(repo):
    remote_policy = mint_event(seq=2, ts="2026-09-01T09:02:00Z", actor="other@b",
                               etype="policy.process_set", payload={"process": "strict"})
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exc:
        block_run(repo, ITEM, "decision", actor=ACTOR,
                  runner=FakeGit(remote_events=[remote_policy]))

    assert exc.value.code == 8
    assert _log_bytes(repo) == before


# ---------------------------------------------------------------------------
# The policy settings
# ---------------------------------------------------------------------------

def test_the_last_process_event_wins(repo):
    run_process(repo, "strict", actor=ACTOR, runner=FakeGit(remote_events=[]))
    assert owner_gates.process_mode(_state(repo)) == "strict"
    run_process(repo, "flow", actor=ACTOR, runner=FakeGit(remote_events=[]))
    assert owner_gates.process_mode(_state(repo)) == "flow"

    block_run(repo, ITEM, "decision", actor=ACTOR, runner=FakeGit(remote_events=[]))
    assert _state(repo)["items"][ITEM]["status"] == "blocked"


def test_an_unknown_process_is_refused_before_anything_is_appended(repo):
    before = _log_bytes(repo)
    with pytest.raises(SystemExit) as exc:
        run_process(repo, "relaxed", actor=ACTOR, runner=FakeGit(remote_events=[]))
    assert exc.value.code == 1
    assert _log_bytes(repo) == before


def test_a_malformed_process_event_sets_nothing(repo):
    _seed(repo, 2, "2026-09-01T09:02:00Z", OWNER, "policy.process_set", {"process": "chaos"})
    assert owner_gates.process_mode(_state(repo)) == "flow"


def test_the_class_list_is_held_in_the_project_policy(repo):
    _strict(repo)
    run_owner_gates(repo, ["Customer money", "  vendor   contract "], actor=ACTOR,
                    runner=FakeGit(remote_events=[]))
    assert owner_gates.classes(_state(repo)) == ("Customer money", "vendor contract")

    with pytest.raises(SystemExit) as exc:
        block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate=DEFAULT_CLASS,
                  runner=FakeGit(remote_events=[]))
    assert exc.value.code == 8

    block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate="customer money",
              runner=FakeGit(remote_events=[]))
    assert _state(repo)["items"][ITEM]["owner_gate"] == "Customer money"


@pytest.mark.parametrize("given", [[], [""], ["publication", " Publication "]])
def test_an_empty_or_repeated_class_list_is_refused(repo, given):
    before = _log_bytes(repo)
    with pytest.raises(SystemExit) as exc:
        run_owner_gates(repo, given, actor=ACTOR, runner=FakeGit(remote_events=[]))
    assert exc.value.code == 1
    assert _log_bytes(repo) == before


def test_a_malformed_class_list_event_sets_nothing(repo):
    _seed(repo, 2, "2026-09-01T09:02:00Z", OWNER, "policy.owner_gate_classes_set",
          {"classes": ["fine", 7]})
    assert owner_gates.classes(_state(repo)) == owner_gates.DEFAULT_CLASSES


# ---------------------------------------------------------------------------
# The class on the item, the board and the item file
# ---------------------------------------------------------------------------

def test_the_board_and_the_item_file_show_the_class(repo):
    block_run(repo, ITEM, "decision", actor=ACTOR, owner_gate="publication",
              runner=FakeGit(remote_events=[]))

    with open(os.path.join(repo, ".ergon", "board.md"), encoding="utf-8") as fh:
        board = fh.read()
    assert f"- {ITEM} · First · gate=decision · owner gate: publication" in board
    with open(os.path.join(repo, ".ergon", "items", f"{ITEM}.md"), encoding="utf-8") as fh:
        page = fh.read()
    assert "\ngate: decision\nowner_gate: publication\n" in page


def test_a_block_without_a_class_shows_nothing_new(repo):
    block_run(repo, ITEM, "decision", actor=ACTOR, runner=FakeGit(remote_events=[]))

    with open(os.path.join(repo, ".ergon", "board.md"), encoding="utf-8") as fh:
        board = fh.read()
    assert f"- {ITEM} · First · gate=decision\n" in board
    with open(os.path.join(repo, ".ergon", "items", f"{ITEM}.md"), encoding="utf-8") as fh:
        assert "owner_gate" not in fh.read()


def test_a_later_block_without_a_class_clears_the_earlier_class(repo):
    _seed(repo, 2, "2026-09-01T09:02:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision", "owner_gate": "publication"})
    _seed(repo, 3, "2026-09-01T09:03:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision"})
    assert "owner_gate" not in _state(repo)["items"][ITEM]


@pytest.mark.parametrize(
    "etype, payload",
    [
        ("item.status_changed", {"item_id": ITEM, "status": "queued"}),
        ("item.completed", {"item_id": ITEM, "briefing": "done"}),
    ],
)
def test_an_item_that_leaves_blocked_drops_its_class(repo, etype, payload):
    _seed(repo, 2, "2026-09-01T09:02:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision", "owner_gate": "publication"})
    _seed(repo, 3, "2026-09-01T09:03:00Z", OWNER, etype, payload)

    state = _state(repo)
    item = state["items"][ITEM]
    assert item["status"] != "blocked"
    assert "owner_gate" not in item
    assert "owner_gate" not in render_item(ITEM, item, state)


def test_a_blocked_item_keeps_its_class_in_the_item_file(repo):
    _seed(repo, 2, "2026-09-01T09:02:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision", "owner_gate": "publication"})

    state = _state(repo)
    assert "\nowner_gate: publication\n" in render_item(ITEM, state["items"][ITEM], state)


# ---------------------------------------------------------------------------
# One owner for the rule's constants and one account of the policy command
# ---------------------------------------------------------------------------

def test_the_set_of_processes_has_one_owner():
    from pinax import event

    assert not hasattr(event, "_PROCESSES")
    assert event.PROCESSES is owner_gates.PROCESSES


def test_the_policy_module_docstring_describes_every_policy_event():
    import pinax.commands.policy as policy_mod

    doc = policy_mod.__doc__
    for etype in ("policy.claim_expiry_set", "policy.process_set",
                  "policy.owner_gate_classes_set"):
        assert etype in doc


# ---------------------------------------------------------------------------
# The on-demand check
# ---------------------------------------------------------------------------

def _seed_blocks(root, seq=3):
    _seed(root, seq, "2026-09-01T09:03:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision", "owner_gate": INVENTED})
    _seed(root, seq + 1, "2026-09-01T09:04:00Z", OWNER, "item.blocked",
          {"item_id": OTHER, "gate": "destructive"})


def test_the_check_reports_and_passes_under_flow(repo, capsys):
    _seed_blocks(repo)

    run_check(repo, as_json=True)

    report = json.loads(capsys.readouterr().out)
    assert report["process"] == "flow"
    assert report["ok"] is True
    assert [(e["id"], e["owner_gate"]) for e in report["unclassed"]] == [
        (ITEM, INVENTED), (OTHER, ""),
    ]


def test_the_check_fails_under_strict(repo, capsys):
    _strict(repo)
    _seed_blocks(repo)

    with pytest.raises(SystemExit) as exc:
        run_check(repo)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert ITEM in out and OTHER in out
    assert f"'{INVENTED}' is not on the list" in out
    assert DEFAULT_CLASS in out


def test_the_check_passes_under_strict_when_every_owner_block_is_classed(repo, capsys):
    _strict(repo)
    _seed(repo, 3, "2026-09-01T09:03:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision", "owner_gate": DEFAULT_CLASS})
    _seed(repo, 4, "2026-09-01T09:04:00Z", OWNER, "item.blocked",
          {"item_id": OTHER, "gate": "scope"})

    run_check(repo, as_json=True)

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "process": "strict",
        "classes": list(owner_gates.DEFAULT_CLASSES),
        "unclassed": [],
        "ok": True,
    }


def test_the_check_ignores_an_item_no_longer_blocked(repo, capsys):
    _strict(repo)
    _seed(repo, 3, "2026-09-01T09:03:00Z", OWNER, "item.blocked",
          {"item_id": ITEM, "gate": "decision"})
    _seed(repo, 4, "2026-09-01T09:04:00Z", OWNER, "item.status_changed",
          {"item_id": ITEM, "status": "queued"})

    run_check(repo, as_json=True)

    assert json.loads(capsys.readouterr().out)["unclassed"] == []


# ---------------------------------------------------------------------------
# The command line carries the option through
# ---------------------------------------------------------------------------

def test_the_cli_passes_owner_gate_to_block(monkeypatch):
    import pinax.__main__ as cli
    import pinax.commands.block as block_mod

    seen = {}
    monkeypatch.setattr(cli, "_find_repo_root", lambda pin=None: "/repo")
    monkeypatch.setattr(block_mod, "run", lambda **kw: seen.update(kw))

    cli.main(["block", ITEM, "--gate", "decision", "--owner-gate", "publication"])

    assert seen["owner_gate"] == "publication"
    assert seen["gate"] == "decision"
