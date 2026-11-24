"""
Concurrent claims, the clock rule, and the projection after a pull.

Two levels, one subject. The first section drives the real command entry
points with the substituted git runner tests/test_sync_sequence.py owns, so
exit 3 and exit 6 are proved with no git process and no network. The second
section carries the deep marker and proves the same rules against real git:
a bare hub, clones beside it, one item, and two machines that reach for it.

Covered here:
1.  A claim the fold reports superseded after its push ends with exit 3,
    and its report names the winning event and actor; the winner ends with
    exit 0. A claim on an item another writer is already known to hold ends
    with exit 3 and writes nothing; a writer's own repeat appends nothing.
2.  A claim whose timestamp sits behind published history by more than the
    tolerance ends with exit 6 before anything is minted; the tolerance can
    be raised for one crossing; an annulled future-dated event no longer
    holds the maximum; only a claim carries the guard.
3.  The pull between push attempts regenerates the projection from the
    merged log and commits it, with and without a conflict in it, and a
    conflict outside it still stops the retry.
4.  On a bare hub: two clones claim one item, exactly one wins, the loser
    exits 3 naming the winner, both folds agree afterwards and pinax verify
    passes in both; and a clone whose clock is behind the hub is refused
    until the future-dated event is annulled.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import finalise_events, fold_events, read_events, read_raw_events
from pinax.projection import regenerate
from pinax.sync import GitResult
from tests.test_sync_sequence import FakeGit, _log_dir, _seed, _shard_events

from pinax.commands.add import run as add_run
from pinax.commands.annul import run as annul_run
from pinax.commands.claim import run as claim_run
from pinax.commands.decide import run as decide_run
from pinax.commands.done import run as done_run


ACTOR = "builder@alpha"
RIVAL = "auditor@beta"
ITEM_ID = "pnx-a1b2"
MINTED_TS = "2026-09-02T10:00:00Z"
FAR_AHEAD_TS = "2027-01-01T00:00:00Z"

_OK = GitResult(returncode=0)


def _fail(text: str) -> GitResult:
    return GitResult(returncode=1, stderr=text)


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


@pytest.fixture()
def repo(tmp_path):
    """A tracker log holding one item, with no git repository around it."""
    root = str(tmp_path)
    os.makedirs(_log_dir(root))
    _seed(
        root,
        seq=0,
        ts="2026-09-01T09:00:00Z",
        actor=RIVAL,
        etype="item.created",
        payload={"item_id": ITEM_ID, "title": "Claimed from two machines", "prefix": "pnx"},
    )
    return root


@pytest.fixture()
def at_a_fixed_moment(monkeypatch):
    """Pin the timestamp a command mints, at its one owner."""
    for module in ("pinax.doctor", "pinax.commands.add", "pinax.commands.decide"):
        monkeypatch.setattr(module + ".utc_now_iso", lambda: MINTED_TS)
    return MINTED_TS


class RacingGit(FakeGit):
    """A remote another machine's events reach between the fetch and the push."""

    def __init__(self, *, arriving: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self.arriving = arriving

    def run(self, *args, env=None):
        if args[0] == "push":
            self.remote_events = list(self.remote_events or []) + self.arriving
            self.arriving = []
        if args[0] == "rev-list":
            self.calls.append(args)
            return GitResult(returncode=0, stdout="1\n")
        return super().run(*args, env=env)


def _remote_claim(ts: str, actor: str = RIVAL) -> dict:
    return mint_event(
        seq=1, ts=ts, actor=actor, etype="item.claimed", payload={"item_id": ITEM_ID}
    )


def _remote_creation(ts: str, item_id: str, actor: str = RIVAL) -> dict:
    return mint_event(
        seq=1,
        ts=ts,
        actor=actor,
        etype="item.created",
        payload={"item_id": item_id, "title": "Recorded elsewhere", "prefix": "pnx"},
    )


def _tombstone(target: dict, ts: str, actor: str = RIVAL) -> dict:
    return mint_event(
        seq=2,
        ts=ts,
        actor=actor,
        etype="event.annulled",
        payload={
            "target_id": target["id"],
            "reason": "the timestamp came from a machine whose clock ran ahead",
        },
    )


# ---------------------------------------------------------------------------
# 1. The fold decides how a claim ends
# ---------------------------------------------------------------------------

def test_a_superseded_claim_exits_three_naming_the_winner(repo, at_a_fixed_moment, capsys):
    """
    The rival's claim reaches the remote after this command's fetch, so this
    one is appended, committed and pushed. The fold over the pushed remote state finds an earlier claim,
    and that fold, not the push, ends the command.
    """
    # The winner sits inside the claim expiry policy window, so the subject
    # here stays supersession rather than a claim the fold has expired.
    winner = _remote_claim("2026-09-02T09:30:00Z")
    git = RacingGit(arriving=[winner])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    # The event really was published: the exit describes the fold, not a
    # refusal to record anything.
    assert git.named("push")
    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.claimed"]

    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "claim_superseded"
    assert report["winner_event_id"] == winner["id"]
    assert report["winner_actor"] == RIVAL
    assert report["superseded_event_id"] == appended[0]["id"]
    assert report["superseded_actor"] == ACTOR
    assert report["pushed"] is True
    assert report["published"] is True
    assert RIVAL in captured.err


def test_a_fold_warning_reaches_the_operator_once(repo, at_a_fixed_moment, capsys):
    """
    A rejected push makes the sequence fold again after the pull, and once
    more over the pushed remote state. The warning those folds keep raising is one line
    for the operator to read, not one line per fold.
    """
    # The winner sits inside the claim expiry policy window, so the subject
    # here stays supersession rather than a claim the fold has expired.
    winner = _remote_claim("2026-09-02T09:30:00Z")
    git = RacingGit(arriving=[winner], pushes=[_fail("rejected: fetch first"), _OK])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert len(git.named("push")) == 2
    spoken = capsys.readouterr().err.splitlines()
    warnings = [line for line in spoken if "claim.superseded" in line]
    assert len(warnings) == 1, warnings


def test_the_earlier_claim_wins_and_ends_with_zero(repo, at_a_fixed_moment, capsys):
    """
    The same sequence, with this machine holding the earlier timestamp: the
    fold names the other claim the superseded one, so this command ends
    normally. The rival's claim is three seconds later, inside the default
    tolerance, so the clock rule has nothing to say about it.
    """
    later = _remote_claim("2026-09-02T10:00:03Z")
    git = RacingGit(arriving=[later])

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert git.named("push")
    result = _last_json(capsys.readouterr().out)
    assert result["item_id"] == ITEM_ID
    assert result["ts"] == at_a_fixed_moment


def test_a_completion_is_not_ended_by_someone_elses_superseded_claim(
    repo, tmp_path, capsys
):
    """
    Only the sequence's own event decides its exit. A superseded claim
    belonging to another actor is never this command's outcome, and this
    command does not repeat its warning.
    """
    briefing = tmp_path / "briefing.md"
    briefing.write_text("the work record", encoding="utf-8")
    early = _remote_claim("2026-09-01T09:10:00Z", actor="operator@gamma")
    late = _remote_claim("2026-09-01T09:20:00Z", actor="reviewer@delta")
    git = FakeGit(remote_events=[early, late])

    done_run(repo, ITEM_ID, str(briefing), actor=ACTOR, runner=git)

    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]
    assert "claim.superseded" not in capsys.readouterr().err


