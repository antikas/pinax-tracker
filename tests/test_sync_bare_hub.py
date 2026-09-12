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
    exit 0 and no command checks ownership before it appends.
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

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold_events, read_events
from pinax.projection import regenerate
from pinax.sync import GitResult
from tests.test_sync_sequence import FakeGit, _log_dir, _seed, _shard_events

from pinax.commands.claim import run as claim_run
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
    monkeypatch.setattr("pinax.doctor.utc_now_iso", lambda: MINTED_TS)
    return MINTED_TS


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
    The claim is appended, committed and pushed with no ownership check of
    any kind. The fold over the pushed remote state finds an earlier claim,
    and that fold, not the push, ends the command.
    """
    # The winner sits inside the claim expiry policy window, so the subject
    # here stays supersession rather than a claim the fold has expired.
    winner = _remote_claim("2026-09-02T09:30:00Z")
    git = FakeGit(remote_events=[winner])

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
    assert RIVAL in captured.err


def test_a_fold_warning_reaches_the_operator_once(repo, at_a_fixed_moment, capsys):
    """
    A rejected push makes the sequence fold three times over one log:
    before the append, again after the pull, and once more over the
    pushed remote state. The warning those folds keep raising is one line
    for the operator to read, not one line per fold.
    """
    # The winner sits inside the claim expiry policy window, so the subject
    # here stays supersession rather than a claim the fold has expired.
    winner = _remote_claim("2026-09-02T09:30:00Z")
    git = FakeGit(pushes=[_fail("rejected: fetch first"), _OK], remote_events=[winner])

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
    git = FakeGit(remote_events=[later])

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
    belonging to another actor is a fold warning, never this command's
    outcome.
    """
    briefing = tmp_path / "briefing.md"
    briefing.write_text("the work record", encoding="utf-8")
    early = _remote_claim("2026-09-01T09:10:00Z", actor="operator@gamma")
    late = _remote_claim("2026-09-01T09:20:00Z", actor="reviewer@delta")
    git = FakeGit(remote_events=[early, late])

    done_run(repo, ITEM_ID, str(briefing), actor=ACTOR, runner=git)

    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]
    assert "claim.superseded" in capsys.readouterr().err


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


def test_the_tolerance_can_be_raised_for_one_crossing(repo, monkeypatch):
    monkeypatch.setenv("PINAX_CLOCK_TOLERANCE_S", "999999999")
    git = FakeGit(remote_events=[_remote_creation(FAR_AHEAD_TS, "pnx-c3d4")])

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


@pytest.mark.deep
@requires_git
def test_two_clones_claim_one_item_and_the_loser_exits_three(
    clone_wired_to_hub, clone_of_hub
):
    """
    Two machines, one item, no lock. Each appends and commits its own claim
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

    won = _pinax(alpha, "claim", item_id, "--actor", WINNER, "--json")
    winning_event = json.loads(won.stdout)["event_id"]

    lost = _pinax(beta, "claim", item_id, "--actor", LOSER, "--json", check=False)
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
