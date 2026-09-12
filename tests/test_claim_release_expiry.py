"""
Claim release and deterministic expiry.

Covered:
1.  pinax release: the publish sequence runs once with its own commit
    subject, on the remote path and on both offline paths, and the fold
    clears the claim it released.
2.  pinax policy claim-expiry: the same sequence, with a repository-level
    commit subject that names no item, and a payload carrying the hours.
3.  The refusals, each before anything is minted: an unknown item, an item
    with no live claim, an empty reason, and an expiry that is not a
    positive number of hours.
4.  The policy in force for a claim: the last policy event at or before it
    in claim order, twenty-four hours when the log carries none before it,
    and a policy set afterwards that does not reach back.
5.  The expiry: the first event past the deadline ends the claim, the
    outcome is recorded, a log that records nothing after a claim never
    expires it, and a fold of the log up to a point before the ending
    event still shows the claim live.
6.  Eligibility: a released or an expired item is a candidate for ready
    and next again, and its next claim is the winner.
7.  Determinism: the same shards folded at two different wall-clock times
    produce byte-identical state, with the clock the status view reads
    moved between the two folds.
8.  The diagnosis threshold: the folded policy, the module default when
    the log sets none, and an explicit value that still wins.
9.  Verification across the two new event types.

The fake git runner is the one test_sync_sequence owns: every command here
runs its whole publish sequence with no git process and no network, and the
runner records every call it was asked to make.
"""

from __future__ import annotations

import datetime
import json
import os
import types

import pytest

from pinax.append import append_event
from pinax.commands import next_cmd, ready
from pinax.commands.policy import run_claim_expiry as policy_run
from pinax.commands.release import run as release_run
from pinax.commands.verify import _inspect
from pinax.doctor import DEFAULT_STALE_HOURS, diagnose
from pinax.event import mint_event
from pinax.fold import (
    DEFAULT_CLAIM_EXPIRY_HOURS,
    claim_expiry_policy_hours,
    compute_next,
    compute_ready,
    fold_events,
    read_events,
    state_to_json_safe,
)
from pinax.projection import regenerate
from pinax.statusview import status_view

from test_sync_sequence import FakeGit

ITEM = "pnx-a1b2"
OTHER = "pnx-c3d4"
OWNER = "worker@alpha"
RIVAL = "worker@beta"
OPERATOR = "operator@hub"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".ergon", "log")


def _at(day: int, hour: int = 0, minute: int = 0, second: int = 0) -> str:
    """One event timestamp in the form every command mints with."""
    return f"2026-03-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}Z"


def _append(
    repo_root: str, seq: int, ts: str, actor: str, etype: str, payload: dict
) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(_log_dir(repo_root), event, actor=actor)
    return event


def _item(repo_root: str, seq: int, ts: str, item_id: str, title: str) -> dict:
    return _append(
        repo_root, seq, ts, OPERATOR, "item.created",
        {"item_id": item_id, "title": title, "prefix": "pnx", "status": "queued"},
    )


def _claim(repo_root: str, seq: int, ts: str, item_id: str, actor: str) -> dict:
    return _append(repo_root, seq, ts, actor, "item.claimed", {"item_id": item_id})


def _release(
    repo_root: str, seq: int, ts: str, item_id: str, reason: str, actor: str = OPERATOR
) -> dict:
    return _append(
        repo_root, seq, ts, actor, "item.claim_released",
        {"item_id": item_id, "reason": reason},
    )


def _policy(repo_root: str, seq: int, ts: str, hours: float, actor: str = OPERATOR) -> dict:
    return _append(
        repo_root, seq, ts, actor, "policy.claim_expiry_set", {"hours": hours},
    )


def _fold(repo_root: str) -> dict:
    return fold_events(read_events(_log_dir(repo_root)))