@pytest.mark.parametrize("held_at", ["2026-09-02T09:30:00Z", "2026-09-02T10:00:03Z"])
def test_a_claim_on_a_held_item_is_refused_and_appends_nothing(
    repo, at_a_fixed_moment, capsys, held_at
):
    holder = _remote_claim(held_at)
    git = FakeGit(remote_events=[holder])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert _shard_events(repo, ACTOR) == []
    assert not git.named("commit")
    assert not git.named("push")
    report = _last_json(capsys.readouterr().out)
    assert report["sync_status"] == report["status"] == "claim_held"
    assert report["published"] is report["appended"] is False
    assert report["holder_actor"] == RIVAL
    assert report["holder_event_id"] == holder["id"]
    assert report["claimed_at"] == held_at
    assert report["message"] == (
        f"item {ITEM_ID} is already claimed by {RIVAL} since {held_at}, "
        "so nothing was appended"
    )


def test_the_holder_is_named_before_the_clock_rule(repo, at_a_fixed_moment, capsys):
    git = FakeGit(remote_events=[_remote_claim("2026-09-02T11:00:00Z")])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert _last_json(capsys.readouterr().out)["status"] == "claim_held"


def test_a_claim_held_only_in_the_local_log_is_refused(repo, at_a_fixed_moment, capsys):
    _seed(repo, 1, "2026-09-02T09:30:00Z", RIVAL, "item.claimed", {"item_id": ITEM_ID})
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert not git.named("commit")
    assert _last_json(capsys.readouterr().out)["status"] == "claim_held"


@pytest.mark.parametrize("holder", [RIVAL, ACTOR])
def test_a_claim_past_the_holders_deadline_is_a_fresh_claim(
    repo, at_a_fixed_moment, capsys, holder
):
    """No event is past the deadline until this claim is: it ends the old one."""
    git = FakeGit(remote_events=[_remote_claim("2026-09-01T09:30:00Z", actor=holder)])

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert _last_json(capsys.readouterr().out)["sync_status"] == "pushed"
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.claimed"]


@pytest.mark.parametrize("held_by", [ACTOR, "builder@Alpha"])
def test_a_repeat_of_a_published_claim_appends_nothing(
    repo, at_a_fixed_moment, capsys, held_by
):
    held = _remote_claim("2026-09-02T09:30:00Z", actor=held_by)
    git = FakeGit(remote_events=[*_shard_events(repo, RIVAL), held])

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    result = _last_json(capsys.readouterr().out)
    assert result["sync_status"] == "already_claimed"
    assert result["published"] is True
    assert result["appended"] is False
    assert (result["event_id"], result["ts"]) == (held["id"], held["ts"])
    assert _shard_events(repo, ACTOR) == []
    assert not git.named("commit")
    assert not git.named("push")


def test_a_waiting_claim_that_lost_meanwhile_exits_three(repo, at_a_fixed_moment, capsys):
    waiting = _seed(
        repo, 1, "2026-09-02T09:40:00Z", ACTOR, "item.claimed", {"item_id": ITEM_ID}
    )
    winner = _remote_claim("2026-09-02T09:30:00Z")
    git = RacingGit(arriving=[winner])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "claim_superseded"
    assert report["published"] is True
    assert report["winner_actor"] == RIVAL
    _waiting, annulment, fresh = _shard_events(repo, ACTOR)
    assert annulment["payload"]["target_id"] == waiting["id"]
    assert report["annulled"]["target_id"] == waiting["id"]
    assert report["superseded_event_id"] == fresh["id"]
    assert captured.err.count("claim.superseded:") == 1


def test_a_waiting_claim_released_and_retaken_meanwhile_exits_three(
    repo, at_a_fixed_moment, capsys
):
    _seed(repo, 1, "2026-09-02T09:30:00Z", ACTOR, "item.claimed", {"item_id": ITEM_ID})
    release = mint_event(
        seq=2, ts="2026-09-02T09:40:00Z", actor=RIVAL, etype="item.claim_released",
        payload={"item_id": ITEM_ID, "reason": "retaken"},
    )
    retaken = _remote_claim("2026-09-02T09:50:00Z")
    git = RacingGit(arriving=[release, retaken])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    report = _last_json(capsys.readouterr().out)
    assert report["status"] == "claim_superseded"
    assert report["winner_event_id"] == retaken["id"]


