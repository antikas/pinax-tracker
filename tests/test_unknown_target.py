"""
An event about something that does not exist is refused before it is appended.

A command such as `pinax claim zzz-` names no item, so it must append
nothing. The rule has one owner, pinax.targets, applied by pinax.sync.run_sequence over the
union fold for every command that publishes an event.

Everything runs with the substituted git runner tests/test_sync_sequence owns,
so there is no git process and no network. One family per command, each with
two proofs:

1.  An unknown id is refused with a non-zero exit and a message naming the
    id, before anything is appended: the log is byte-identical afterwards
    and the sequence made no commit and no push.
2.  A valid id still works: exactly one event of the command's type is
    appended.

A dependency edge is checked at each end. Plus the structural proofs: an item
that exists only on the remote is accepted (the check reads the union, not the
local log alone); an annulment names an event, not an item; item creation is
unaffected; every event type the fold handles is either covered by the rule
or exempt on purpose; and no module other than pinax.targets decides what an
unknown item is.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from pinax import sync
from pinax.commands.add import run as add_run
from pinax.commands.annul import run as annul_run
from pinax.commands.block import run as block_run
from pinax.commands.claim import run as claim_run
from pinax.commands.dep import run_add as dep_add_run
from pinax.commands.dep import run_rm as dep_rm_run
from pinax.commands.done import run as done_run
from pinax.commands.note import run as note_run
from pinax.commands.park import run as park_run
from pinax.commands.priority import run as priority_run
from pinax.commands.release import run as release_run
from pinax.commands.status_cmd import run as status_run
from pinax.event import mint_event
from pinax.fold import _HANDLERS
from pinax.targets import EVENT_REFERENCES, ITEM_REFERENCES

from test_sync_sequence import FakeGit, _log_dir, _seed, _shard_events

ACTOR = "coordinator@host-a"
OWNER = "worker@alpha"
ITEM = "pnx-a1b2"
OTHER = "pnx-c3d4"
GHOST = "zzz-"          # a bare prefix: the shape of id most easily mistyped
GHOST_2 = "pnx-nope"
NOTE_REF = "docs/some-note.md"


@pytest.fixture()
def repo(tmp_path):
    """A tracker log holding two items, one of them with a live claim."""
    root = str(tmp_path)
    os.makedirs(_log_dir(root))
    _seed(root, 0, "2026-09-01T09:00:00Z", OWNER, "item.created",
          {"item_id": ITEM, "title": "First", "prefix": "pnx"})
    _seed(root, 1, "2026-09-01T09:01:00Z", OWNER, "item.created",
          {"item_id": OTHER, "title": "Second", "prefix": "pnx"})
    _seed(root, 2, "2026-09-01T09:30:00Z", OWNER, "item.claimed", {"item_id": ITEM})
    return root


@pytest.fixture()
def briefing(tmp_path):
    path = tmp_path / "briefing.md"
    path.write_text("the work record", encoding="utf-8")
    return str(path)


def _log_bytes(repo_root: str) -> dict:
    """Every shard in the log, by name, as raw bytes."""
    log_dir = _log_dir(repo_root)
    out = {}
    for name in sorted(os.listdir(log_dir)):
        with open(os.path.join(log_dir, name), "rb") as fh:
            out[name] = fh.read()
    return out


# One entry per command family: a runner and the event type it appends. Each
# runner takes the repo, the id to aim at, the git double and the briefing.

def _claim(repo, target, git, briefing):
    claim_run(repo, target, actor=ACTOR, runner=git)


def _done(repo, target, git, briefing):
    done_run(repo, target, briefing, actor=ACTOR, runner=git)


def _block(repo, target, git, briefing):
    block_run(repo, target, "scope", actor=ACTOR, runner=git)


def _park(repo, target, git, briefing):
    park_run(repo, target, "later", actor=ACTOR, runner=git)


def _release(repo, target, git, briefing):
    release_run(repo, target, "worker stopped", actor=ACTOR, runner=git)


def _priority(repo, target, git, briefing):
    priority_run(repo, target, "3", actor=ACTOR, runner=git)


def _priority_bump(repo, target, git, briefing):
    priority_run(repo, target, "bump", actor=ACTOR, runner=git)


def _note(repo, target, git, briefing):
    note_run(repo, target, NOTE_REF, None, actor=ACTOR, runner=git)


def _dep_add_from(repo, target, git, briefing):
    dep_add_run(repo, target, OTHER, actor=ACTOR, runner=git)


def _dep_add_to(repo, target, git, briefing):
    dep_add_run(repo, ITEM, target, actor=ACTOR, runner=git)


def _dep_rm_from(repo, target, git, briefing):
    dep_rm_run(repo, target, OTHER, actor=ACTOR, runner=git)


def _dep_rm_to(repo, target, git, briefing):
    dep_rm_run(repo, ITEM, target, actor=ACTOR, runner=git)


# name -> (runner, event type appended, id a valid run aims at)
FAMILIES = {
    "claim": (_claim, "item.claimed", OTHER),
    "done": (_done, "item.completed", ITEM),
    "block": (_block, "item.blocked", ITEM),
    "park": (_park, "item.parked", ITEM),
    "release": (_release, "item.claim_released", ITEM),
    "priority": (_priority, "item.priority_set", ITEM),
    "priority-bump": (_priority_bump, "item.priority_set", ITEM),
    "note-add": (_note, "note.added", ITEM),
    "dep-add-from": (_dep_add_from, "dep.added", ITEM),
    "dep-add-to": (_dep_add_to, "dep.added", OTHER),
    "dep-rm-from": (_dep_rm_from, "dep.removed", ITEM),
    "dep-rm-to": (_dep_rm_to, "dep.removed", OTHER),
}


# ---------------------------------------------------------------------------
# 1 and 2. Every command refuses an unknown id and still accepts a real one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(FAMILIES))
@pytest.mark.parametrize("ghost", [GHOST, GHOST_2])
def test_an_unknown_item_is_refused_and_the_log_is_byte_identical(
    repo, briefing, capsys, name, ghost
):
    run = FAMILIES[name][0]
    git = FakeGit(remote_events=[])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        run(repo, ghost, git, briefing)

    assert exited.value.code not in (0, None)
    captured = capsys.readouterr()
    assert repr(ghost) in captured.err, "the refusal must name the unknown id"
    assert _log_bytes(repo) == before
    assert not git.named("commit")
    assert not git.named("push")


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_a_valid_item_still_works(repo, briefing, name):
    run, etype, target = FAMILIES[name]
    git = FakeGit(remote_events=[])

    run(repo, target, git, briefing)

    assert [event["type"] for event in _shard_events(repo, ACTOR)] == [etype]


def test_the_refusal_is_a_machine_readable_report_with_exit_one(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, GHOST, actor=ACTOR, runner=git)

    assert exited.value.code == 1
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report["status"] == "unknown_target"
    assert report["appended"] is False
    assert report["event_type"] == "item.claimed"
    assert GHOST in report["message"]


def test_a_bare_prefix_id_is_refused(repo, capsys):
    """A claim on a bare prefix with no item behind it appends nothing."""
    git = FakeGit(remote_events=[])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, "zzz-", actor=ACTOR, runner=git)

    assert exited.value.code == 1
    assert "'zzz-'" in capsys.readouterr().err
    assert _log_bytes(repo) == before


# ---------------------------------------------------------------------------
# The status setter appends without the sequence and asks the same rule
# ---------------------------------------------------------------------------

def test_the_status_setter_refuses_an_unknown_item(repo, capsys):
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        status_run(repo, GHOST, "done", actor=ACTOR)

    assert exited.value.code == 1
    assert repr(GHOST) in capsys.readouterr().err
    assert _log_bytes(repo) == before


def test_the_status_setter_still_accepts_a_real_item(repo):
    status_run(repo, ITEM, "done", actor=ACTOR)

    assert [e["type"] for e in _shard_events(repo, ACTOR)] == ["item.status_changed"]


# ---------------------------------------------------------------------------
# The rule reads the union fold, not the local log alone
# ---------------------------------------------------------------------------

def _remote_creation(item_id: str) -> dict:
    return mint_event(
        seq=7, ts="2026-09-02T10:00:00Z", actor="worker@beta", etype="item.created",
        payload={"item_id": item_id, "title": "Only on the remote", "prefix": "pnx"},
    )


def test_an_item_that_exists_only_on_the_remote_is_accepted(repo):
    git = FakeGit(remote_events=[_remote_creation("pnx-r3m0")])

    claim_run(repo, "pnx-r3m0", actor=ACTOR, runner=git)

    assert [e["type"] for e in _shard_events(repo, ACTOR)] == ["item.claimed"]


def test_an_unknown_item_is_refused_even_when_the_remote_holds_other_items(repo, capsys):
    git = FakeGit(remote_events=[_remote_creation("pnx-r3m0")])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit):
        claim_run(repo, GHOST, actor=ACTOR, runner=git)

    assert _log_bytes(repo) == before
    assert not git.named("commit")


def test_offline_mode_refuses_too(repo):
    git = FakeGit(remote_events=[])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        park_run(repo, GHOST, "later", actor=ACTOR, offline=True, runner=git)

    assert exited.value.code == 1
    assert _log_bytes(repo) == before


# ---------------------------------------------------------------------------
# An annulment names an event, never an item
# ---------------------------------------------------------------------------

def test_an_annulment_of_an_event_that_exists_only_on_the_remote_is_accepted(repo):
    remote_event = _remote_creation("pnx-r3m0")
    git = FakeGit(remote_events=[remote_event])

    annul_run(repo, remote_event["id"], "junk", actor=ACTOR, runner=git)

    assert [e["type"] for e in _shard_events(repo, ACTOR)] == ["event.annulled"]


def test_an_annulment_of_a_phantom_event_is_refused(repo, capsys):
    git = FakeGit(remote_events=[])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        annul_run(repo, "an-id-that-never-appears", "junk", actor=ACTOR, runner=git)

    assert exited.value.code == 1
    assert "'an-id-that-never-appears'" in capsys.readouterr().err
    assert _log_bytes(repo) == before
    assert not git.named("commit")


def test_an_item_id_is_not_an_event_id(repo, capsys):
    git = FakeGit(remote_events=[])
    before = _log_bytes(repo)

    with pytest.raises(SystemExit) as exited:
        annul_run(repo, ITEM, "junk", actor=ACTOR, runner=git)

    assert exited.value.code == 1
    assert repr(ITEM) in capsys.readouterr().err
    assert _log_bytes(repo) == before


# ---------------------------------------------------------------------------
# Item creation and repository-level events are unaffected
# ---------------------------------------------------------------------------

def test_creating_an_item_is_unaffected(repo):
    git = FakeGit(remote_events=[])

    add_run(repo, "A brand new item", "pnx", actor=ACTOR, runner=git)

    appended = _shard_events(repo, ACTOR)
    assert [e["type"] for e in appended] == ["item.created"]
    assert appended[0]["payload"]["item_id"].startswith("pnx-")


def test_a_repository_level_event_is_unaffected(repo):
    git = FakeGit(remote_events=[])

    outcome = sync.run_sequence(
        repo,
        event_type="policy.claim_expiry_set",
        payload={"hours": 8.0},
        actor=ACTOR,
        ts="2026-09-02T10:00:00Z",
        item_id="",
        runner=git,
    )

    assert outcome.status != sync.UNKNOWN_TARGET
    assert outcome.event is not None


# ---------------------------------------------------------------------------
# One owner, and no event type slips past it unseen
# ---------------------------------------------------------------------------

# Event types the fold handles that reference nothing the rule checks, each
# for a stated reason.
_EXEMPT = {
    "ergon.created": "repository-level",
    "phase.opened": "repository-level",
    "item.created": "creates the id it carries",
    "policy.claim_expiry_set": "repository-level",
    "registry.repo_added": "names a repository, not an item",
    "registry.repo_removed": "names a repository, not an item",
}


def test_every_event_type_the_fold_handles_is_covered_or_exempt_on_purpose():
    covered = set(ITEM_REFERENCES) | set(EVENT_REFERENCES)
    unclassified = set(_HANDLERS) - covered - set(_EXEMPT)
    assert not unclassified, (
        f"event types {sorted(unclassified)} are neither covered by pinax.targets "
        "nor exempt here: a command that appends one would skip the rule"
    )
    assert not (covered & set(_EXEMPT))
    assert covered <= set(_HANDLERS), "the rule lists an event type the fold does not handle"


def test_only_pinax_targets_decides_what_an_unknown_item_is():
    """
    No command or sequence module carries its own existence check on an item
    id: the 'unknown item' wording and the membership test against the fold's
    items live in pinax/targets.py alone. (pinax.fold's own warnings are the
    fold ignoring a bad line it meets in a log, a different job.)
    """
    pinax_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pinax"
    )
    membership = re.compile(r"\b(?:item_id|from_id|to_id)\s+not\s+in\s+items\b")
    hits = []
    for folder, _dirs, files in os.walk(pinax_dir):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, pinax_dir).replace(os.sep, "/")
            if rel in ("targets.py", "fold.py", "projection.py", "commands/verify.py"):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
            if membership.search(text):
                hits.append(rel)
            if "unknown item '" in text or 'unknown item "' in text:
                hits.append(rel + " (message)")
    assert not hits, "a second owner of the unknown-item rule: " + ", ".join(hits)
