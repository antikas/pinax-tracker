"""
The publish sequence: fetch, union fold, append, commit, push.

Every case here drives the real command entry points (pinax.commands.claim
and pinax.commands.done, and, from section 9 on, the other seven mutating
commands, all routed onto the same sequence) with a substituted git
runner, so the whole sequence runs with no git process and no network: the
runner answers from a script and records every call it was asked to make.

Covered:
1.  The happy path: fetch, one union fold, append, commit with the
    'pinax: <event type> <item id>' subject, push of exactly the remote
    default branch, exit 0.
2.  The union fold decides the sequence number and the predecessor
    reference, so a remote event nobody has locally is respected.
3.  The two remote-read outcomes told apart: an unpublished branch folds as
    an empty remote, and a published branch whose shards cannot be read
    ends the command with exit 4 instead of silently losing its events.
4.  Off the remote default branch: append and commit, no push, the refused
    branch is printed, exit 0 for done and exit 4 for claim.
5.  No reachable remote, and a reachable remote with no default branch:
    exit 4 and nothing appended for claim, a local commit for done, each
    with a message that says which of the two happened.
6.  The commit's environment, a failed staging and a refused commit: the
    commit hands the hooks the resolved tracker root as PINAX_ROOT, so a
    hook that calls back into Pinax works on the tracker the command
    resolved and a stale pin in the ambient environment cannot refuse the
    command's own commit; a failed staging and a refused commit exit 7
    either way, reported apart, with the event appended and uncommitted
    and no push, a hook refusing a swallowed event log among them; and,
    when no hook refuses, the one-line notice about that swallowed log is
    printed once and the command ends normally.
7.  A rejected push: three attempts with a fetch and a merge between them,
    then exit 5 with a report naming the remote head and the local shard;
    for claim the just-minted event is annulled in the same shard and the
    report is still the only thing on stdout.
8.  The three source gates: no history-rewriting git command anywhere in
    pinax/, no clock read in the fold path, no hook bypass.
9.  Each of the seven remaining mutating commands (add, block, park,
    priority, dep add, dep rm, note add) runs the sequence exactly once,
    with its own event type in the commit subject.
10. The actor-handle rule: refused without '@host', accepted with it, for
    every command that mints an event.
11. Offline mode: the --offline flag, the PINAX_OFFLINE=1 environment
    variable, and claim's immunity to both.
12. The no-origin fallback for a command other than done: a warning naming
    the remote, and a local commit regardless.
13. No duplicated logic: no second git-commit function or exit mapping
    outside pinax.sync, and no leftover per-command copy of the actor and
    timestamp helpers now shared from pinax.doctor.
14. pinax.doctor: unsynced shards (committed, not on the remote) reported
    apart from uncommitted events (appended, not committed).
"""

from __future__ import annotations

import json
import os
import re

import pytest

from pinax import doctor, sync
from pinax.append import append_event
from pinax.commands.add import run as add_run
from pinax.commands.annul import run as annul_run
from pinax.commands.block import run as block_run
from pinax.commands.claim import run as claim_run
from pinax.commands.dep import run_add as dep_add_run
from pinax.commands.dep import run_rm as dep_rm_run
from pinax.commands.doctor_cmd import _print_report
from pinax.commands.done import run as done_run
from pinax.commands.note import run as note_run
from pinax.commands.park import run as park_run
from pinax.commands.priority import run as priority_run
from pinax.event import mint_event
from pinax.sync import GitResult


ACTOR = "builder@alpha"
OTHER_ACTOR = "operator@beta"
ITEM_ID = "pnx-a1b2"
REMOTE_HEAD = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"
MAIN_REF = "refs/remotes/origin/main"

PINAX_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pinax")

_OK = GitResult(returncode=0)


def _fail(text: str) -> GitResult:
    return GitResult(returncode=1, stderr=text)


class FakeGit:
    """
    A git runner that answers from a script and records what it was asked.

    It accepts exactly the calls the sequence is allowed to make; any other
    call fails the test on the spot.
    """

    def __init__(
        self,
        *,
        head_branch: str = "main",
        default_branch: str | None = "main",
        fetch_ok: bool = True,
        stage: GitResult = _OK,
        commit: GitResult = _OK,
        pushes: list[GitResult] | None = None,
        merge: GitResult = _OK,
        remote_events: list[dict] | None = None,
        read_error: str = "",
        toplevel: object = "<match>",
    ) -> None:
        self.head_branch = head_branch
        self.default_branch = default_branch
        self.fetch_ok = fetch_ok
        self.stage = stage
        self.commit = commit
        self.pushes = list(pushes) if pushes is not None else []
        self.merge = merge
        self.remote_events = remote_events
        self.read_error = read_error
        self.calls: list[tuple[str, ...]] = []
        # The environment handed to each recorded call, in the same order,
        # so a test can read what a git process (and the hooks it runs)
        # would have received. None means "this process's environment".
        self.call_envs: list[dict | None] = []
        self.refs_read: list[str] = []
        # "<match>" (default): repo_toplevel() echoes back whatever
        # repo_root it is asked about, i.e. repo_root is its own top
        # level -- the ordinary case every other test in this file
        # exercises. None: repo_root is not inside a git repository at
        # all. Any other string: the actual (different) top level
        # repo_root sits inside -- a foreign repository.
        self.toplevel = toplevel

    def repo_toplevel(self, repo_root: str) -> str | None:
        if self.toplevel is None:
            return None
        if self.toplevel == "<match>":
            return repo_root
        return self.toplevel

    def run(self, *args: str, env: dict | None = None) -> GitResult:
        self.calls.append(args)
        self.call_envs.append(env)
        name = args[0]
        if name == "fetch":
            return _OK if self.fetch_ok else _fail("fatal: could not read from remote")
        if name == "symbolic-ref":
            if self.default_branch is None:
                return _fail("fatal: ref refs/remotes/origin/HEAD is not a symbolic ref")
            return GitResult(returncode=0, stdout="origin/" + self.default_branch + "\n")
        if name == "for-each-ref":
            return GitResult(returncode=0, stdout="")
        if name == "add":
            return self.stage
        if name == "commit":
            return self.commit
        if name == "rev-parse":
            if "--abbrev-ref" in args:
                return GitResult(returncode=0, stdout=self.head_branch + "\n")
            return GitResult(returncode=0, stdout=REMOTE_HEAD + "\n")
        if name == "push":
            return self.pushes.pop(0) if self.pushes else _OK
        if name == "merge":
            return self.merge
        raise AssertionError(f"the sequence made an unexpected git call: {args}")

    def raw_events_at_ref(self, ref: str) -> list[dict] | None:
        self.refs_read.append(ref)
        if self.read_error:
            raise sync.RemoteReadError(ref, self.read_error)
        return list(self.remote_events) if self.remote_events is not None else None

    def named(self, name: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[0] == name]

    def env_for(self, name: str) -> dict | None:
        """The environment the first call to this git command received."""
        for call, env in zip(self.calls, self.call_envs):
            if call[0] == name:
                return env
        raise AssertionError(f"the sequence made no {name} call")