def test_a_waiting_claim_off_the_default_branch_exits_four(
    repo, at_a_fixed_moment, capsys
):
    _seed(repo, 1, "2026-09-02T09:30:00Z", ACTOR, "item.claimed", {"item_id": ITEM_ID})
    git = FakeGit(remote_events=[], head_branch="feature")

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 4
    assert _last_json(capsys.readouterr().out)["published"] is False
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == [
        "item.claimed", "event.annulled", "item.claimed",
    ]


WAITING_TS = "2026-09-02T09:30:00Z"
RIVAL_TS = "2026-09-02T09:40:00Z"


def _waiting_claim(repo: str, item_id: str = ITEM_ID) -> dict:
    return _seed(repo, 1, WAITING_TS, ACTOR, "item.claimed", {"item_id": item_id})


def _annulled_by(events: list[dict]) -> list[str]:
    return [
        event["payload"]["target_id"] for event in events
        if event["type"] == "event.annulled"
    ]


def _hub_state(repo: str, git: FakeGit) -> dict:
    return fold_events(finalise_events(
        read_raw_events(_log_dir(repo)) + [dict(event) for event in git.remote_events]
    ))


def _annulled_line(claim: dict, holder: str = RIVAL) -> str:
    return (
        f"pinax: the waiting claim {claim['id']} on {ITEM_ID} was annulled, "
        f"because {holder} already holds the item on the remote"
    )


def test_a_claim_does_not_take_a_held_item_with_a_waiting_claim(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    rival = _remote_claim(RIVAL_TS)
    git = RacingGit(arriving=[], remote_events=[rival])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert not git.named("push")
    assert not git.named("commit")
    shard = _shard_events(repo, ACTOR)
    assert [event["type"] for event in shard] == ["item.claimed", "event.annulled"]
    assert shard[1]["payload"] == {
        "target_id": waiting["id"],
        "reason": "the event could not be published, so it never took effect",
    }
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == report["sync_status"] == "claim_held"
    assert report["published"] is False
    assert report["holder_actor"] == RIVAL
    assert report["holder_event_id"] == rival["id"]
    assert report["annulled"]["target_id"] == waiting["id"]
    assert report["annulled"]["target_ids"] == [waiting["id"]]
    assert report["message"] == (
        f"item {ITEM_ID} is already claimed by {RIVAL} since {RIVAL_TS}, so no "
        f"claim was appended; the waiting claim {waiting['id']} was annulled"
    )
    assert _annulled_line(waiting) in captured.err.splitlines()


def test_the_annulment_of_a_waiting_claim_cannot_be_annulled(
    repo, at_a_fixed_moment, capsys
):
    waiting = _seed(
        repo, 1, WAITING_TS, "builder@Alpha", "item.claimed", {"item_id": ITEM_ID}
    )
    git = RacingGit(arriving=[], remote_events=[_remote_claim(RIVAL_TS)])
    with pytest.raises(SystemExit):
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)
    annulment = next(
        event for event in read_events(_log_dir(repo)) if event["type"] == "event.annulled"
    )
    assert annulment["payload"]["target_id"] == waiting["id"]
    capsys.readouterr()

    with pytest.raises(SystemExit) as exited:
        annul_run(repo, annulment["id"], "undo it", actor=RIVAL, as_json=True, runner=git)

    assert exited.value.code == 1
    assert _last_json(capsys.readouterr().out)["status"] == "final_annulment"