def _fold_upto(repo_root: str, ts: str) -> dict:
    """
    Fold only the events a commit made before `ts` could have carried.

    The prefix stands for the log as it existed at an earlier commit, which
    is what a replay at that ref folds: the same fold over fewer events.
    """
    events = [e for e in read_events(_log_dir(repo_root)) if e["ts"] <= ts]
    return fold_events(events)


def _shard_events(repo_root: str, actor: str) -> list[dict]:
    shard = os.path.join(_log_dir(repo_root), actor.replace("@", "-") + ".jsonl")
    if not os.path.isfile(shard):
        return []
    with open(shard, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _last_json(text: str) -> dict:
    return json.loads(text.strip().splitlines()[-1])


def _state_bytes(state: dict) -> bytes:
    return json.dumps(
        state_to_json_safe(state), sort_keys=True, ensure_ascii=True
    ).encode("utf-8")


@pytest.fixture()
def repo(tmp_path):
    """
    Two items, one of them carrying a live claim.

    The claim is the last event in claim order, so nothing in the fixture
    itself ends it: each test decides what happens after it.
    """
    root = str(tmp_path)
    os.makedirs(_log_dir(root))
    _append(root, 0, _at(1, 9, 0, 0), OPERATOR, "ergon.created", {"repo": "sequence"})
    _item(root, 1, _at(1, 9, 0, 1), ITEM, "Claimed item")
    _item(root, 2, _at(1, 9, 0, 2), OTHER, "Unclaimed item")
    _claim(root, 3, _at(1, 9, 0, 3), ITEM, OWNER)
    return root


@pytest.fixture()
def at_the_claim_moment(monkeypatch):
    """
    Pin the timestamp the release command mints, at the reference the
    command itself holds, to a few seconds after the fixture's claim.

    A release recorded long after a claim ends that claim twice over: by
    the release, and by the expiry the very same event triggers. Minting
    inside the policy window leaves the release as the only rule that can
    end it, which is what a release test is about.
    """
    minted = _at(1, 9, 0, 10)
    monkeypatch.setattr("pinax.commands.release.utc_now_iso", lambda: minted)
    return minted


@pytest.fixture()
def quiet(tmp_path):
    """The same shape with no claim at all, for the policy command."""
    root = str(tmp_path)
    os.makedirs(_log_dir(root))
    _append(root, 0, _at(1, 9, 0, 0), OPERATOR, "ergon.created", {"repo": "sequence"})
    _item(root, 1, _at(1, 9, 0, 1), ITEM, "Claimed item")
    return root


# ---------------------------------------------------------------------------
# 1. pinax release runs the sequence
# ---------------------------------------------------------------------------

def test_release_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    release_run(repo, ITEM, "the builder stopped", actor=OPERATOR, runner=git)

    assert ("fetch", "origin") in git.calls
    assert ("commit", "-m", f"pinax: item.claim_released {ITEM}") in git.calls
    assert ("push", "origin", "main:refs/heads/main") in git.calls
    assert len(git.named("fetch")) == 2
    appended = _shard_events(repo, OPERATOR)
    assert [event["type"] for event in appended][-1:] == ["item.claim_released"]
    assert appended[-1]["payload"] == {
        "item_id": ITEM,
        "reason": "the builder stopped",
    }


def test_release_clears_the_claim_and_reports_what_it_released(
    repo, at_the_claim_moment, capsys
):
    git = FakeGit(remote_events=[])

    release_run(repo, ITEM, "the builder stopped", actor=OPERATOR,
                as_json=True, runner=git)

    payload = _last_json(capsys.readouterr().out)
    assert payload["item_id"] == ITEM
    assert payload["released_owner"] == OWNER
    assert payload["type"] == "item.claim_released"
    assert payload["ts"] == at_the_claim_moment

    item = _fold(repo)["items"][ITEM]
    assert "owner" not in item
    assert "claimed_at" not in item
    assert "claim_event_id" not in item
    assert item["status"] == "queued"


def test_release_offline_flag_skips_fetch_and_push_but_still_commits(repo):
    git = FakeGit(remote_events=[])

    release_run(repo, ITEM, "the builder stopped", actor=OPERATOR,
                offline=True, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")
    assert [e["type"] for e in _shard_events(repo, OPERATOR)][-1] == "item.claim_released"


def test_release_offline_env_var_skips_fetch_and_push_but_still_commits(repo, monkeypatch):
    monkeypatch.setenv("PINAX_OFFLINE", "1")
    git = FakeGit(remote_events=[])

    release_run(repo, ITEM, "the builder stopped", actor=OPERATOR, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")


# ---------------------------------------------------------------------------
# 2. pinax policy claim-expiry runs the sequence
# ---------------------------------------------------------------------------

def test_policy_runs_the_sequence_once_with_a_repository_level_subject(quiet, capsys):
    git = FakeGit(remote_events=[])

    policy_run(quiet, 12.0, actor=OPERATOR, as_json=True, runner=git)

    # The event names no item, so the subject is the event type alone.
    assert ("commit", "-m", "pinax: policy.claim_expiry_set") in git.calls
    assert git.named("push")
    appended = _shard_events(quiet, OPERATOR)[-1]
    assert appended["type"] == "policy.claim_expiry_set"
    assert appended["payload"] == {"hours": 12.0}
    payload = _last_json(capsys.readouterr().out)
    assert payload["hours"] == 12.0
    assert "item_id" not in payload


def test_policy_folds_to_the_repository_policy(quiet):
    git = FakeGit(remote_events=[])

    policy_run(quiet, 6.0, actor=OPERATOR, runner=git)

    state = _fold(quiet)
    assert state["policy"]["claim_expiry_hours"] == 6.0
    assert state["policy"]["set_by"] == OPERATOR
    assert claim_expiry_policy_hours(state) == 6.0


def test_policy_offline_flag_skips_fetch_and_push_but_still_commits(quiet):
    git = FakeGit(remote_events=[])

    policy_run(quiet, 8.0, actor=OPERATOR, offline=True, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")
    assert _shard_events(quiet, OPERATOR)[-1]["type"] == "policy.claim_expiry_set"


def test_policy_offline_env_var_skips_fetch_and_push_but_still_commits(quiet, monkeypatch):
    monkeypatch.setenv("PINAX_OFFLINE", "1")
    git = FakeGit(remote_events=[])

    policy_run(quiet, 8.0, actor=OPERATOR, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")


# ---------------------------------------------------------------------------
# 3. The refusals
# ---------------------------------------------------------------------------

def test_release_refuses_an_unknown_item(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        release_run(repo, "pnx-nope", "gone", actor=OPERATOR, runner=git)

    assert exited.value.code == 1
    assert "unknown item 'pnx-nope'" in capsys.readouterr().err
    assert git.calls == []
    assert "item.claim_released" not in [e["type"] for e in _shard_events(repo, OPERATOR)]


def test_release_refuses_an_item_with_no_live_claim(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        release_run(repo, OTHER, "gone", actor=OPERATOR, runner=git)

    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert "no live claim" in err
    assert git.calls == []


def test_release_refuses_an_empty_reason(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        release_run(repo, ITEM, "   ", actor=OPERATOR, runner=git)

    assert exited.value.code == 1
    assert "non-empty --reason" in capsys.readouterr().err
    assert git.calls == []


def test_release_refuses_a_claim_the_fold_has_already_ended(repo, capsys):
    # The claim expires against a later event, so there is nothing left to
    # release and the refusal says so rather than appending a second end.
    _item(repo, 4, _at(3, 9, 0, 0), "pnx-e5f6", "Much later item")
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        release_run(repo, ITEM, "already gone", actor=OPERATOR, runner=git)

    assert exited.value.code == 1
    assert "no live claim" in capsys.readouterr().err
    assert git.calls == []


@pytest.mark.parametrize("hours", [0.0, -4.0, float("inf"), float("nan")])
def test_policy_refuses_an_expiry_that_is_not_positive_hours(quiet, capsys, hours):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        policy_run(quiet, hours, actor=OPERATOR, runner=git)

    assert exited.value.code == 1
    assert "positive number of hours" in capsys.readouterr().err
    assert git.calls == []
    assert "policy.claim_expiry_set" not in [
        e["type"] for e in _shard_events(quiet, OPERATOR)
    ]


# ---------------------------------------------------------------------------
# 4. The policy in force for a claim
# ---------------------------------------------------------------------------

def test_a_claim_with_no_policy_before_it_runs_on_the_default(repo):
    # One hour past the default, and an event to notice it.
    _item(repo, 4, _at(2, 9, 0, 4), "pnx-e5f6", "Later item")

    state = _fold(repo)

    assert "owner" not in state["items"][ITEM]
    assert state["claim_expired"][0]["expiry_hours"] == DEFAULT_CLAIM_EXPIRY_HOURS


def test_the_policy_in_force_is_the_last_one_at_or_before_the_claim(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 48.0)
    _policy(repo, 5, _at(1, 8, 30, 0), 2.0)
    # Two hours and one second after the claim: past the two-hour policy.
    _item(repo, 6, _at(1, 11, 0, 4), "pnx-e5f6", "Later item")

    state = _fold(repo)

    expired = state["claim_expired"][0]
    assert expired["item_id"] == ITEM
    assert expired["expiry_hours"] == 2.0
    assert expired["claim_actor"] == OWNER


def test_a_policy_set_after_a_claim_does_not_govern_it(repo):
    # A one-hour policy recorded after the claim, then an event two hours
    # after the claim: under the policy in force for that claim, which is
    # the default, the claim is still live.
    _policy(repo, 4, _at(1, 10, 0, 0), 1.0)
    _item(repo, 5, _at(1, 11, 0, 4), "pnx-e5f6", "Later item")

    state = _fold(repo)

    assert state["items"][ITEM]["owner"] == OWNER
    assert "claim_expired" not in state
    # The repository's current policy is still the one hour just set.
    assert claim_expiry_policy_hours(state) == 1.0


def test_a_policy_event_without_positive_hours_sets_no_policy(repo):
    _append(repo, 4, _at(1, 8, 0, 0), OPERATOR, "policy.claim_expiry_set", {"hours": 0})
    _append(repo, 5, _at(1, 8, 0, 1), OPERATOR, "policy.claim_expiry_set", {"hours": "6"})

    state = _fold(repo)

    assert "policy" not in state
    assert claim_expiry_policy_hours(state) == DEFAULT_CLAIM_EXPIRY_HOURS


# ---------------------------------------------------------------------------
# 5. The expiry
# ---------------------------------------------------------------------------

def test_a_live_claim_expires_at_the_first_event_past_the_deadline(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 1.0)
    within = _item(repo, 5, _at(1, 9, 30, 0), "pnx-e5f6", "Within the hour")
    past = _item(repo, 6, _at(1, 10, 30, 0), "pnx-g7h8", "Past the hour")
    _item(repo, 7, _at(1, 11, 30, 0), "pnx-i9j0", "Later still")

    state = _fold(repo)

    expired = state["claim_expired"]
    assert len(expired) == 1
    assert expired[0]["expiring_event_id"] == past["id"]
    assert expired[0]["expired_at"] == past["ts"]
    assert expired[0]["claim_event_id"] != within["id"]
    item = state["items"][ITEM]
    assert "owner" not in item and "claimed_at" not in item
    assert "claim_event_id" not in item
    # The status the item carried is the status it keeps.
    assert item["status"] == "queued"
    # And it is a candidate for dispatch again.
    assert ITEM in compute_ready(state)
    assert compute_next(state) is not None


def test_a_quiet_log_never_expires_a_claim(repo):
    state = _fold(repo)

    assert state["items"][ITEM]["owner"] == OWNER
    assert state["items"][ITEM]["claimed_at"] == _at(1, 9, 0, 3)
    assert "claim_expired" not in state


def test_a_fold_before_the_expiring_event_still_shows_the_claim_live(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 1.0)
    _item(repo, 5, _at(1, 10, 30, 0), "pnx-e5f6", "Past the hour")

    earlier = _fold_upto(repo, _at(1, 10, 0, 0))
    assert earlier["items"][ITEM]["owner"] == OWNER
    assert "claim_expired" not in earlier

    assert "owner" not in _fold(repo)["items"][ITEM]


def test_a_release_is_recorded_as_a_release_even_past_the_deadline(repo):
    # The release is itself the first event past the deadline: the fold
    # names the release, not the expiry, so the reason is not lost.
    released = _release(repo, 4, _at(3, 9, 0, 0), ITEM, "the builder stopped")

    state = _fold(repo)

    assert "claim_expired" not in state
    entry = state["claim_released"][0]
    assert entry["item_id"] == ITEM
    assert entry["claim_actor"] == OWNER
    assert entry["release_actor"] == OPERATOR
    assert entry["release_event_id"] == released["id"]
    assert entry["reason"] == "the builder stopped"


def test_a_release_without_a_reason_ends_nothing(repo):
    _append(repo, 4, _at(1, 9, 0, 4), OPERATOR, "item.claim_released", {"item_id": ITEM})

    state = _fold(repo)

    assert state["items"][ITEM]["owner"] == OWNER
    assert "claim_released" not in state


# ---------------------------------------------------------------------------
# 6. A released or expired item is claimable and ready again
# ---------------------------------------------------------------------------

def test_a_released_item_is_ready_and_next_again(repo):
    _release(repo, 4, _at(1, 9, 0, 4), ITEM, "the builder stopped")

    state = _fold(repo)

    assert ITEM in compute_ready(state)
    assert compute_next(state) in (ITEM, OTHER)
    assert ITEM in compute_ready(state, under=None)


def test_a_later_claim_after_a_release_is_the_winner(repo):
    _release(repo, 4, _at(1, 9, 0, 4), ITEM, "the builder stopped")
    second = _claim(repo, 5, _at(1, 9, 0, 5), ITEM, RIVAL)

    state = _fold(repo)

    item = state["items"][ITEM]
    assert item["owner"] == RIVAL
    assert item["claim_event_id"] == second["id"]
    # The released claim is an ended claim, not a superseded one.
    assert state["claim_superseded"] == []
    assert state["claim_released"][0]["claim_actor"] == OWNER
    assert ITEM not in compute_ready(state)


def test_a_later_claim_after_an_expiry_is_the_winner(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 1.0)
    _item(repo, 5, _at(1, 10, 30, 0), "pnx-e5f6", "Past the hour")
    second = _claim(repo, 6, _at(1, 11, 0, 0), ITEM, RIVAL)

    state = _fold(repo)

    assert state["items"][ITEM]["owner"] == RIVAL
    assert state["items"][ITEM]["claim_event_id"] == second["id"]
    assert state["claim_expired"][0]["claim_actor"] == OWNER
    assert state["claim_superseded"] == []


def test_a_claim_made_while_another_is_live_is_still_superseded(repo):
    loser = _claim(repo, 4, _at(1, 9, 0, 4), ITEM, RIVAL)

    state = _fold(repo)

    assert state["items"][ITEM]["owner"] == OWNER
    superseded = state["claim_superseded"]
    assert [entry["superseded_event_id"] for entry in superseded] == [loser["id"]]
    assert superseded[0]["winner_actor"] == OWNER


def test_a_released_item_is_ready_again_through_the_command(repo, capsys):
    _release(repo, 4, _at(1, 9, 0, 4), ITEM, "the builder stopped")

    ready.run(repo_root=repo, as_json=True)
    listed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert ITEM in listed

    next_cmd.run(repo_root=repo, as_json=True)
    chosen = _last_json(capsys.readouterr().out)
    assert chosen["item_id"] in (ITEM, OTHER)


# ---------------------------------------------------------------------------
# 7. The decision is a pure function of the events
# ---------------------------------------------------------------------------

def _freeze(monkeypatch, moment: datetime.datetime) -> None:
    """
    Move the clock the status view reads, and nothing else.

    pinax.statusview is the module that asks the machine what time it is
    (it dates the claim ages it reports). The fold asks nobody, which is
    what the two folds below prove.
    """

    class _Frozen(datetime.datetime):
        @classmethod
        def utcnow(cls):
            return moment

    monkeypatch.setattr(
        "pinax.statusview.datetime",
        types.SimpleNamespace(
            datetime=_Frozen,
            timedelta=datetime.timedelta,
            timezone=datetime.timezone,
        ),
    )


def test_the_same_shards_fold_identically_at_two_wall_clock_times(repo, monkeypatch):
    # One claim the log expires and one it leaves live, so both halves of
    # the decision are in the compared state.
    _policy(repo, 4, _at(1, 8, 0, 0), 1.0)
    _item(repo, 5, _at(1, 12, 0, 0), "pnx-e5f6", "Past the hour")
    _claim(repo, 6, _at(1, 12, 0, 1), OTHER, RIVAL)

    _freeze(monkeypatch, datetime.datetime(2026, 3, 2, 0, 0, 0))
    first = _state_bytes(_fold(repo))
    first_view = status_view(repo_root=repo, scope="repo")

    _freeze(monkeypatch, datetime.datetime(2031, 6, 5, 12, 0, 0))
    second = _state_bytes(_fold(repo))
    second_view = status_view(repo_root=repo, scope="repo")

    assert first == second
    # The clock really did move between the two folds.
    first_ages = [row["age_hours"] for row in first_view["repo"]["building"]]
    second_ages = [row["age_hours"] for row in second_view["repo"]["building"]]
    assert first_ages and first_ages != second_ages


# ---------------------------------------------------------------------------
# 8. The diagnosis threshold
# ---------------------------------------------------------------------------

def test_the_diagnosis_threshold_defaults_to_the_folded_policy(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 3.0)
    now = datetime.datetime(2026, 3, 1, 10, 0, 0)

    report = diagnose(repo_root=repo, log_dir=_log_dir(repo), now=now)

    assert report["stale_hours"] == 3.0
    # An hour after the claim under a three-hour policy: not stale yet.
    assert report["stale_claims"] == []


def test_the_diagnosis_threshold_defaults_to_the_module_default(repo):
    now = datetime.datetime(2026, 3, 1, 10, 0, 0)

    report = diagnose(repo_root=repo, log_dir=_log_dir(repo), now=now)

    assert report["stale_hours"] == DEFAULT_STALE_HOURS
    assert report["stale_claims"] == []


def test_an_explicit_diagnosis_threshold_still_wins(repo):
    _policy(repo, 4, _at(1, 8, 0, 0), 48.0)
    now = datetime.datetime(2026, 3, 1, 10, 0, 0)

    report = diagnose(
        repo_root=repo, log_dir=_log_dir(repo), now=now, stale_hours=0.5
    )

    assert report["stale_hours"] == 0.5
    assert [c["item_id"] for c in report["stale_claims"]] == [ITEM]
    assert report["stale_claims"][0]["owner"] == OWNER


# ---------------------------------------------------------------------------
# 9. Verification across the new event types
# ---------------------------------------------------------------------------

def test_verification_passes_across_the_new_event_types(repo):
    """
    The command's own inspection: every event id recomputes, and the
    projection regenerated from the log matches the log. Driven directly so
    the fast lane runs no subprocess.
    """
    _policy(repo, 4, _at(1, 8, 0, 0), 5.0)
    _release(repo, 5, _at(1, 9, 0, 5), ITEM, "the builder stopped")
    _claim(repo, 6, _at(1, 9, 0, 6), ITEM, RIVAL)
    regenerate(repo)

    drift_files, invalid_events = _inspect(repo)

    assert invalid_events == []
    assert drift_files == []