def _log_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".ergon", "log")


def _seed(repo_root: str, seq: int, ts: str, actor: str, etype: str, payload: dict) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(_log_dir(repo_root), event, actor=actor)
    return event


def _shard_events(repo_root: str, actor: str) -> list[dict]:
    shard = os.path.join(_log_dir(repo_root), actor.replace("@", "-") + ".jsonl")
    if not os.path.isfile(shard):
        return []
    with open(shard, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


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
        actor=OTHER_ACTOR,
        etype="item.created",
        payload={"item_id": ITEM_ID, "title": "Sequence under test", "prefix": "pnx"},
    )
    return root


@pytest.fixture()
def briefing(tmp_path):
    path = tmp_path / "briefing.md"
    path.write_text("the work record", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# 1. The happy path
# ---------------------------------------------------------------------------

def test_claim_fetches_commits_and_pushes_the_default_branch(repo, capsys):
    git = FakeGit(remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert ("fetch", "origin") in git.calls
    # Twice: the fold before the append, and the confirming fold over the
    # state the accepted push left on the remote.
    assert git.refs_read == [MAIN_REF, MAIN_REF]
    assert ("commit", "-m", f"pinax: item.claimed {ITEM_ID}") in git.calls
    assert ("push", "origin", "main:refs/heads/main") in git.calls
    # The fetch precedes the append, and the commit precedes the push.
    names = [call[0] for call in git.calls]
    assert names.index("fetch") < names.index("commit") < names.index("push")

    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.claimed"]
    payload = _last_json(capsys.readouterr().out)
    assert payload["item_id"] == ITEM_ID
    assert payload["event_id"] == appended[0]["id"]
    board = os.path.join(repo, ".ergon", "board.md")
    assert os.path.isfile(board)


def test_done_commits_and_pushes_with_its_own_subject(repo, briefing):
    git = FakeGit(remote_events=[])

    done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: item.completed {ITEM_ID}") in git.calls
    assert ("push", "origin", "main:refs/heads/main") in git.calls
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]


# ---------------------------------------------------------------------------
# 2. The union fold owns the sequence number and the predecessor reference
# ---------------------------------------------------------------------------

def test_union_fold_with_the_remote_decides_seq_and_prev(repo):
    remote_only = mint_event(
        seq=7,
        ts="2026-09-02T10:00:00Z",
        actor=ACTOR,
        etype="item.created",
        payload={"item_id": "pnx-c3d4", "title": "Only on the remote", "prefix": "pnx"},
    )
    git = FakeGit(remote_events=[remote_only])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    appended = _shard_events(repo, ACTOR)[-1]
    # The local log's highest seq is 0; the union's is 7.
    assert appended["seq"] == 8
    assert appended["prev"] == remote_only["id"]


# ---------------------------------------------------------------------------
# 3. An empty remote and an unreadable one are different outcomes
# ---------------------------------------------------------------------------

def test_an_unpublished_remote_branch_folds_as_an_empty_remote(repo):
    git = FakeGit(remote_events=None)

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert git.refs_read == [MAIN_REF, MAIN_REF]
    assert git.named("push")
    assert _shard_events(repo, ACTOR)[-1]["seq"] == 1


def test_an_unreadable_remote_exits_four_and_appends_nothing(repo, capsys):
    failure = "git cat-file failed for 9a8b:.ergon/log/one.jsonl: bad object"
    git = FakeGit(read_error=failure)

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    assert not git.named("commit")
    assert not git.named("push")
    assert _shard_events(repo, ACTOR) == []
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "remote_unreadable"
    assert report["appended"] is False
    assert report["committed"] is False
    assert report["remote_ref"] == MAIN_REF
    assert failure in report["git_output"]
    assert MAIN_REF in captured.err


def test_an_unreadable_remote_also_stops_a_completion(repo, briefing, capsys):
    git = FakeGit(read_error="git ls-tree failed for 9a8b:.ergon/log: bad object")

    with pytest.raises(SystemExit) as exited:
        done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    assert _shard_events(repo, ACTOR) == []
    assert _last_json(capsys.readouterr().out)["status"] == "remote_unreadable"


def test_the_remote_ref_is_read_through_replay(repo, monkeypatch):
    seen = []

    def _reader(repo_root, ref, *args, **kwargs):
        seen.append((repo_root, ref))
        return []

    monkeypatch.setattr(sync, "read_raw_events_at_ref", _reader)
    runner = sync.GitRunner(repo)
    monkeypatch.setattr(runner, "run", lambda *args: GitResult(0, "9a8b7c6d\n"))

    assert runner.raw_events_at_ref(MAIN_REF) == []
    assert seen == [(repo, MAIN_REF)]


def test_an_unresolvable_ref_is_an_empty_remote_without_a_read(repo, monkeypatch):
    def _reader(repo_root, ref, *args, **kwargs):
        raise AssertionError("an unresolvable ref must not be read at all")

    monkeypatch.setattr(sync, "read_raw_events_at_ref", _reader)
    runner = sync.GitRunner(repo)
    monkeypatch.setattr(runner, "run", lambda *args: _fail(""))

    assert runner.raw_events_at_ref(MAIN_REF) is None


def test_a_failed_shard_read_is_raised_not_folded_as_empty(repo, monkeypatch):
    def _reader(repo_root, ref, *args, **kwargs):
        raise sync.ReplayRefError("git cat-file failed for 9a8b:.ergon/log/one.jsonl")

    monkeypatch.setattr(sync, "read_raw_events_at_ref", _reader)
    runner = sync.GitRunner(repo)
    monkeypatch.setattr(runner, "run", lambda *args: GitResult(0, "9a8b7c6d\n"))

    with pytest.raises(sync.RemoteReadError) as raised:
        runner.raw_events_at_ref(MAIN_REF)

    assert raised.value.ref == MAIN_REF
    assert "cat-file" in raised.value.detail


def test_the_sequence_never_uses_the_remote_module():
    with open(os.path.join(PINAX_ROOT, "sync.py"), "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "fetch_remote_events" not in source
    assert "from .remote" not in source


# ---------------------------------------------------------------------------
# 4. Off the remote default branch
# ---------------------------------------------------------------------------

def test_done_off_the_default_branch_commits_locally_and_names_the_branch(
    repo, briefing, capsys
):
    git = FakeGit(head_branch="feature/x", remote_events=[])

    done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert git.named("commit")
    assert not git.named("push")
    captured = capsys.readouterr()
    assert "feature/x" in captured.err
    assert "main" in captured.err
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]


def test_claim_off_the_default_branch_exits_four_with_the_same_report(repo, capsys):
    git = FakeGit(head_branch="feature/x", remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    assert git.named("commit")
    assert not git.named("push")
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "committed_local"
    assert report["head_branch"] == "feature/x"
    assert report["remote_branch"] == "main"
    assert report["pushed"] is False
    assert "feature/x" in captured.err
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.claimed"]


# ---------------------------------------------------------------------------
# 5. No reachable remote, and a reachable remote with no default branch
# ---------------------------------------------------------------------------

def test_claim_without_a_reachable_remote_exits_four_and_appends_nothing(repo, capsys):
    git = FakeGit(fetch_ok=False)

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    assert git.named("fetch")
    assert not git.named("commit")
    assert not git.named("push")
    assert _shard_events(repo, ACTOR) == []
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "remote_unreachable"
    assert report["appended"] is False
    assert "could not be reached" in report["message"]
    assert "could not be reached" in captured.err


def test_done_without_a_reachable_remote_commits_locally(repo, briefing, capsys):
    git = FakeGit(fetch_ok=False)

    done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert git.named("commit")
    assert not git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]
    assert "could not be reached" in capsys.readouterr().err


def test_a_reachable_remote_with_no_default_branch_says_which_it_is(repo, capsys):
    git = FakeGit(default_branch=None)

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    assert _shard_events(repo, ACTOR) == []
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "no_default_branch"
    assert "was reached and publishes no default branch" in report["message"]
    assert "not reachable" not in captured.err
    assert "could not be reached" not in captured.err


def test_done_against_a_remote_with_no_default_branch_commits_locally(
    repo, briefing, capsys
):
    git = FakeGit(default_branch=None)

    done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert git.named("commit")
    assert not git.named("push")
    assert "publishes no default branch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 6. The commit's environment, a failed staging and a refused commit
# ---------------------------------------------------------------------------

# What the shipped pre-commit hook's own 'pinax verify' prints and fails
# with when a .gitignore rule swallows the event log.
SWALLOWED_LOG_REFUSAL = (
    "pinax verify: LOG SWALLOWED BY GITIGNORE - "
    ".ergon/log/pinax-doctor-probe.jsonl would be git-ignored right now."
)


def test_the_commit_hands_the_hooks_the_resolved_root(repo):
    """
    A hook gets no arguments from the sequence, so the resolved tracker
    root travels to it in the environment. Everything else the process
    already had is carried through unchanged.
    """
    git = FakeGit(remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    env = git.env_for("commit")
    assert env is not None, "the commit must be given an environment of its own"
    assert env["PINAX_ROOT"] == os.path.normpath(os.path.abspath(repo))
    assert {key: value for key, value in env.items() if key != "PINAX_ROOT"} == {
        key: value for key, value in os.environ.items() if key != "PINAX_ROOT"
    }


def test_a_stale_root_pin_in_the_environment_never_reaches_the_hook(repo, monkeypatch):
    """
    The whole point of passing the root: a command run with a correct root
    and a stale PINAX_ROOT in its own environment must not hand that stale
    value to the hook that verifies its commit.
    """
    monkeypatch.setenv("PINAX_ROOT", os.path.join(repo, "somewhere-else"))
    git = FakeGit(remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert git.env_for("commit")["PINAX_ROOT"] == os.path.normpath(os.path.abspath(repo))


def test_only_the_commit_carries_an_environment_of_its_own(repo):
    """
    Staging, fetching and pushing run no hook, so they keep the process's
    own environment: one place builds the commit's environment and nothing
    else is touched.
    """
    git = FakeGit(remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert git.env_for("add") is None
    assert git.env_for("fetch") is None
    assert git.env_for("push") is None


def test_a_refused_commit_exits_seven_and_leaves_the_event_uncommitted(repo, capsys):
    refusal = "pinax: frontmatter guard refused this commit"
    git = FakeGit(commit=GitResult(returncode=1, stderr=refusal), remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 7
    assert not git.named("push")

    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.claimed"]

    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "commit_refused"
    assert report["phase"] == "commit"
    assert report["committed"] is False
    assert report["event_id"] == appended[0]["id"]
    assert report["shard"].endswith("builder-alpha.jsonl")
    assert refusal in report["git_output"]
    assert appended[0]["id"] in captured.err
    assert "builder-alpha.jsonl" in captured.err
    assert refusal in captured.err


def test_a_failed_staging_is_reported_apart_from_a_refusal(repo, capsys):
    trouble = "fatal: unable to write new index file"
    git = FakeGit(stage=GitResult(returncode=1, stderr=trouble), remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 7
    assert not git.named("commit")
    assert not git.named("push")
    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "stage_failed"
    assert report["phase"] == "stage"
    assert "could not be staged" in report["message"]
    assert trouble in report["git_output"]
    assert "refused" not in report["message"]
    assert "could not be staged" in captured.err


def test_a_hook_refusing_a_swallowed_log_exits_seven_like_any_other_refusal(
    repo, capsys
):
    """
    A swallowed event log is refused by the shipped hook's own verify, so
    the command that appended the event ends exactly as any other hook
    refusal does: the event is appended and uncommitted, nothing is
    published, the warning names the shard and the event id, the hook's
    own output is reported, and the exit is 7. The refusal is never turned
    into a warning the command survives.
    """
    git = FakeGit(
        commit=GitResult(returncode=1, stderr=SWALLOWED_LOG_REFUSAL),
        remote_events=[],
    )

    with pytest.raises(SystemExit) as exited:
        add_run(repo, "still recorded", prefix="pnx", actor=ACTOR, runner=git)

    assert exited.value.code == 7
    assert not git.named("push")

    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.created"]

    captured = capsys.readouterr()
    report = _last_json(captured.out)
    assert report["status"] == "commit_refused"
    assert report["committed"] is False
    assert report["event_id"] == appended[0]["id"]
    assert SWALLOWED_LOG_REFUSAL in report["git_output"]

    assert appended[0]["id"] in captured.err
    assert "builder-alpha.jsonl" in captured.err
    assert "appended and not committed" in captured.err
    assert SWALLOWED_LOG_REFUSAL in captured.err


def test_a_swallowed_log_no_hook_refuses_is_one_notice_and_a_normal_exit(
    repo, capsys, monkeypatch
):
    """
    The other half of the swallowed-log behaviour: in a repository whose
    hooks refuse nothing, the command commits, prints its one-line notice
    exactly once, and ends normally. The notice reports the condition; it
    never decides the commit.

    The git probe behind the notice needs a real repository and is the
    deep lane's subject; what is pinned here is the notice a command
    prints when that probe says the log is swallowed, and the fact that
    the command still finishes.
    """
    monkeypatch.setattr(
        doctor,
        "log_tracking_status",
        lambda repo_root, log_dir, log_subpath=".ergon/log": {
            "available": True,
            "ignored": True,
            "probe_path": ".ergon/log/pinax-doctor-probe.jsonl",
            "shards_on_disk": 1,
            "shards_tracked": 1,
        },
    )
    git = FakeGit(remote_events=[])

    add_run(repo, "recorded while the log is ignored", prefix="pnx", actor=ACTOR,
            as_json=True, runner=git)

    captured = capsys.readouterr()
    notices = [line for line in captured.err.splitlines() if "pinax: WARNING" in line]
    assert len(notices) == 1
    assert notices[0].startswith(
        "pinax: WARNING - the event log is being swallowed by a .gitignore rule "
        "(.ergon/log/pinax-doctor-probe.jsonl would be git-ignored right now)."
    )
    assert "pinax init" in notices[0] and "pinax doctor" in notices[0]

    # The command ran to its end: the commit was made and the result was
    # printed, which only happens when the sequence did not end the
    # command with a non-zero code.
    appended = _shard_events(repo, ACTOR)
    new_item_id = appended[0]["payload"]["item_id"]
    assert ("commit", "-m", f"pinax: item.created {new_item_id}") in git.calls
    assert json.loads(captured.out.strip().splitlines()[-1])["item_id"] == new_item_id


# ---------------------------------------------------------------------------
# 7. A rejected push
# ---------------------------------------------------------------------------

def test_a_second_attempt_pulls_refolds_and_succeeds(repo):
    git = FakeGit(pushes=[_fail("rejected: fetch first"), _OK], remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert len(git.named("push")) == 2
    assert ("fetch", "origin", "main") in git.calls
    assert git.named("merge") == [("merge", "--no-edit", "FETCH_HEAD")]
    # Three reads of the remote ref: the fold before the append, the re-fold
    # after the merge, and the confirming fold after the accepted push.
    assert git.refs_read == [MAIN_REF, MAIN_REF, MAIN_REF]


def test_three_rejected_pushes_exit_five_and_annul_the_claim(repo, capsys):
    rejection = "rejected: non-fast-forward"
    git = FakeGit(
        pushes=[_fail(rejection), _fail(rejection), _fail(rejection)],
        remote_events=[],
    )

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    assert exited.value.code == 5
    assert len(git.named("push")) == 3
    assert len([call for call in git.calls if call[:2] == ("fetch", "origin") and len(call) == 3]) == 2
    assert len(git.named("merge")) == 2

    captured = capsys.readouterr()
    # Under --json the report is the only thing on stdout: the annulment's
    # own console output is folded into it, never printed beside it.
    report = json.loads(captured.out)
    assert report["status"] == "push_rejected"
    assert report["attempts"] == 3
    assert report["remote_head"] == REMOTE_HEAD
    assert report["shard"].endswith("builder-alpha.jsonl")
    assert rejection in report["git_output"]

    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.claimed", "event.annulled"]
    assert appended[1]["payload"]["target_id"] == appended[0]["id"]
    assert report["annulled"]["target_id"] == appended[0]["id"]
    assert appended[1]["id"][:12] in report["annulled"]["output"]


def test_three_rejected_pushes_exit_five_without_annulling_a_completion(
    repo, briefing, capsys
):
    rejection = "rejected: non-fast-forward"
    git = FakeGit(
        pushes=[_fail(rejection), _fail(rejection), _fail(rejection)],
        remote_events=[],
    )

    with pytest.raises(SystemExit) as exited:
        done_run(repo, ITEM_ID, briefing, actor=ACTOR, runner=git)

    assert exited.value.code == 5
    report = _last_json(capsys.readouterr().out)
    assert "annulled" not in report
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.completed"]


def test_a_projection_conflict_in_the_pull_is_resolved_by_regeneration(repo):
    """
    A pull that conflicts in the generated Markdown does not stop the
    retry. The projection is regenerated from the merged log and committed,
    which is also what resolves the conflict (ADR-002), and the next push
    carries it. A conflict anywhere else is git's to refuse; that case is
    covered in tests/test_sync_bare_hub.py.
    """
    git = FakeGit(
        pushes=[_fail("rejected"), _OK, _OK],
        merge=GitResult(
            returncode=1, stdout="CONFLICT (content): Merge conflict in .ergon/board.md"
        ),
        remote_events=[],
    )

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert len(git.named("push")) == 2
    assert (
        "commit",
        "-m",
        "pinax: regenerate the projection from the merged main",
    ) in git.calls


def test_an_unreadable_remote_during_a_retry_exits_four(repo, capsys):
    class FailsOnSecondRead(FakeGit):
        def raw_events_at_ref(self, ref):
            if self.refs_read:
                self.refs_read.append(ref)
                raise sync.RemoteReadError(ref, "git ls-tree failed: bad object")
            return super().raw_events_at_ref(ref)

    git = FailsOnSecondRead(pushes=[_fail("rejected"), _OK], remote_events=[])

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == 4
    report = _last_json(capsys.readouterr().out)
    assert report["status"] == "remote_unreadable"
    assert report["appended"] is True
    assert report["committed"] is True


def test_a_failing_annulment_keeps_its_output_and_surfaces(repo, capsys, monkeypatch):
    """
    The annulment's console output is folded into the report even when the
    annulment itself fails, and the failure is never swallowed.
    """
    def _raising(*args, **kwargs):
        print("pinax: event annulled")
        raise RuntimeError("the annulment could not be appended")

    monkeypatch.setattr("pinax.commands.annul.append_local", _raising)

    record = {"reason": "unpublished", "output": ""}
    with pytest.raises(RuntimeError):
        sync._annul_own_event(repo, {"id": "9a8b7c6d"}, ACTOR, record)
    assert "annulled" in record["output"]

    rejection = "rejected: non-fast-forward"
    git = FakeGit(
        pushes=[_fail(rejection), _fail(rejection), _fail(rejection)],
        remote_events=[],
    )
    with pytest.raises(RuntimeError):
        claim_run(repo, ITEM_ID, actor=ACTOR, as_json=True, runner=git)

    # Console output stays where it belongs: nothing leaked to stdout, and
    # stdout is still the real one after the redirect unwound.
    assert "annulled" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 8. Source gates
# ---------------------------------------------------------------------------

def _python_sources(root: str) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        for filename in filenames:
            if filename.endswith(".py"):
                found.append(os.path.join(dirpath, filename))
    return sorted(found)


def _matches(paths: list[str], patterns: tuple[str, ...]) -> list[str]:
    hits = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                for pattern in patterns:
                    if re.search(pattern, line):
                        hits.append(f"{os.path.basename(path)}:{number}: {line.strip()}")
    return hits


_REWRITE_PATTERNS = (
    r"\brebase\b",
    r"--force\b",
    r"\bamend\b",
    r"\breset\b",
    r"\bfilter-branch\b",
    r"\bupdate-ref\b",
    r"--delete\b",
    # A forced push written as a refspec, for example "+main:refs/heads/main".
    r"[\"']\+[^\"']*:",
    r"[\"']-f[\"']",
    r"[\"']-D[\"']",
    r"[\"']-d[\"']",
)

_CLOCK_PATTERNS = (
    r"\butcnow\b",
    r"datetime\.now\b",
    r"\btime\.time\b",
)

# Both ways a commit can be made to run no hook: skipping verification, and
# pointing the repository at a different (or empty) hooks directory.
_HOOK_BYPASS_PATTERNS = (r"--no-verify", r"hooksPath")


def test_no_history_rewrite_command_anywhere_in_pinax():
    """
    Nothing under pinax/ can re-apply commits onto a new base, overwrite a
    remote branch, discard a commit, rewrite one, or delete a branch or a
    remote ref. The log is append-only and published history is never
    rewritten (ADR-001).

    Strength of this gate: it is a per-line pattern check over the source
    text, so it catches the literal forms a change would normally
    introduce. An argument list assembled at run time from variables, or a
    token split across lines, would evade it.
    """
    hits = _matches(_python_sources(PINAX_ROOT), _REWRITE_PATTERNS)
    assert not hits, "history-rewriting git usage found under pinax/:\n" + "\n".join(hits)


def test_no_clock_read_in_the_fold_path():
    """
    The fold and the sequence's fold path are pure functions of the events
    they read (ADR-001). A command still mints its event timestamp; that is
    the caller's job and stays in the command.

    Same strength and same blind spot as the gate above: it reads the source
    text line by line.
    """
    paths = [os.path.join(PINAX_ROOT, "fold.py"), os.path.join(PINAX_ROOT, "sync.py")]
    hits = _matches(paths, _CLOCK_PATTERNS)
    assert not hits, "a clock read entered the fold path:\n" + "\n".join(hits)


def test_no_hook_bypass_anywhere_in_pinax():
    """
    A repository's hooks run on every commit the tracker makes: nothing
    under pinax/ skips verification or repoints the hooks directory. Same
    per-line source check, same blind spot for a run-time-assembled
    argument list.
    """
    hits = _matches(_python_sources(PINAX_ROOT), _HOOK_BYPASS_PATTERNS)
    assert not hits, "a commit bypasses the repository's hooks:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 9. Each of the seven remaining mutating commands routes through the sequence
# ---------------------------------------------------------------------------

def test_add_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    add_run(repo, "A new item", prefix="pnx", actor=ACTOR, as_json=True, runner=git)

    appended = _shard_events(repo, ACTOR)
    assert [event["type"] for event in appended] == ["item.created"]
    new_item_id = appended[0]["payload"]["item_id"]
    # One sequence: the fetch before the fold and the confirming fetch after
    # the accepted push, and no third one.
    assert len(git.named("fetch")) == 2
    assert ("commit", "-m", f"pinax: item.created {new_item_id}") in git.calls
    assert git.named("push")


def test_block_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    block_run(repo, ITEM_ID, "scope", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: item.blocked {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.blocked"]


def test_park_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    park_run(repo, ITEM_ID, "waiting on X", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: item.parked {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.parked"]


def test_priority_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    priority_run(repo, ITEM_ID, "top", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: item.priority_set {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.priority_set"]


def test_dep_add_runs_the_sequence_once_with_its_own_subject(repo):
    other_id = "pnx-c3d4"
    _seed(
        repo, seq=1, ts="2026-09-01T09:01:00Z", actor=OTHER_ACTOR, etype="item.created",
        payload={"item_id": other_id, "title": "Other", "prefix": "pnx"},
    )
    git = FakeGit(remote_events=[])

    dep_add_run(repo, ITEM_ID, other_id, edge_type="blocks", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: dep.added {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["dep.added"]


def test_dep_rm_runs_the_sequence_once_with_its_own_subject(repo):
    other_id = "pnx-c3d4"
    _seed(
        repo, seq=1, ts="2026-09-01T09:01:00Z", actor=OTHER_ACTOR, etype="item.created",
        payload={"item_id": other_id, "title": "Other", "prefix": "pnx"},
    )
    git = FakeGit(remote_events=[])

    dep_rm_run(repo, ITEM_ID, other_id, edge_type="blocks", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: dep.removed {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["dep.removed"]


def test_note_add_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])

    note_run(repo, ITEM_ID, "docs/example.md", None, actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: note.added {ITEM_ID}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["note.added"]


def test_annul_runs_the_sequence_once_with_its_own_subject(repo):
    git = FakeGit(remote_events=[])
    target = "9a8b7c6d5e4f"

    annul_run(repo, target, "bad event", actor=ACTOR, runner=git)

    assert ("commit", "-m", f"pinax: event.annulled {target}") in git.calls
    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["event.annulled"]


# ---------------------------------------------------------------------------
# 10. The actor-handle rule
# ---------------------------------------------------------------------------

def test_run_sequence_refuses_an_actor_without_at_host(repo):
    git = FakeGit(remote_events=[])

    outcome = sync.run_sequence(
        repo,
        event_type="item.blocked",
        payload={"item_id": ITEM_ID, "gate": "scope"},
        actor="builder",
        ts="2026-09-01T10:00:00Z",
        item_id=ITEM_ID,
        runner=git,
    )

    assert outcome.status == sync.INVALID_ACTOR
    assert outcome.exit_code == sync.EXIT_INVALID_ACTOR
    assert outcome.event is None
    # Nothing was even asked of the runner: the refusal fires before the
    # sequence touches git at all, not merely before it commits.
    assert not git.calls
    assert _shard_events(repo, "builder") == []
    # run_sequence itself never prints; conclude() does that at the CLI
    # boundary. The refusal is carried in the outcome's notes and report.
    assert any("builder" in note and "role@host" in note for note in outcome.notes)
    assert "builder" in outcome.report["message"]
    assert "role@host" in outcome.report["message"]


def test_run_sequence_accepts_an_actor_with_at_host(repo):
    git = FakeGit(remote_events=[])

    outcome = sync.run_sequence(
        repo,
        event_type="item.blocked",
        payload={"item_id": ITEM_ID, "gate": "scope"},
        actor="builder@olympos",
        ts="2026-09-01T10:00:00Z",
        item_id=ITEM_ID,
        runner=git,
    )

    assert outcome.status == sync.PUSHED
    assert outcome.exit_code == sync.EXIT_OK


def test_block_refuses_an_actor_without_at_host(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        block_run(repo, ITEM_ID, "scope", actor="builder", runner=git)

    assert exited.value.code == sync.EXIT_INVALID_ACTOR
    assert not git.calls
    assert _shard_events(repo, "builder") == []
    assert "role@host" in capsys.readouterr().err


def test_block_accepts_an_actor_with_at_host(repo):
    git = FakeGit(remote_events=[])

    block_run(repo, ITEM_ID, "scope", actor="builder@olympos", runner=git)

    appended = _shard_events(repo, "builder@olympos")
    assert [event["type"] for event in appended] == ["item.blocked"]


# ---------------------------------------------------------------------------
# 11. Offline mode
# ---------------------------------------------------------------------------

def test_offline_flag_skips_fetch_and_push_but_still_commits(repo):
    git = FakeGit(remote_events=[])

    add_run(repo, "Offline item", prefix="pnx", actor=ACTOR, as_json=True, offline=True, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.created"]


def test_pinax_offline_env_var_skips_fetch_and_push_but_still_commits(repo, monkeypatch):
    monkeypatch.setenv("PINAX_OFFLINE", "1")
    git = FakeGit(remote_events=[])

    block_run(repo, ITEM_ID, "scope", actor=ACTOR, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert git.named("commit")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.blocked"]


def test_offline_mode_reports_a_clear_local_only_note(repo, capsys):
    git = FakeGit(remote_events=[])

    park_run(repo, ITEM_ID, "waiting", actor=ACTOR, offline=True, runner=git)

    err = capsys.readouterr().err
    assert "offline" in err.lower()
    assert "recorded locally" in err


def test_claim_never_goes_offline_even_with_the_env_var_set(repo, monkeypatch):
    """
    claim has no --offline flag in __main__.py, and pinax.sync gates offline
    strictly on requires_remote: PINAX_OFFLINE=1 must not make claim skip
    its fetch or its push.
    """
    monkeypatch.setenv("PINAX_OFFLINE", "1")
    git = FakeGit(remote_events=[])

    claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert git.named("fetch")
    assert git.named("push")


def test_claim_parser_has_no_offline_flag():
    from pinax.__main__ import main

    with pytest.raises(SystemExit) as exited:
        main(["claim", ITEM_ID, "--offline"])

    # argparse's own usage-error exit code for an unrecognised argument;
    # this never reaches _find_repo_root or touches a repository at all.
    assert exited.value.code == 2


# ---------------------------------------------------------------------------
# 12. The no-origin fallback for a command other than done
# ---------------------------------------------------------------------------

def test_park_without_a_reachable_remote_commits_locally_with_a_warning(repo, capsys):
    git = FakeGit(fetch_ok=False)

    park_run(repo, ITEM_ID, "blocked on X", actor=ACTOR, runner=git)

    assert git.named("commit")
    assert not git.named("push")
    err = capsys.readouterr().err
    assert "origin" in err
    assert "could not be reached" in err
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.parked"]


# ---------------------------------------------------------------------------
# 13. No duplicated logic
# ---------------------------------------------------------------------------

_SEVEN_COMMAND_FILES = (
    "add.py", "block.py", "park.py", "priority.py", "dep.py", "note.py", "annul.py",
)


def test_no_command_module_reimplements_the_commit_or_exit_mapping():
    """
    The seven command modules hand their event to sync.run_sequence and
    stop: none of them builds its own git commit call or its own
    status-to-exit-code mapping.
    """
    hits = []
    for name in _SEVEN_COMMAND_FILES:
        path = os.path.join(PINAX_ROOT, "commands", name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for pattern in (r"_exit_code\s*\(", r"class\s+GitRunner", r'"commit"\s*,\s*"-m"'):
            if re.search(pattern, text):
                hits.append(f"{name}: matched {pattern}")
    assert not hits, "a command module duplicates sync's own machinery:\n" + "\n".join(hits)


def test_no_leftover_utc_now_iso_or_default_actor_copy():
    """
    _utc_now_iso and _default_actor have one owner, pinax.doctor
    (utc_now_iso and default_actor there); the seven command modules and
    doctor_cmd import it instead of keeping a per-module copy.

    claim.py and done.py are not checked here: they still carry their own
    copies of these two helpers, a known duplication left for the module
    that owns them to close.
    """
    hits = []
    for name in _SEVEN_COMMAND_FILES + ("doctor_cmd.py",):
        path = os.path.join(PINAX_ROOT, "commands", name)
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        for pattern in (r"def _utc_now_iso\s*\(", r"def _default_actor\s*\("):
            if re.search(pattern, text):
                hits.append(f"{name}: matched {pattern}")
    assert not hits, "a leftover per-command copy remains:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# 14. pinax.doctor: unsynced shards, reported apart from uncommitted events
# ---------------------------------------------------------------------------

class FakeRefGit:
    """
    A runner that answers raw_events_at_ref per exact ref, for testing
    doctor.unsynced_shard_events.

    Distinct from FakeGit above: FakeGit answers every ref with the same
    canned pool (fine for the sequence, which reads at most one remote ref
    per call), but proving the unsynced diff needs HEAD and the remote ref
    to carry different content in the same test.
    """

    def __init__(self, *, default_branch="main", refs=None, read_error_refs=None):
        self.default_branch = default_branch
        self.refs = refs if refs is not None else {}
        self.read_error_refs = read_error_refs if read_error_refs is not None else set()
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> GitResult:
        self.calls.append(args)
        name = args[0]
        if name == "symbolic-ref":
            if self.default_branch is None:
                return _fail("fatal: ref refs/remotes/origin/HEAD is not a symbolic ref")
            return GitResult(returncode=0, stdout="origin/" + self.default_branch + "\n")
        if name == "for-each-ref":
            return GitResult(returncode=0, stdout="")
        raise AssertionError(f"unexpected git call in FakeRefGit: {args}")

    def raw_events_at_ref(self, ref: str):
        if ref in self.read_error_refs:
            raise sync.RemoteReadError(ref, "boom")
        return self.refs.get(ref)


def test_unsynced_shard_events_lists_only_what_head_has_that_the_remote_lacks():
    head_only = mint_event(
        seq=0, ts="2026-09-01T09:00:00Z", actor=ACTOR, etype="item.created",
        payload={"item_id": ITEM_ID, "title": "x", "prefix": "pnx"},
    )
    on_both = mint_event(
        seq=1, ts="2026-09-01T09:01:00Z", actor=ACTOR, etype="item.blocked",
        payload={"item_id": ITEM_ID, "gate": "scope"},
    )
    git = FakeRefGit(refs={
        "HEAD": [head_only, on_both],
        "refs/remotes/origin/main": [on_both],
    })

    result = doctor.unsynced_shard_events("/repo", runner=git)

    assert result["available"] is True
    assert result["remote_branch"] == "main"
    assert [e["id"] for e in result["events"]] == [head_only["id"]]


def test_unsynced_shard_events_with_no_default_branch_reports_all_head_events():
    only_local = mint_event(
        seq=0, ts="2026-09-01T09:00:00Z", actor=ACTOR, etype="item.created",
        payload={"item_id": ITEM_ID, "title": "x", "prefix": "pnx"},
    )
    git = FakeRefGit(default_branch=None, refs={"HEAD": [only_local]})

    result = doctor.unsynced_shard_events("/repo", runner=git)

    assert result["available"] is True
    assert result["remote_branch"] is None
    assert [e["id"] for e in result["events"]] == [only_local["id"]]


def test_unsynced_shard_events_reports_unavailable_on_a_read_error():
    git = FakeRefGit(refs={"HEAD": []}, read_error_refs={"refs/remotes/origin/main"})

    result = doctor.unsynced_shard_events("/repo", runner=git)

    assert result["available"] is False
    assert result["remote_branch"] == "main"


def test_doctor_report_shows_unsynced_and_uncommitted_as_two_distinct_sections(capsys):
    report = {
        "now": "2026-09-08T12:00:00Z",
        "stale_hours": 24,
        "install_health": {
            "executable": None, "hook_resolution_mode": "absent",
            "console": {}, "editable_install_target": None,
        },
        "uncommitted": {
            "available": True,
            "files": [{"path": ".ergon/log/builder-alpha.jsonl", "state": "modified"}],
            "events": [{
                "id": "aaaaaaaaaaaa", "seq": 3, "ts": "2026-09-08T11:00:00Z",
                "actor": "builder@alpha", "type": "item.blocked", "item_id": ITEM_ID,
                "shard": ".ergon/log/builder-alpha.jsonl",
            }],
        },
        "unsynced": {
            "available": True,
            "remote_branch": "main",
            "events": [{
                "id": "bbbbbbbbbbbb", "seq": 2, "ts": "2026-09-08T10:00:00Z",
                "actor": "builder@alpha", "type": "item.parked", "item_id": ITEM_ID,
                "shard": ".ergon/log/builder-alpha.jsonl",
            }],
        },
        "stale_claims": [],
        "legacy": {"checked": False, "path": None, "contradictions": []},
        "log_tracking": {
            "available": True, "ignored": False, "probe_path": "x",
            "shards_on_disk": 1, "shards_tracked": 1,
        },
        "findings": 1,
    }

    _print_report("/repo", report)

    out = capsys.readouterr().out
    assert "[1] uncommitted shard events: 1 event(s)" in out
    assert "[5] unsynced shards: 1 event(s)" in out
    assert "aaaaaaaaaaaa" in out
    assert "bbbbbbbbbbbb" in out
    # Distinct wording: the unsynced section always names what it is not
    # yet on; the uncommitted section never does.
    assert "not yet on origin/main" in out
    assert "uncommitted .ergon file(s)" in out
    assert "not yet on" not in out.split("[1]")[1].split("[2]")[0]


# ---------------------------------------------------------------------------
# 15. repo_root must be the top level of its own git repository
# ---------------------------------------------------------------------------

def test_repo_root_as_the_top_level_publishes_normally(repo):
    """The ordinary case every other test in this file already exercises,
    named explicitly: repo_toplevel() matching repo_root is not a special
    case, it is the default FakeGit already answers."""
    git = FakeGit(remote_events=[], toplevel="<match>")

    block_run(repo, ITEM_ID, "scope", actor=ACTOR, runner=git)

    assert git.named("push")
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.blocked"]


def test_no_git_repository_at_all_still_appends_locally_with_a_warning(repo, capsys):
    """
    The direct test the no-repository path lacked: repo_toplevel()
    returning None (rev-parse --show-toplevel fails) is the existing
    not-a-git-repository fallback, exercised here through the fake runner
    instead of only through a real non-git directory.
    """
    git = FakeGit(remote_events=[], toplevel=None)

    block_run(repo, ITEM_ID, "scope", actor=ACTOR, runner=git)

    assert not git.named("fetch")
    assert not git.named("push")
    assert not git.named("commit")
    err = capsys.readouterr().err
    assert "not a git repository" in err
    assert [event["type"] for event in _shard_events(repo, ACTOR)] == ["item.blocked"]


def test_repo_root_inside_a_foreign_repository_refuses_and_appends_nothing(repo, capsys):
    """
    repo_root resolving to a git repository whose top level is a DIFFERENT
    directory (repo_root is inside it, but is not it) must never be
    committed into: that history belongs to the enclosing repository, not
    this tracker's own log.
    """
    foreign_toplevel = os.path.dirname(repo)
    git = FakeGit(remote_events=[], toplevel=foreign_toplevel)

    with pytest.raises(SystemExit) as exited:
        block_run(repo, ITEM_ID, "scope", actor=ACTOR, runner=git)

    assert exited.value.code == sync.EXIT_REMOTE_REQUIRED
    assert not git.named("fetch")
    assert not git.named("add")
    assert not git.named("commit")
    assert not git.named("push")
    assert _shard_events(repo, ACTOR) == []
    err = capsys.readouterr().err
    assert repo in err
    assert foreign_toplevel in err
    assert "so nothing was appended" in err


def test_claim_inside_a_foreign_repository_also_refuses(repo, capsys):
    foreign_toplevel = os.path.dirname(repo)
    git = FakeGit(remote_events=[], toplevel=foreign_toplevel)

    with pytest.raises(SystemExit) as exited:
        claim_run(repo, ITEM_ID, actor=ACTOR, runner=git)

    assert exited.value.code == sync.EXIT_REMOTE_REQUIRED
    assert not git.calls
    assert _shard_events(repo, ACTOR) == []


# ---------------------------------------------------------------------------
# 16. add's actor refusal never reports an id for an item that was never
#     created
# ---------------------------------------------------------------------------

def test_add_with_an_invalid_actor_reports_no_item_id(repo, capsys):
    git = FakeGit(remote_events=[])

    with pytest.raises(SystemExit) as exited:
        add_run(repo, "Should not exist", prefix="pnx", actor="builder", as_json=True, runner=git)

    assert exited.value.code == sync.EXIT_INVALID_ACTOR
    assert not git.calls

    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert report.get("item_id") is None
    assert "no item was created" in report["message"]

    # No item.created event exists for any actor: nothing was minted, let
    # alone appended, and the fold still holds only the seeded item.
    from pinax.fold import fold
    state = fold(os.path.join(repo, ".ergon", "log"))
    assert set(state["items"].keys()) == {ITEM_ID}