def test_a_clock_refusal_names_the_waiting_claim_it_annulled(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    git = RacingGit(arriving=[], remote_events=[_remote_creation("2026-09-02T11:00:00Z", "pnx-c3d4")])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 6
    report = _last_json(capsys.readouterr().out)
    assert report["annulled"]["target_id"] == waiting["id"]
    assert "no claim was appended" in report["message"]
    assert _annulled_by(_shard_events(repo, ACTOR)) == [waiting["id"]]


def test_a_published_claim_is_a_repeat_beside_a_waiting_one(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    held = _remote_claim(RIVAL_TS, actor=ACTOR)
    git = RacingGit(arriving=[], remote_events=[*_shard_events(repo, RIVAL), held])

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    result = _last_json(capsys.readouterr().out)
    assert result["sync_status"] == "already_claimed"
    assert result["event_id"] == held["id"]
    assert result["annulled"]["target_id"] == waiting["id"]
    assert not git.named("push")


def test_another_write_does_not_take_a_held_item_with_a_waiting_claim(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    git = FakeGit(remote_events=[_remote_claim(RIVAL_TS)])

    add_run(repo, "T", actor=ACTOR, as_json=True, runner=git)

    captured = capsys.readouterr()
    assert _last_json(captured.out)["sync_status"] == "pushed"
    assert len(git.named("commit")) == len(git.named("push")) == 1
    assert _annulled_by(_shard_events(repo, ACTOR)) == [waiting["id"]]
    assert captured.err.splitlines().count(_annulled_line(waiting)) == 1
    state = _hub_state(repo, git)
    assert state["items"][ITEM_ID]["owner"] == RIVAL
    assert state["claim_superseded"] == []


def test_a_rival_arriving_at_the_first_push_keeps_the_item(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    git = RacingGit(
        arriving=[_remote_claim(RIVAL_TS)],
        remote_events=[],
        pushes=[_fail("rejected: fetch first"), _OK],
    )

    add_run(repo, "T", actor=ACTOR, as_json=True, runner=git)

    captured = capsys.readouterr()
    assert _last_json(captured.out)["sync_status"] == "pushed"
    order = [call[0] for call in git.calls if call[0] in ("commit", "push", "merge")]
    assert order == ["commit", "push", "merge", "commit", "push"]
    assert _annulled_by(_shard_events(repo, ACTOR)) == [waiting["id"]]
    assert captured.err.splitlines().count(_annulled_line(waiting)) == 1
    state = _hub_state(repo, git)
    assert state["items"][ITEM_ID]["owner"] == RIVAL
    assert state["claim_superseded"] == []


def test_a_claim_annuls_a_waiting_claim_on_another_held_item(
    repo, at_a_fixed_moment, capsys
):
    _seed(
        repo, 1, "2026-09-01T09:01:00Z", RIVAL, "item.created",
        {"item_id": "pnx-c3d4", "title": "Free", "prefix": "pnx"},
    )
    waiting = _waiting_claim(repo)
    git = FakeGit(remote_events=[_remote_claim(RIVAL_TS)])

    claim_run(repo, "pnx-c3d4", actor=ACTOR, as_json=True, runner=git)

    captured = capsys.readouterr()
    result = _last_json(captured.out)
    assert result["sync_status"] == "pushed"
    assert "annulled" not in result
    assert _annulled_line(waiting) in captured.err.splitlines()
    assert _hub_state(repo, git)["items"][ITEM_ID]["owner"] == RIVAL


@pytest.mark.parametrize("remote", ["free", "ended", "feature"])
def test_another_write_leaves_a_waiting_claim_that_takes_nothing(
    repo, at_a_fixed_moment, capsys, remote
):
    waiting = _waiting_claim(repo)
    events = {
        "free": [],
        "ended": [
            _remote_claim("2026-09-01T09:30:00Z"),
            _remote_creation("2026-09-02T09:31:00Z", "pnx-c3d4"),
        ],
        "feature": [_remote_claim(RIVAL_TS)],
    }[remote]
    git = FakeGit(
        remote_events=events, head_branch="feature" if remote == "feature" else "main"
    )

    add_run(repo, "T", actor=ACTOR, as_json=True, runner=git)

    assert _annulled_by(_shard_events(repo, ACTOR)) == []
    assert waiting["id"] not in capsys.readouterr().err


class SeparateHead(RacingGit):
    """A runner whose HEAD holds its own events, ahead of the remote's."""

    def __init__(self, *, head_events: list[dict], **kwargs) -> None:
        super().__init__(arriving=[], **kwargs)
        self.head_events = head_events

    def raw_events_at_ref(self, ref: str):
        if ref == "HEAD":
            return [dict(event) for event in self.head_events]
        return super().raw_events_at_ref(ref)


def test_a_repeat_commits_an_annulment_head_lacks_before_it_pushes(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    _seed(
        repo, 2, "2026-09-02T09:35:00Z", ACTOR, "item.decided",
        {"item_id": ITEM_ID, "option": "a", "label": "A", "effect": "release", "reason": "r"},
    )
    head = read_events(_log_dir(repo))
    _seed(
        repo, 3, "2026-09-02T09:50:00Z", ACTOR, "event.annulled",
        {"target_id": waiting["id"],
         "reason": "the event could not be published, so it never took effect"},
    )
    git = SeparateHead(head_events=head, remote_events=[_remote_claim(RIVAL_TS)])

    decide_run(
        repo, ITEM_ID, option="a", label="A", effect="release", reason="r",
        actor=ACTOR, as_json=True, runner=git,
    )

    assert _last_json(capsys.readouterr().out)["appended"] is False
    order = [call[0] for call in git.calls if call[0] in ("commit", "push")]
    assert order == ["commit", "push"]
    assert _annulled_by(_shard_events(repo, ACTOR)) == [waiting["id"]]


def test_a_waiting_claim_ended_meanwhile_leaves_the_fresh_one_holding(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    rival = _remote_claim("2026-09-02T09:50:00Z")
    release = mint_event(
        seq=2, ts="2026-09-02T09:55:00Z", actor=RIVAL, etype="item.claim_released",
        payload={"item_id": ITEM_ID, "reason": "not now"},
    )
    git = RacingGit(
        arriving=[rival, release],
        remote_events=[],
        pushes=[_fail("rejected: fetch first"), _OK],
    )

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    result = _last_json(capsys.readouterr().out)
    assert result["sync_status"] == "pushed"
    assert result["event_id"] != waiting["id"]
    item = _hub_state(repo, git)["items"][ITEM_ID]
    assert item["owner"] == ACTOR
    assert item["claim_event_id"] == result["event_id"]


def test_a_claim_released_while_it_is_published_exits_three(
    repo, at_a_fixed_moment, capsys
):
    release = mint_event(
        seq=2, ts="2026-09-02T10:00:02Z", actor=RIVAL, etype="item.claim_released",
        payload={"item_id": ITEM_ID, "reason": "not now"},
    )
    git = RacingGit(arriving=[release], remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    report = _last_json(capsys.readouterr().out)
    assert report["status"] == "claim_superseded"
    assert report["published"] is True
    assert report["winner_actor"] is report["winner_event_id"] is None
    assert "nobody holds the item" in report["message"]


def test_a_claim_absorbed_by_the_writers_other_claim_exits_three(
    repo, at_a_fixed_moment, capsys
):
    other = _remote_claim(RIVAL_TS, actor=ACTOR)
    git = RacingGit(arriving=[other], remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    report = _last_json(capsys.readouterr().out)
    assert report["winner_event_id"] == other["id"]
    assert report["message"] == (
        f"the claim on {ITEM_ID} does not hold it, because another claim by "
        f"{ACTOR} already does"
    )


def test_a_rejected_claim_annuls_every_claim_it_tried_to_publish(
    repo, at_a_fixed_moment, capsys
):
    waiting = _waiting_claim(repo)
    git = RacingGit(
        arriving=[], remote_events=[], pushes=[_fail("rejected")] * 3
    )

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 5
    shard = _shard_events(repo, ACTOR)
    claims = [event["id"] for event in shard if event["type"] == "item.claimed"]
    assert len(claims) == 2 and claims[0] == waiting["id"]
    assert _annulled_by(shard) == claims
    report = _last_json(capsys.readouterr().out)
    assert report["annulled"]["target_id"] == claims[1]
    assert report["annulled"]["target_ids"] == claims


# ---------------------------------------------------------------------------
# 2. A claim refuses a clock behind published history
# ---------------------------------------------------------------------------

def test_a_claim_behind_published_history_exits_six_before_minting(repo, capsys):
    ahead = _remote_creation(FAR_AHEAD_TS, "pnx-c3d4")
    git = FakeGit(remote_events=[ahead])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 6
    # Before anything is minted: nothing appended, nothing committed,
    # nothing pushed.
    assert _shard_events(repo, ACTOR) == []
    assert not git.named("commit")
    assert not git.named("push")

    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "clock_behind_remote"
    assert report["appended"] is False
    assert report["remote_newest_ts"] == FAR_AHEAD_TS
    assert report["intended_ts"] < report["remote_newest_ts"]
    assert report["behind_s"] > report["tolerance_s"]
    assert report["tolerance_s"] == 5.0
    assert report["offending_event_id"] == ahead["id"]
    assert report["offending_actor"] == RIVAL
    assert "pinax annul" in report["remedy"]
    assert "PINAX_CLOCK_TOLERANCE_S" in report["remedy"]
    assert FAR_AHEAD_TS in captured.err


def test_a_claim_the_remote_has_already_expired_exits_three(repo, monkeypatch, capsys):
    monkeypatch.setenv("PINAX_CLOCK_TOLERANCE_S", "999999999")
    monkeypatch.setattr("pinax.doctor.utc_now_iso", lambda: MINTED_TS)
    git = FakeGit(remote_events=[_remote_creation(FAR_AHEAD_TS, "pnx-c3d4")])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 3
    assert "nobody holds the item" in _last_json(capsys.readouterr().out)["message"]


def test_the_tolerance_can_be_raised_for_one_crossing(
    repo, at_a_fixed_moment, monkeypatch
):
    monkeypatch.setenv("PINAX_CLOCK_TOLERANCE_S", "999999999")
    git = FakeGit(remote_events=[_remote_creation("2026-09-02T22:00:00Z", "pnx-c3d4")])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.claimed"]
    assert git.named("push")


@pytest.mark.parametrize(
    "value, said",
    [
        ("soon", "is not a number of seconds"),
        ("nan", "is not a number of seconds"),
        ("-30", "is a negative tolerance, which is not accepted"),
    ],
)
def test_a_tolerance_the_rule_cannot_use_falls_back_and_says_which(
    repo, capsys, monkeypatch, value, said
):
    """
    A setting the rule cannot work with never passes silently, and the
    operator is told which kind it was: a value that is not a number of
    seconds, or a number the rule refuses.
    """
    monkeypatch.setenv("PINAX_CLOCK_TOLERANCE_S", value)
    git = FakeGit(remote_events=[_remote_creation(FAR_AHEAD_TS, "pnx-c3d4")])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 6
    captured = capsys.readouterr()
    assert _last_json(captured.out)["tolerance_s"] == 5.0
    assert "PINAX_CLOCK_TOLERANCE_S" in captured.err
    assert said in captured.err
    assert "the default of 5 seconds applies" in captured.err


def test_a_timestamp_the_sequence_cannot_read_is_refused(repo, capsys, monkeypatch):
    """
    The command minted the timestamp itself, in the one form every event
    carries, so a value that cannot be read contradicts the sequence.
    That is an integrity refusal reported apart from the clock rule, and
    never something a tolerance answers.
    """
    monkeypatch.setattr("pinax.doctor.utc_now_iso", lambda: "2026-09-02 10:00:00")
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 6
    assert _shard_events(repo, ACTOR) == []
    assert not git.named("commit")
    assert not git.named("push")

    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "unreadable_timestamp"
    assert report["appended"] is False
    assert report["intended_ts"] == "2026-09-02 10:00:00"
    assert report["expected_ts_form"] == "%Y-%m-%dT%H:%M:%SZ"
    assert "remedy" not in report
    assert "PINAX_CLOCK_TOLERANCE_S" not in captured.err


def test_an_annulled_future_event_no_longer_holds_the_clock_back(repo):
    """
    The remedy the refusal prints actually works: once a valid tombstone
    names the future-dated event, the maximum is taken over what is left.
    """
    ahead = _remote_creation(FAR_AHEAD_TS, "pnx-c3d4")
    retired = _tombstone(ahead, "2026-09-01T09:30:00Z")
    git = FakeGit(remote_events=[ahead, retired])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.claimed"]
    assert git.named("push")


def test_only_a_claim_carries_the_clock_guard(repo, tmp_path):
    briefing = tmp_path / "briefing.md"
    briefing.write_text("the work record", encoding="utf-8")
    git = FakeGit(remote_events=[_remote_creation(FAR_AHEAD_TS, "pnx-c3d4")])

    done_run(repo, ITEM_ID, str(briefing), actor=ACTOR, runner=git)

    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]
    assert git.named("push")


# ---------------------------------------------------------------------------
# 3. The pull regenerates the projection before the next push
# ---------------------------------------------------------------------------

class MergeBringsAnEvent(FakeGit):
    """A runner whose merge lands one more event in the working-tree log."""

    def __init__(self, *, log_dir: str, incoming: dict, **kwargs) -> None:
        super().__init__(**kwargs)
        self.log_dir = log_dir
        self.incoming = incoming

    def run(self, *args: str, env: dict | None = None) -> GitResult:
        result = super().run(*args, env=env)
        if args and args[0] == "merge" and self.incoming is not None:
            append_event(self.log_dir, self.incoming, actor=self.incoming["actor"])
            self.incoming = None
        return result


class RefusesTheSecondCommit(FakeGit):
    """A runner that refuses every commit after the first."""

    def __init__(self, *, refusal: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refusal = refusal
        self.commits = 0

    def run(self, *args: str, env: dict | None = None) -> GitResult:
        if args and args[0] == "commit":
            self.commits += 1
            if self.commits > 1:
                self.commit = _fail(self.refusal)
        return super().run(*args, env=env)


def test_a_clean_pull_still_regenerates_the_projection_before_the_push(repo):
    """
    The merged log has gained the other side's events, so the generated
    Markdown is regenerated and committed before the next attempt, whether
    or not git reported a conflict.
    """
    incoming = _remote_creation("2026-09-01T09:40:00Z", "pnx-e5f6", actor="operator@gamma")
    git = MergeBringsAnEvent(
        log_dir=_log_dir(repo),
        incoming=incoming,
        pushes=[_fail("rejected: fetch first"), _OK],
        remote_events=[],
    )

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert len(git.named("push")) == 2
    assert (
        "commit",
        "-m",
        "pinax: regenerate the projection from the merged main",
    ) in git.calls
    board = os.path.join(repo, ".ergon", "board.md")
    with open(board, "r", encoding="utf-8") as fh:
        assert "pnx-e5f6" in fh.read()


def test_a_conflict_outside_the_projection_stops_the_retry(repo, capsys):
    """
    Regeneration resolves the generated Markdown and nothing else. A merge
    that conflicted elsewhere leaves the commit that would conclude it
    refused, and git's own words end the retry.
    """
    refusal = "error: Committing is not possible because you have unmerged files."
    git = RefusesTheSecondCommit(
        refusal=refusal,
        pushes=[_fail("rejected"), _OK, _OK],
        merge=GitResult(
            returncode=1, stdout="CONFLICT (content): Merge conflict in notes/plan.md"
        ),
        remote_events=[],
    )

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 5
    assert len(git.named("push")) == 1
    assert refusal in _last_json(capsys.readouterr().out)["git_output"]


# ---------------------------------------------------------------------------
# 4. The bare hub: two clones, one item
# ---------------------------------------------------------------------------

WINNER = "operator@alpha"
LOSER = "reviewer@beta"
AHEAD_ACTOR = "operator@gamma"

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing if existing else "")
    return env


def _git(repo_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, env=_build_env()
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_root}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git not available on PATH")


# The line-ending rule has to be in place before git writes any file, so a
# clone carries it from the start rather than being corrected afterwards.
_CHECKOUT_CONFIG = {"core.autocrlf": "false"}


def _configure(repo_root: str) -> None:
    _git(repo_root, "config", "user.email", "clone@pinax.example")
    _git(repo_root, "config", "user.name", "Pinax Clone")
    for key, value in _CHECKOUT_CONFIG.items():
        _git(repo_root, "config", key, value)


def _init_repo(repo_root: str) -> str:
    os.makedirs(repo_root, exist_ok=True)
    _git(repo_root, "init", "-b", "main")
    _configure(repo_root)
    return repo_root


def _pinax(repo_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo_root, capture_output=True, text=True, env=_build_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo_root} with {result.returncode}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _commit_all(repo_root: str, message: str) -> None:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", message)


def _fold_repo(repo_root: str) -> dict:
    return fold_events(read_events(os.path.join(repo_root, ".ergon", "log")))


def _canonical(state: dict) -> str:
    def _plain(value):
        if isinstance(value, dict):
            return {str(key): _plain(item) for key, item in value.items()}
        if isinstance(value, (set, frozenset)):
            return sorted(_plain(item) for item in value)
        if isinstance(value, (list, tuple)):
            return [_plain(item) for item in value]
        return value

    return json.dumps(_plain(state), sort_keys=True, ensure_ascii=True, default=str)


def _board(repo_root: str) -> str:
    with open(os.path.join(repo_root, ".ergon", "board.md"), encoding="utf-8") as fh:
        return fh.read()


def _seed_one_item(repo_root: str, actor: str) -> str:
    added = _pinax(
        repo_root, "add", "--title", "Claimed from two machines",
        "--prefix", "pnx", "--actor", actor, "--json",
    )
    return json.loads(added.stdout)["item_id"]


def _publish_an_item_and_an_event_from_a_clock_that_ran_ahead(
    repo_root: str,
) -> tuple[str, str]:
    """
    Publish one ordinary item and, beside it, one valid event whose
    timestamp is an hour ahead of now: the shape a machine with a fast
    clock leaves on the hub. Both go in one commit and one push, so the
    test asks the filesystem for as little as the subject needs.

    Returns the ordinary item's id and the id of the event that ran ahead.
    """
    ahead_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    log_dir = os.path.join(repo_root, ".ergon", "log")
    seeded = read_events(log_dir)
    next_seq = (max(item["seq"] for item in seeded) + 1) if seeded else 0

    item_id = "pnx-slow"
    ordinary = mint_event(
        seq=next_seq,
        ts="2026-09-01T09:00:00Z",
        actor=AHEAD_ACTOR,
        etype="item.created",
        payload={"item_id": item_id, "title": "Ordinary work", "prefix": "pnx"},
        prev="",
    )
    ahead = mint_event(
        seq=next_seq + 1,
        ts=ahead_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        actor=AHEAD_ACTOR,
        etype="item.created",
        payload={
            "item_id": "pnx-ahead",
            "title": "Recorded by a clock that ran ahead",
            "prefix": "pnx",
        },
        prev=ordinary["id"],
    )
    for event in (ordinary, ahead):
        append_event(log_dir, event, actor=AHEAD_ACTOR)
    regenerate(repo_root)
    _commit_all(repo_root, "record an event from a machine whose clock ran ahead")
    _git(repo_root, "push", "origin", "main")
    return item_id, ahead["id"]


def _race(alpha: str, beta: str, item_id: str) -> tuple[str, subprocess.CompletedProcess]:
    """
    WINNER's claim waits unpublished in alpha and reaches the hub from
    beta's pre-commit hook, so after LOSER's command has fetched and
    appended, and before it pushes.
    """
    _pinax(alpha, "dispatch", "--claim", "--actor", WINNER)
    _commit_all(alpha, "claim waiting for a push")
    winning = [
        event["id"]
        for event in read_events(os.path.join(alpha, ".ergon", "log"))
        if event["type"] == "item.claimed" and event["payload"]["item_id"] == item_id
    ]
    assert len(winning) == 1
    hooks = os.path.join(beta, _git(beta, "rev-parse", "--git-path", "hooks").stdout.strip())
    os.makedirs(hooks, exist_ok=True)
    hook = os.path.join(hooks, "pre-commit")
    with open(hook, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "#!/bin/sh\nunset $(git rev-parse --local-env-vars)\n"
            f"cd '{alpha}' && git push -q origin main\n"
        )
    os.chmod(hook, 0o755)
    try:
        lost = _pinax(beta, "claim", item_id, "--actor", LOSER, "--json", check=False)
    finally:
        os.remove(hook)
    return winning[0], lost


@requires_git
def test_a_claim_held_on_the_hub_changes_no_clone_and_no_hub(
    clone_wired_to_hub, clone_of_hub
):
    alpha = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(alpha, WINNER)
    beta = clone_of_hub(
        git=_git, name="beta", config=_CHECKOUT_CONFIG, configure=_configure
    )
    _pinax(alpha, "claim", item_id, "--actor", WINNER)

    def snapshot() -> tuple:
        log_dir = os.path.join(beta, ".ergon", "log")
        shards = {}
        for name in sorted(os.listdir(log_dir)):
            with open(os.path.join(log_dir, name), "rb") as fh:
                shards[name] = fh.read()
        return (
            shards,
            _git(beta, "log", "--format=%H").stdout,
            _git(beta, "ls-remote", "origin", "refs/heads/main").stdout,
            _git(beta, "status", "--porcelain").stdout,
        )

    before = snapshot()
    for _attempt in range(2):
        lost = _pinax(beta, "claim", item_id, "--actor", LOSER, "--json", check=False)
        assert lost.returncode == 3, lost.stdout + lost.stderr
        report = json.loads(lost.stdout)
        assert report["sync_status"] == "claim_held"
        assert report["published"] is False
        assert report["holder_actor"] == WINNER
        assert snapshot() == before
        _git(beta, "pull", "--no-rebase", "-q", "origin", "main")
        before = snapshot()
    for root in (alpha, beta):
        assert "## Claim conflicts (resolved)" not in _pinax(root, "board").stdout


@requires_git
def test_three_claims_by_one_writer_record_one(clone_wired_to_hub):
    root = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(root, WINNER)

    runs = [_pinax(root, "claim", item_id, "--actor", WINNER) for _ in range(3)]

    claims = [
        event for event in read_events(os.path.join(root, ".ergon", "log"))
        if event["type"] == "item.claimed"
    ]
    assert len(claims) == 1
    for repeat in runs[1:]:
        assert repeat.stdout == (
            f"pinax: item {item_id} is already claimed by {WINNER} since "
            f"{claims[0]['ts']}; nothing was appended\n"
        )
        assert "claim.superseded" not in repeat.stderr
    assert "## Claim conflicts (resolved)" not in _pinax(root, "board").stdout


@requires_git
def test_a_claim_after_dispatch_replaces_the_dispatched_claim(clone_wired_to_hub):
    root = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(root, WINNER)
    _pinax(root, "dispatch", "--claim", "--actor", WINNER)

    result = json.loads(
        _pinax(root, "claim", item_id, "--actor", WINNER, "--json").stdout
    )

    dispatched, annulment, fresh = [
        event for event in read_events(os.path.join(root, ".ergon", "log"))
        if event["type"] in ("item.claimed", "event.annulled")
    ]
    assert annulment["payload"]["target_id"] == dispatched["id"]
    assert fresh["id"] == result["event_id"]
    assert fresh["ts"] == result["ts"]
    assert result["sync_status"] == "pushed"
    assert result["published"] is True
    assert result["annulled"]["target_id"] == dispatched["id"]
    assert _git(root, "status", "--porcelain").stdout == ""
    _git(root, "fetch", "origin")
    assert result["event_id"] in _git(
        root, "show", "origin/main:.ergon/log/operator-alpha.jsonl"
    ).stdout
    board = _pinax(root, "board").stdout
    assert f"· {WINNER} ·" in board
    assert "## Claim conflicts (resolved)" not in board


@requires_git
def test_a_claim_after_dispatch_says_so_in_plain_output(clone_wired_to_hub):
    root = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(root, WINNER)
    _pinax(root, "dispatch", "--claim", "--actor", WINNER)
    dispatched = _fold_repo(root)["items"][item_id]["claim_event_id"]

    claimed = _pinax(root, "claim", item_id, "--actor", WINNER)

    assert claimed.stdout.startswith(f"pinax: claimed item {item_id} by {WINNER} ")
    assert claimed.stdout.endswith(f"pinax: the waiting claim {dispatched} was annulled\n")


@requires_git
def test_a_dispatched_claim_does_not_take_an_item_claimed_since(
    clone_wired_to_hub, clone_of_hub
):
    alex, sam = "alex@laptop", "sam@desktop"
    laptop = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=alex, name="laptop",
    )
    item_id = _seed_one_item(laptop, alex)
    desktop = clone_of_hub(
        git=_git, name="desktop", config=_CHECKOUT_CONFIG, configure=_configure
    )
    _pinax(desktop, "dispatch", "--claim", "--actor", sam)
    # Timestamps hold whole seconds, and a tie would go to alex by name.
    time.sleep(1.1)
    _pinax(laptop, "claim", item_id, "--actor", alex)

    lost = _pinax(desktop, "claim", item_id, "--actor", sam, "--json", check=False)

    assert lost.returncode == 3, lost.stdout + lost.stderr
    assert json.loads(lost.stdout)["sync_status"] == "claim_held"
    for root in (laptop, desktop):
        _git(root, "pull", "--no-rebase", "-q", "origin", "main")
        board = _pinax(root, "board").stdout
        assert f"· {alex} ·" in board
        assert sam not in board
        assert "## Claim conflicts (resolved)" not in board
    _pinax(desktop, "add", "--title", "Later", "--prefix", "pnx", "--actor", sam)
    _git(laptop, "pull", "--no-rebase", "-q", "origin", "main")
    for root in (laptop, desktop):
        assert _fold_repo(root)["items"][item_id]["owner"] == alex
        assert "## Claim conflicts (resolved)" not in _pinax(root, "board").stdout
        assert _pinax(root, "verify", check=False).returncode == 0


@requires_git
def test_a_resolved_conflict_is_not_repeated_by_a_later_write(
    clone_wired_to_hub, clone_of_hub
):
    alpha = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(alpha, WINNER)
    beta = clone_of_hub(
        git=_git, name="beta", config=_CHECKOUT_CONFIG, configure=_configure
    )
    _winning, lost = _race(alpha, beta, item_id)
    assert lost.returncode == 3, lost.stdout + lost.stderr
    assert json.loads(lost.stdout)["status"] == "claim_superseded"
    assert "claim.superseded:" in lost.stderr

    for root, actor in ((beta, LOSER), (alpha, WINNER)):
        _git(root, "pull", "--no-rebase", "-q", "origin", "main")
        added = _pinax(
            root, "add", "--title", "Later work", "--prefix", "pnx",
            "--actor", actor, "--json",
        )
        assert "claim.superseded" not in added.stderr
        assert f"- {item_id}: {LOSER} superseded by {WINNER}" in _pinax(root, "board").stdout


@pytest.mark.deep
@requires_git
def test_two_clones_claim_one_item_and_the_loser_exits_three(
    clone_wired_to_hub, clone_of_hub
):
    """
    Two machines, one item, no lock, and neither has seen the other. Each appends and commits its own claim
    in its own repository. The second push is rejected, its command pulls,
    resolves the generated Markdown by regenerating it from the merged log,
    pushes, and the fold over the pushed state tells it that the earlier
    claim won. Both folds agree afterwards and pinax verify passes in both.
    """
    alpha = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id = _seed_one_item(alpha, WINNER)
    beta = clone_of_hub(
        git=_git, name="beta", config=_CHECKOUT_CONFIG, configure=_configure
    )

    winning_event, lost = _race(alpha, beta, item_id)
    assert lost.returncode == 3, (
        "the second claim did not end from the fold over the pushed state:\n"
        f"stdout: {lost.stdout}\nstderr: {lost.stderr}"
    )
    report = json.loads(lost.stdout.strip().splitlines()[-1])
    assert report["status"] == "claim_superseded"
    assert report["winner_actor"] == WINNER
    assert report["winner_event_id"] == winning_event
    assert report["superseded_actor"] == LOSER
    assert report["pushed"] is True

    # The loser published its claim: the hub carries both, and the winner's
    # clone reaches the same state by a fast-forward.
    _git(alpha, "fetch", "origin")
    _git(alpha, "merge", "--ff-only", "origin/main")
    assert _git(alpha, "rev-parse", "HEAD").stdout == _git(beta, "rev-parse", "HEAD").stdout

    for root, label in ((alpha, "alpha"), (beta, "beta")):
        state = _fold_repo(root)
        assert state["items"][item_id]["owner"] == WINNER, f"{label}: wrong owner"
        superseded = state.get("claim_superseded", [])
        assert [entry["superseded_actor"] for entry in superseded] == [LOSER], label
        assert [entry["winner_actor"] for entry in superseded] == [WINNER], label
        assert "<<<<<<<" not in _board(root), f"{label}: a conflict marker survived"
        assert _pinax(root, "verify", check=False).returncode == 0, f"{label}: verify"
        assert _git(root, "status", "--porcelain").stdout.strip() == "", label

    assert _canonical(_fold_repo(alpha)) == _canonical(_fold_repo(beta))


@pytest.mark.deep
@requires_git
def test_a_clock_behind_the_hub_is_refused_until_the_event_ahead_is_annulled(
    clone_wired_to_hub
):
    """
    The hub carries an event an hour ahead of this machine's clock, so a
    claim is refused with nothing recorded. The remedy the refusal prints
    is then followed, and the same claim succeeds.
    """
    root = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor=WINNER, name="alpha",
    )
    item_id, ahead_id = _publish_an_item_and_an_event_from_a_clock_that_ran_ahead(root)

    refused = _pinax(root, "claim", item_id, "--actor", WINNER, "--json", check=False)
    assert refused.returncode == 6, (
        f"stdout: {refused.stdout}\nstderr: {refused.stderr}"
    )
    report = json.loads(refused.stdout.strip().splitlines()[-1])
    assert report["status"] == "clock_behind_remote"
    assert report["appended"] is False
    assert report["offending_event_id"] == ahead_id
    assert report["offending_actor"] == AHEAD_ACTOR
    assert report["behind_s"] > report["tolerance_s"]
    assert "pinax annul" in report["remedy"]

    # Nothing was recorded and nothing was left behind.
    assert not any(
        event["type"] == "item.claimed" for event in read_events(
            os.path.join(root, ".ergon", "log")
        )
    )
    assert _git(root, "status", "--porcelain").stdout.strip() == ""

    _pinax(
        root, "annul", ahead_id,
        "--reason", "the timestamp came from a machine whose clock ran ahead",
        "--actor", WINNER, "--json",
    )
    claimed = _pinax(root, "claim", item_id, "--actor", WINNER, "--json")
    assert json.loads(claimed.stdout)["item_id"] == item_id
    assert _fold_repo(root)["items"][item_id]["owner"] == WINNER
    assert _pinax(root, "verify", check=False).returncode == 0
