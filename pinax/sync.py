"""
pinax.sync - the one publish sequence every mutating command runs.

A tracker state change must be visible on every other machine as soon as it
happens, so a command does not simply append: it hands this module the event
it wants to record and this module runs the whole sequence.

  refuse an actor handle that is not role@host
  fetch origin, unless offline mode applies
  fold the union of the local log and the remote default branch's
    committed shards (local log only, when offline)
  refuse a claim whose intended timestamp is behind the newest published
    event by more than the clock tolerance, before anything is minted
  append the event
  regenerate the projection
  commit the shard and the projection with the repository's hooks running
    and the resolved tracker root in their environment
  push the remote default branch, and only when it is checked out and
    offline mode does not apply
  fetch once more, fold over the state the remote now holds, and end the
    command from that fold

Offline mode is requested with the caller's offline=True (the CLI's
--offline flag) or the PINAX_OFFLINE=1 environment variable, and it always
applies when a command that does not require the remote runs in a
repository with no reachable origin or no published default branch. A
command that requires the remote (claim is the only one) never goes
offline: it always fetches, and a remote it cannot reach or read ends the
command instead of silently continuing without it.

repo_root must itself be the top level of its git repository. When
repo_root resolves to a git repository whose top level is a different
directory (repo_root sits inside it, but is not it), the sequence refuses
before doing anything else: committing into an enclosing repository's own
history would be a different, foreign log, never this one's.

The commit runs the repository's hooks and hands them the resolved tracker
root as PINAX_ROOT. A hook receives no arguments from this sequence, so a
hook that calls back into Pinax (the shipped pre-commit hook runs
'pinax verify') would otherwise pin its root from whatever the caller's
environment happened to carry, and a stale value there would refuse the
command's own commit. Nothing else in the environment changes and no hook
is skipped. A hook that refuses the commit still ends the command with
exit 7 and leaves the appended event uncommitted: a swallowed event log,
which the shipped hook's own verify refuses, ends that way like any other
refusal.

The fold is the shared one in pinax.fold, over the union of
pinax.fold.read_raw_events (the working tree) and
pinax.replay.read_raw_events_at_ref (the remote default branch as committed).
There is no second fold here. There is no clock read here either: the caller
mints the event timestamp and passes it in, so the sequence is a function of
the repository state and its inputs alone.

The fold also decides how a published claim ends. After an accepted push
the sequence fetches once more and folds again over the state the remote
now holds; a claim that fold reports superseded ends with exit 3. The fold
names a superseded claim by the id of the event it superseded, and this
module minted the event it is asking about, so its own event is recognised
by that id alone. There is no ownership check anywhere here, before the
append or after it: the fold is the sole reconciler.

Every git call goes through GitRunner, so a caller can substitute a runner
and drive the whole sequence with no git process at all. No call in this
module rewrites published history: it never re-applies commits onto a new
base, never overwrites a remote branch, never discards a commit and never
removes a branch, and it never bypasses a hook. The pull after a rejected
push is written as its two halves, a fetch and a merge, so no local
configuration can turn it into a rewrite. After that pull the projection is
regenerated from the merged log and committed before the next push
attempt, which is also how a conflict in the generated Markdown is
resolved (ADR-002).

Exit codes are mapped in exactly one place, _exit_code:

  0  the event is committed, and pushed when the remote default branch is
     the checked-out branch
  2  the actor handle is not role@host; nothing was minted
  3  the event is published and the fold over the pushed remote state
     reports this claim superseded by an earlier one
  4  the remote's state could not be read, the command needed the remote
     and the event could not be published, or repo_root is not the top
     level of its git repository (nothing was minted either way)
  5  the push was rejected on every attempt
  6  a claim's intended timestamp is behind the newest published event by
     more than the clock tolerance, or cannot be read in the envelope's
     own form; the two are reported apart and nothing was minted either way
  7  the shard and the projection could not be staged, or the commit was
     refused; either way the appended event stays uncommitted

pinax.doctor reuses GitRunner and _default_branch, read-only, to compare
what HEAD carries against the remote default branch without fetching; see
pinax.doctor.unsynced_shard_events. That is the only reuse of this module's
internals from outside it.
"""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from .append import append_event
from .event import mint_event
from .fold import _collect_annulled_ids, finalise_events, fold_events, read_raw_events
from .projection import regenerate
from .replay import ReplayRefError, read_raw_events_at_ref

_REMOTE = "origin"
_REMOTE_REF_PREFIX = f"refs/remotes/{_REMOTE}/"
_MAX_PUSH_ATTEMPTS = 3
_ANNUL_REASON = "the event could not be published, so it never took effect"
_OFFLINE_ENV_VAR = "PINAX_OFFLINE"
_ROOT_ENV_VAR = "PINAX_ROOT"
_CLOCK_TOLERANCE_ENV_VAR = "PINAX_CLOCK_TOLERANCE_S"

# How far behind the newest published event a claim's own timestamp may sit
# before the claim is refused, in seconds. PINAX_CLOCK_TOLERANCE_S overrides
# it; _clock_tolerance is the one place either value is read.
_DEFAULT_CLOCK_TOLERANCE_S = 5.0

# The timestamp form every Pinax event carries: UTC, whole seconds, fixed
# width. Parsing it is arithmetic on recorded text, not a clock read.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# The generated Markdown the projection owns, relative to .ergon. A pull
# resolves a conflict in these by regenerating them from the merged log
# (ADR-002), so the sequence needs to know which files those are.
_BOARD_NAME = "board.md"
_ITEMS_DIR_NAME = "items"

# role@host: one '@', a non-empty role and a non-empty host, neither side
# carrying whitespace or a second '@'.
_ACTOR_HANDLE_RE = re.compile(r"^[^\s@]+@[^\s@]+$")

EXIT_OK = 0
EXIT_INVALID_ACTOR = 2
EXIT_CLAIM_SUPERSEDED = 3
EXIT_REMOTE_REQUIRED = 4
EXIT_PUSH_EXHAUSTED = 5
EXIT_CLOCK_BEHIND = 6
EXIT_COMMIT_REFUSED = 7

INVALID_ACTOR = "invalid_actor"
PUSHED = "pushed"
COMMITTED_LOCAL = "committed_local"
REMOTE_UNREACHABLE = "remote_unreachable"
NO_DEFAULT_BRANCH = "no_default_branch"
NOT_A_GIT_REPOSITORY = "not_a_git_repository"
FOREIGN_REPOSITORY = "foreign_repository"
REMOTE_UNREADABLE = "remote_unreadable"
STAGE_FAILED = "stage_failed"
COMMIT_REFUSED = "commit_refused"
PUSH_REJECTED = "push_rejected"
CLAIM_SUPERSEDED = "claim_superseded"
CLOCK_BEHIND_REMOTE = "clock_behind_remote"
UNREADABLE_TIMESTAMP = "unreadable_timestamp"

# Outcomes that published nothing but left the repository consistent. They
# are acceptable for a command that tolerates local-only work and are a
# failure for one that does not.
_LOCAL_ONLY = frozenset(
    {COMMITTED_LOCAL, REMOTE_UNREACHABLE, NO_DEFAULT_BRANCH, NOT_A_GIT_REPOSITORY}
)

_EXIT_BY_STATUS = {
    PUSHED: EXIT_OK,
    COMMITTED_LOCAL: EXIT_OK,
    REMOTE_UNREACHABLE: EXIT_OK,
    NO_DEFAULT_BRANCH: EXIT_OK,
    NOT_A_GIT_REPOSITORY: EXIT_OK,
    INVALID_ACTOR: EXIT_INVALID_ACTOR,
    # repo_root sits inside a different repository's history: the sequence
    # cannot publish there for any command, required-remote or not.
    FOREIGN_REPOSITORY: EXIT_REMOTE_REQUIRED,
    # The remote answered and its state could not be read, so the union is
    # unknown to every command, not only to one that needs the remote.
    REMOTE_UNREADABLE: EXIT_REMOTE_REQUIRED,
    STAGE_FAILED: EXIT_COMMIT_REFUSED,
    COMMIT_REFUSED: EXIT_COMMIT_REFUSED,
    PUSH_REJECTED: EXIT_PUSH_EXHAUSTED,
    # The event is published; the fold over the pushed remote state says
    # an earlier claim owns the item.
    CLAIM_SUPERSEDED: EXIT_CLAIM_SUPERSEDED,
    # Nothing was minted: the claim's own timestamp sits behind published
    # history by more than the tolerance, or cannot be read at all. The
    # two are reported apart and end the command the same way, as a
    # staging failure and a refused commit do.
    CLOCK_BEHIND_REMOTE: EXIT_CLOCK_BEHIND,
    UNREADABLE_TIMESTAMP: EXIT_CLOCK_BEHIND,
}


class RemoteReadError(Exception):
    """
    Raised when the remote branch exists and its shards cannot be read.

    Distinct from an unpublished branch, which is an empty remote and a
    normal outcome. A read failure must never fold as an empty remote: the
    union would silently lose every event the remote holds.
    """

    def __init__(self, ref: str, detail: str) -> None:
        super().__init__(f"could not read the events committed at {ref}: {detail}")
        self.ref = ref
        self.detail = detail


def _exit_code(status: str, requires_remote: bool) -> int:
    """The one place an outcome becomes an exit code."""
    if requires_remote and status in _LOCAL_ONLY:
        return EXIT_REMOTE_REQUIRED
    return _EXIT_BY_STATUS[status]


def _normalised_path(path: str) -> str:
    """A path resolved, normalised and case-folded for comparison."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _same_path(a: str, b: str) -> bool:
    """True when a and b resolve to the same filesystem location."""
    return _normalised_path(a) == _normalised_path(b)


def invalid_actor_message(actor: str) -> str | None:
    """
    None when actor is a valid role@host handle; a caller-facing refusal
    message otherwise. The one place every mutating command's actor shape
    is checked, so the rule reads the same everywhere it fires.

    Public (no leading underscore) because add.py checks it before
    minting a new item id, so a refused actor is never reported with an
    id for an item that was never actually created; run_sequence below
    is still where the refusal itself is decided and reported.
    """
    if _ACTOR_HANDLE_RE.match(actor or ""):
        return None
    return (
        f"the actor {actor!r} is not a valid handle - every mutating "
        "command requires role@host (for example builder@olympos), so "
        "the event log can tell every writer apart across machines"
    )


@dataclass(frozen=True)
class GitResult:
    """One git invocation's exit status and captured text."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output(self) -> str:
        """stdout and stderr joined, empty when the call said nothing."""
        parts = [part.strip() for part in (self.stdout, self.stderr)]
        return "\n".join(part for part in parts if part)


class GitRunner:
    """
    Every git read and write the sequence performs.

    A caller may pass any object with these methods. The test suite passes
    one that answers from a script, so the sequence is exercised without a
    git process.
    """

    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root

    def repo_toplevel(self, repo_root: str) -> str | None:
        """
        The resolved top-level directory of the git repository at
        repo_root, or None when repo_root is not inside a git repository
        at all.

        repo_root is accepted as a parameter (rather than read only from
        self.repo_root, which is already the same value in production) so
        a test double can answer this without needing to be constructed
        with the repository path in advance.
        """
        result = self.run("rev-parse", "--show-toplevel")
        if not result.ok:
            return None
        toplevel = result.stdout.strip()
        return toplevel or None

    def run(self, *args: str, env: dict[str, str] | None = None) -> GitResult:
        """
        Run one git command in the repository and capture its output.

        env, when given, is the complete environment the git process and
        every hook it runs receive. None, the default, means this
        process's own environment unchanged.
        """
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return GitResult(returncode=1, stderr=str(exc))
        return GitResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def raw_events_at_ref(self, ref: str) -> list[dict] | None:
        """
        The raw events committed at ref, through the one git-ref reader.

        The ref is resolved first, so the one legitimate empty case is told
        apart from every failure: a ref that does not resolve is a branch
        nobody has published, and returns None. Once the ref resolves, any
        failure to list or read its shards is a read failure and is raised,
        never reported as an empty remote.
        """
        resolved = self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if not resolved.ok or not resolved.stdout.strip():
            return None
        try:
            return read_raw_events_at_ref(self.repo_root, ref)
        except ReplayRefError as exc:
            raise RemoteReadError(ref, str(exc)) from exc


@dataclass
class SyncOutcome:
    """What the sequence did, what it left behind, and how it ends."""

    status: str
    exit_code: int
    report: dict
    notes: list[str] = field(default_factory=list)
    event: dict | None = None
    shard_path: str | None = None
    remote_branch: str | None = None
    state: dict = field(default_factory=dict)


def _default_branch(git) -> str | None:
    """
    The remote's default branch, from the refs the fetch has just updated.

    The remote HEAD symref is the answer whenever it is set. A remote whose
    HEAD is not set falls back to main, then master, then the alphabetically
    first published branch, which is the documented tie-break the rest of
    Pinax uses. None means nothing is published.
    """
    symref = git.run("symbolic-ref", "--quiet", "--short", _REMOTE_REF_PREFIX + "HEAD")
    name = symref.stdout.strip() if symref.ok else ""
    if name.startswith(_REMOTE + "/"):
        return name[len(_REMOTE) + 1:]
    if name:
        return name

    listed = git.run(
        "for-each-ref", "--format=%(refname:short)", "refs/remotes/" + _REMOTE
    )
    published = []
    if listed.ok:
        for line in listed.stdout.splitlines():
            entry = line.strip()
            if not entry.startswith(_REMOTE + "/") or entry == _REMOTE + "/HEAD":
                continue
            published.append(entry[len(_REMOTE) + 1:])
    branches = sorted(set(published))
    for candidate in ("main", "master"):
        if candidate in branches:
            return candidate
    return branches[0] if branches else None


def _union_fold(
    log_dir: str, git, remote_ref: str | None
) -> tuple[list[dict], dict, list[dict]]:
    """
    Fold the union of the working-tree log and the remote branch's shards.

    One determinism layer and one fold: the raw pools are concatenated and
    handed to pinax.fold.finalise_events, then to pinax.fold.fold_events.

    Returns the ordered events, the folded state, and the remote branch's
    own raw pool. The remote pool is returned rather than read a second
    time because the clock rule below asks about the remote alone, and one
    read of the ref is one read.
    """
    raw_events = list(read_raw_events(log_dir))
    remote_raw = git.raw_events_at_ref(remote_ref) if remote_ref is not None else None
    if remote_raw:
        raw_events.extend(remote_raw)
    events = finalise_events(raw_events)
    return events, fold_events(events), list(remote_raw or [])


def _clock_tolerance() -> tuple[float, str]:
    """
    How far behind published history a claim's timestamp may sit, and a
    note when the environment could not be used.

    The one place the tolerance is read. PINAX_CLOCK_TOLERANCE_S names a
    number of seconds; anything else falls back to the default and says so,
    because a silently ignored setting would refuse or admit a claim for a
    reason the operator never sees.
    """
    raw = (os.environ.get(_CLOCK_TOLERANCE_ENV_VAR) or "").strip()
    if not raw:
        return _DEFAULT_CLOCK_TOLERANCE_S, ""
    fallback = (
        f", so the default of {_DEFAULT_CLOCK_TOLERANCE_S:g} seconds applies."
    )
    try:
        seconds = float(raw)
    except ValueError:
        seconds = float("nan")
    # A value that is not a number at all, and one that parses as "not a
    # number", are the same unusable setting and are told apart from a
    # number the rule refuses to work with.
    if seconds != seconds:
        return (
            _DEFAULT_CLOCK_TOLERANCE_S,
            f"pinax: {_CLOCK_TOLERANCE_ENV_VAR}={raw!r} is not a number of "
            "seconds" + fallback,
        )
    if seconds < 0:
        return (
            _DEFAULT_CLOCK_TOLERANCE_S,
            f"pinax: {_CLOCK_TOLERANCE_ENV_VAR}={raw!r} is a negative "
            "tolerance, which is not accepted" + fallback,
        )
    return seconds, ""


def _parse_ts(value) -> datetime.datetime | None:
    """
    One recorded timestamp as a comparable value, or None when it is not
    written in the envelope's form. No clock is read here.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, _TS_FORMAT)
    except ValueError:
        return None


def _newest_live_remote_event(remote_raw: list[dict]) -> tuple[dict | None, object]:
    """
    The remote event carrying the greatest timestamp that a valid
    event.annulled tombstone does not name.

    Annulment is asked of pinax.fold._collect_annulled_ids, the one owner
    of "which ids are annulled", over the same pool, so a retired
    future-dated event leaves the maximum exactly as the operator expects
    after running pinax annul. An event whose timestamp is not written in
    the envelope's form cannot be compared and is passed over; the id
    breaks a tie so the answer is a function of the pool alone.
    """
    annulled = _collect_annulled_ids(remote_raw)
    newest: dict | None = None
    newest_at: datetime.datetime | None = None
    for event in remote_raw:
        if event.get("id") in annulled:
            continue
        at = _parse_ts(event.get("ts"))
        if at is None:
            continue
        if newest_at is None or (at, str(event.get("id"))) > (
            newest_at, str(newest.get("id"))
        ):
            newest, newest_at = event, at
    return newest, newest_at


def _clock_behind_remote(remote_raw: list[dict], ts: str) -> dict | None:
    """
    The one place the clock rule is decided.

    None when the intended timestamp may be recorded. Otherwise the facts
    the refusal reports, under a closed 'reason': a timestamp this
    sequence cannot read at all, or one behind published history with both
    timestamps, the difference in seconds, the tolerance in force, and the
    event and actor that set the maximum.

    An unreadable intended timestamp is an integrity refusal and never a
    policy one. The command minted it moments earlier in the envelope's
    own form, so a value that cannot be read contradicts the sequence
    itself; raising the tolerance is no answer to it, and passing it
    through would retire the rule without saying so.
    """
    intended = _parse_ts(ts)
    if intended is None:
        return {"reason": UNREADABLE_TIMESTAMP, "intended_ts": ts}
    newest, newest_at = _newest_live_remote_event(remote_raw)
    if newest is None:
        return None
    tolerance, tolerance_note = _clock_tolerance()
    behind = (newest_at - intended).total_seconds()
    if behind <= tolerance:
        return None
    return {
        "reason": CLOCK_BEHIND_REMOTE,
        "intended_ts": ts,
        "remote_newest_ts": newest["ts"],
        "behind_s": behind,
        "tolerance_s": tolerance,
        "offending_event_id": newest.get("id", ""),
        "offending_actor": newest.get("actor", ""),
        "tolerance_note": tolerance_note,
    }


def _superseded_by(state: dict, event: dict) -> dict | None:
    """
    The one place a published event is recognised as a superseded claim.

    The fold records every superseded claim with the id of the event it
    superseded, and this module minted the event it is asking about, so the
    two are matched by that id and nothing else. No second reconciliation
    rule is needed to tell this command's own claim from the winner's.
    """
    for entry in state.get("claim_superseded", []):
        if entry.get("superseded_event_id") == event.get("id"):
            return entry
    return None


def _next_seq(events: list[dict]) -> int:
    """The sequence number after every event in the union."""
    return (max(event["seq"] for event in events) + 1) if events else 0


def _prev_for_actor(events: list[dict], actor: str) -> str:
    """This actor's latest event id in the union, or the empty sentinel."""
    owned = [event for event in events if event.get("actor") == actor]
    return owned[-1]["id"] if owned else ""


def _hook_environment(repo_root: str) -> dict[str, str]:
    """
    The environment the commit, and every hook it runs, receives.

    The one place this sequence builds an environment. It is this process's
    own environment with the resolved tracker root added as PINAX_ROOT: a
    hook gets no arguments from here, so a hook that calls back into Pinax
    (the shipped pre-commit hook runs 'pinax verify') would otherwise pin
    its root from whatever the caller's environment carried, and a stale
    value there would refuse this command's own commit. Nothing else in the
    environment changes and no hook is skipped.
    """
    environment = dict(os.environ)
    environment[_ROOT_ENV_VAR] = os.path.normpath(os.path.abspath(repo_root))
    return environment


def _commit(
    git, repo_root: str, ergon_dir: str, shard_path: str, message: str
) -> tuple[str, GitResult]:
    """
    Stage the shard and the projection, then commit with hooks running.

    Only the commit carries _hook_environment: staging runs no hook. A hook
    that refuses the commit is reported as a refusal and never worked
    around.

    Returns the phase that failed and its result, so a repository that could
    not stage the files is never reported as a refused commit.
    """
    staged = git.run(
        "add",
        "--",
        shard_path,
        os.path.join(ergon_dir, "board.md"),
        os.path.join(ergon_dir, "items"),
    )
    if not staged.ok:
        return "stage", staged
    return "commit", git.run(
        "commit", "-m", message, env=_hook_environment(repo_root)
    )


def _commit_message(event_type: str, item_id: str) -> str:
    """
    The commit subject every sync commit carries.

    A repository-level event names no item (ADR-007's expiry policy is the
    first of them), so its subject is the event type alone rather than the
    type followed by nothing.
    """
    if not item_id:
        return f"pinax: {event_type}"
    return f"pinax: {event_type} {item_id}"


def _pull_commit_message(remote_branch: str) -> str:
    """The commit subject the pull's own regeneration carries."""
    return f"pinax: regenerate the projection from the merged {remote_branch}"


def _projection_bytes(ergon_dir: str) -> dict[str, bytes]:
    """
    The generated Markdown as it currently sits on disk, by file name.

    Read so the sequence can tell whether regenerating after a pull
    actually changed anything, without asking git a second question.
    """
    files: dict[str, bytes] = {}
    board = os.path.join(ergon_dir, _BOARD_NAME)
    if os.path.isfile(board):
        with open(board, "rb") as fh:
            files[_BOARD_NAME] = fh.read()
    items_dir = os.path.join(ergon_dir, _ITEMS_DIR_NAME)
    if os.path.isdir(items_dir):
        for name in sorted(os.listdir(items_dir)):
            path = os.path.join(items_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    files[_ITEMS_DIR_NAME + "/" + name] = fh.read()
    return files


def _regenerate_after_pull(
    git, repo_root: str, ergon_dir: str, shard_path: str, merged: GitResult, message: str
) -> str:
    """
    Bring the generated Markdown back in line with the merged log, and
    commit it, before the next push attempt.

    ADR-002 owns the rule: board.md and items/*.md are regenerated from the
    merged log and never merged, by hand or by a driver. That is how a
    conflict in them is resolved, and staging the regenerated files also
    concludes a merge they conflicted in. It runs whether or not git
    reported a conflict, because a merge that succeeded has still just
    added the other side's events to the log the projection describes.

    A merge that conflicted anywhere else is left for git to refuse: the
    commit fails, and its own words become the obstacle returned here. An
    empty string means there is nothing standing in the way.
    """
    before = _projection_bytes(ergon_dir)
    regenerate(repo_root)
    if merged.ok and _projection_bytes(ergon_dir) == before:
        return ""
    _phase, committed = _commit(git, repo_root, ergon_dir, shard_path, message)
    return "" if committed.ok else committed.output()


def _fold_warnings(state: dict) -> list[str]:
    """Warnings the union fold raised, so they reach the operator."""
    warnings = state.get("report", {}).get("warnings", [])
    return [f"pinax: {warning}" for warning in warnings]


def _note_fold_warnings(notes: list[str], state: dict) -> None:
    """
    Put this fold's warnings in front of the operator, each line once.

    The one owner for collecting them. The sequence folds up to three
    times over the same log: before the append, again after a pull, and
    once more over the pushed remote state. A fold that keeps warning
    about the same events would otherwise say the same sentence to the
    operator once per fold, which reads as several findings rather than
    one.
    """
    for warning in _fold_warnings(state):
        if warning not in notes:
            notes.append(warning)


def _annul_own_event(repo_root: str, event: dict, actor: str, record: dict) -> None:
    """
    Retire the event this sequence just minted, in its own shard.

    Used when the event could not be published and must not be carried
    forward by a later mutation. The tombstone is an ordinary appended
    event, so the append-only log stays intact.

    The annulment is part of this sequence's report, so its own console
    output is captured and folded into `record` rather than printed beside
    a report a caller may be parsing. `record` is the same object the
    report already holds and is filled even when the annulment fails, so
    the report is complete before the failure surfaces to the caller.
    """
    from .commands.annul import append_local as annul_run

    spoken = io.StringIO()
    try:
        with contextlib.redirect_stdout(spoken):
            annul_run(
                repo_root=repo_root,
                target_id=event["id"],
                reason=record["reason"],
                actor=actor,
                as_json=False,
            )
    finally:
        record["output"] = spoken.getvalue().strip()


def _unreadable_outcome(
    error: RemoteReadError,
    report_base: dict,
    *,
    appended: bool,
    committed: bool = False,
    requires_remote: bool = False,
    notes: list[str] | None = None,
    event: dict | None = None,
    shard_path: str | None = None,
    remote_branch: str | None = None,
    state: dict | None = None,
) -> SyncOutcome:
    """The outcome for a remote whose committed events could not be read."""
    lines = list(notes or [])
    lines.append(
        f"pinax: the events committed at {error.ref} could not be read, so the "
        "union with the remote is unknown."
    )
    if error.detail:
        lines.append(error.detail)
    return SyncOutcome(
        status=REMOTE_UNREADABLE,
        exit_code=_exit_code(REMOTE_UNREADABLE, requires_remote),
        report=dict(
            report_base,
            status=REMOTE_UNREADABLE,
            appended=appended,
            committed=committed,
            remote_ref=error.ref,
            git_output=error.detail,
            message=(
                "the remote branch is published and its committed events "
                "could not be read"
            ),
        ),
        notes=lines,
        event=event,
        shard_path=shard_path,
        remote_branch=remote_branch,
        state=state if state is not None else {},
    )


def run_sequence(
    repo_root: str,
    *,
    event_type: str,
    payload: dict,
    actor: str,
    ts: str,
    item_id: str,
    requires_remote: bool = False,
    guard_clock: bool = False,
    offline: bool = False,
    runner: GitRunner | None = None,
) -> SyncOutcome:
    """
    Run the publish sequence for one event and report what happened.

    The caller supplies the event's type, payload, actor and timestamp; this
    function owns the actor check, the fetch, the union fold, the sequence
    number, the predecessor reference, the append, the projection, the
    commit and the push. It never ends the process: conclude() turns the
    outcome into an exit code.

    item_id names the item the event is about. A repository-level event, one
    that applies to the whole tracker rather than to an item, passes the
    empty string: its commit subject is then the event type alone and its
    report carries no item.

    requires_remote marks a command whose whole purpose is cross-machine
    visibility. Such a command fails when the sequence published nothing,
    and it never goes offline: offline is ignored whenever requires_remote
    is true, so claim keeps fetching and reporting exactly as before.

    guard_clock marks a command that must not record a timestamp behind
    published history. Such a command ends with exit 6, before anything is
    minted, when its intended timestamp is earlier than the newest live
    event on the remote by more than the clock tolerance. Only claim asks
    for it, because only a claim asserts ownership from a moment in time.

    offline is the caller's --offline flag. Offline mode also applies when
    the PINAX_OFFLINE=1 environment variable is set (checked here, in this
    one place, so no command needs to read it itself), and applies on its
    own once the remote turns out unreachable or unpublished (unchanged
    from before this parameter existed). Offline mode skips the fetch and
    the push; the event is still appended, folded against the local log
    alone, and committed.

    runner is injectable for tests only; production callers pass none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    report_base = {
        "event_type": event_type,
        "item_id": item_id,
        "actor": actor,
        "root": ergon_dir,
    }

    invalid = invalid_actor_message(actor)
    if invalid is not None:
        # item_id may already name an item that was never actually
        # created (add mints its new item's id before this check runs):
        # a refusal must never report an id as if it were real. A
        # repository-level event carries no item id at all, and its
        # refusal says nothing about an item either way.
        no_item_created = not item_id and event_type == "item.created"
        detail = invalid + ("; no item was created" if no_item_created else "")
        return SyncOutcome(
            status=INVALID_ACTOR,
            exit_code=_exit_code(INVALID_ACTOR, requires_remote),
            report=dict(
                report_base,
                item_id=(item_id or None),
                status=INVALID_ACTOR,
                appended=False,
                message="pinax: " + detail,
            ),
            notes=["pinax: " + detail + "."],
        )

    git = runner if runner is not None else GitRunner(repo_root)
    log_dir = os.path.join(ergon_dir, "log")
    notes: list[str] = []
    message = _commit_message(event_type, item_id)

    # repo_root must be the top level of its own git repository. A
    # directory with no git repository at all cannot be fetched from,
    # committed to, or pushed from; a directory inside a DIFFERENT
    # repository must never receive a commit belonging to this one. Both
    # are facts about the environment, not a caller preference, so both
    # apply to every command including claim.
    toplevel = git.repo_toplevel(repo_root)
    no_git_repo = toplevel is None
    if toplevel is not None and not _same_path(toplevel, repo_root):
        foreign_reason = (
            repo_root + " is not the top level of its git repository "
            "(the top level is " + toplevel + "); refusing to publish "
            "into a different repository's history"
        )
        return SyncOutcome(
            status=FOREIGN_REPOSITORY,
            exit_code=_exit_code(FOREIGN_REPOSITORY, requires_remote),
            report=dict(
                report_base,
                status=FOREIGN_REPOSITORY,
                remote_branch=None,
                appended=False,
                message=foreign_reason + ", so nothing was appended",
            ),
            notes=["pinax: " + foreign_reason + ", so nothing was appended."],
        )

    is_offline = (
        not requires_remote
        and (offline or os.environ.get(_OFFLINE_ENV_VAR) == "1")
    )
    skip_fetch = is_offline or no_git_repo
    report_base = dict(report_base, offline=is_offline)

    unpublished_status = COMMITTED_LOCAL
    unpublished_reason = ""
    if skip_fetch:
        remote_branch = None
        if no_git_repo:
            unpublished_status = NOT_A_GIT_REPOSITORY
            unpublished_reason = repo_root + " is not a git repository"
        else:
            unpublished_reason = "offline mode was requested (--offline or PINAX_OFFLINE=1)"
        if requires_remote:
            # Only no_git_repo reaches here: is_offline never combines with
            # requires_remote, so this is the no-git-repository case, the
            # same shape as the remote-unreachable early exit below.
            return SyncOutcome(
                status=unpublished_status,
                exit_code=_exit_code(unpublished_status, requires_remote),
                report=dict(
                    report_base,
                    status=unpublished_status,
                    remote_branch=None,
                    appended=False,
                    message=unpublished_reason + ", so nothing was appended",
                ),
                notes=["pinax: " + unpublished_reason + ", so nothing was appended."],
            )
        notes.append(
            "pinax: " + unpublished_reason + "; the event is recorded locally "
            "and not published."
        )
    else:
        fetched = git.run("fetch", _REMOTE)
        remote_branch = _default_branch(git) if fetched.ok else None

        if remote_branch is None:
            if fetched.ok:
                unpublished_status = NO_DEFAULT_BRANCH
                unpublished_reason = (
                    _REMOTE + " was reached and publishes no default branch"
                )
                detail = "no branch is published on " + _REMOTE
            else:
                unpublished_status = REMOTE_UNREACHABLE
                unpublished_reason = _REMOTE + " could not be reached"
                detail = fetched.output()
            if requires_remote:
                return SyncOutcome(
                    status=unpublished_status,
                    exit_code=_exit_code(unpublished_status, requires_remote),
                    report=dict(
                        report_base,
                        status=unpublished_status,
                        remote_branch=None,
                        appended=False,
                        git_output=detail,
                        message=unpublished_reason + ", so nothing was appended",
                    ),
                    notes=["pinax: " + unpublished_reason + ", so nothing was appended."],
                )
            notes.append(
                "pinax: " + unpublished_reason + "; the event is recorded locally "
                "and not published."
            )

    remote_ref = _REMOTE_REF_PREFIX + remote_branch if remote_branch else None
    try:
        events, state, remote_raw = _union_fold(log_dir, git, remote_ref)
    except RemoteReadError as exc:
        return _unreadable_outcome(
            exc, report_base, appended=False, requires_remote=requires_remote
        )
    _note_fold_warnings(notes, state)

    # The clock rule, on the pre-append fold and before anything is minted:
    # a command that asserts ownership from a moment in time may not record
    # a moment the published log has already passed.
    behind = _clock_behind_remote(remote_raw, ts) if guard_clock else None
    if behind is not None and behind["reason"] == UNREADABLE_TIMESTAMP:
        # An integrity refusal, not the policy gate below: the sequence
        # was handed a timestamp it cannot read, in the one form every
        # event carries, so it refuses rather than skipping the rule.
        summary = (
            f"the timestamp {behind['intended_ts']!r} this command minted is "
            f"not written as {_TS_FORMAT}, so it cannot be compared with "
            "published history and nothing was appended"
        )
        notes.append("pinax: " + summary + ".")
        return SyncOutcome(
            status=UNREADABLE_TIMESTAMP,
            exit_code=_exit_code(UNREADABLE_TIMESTAMP, requires_remote),
            report=dict(
                report_base,
                status=UNREADABLE_TIMESTAMP,
                remote_branch=remote_branch,
                appended=False,
                committed=False,
                pushed=False,
                intended_ts=behind["intended_ts"],
                expected_ts_form=_TS_FORMAT,
                message=summary,
            ),
            notes=notes,
            remote_branch=remote_branch,
            state=state,
        )
    if behind is not None:
        remedy = (
            "retire the future dated event with 'pinax annul "
            + str(behind["offending_event_id"])
            + " --reason <text>', which takes it out of the maximum, or set "
            + _CLOCK_TOLERANCE_ENV_VAR
            + " above "
            + f"{behind['behind_s']:g}"
            + " for a single crossing, then run the command again"
        )
        summary = (
            f"the intended timestamp {behind['intended_ts']} is "
            f"{behind['behind_s']:g} seconds behind the newest published "
            f"event {behind['remote_newest_ts']} from "
            f"{behind['offending_actor']}, more than the "
            f"{behind['tolerance_s']:g} second tolerance, so nothing was "
            "appended"
        )
        if behind["tolerance_note"]:
            notes.append(behind["tolerance_note"])
        notes.append("pinax: " + summary + ".")
        notes.append("pinax: " + remedy + ".")
        return SyncOutcome(
            status=CLOCK_BEHIND_REMOTE,
            exit_code=_exit_code(CLOCK_BEHIND_REMOTE, requires_remote),
            report=dict(
                report_base,
                status=CLOCK_BEHIND_REMOTE,
                remote_branch=remote_branch,
                appended=False,
                committed=False,
                pushed=False,
                intended_ts=behind["intended_ts"],
                remote_newest_ts=behind["remote_newest_ts"],
                behind_s=behind["behind_s"],
                tolerance_s=behind["tolerance_s"],
                offending_event_id=behind["offending_event_id"],
                offending_actor=behind["offending_actor"],
                remedy=remedy,
                message=summary,
            ),
            notes=notes,
            remote_branch=remote_branch,
            state=state,
        )

    event = mint_event(
        seq=_next_seq(events),
        ts=ts,
        actor=actor,
        etype=event_type,
        payload=payload,
        prev=_prev_for_actor(events, actor),
    )
    shard_path = append_event(log_dir, event, actor=actor)
    regenerate(repo_root)

    report_base = dict(
        report_base,
        event_id=event["id"],
        seq=event["seq"],
        shard=shard_path,
        remote_branch=remote_branch,
        commit_message=message,
    )

    if no_git_repo:
        # Nothing to stage or commit into: the event is appended to the
        # log and the projection is regenerated, exactly as this command
        # behaved before it was routed through this sequence.
        return SyncOutcome(
            status=NOT_A_GIT_REPOSITORY,
            exit_code=_exit_code(NOT_A_GIT_REPOSITORY, requires_remote),
            report=dict(
                report_base,
                status=NOT_A_GIT_REPOSITORY,
                committed=False,
                pushed=False,
                message="the event is appended locally; " + unpublished_reason,
            ),
            notes=notes,
            event=event,
            shard_path=shard_path,
            remote_branch=None,
            state=state,
        )

    phase, committed = _commit(git, repo_root, ergon_dir, shard_path, message)
    if not committed.ok:
        detail = committed.output()
        if phase == "stage":
            status = STAGE_FAILED
            headline = "the shard and the projection could not be staged"
            summary = (
                "the shard and the projection could not be staged, so the "
                "appended event stays uncommitted; fix the repository and "
                "commit them"
            )
        else:
            status = COMMIT_REFUSED
            headline = "the commit was refused"
            summary = (
                "the commit was refused, so the appended event stays "
                "uncommitted; fix the refusal and commit the shard and "
                "the projection"
            )
        notes.append(
            f"pinax: {headline}, so event {event['id']} in {shard_path} "
            "is appended and not committed."
        )
        if detail:
            notes.append(detail)
        return SyncOutcome(
            status=status,
            exit_code=_exit_code(status, requires_remote),
            report=dict(
                report_base,
                status=status,
                committed=False,
                phase=phase,
                git_output=detail,
                message=summary,
            ),
            notes=notes,
            event=event,
            shard_path=shard_path,
            remote_branch=remote_branch,
            state=state,
        )

    if remote_branch is None:
        return SyncOutcome(
            status=unpublished_status,
            exit_code=_exit_code(unpublished_status, requires_remote),
            report=dict(
                report_base,
                status=unpublished_status,
                committed=True,
                pushed=False,
                message="the event is committed locally; " + unpublished_reason,
            ),
            notes=notes,
            event=event,
            shard_path=shard_path,
            remote_branch=None,
            state=state,
        )

    head = git.run("rev-parse", "--abbrev-ref", "HEAD")
    head_branch = head.stdout.strip() if head.ok else ""
    if head_branch != remote_branch:
        refused = head_branch or "the checked-out branch"
        notes.append(
            f"pinax: the push was refused because {refused} is not the "
            f"{_REMOTE} default branch {remote_branch}; the event is "
            "committed locally and not published."
        )
        return SyncOutcome(
            status=COMMITTED_LOCAL,
            exit_code=_exit_code(COMMITTED_LOCAL, requires_remote),
            report=dict(
                report_base,
                status=COMMITTED_LOCAL,
                committed=True,
                pushed=False,
                head_branch=head_branch,
                message=(
                    f"the sync publishes only {remote_branch} and {refused} "
                    "is checked out"
                ),
            ),
            notes=notes,
            event=event,
            shard_path=shard_path,
            remote_branch=remote_branch,
            state=state,
        )

    attempts = 0
    obstacle = ""
    while attempts < _MAX_PUSH_ATTEMPTS:
        attempts += 1
        pushed = git.run(
            "push", _REMOTE, f"{remote_branch}:refs/heads/{remote_branch}"
        )
        if pushed.ok:
            # The confirming fold: read the remote once more and let the
            # fold, not the push, say how this command ends.
            refreshed = git.run("fetch", _REMOTE)
            if not refreshed.ok:
                notes.append(
                    "pinax: the confirming fetch of " + _REMOTE + " failed, so "
                    "the fold below reads the remote state this push recorded."
                )
                detail = refreshed.output()
                if detail:
                    notes.append(detail)
            try:
                events, state, remote_raw = _union_fold(log_dir, git, remote_ref)
            except RemoteReadError as exc:
                return _unreadable_outcome(
                    exc,
                    report_base,
                    appended=True,
                    committed=True,
                    requires_remote=requires_remote,
                    notes=notes,
                    event=event,
                    shard_path=shard_path,
                    remote_branch=remote_branch,
                    state=state,
                )
            _note_fold_warnings(notes, state)
            superseded = _superseded_by(state, event)
            if superseded is not None:
                summary = (
                    f"the claim on {item_id} is superseded by an earlier claim "
                    f"from {superseded.get('winner_actor')}, so the item is not "
                    "owned by this actor"
                )
                notes.append(
                    "pinax: " + summary + "; the winning event is "
                    f"{superseded.get('winner_event_id')} and this claim stays "
                    "in the log, published and superseded."
                )
                return SyncOutcome(
                    status=CLAIM_SUPERSEDED,
                    exit_code=_exit_code(CLAIM_SUPERSEDED, requires_remote),
                    report=dict(
                        report_base,
                        status=CLAIM_SUPERSEDED,
                        committed=True,
                        pushed=True,
                        attempts=attempts,
                        winner_event_id=superseded.get("winner_event_id"),
                        winner_actor=superseded.get("winner_actor"),
                        superseded_event_id=event["id"],
                        superseded_actor=actor,
                        message=summary,
                    ),
                    notes=notes,
                    event=event,
                    shard_path=shard_path,
                    remote_branch=remote_branch,
                    state=state,
                )
            return SyncOutcome(
                status=PUSHED,
                exit_code=_exit_code(PUSHED, requires_remote),
                report=dict(
                    report_base,
                    status=PUSHED,
                    committed=True,
                    pushed=True,
                    attempts=attempts,
                    message=f"the event is committed and published on {remote_branch}",
                ),
                notes=notes,
                event=event,
                shard_path=shard_path,
                remote_branch=remote_branch,
                state=state,
            )
        obstacle = pushed.output()
        if attempts == _MAX_PUSH_ATTEMPTS:
            break
        # The pull, written as its two halves so that no local configuration
        # can turn it into a rewrite of the commits already made here.
        refreshed = git.run("fetch", _REMOTE, remote_branch)
        if not refreshed.ok:
            obstacle = refreshed.output()
            break
        merged = git.run("merge", "--no-edit", "FETCH_HEAD")
        # The generated Markdown is brought back in line with the merged log
        # and committed before the next attempt, which is also how a conflict
        # in it is resolved (ADR-002). A conflict anywhere else stops here in
        # git's own words.
        blocked = _regenerate_after_pull(
            git,
            repo_root,
            ergon_dir,
            shard_path,
            merged,
            _pull_commit_message(remote_branch),
        )
        if blocked:
            obstacle = blocked
            break
        try:
            events, state, remote_raw = _union_fold(log_dir, git, remote_ref)
        except RemoteReadError as exc:
            return _unreadable_outcome(
                exc,
                report_base,
                appended=True,
                committed=True,
                requires_remote=requires_remote,
                notes=notes,
                event=event,
                shard_path=shard_path,
                remote_branch=remote_branch,
                state=state,
            )
        _note_fold_warnings(notes, state)

    remote_head = git.run("rev-parse", remote_ref)
    report = dict(
        report_base,
        status=PUSH_REJECTED,
        committed=True,
        pushed=False,
        attempts=attempts,
        remote_head=remote_head.stdout.strip() if remote_head.ok else "",
        git_output=obstacle,
        message=f"the push to {remote_branch} was rejected and the event is not published",
    )
    if requires_remote:
        annulment = {
            "target_id": event["id"],
            "reason": _ANNUL_REASON,
            "item_id": item_id,
            "output": "",
        }
        report["annulled"] = annulment
        _annul_own_event(repo_root, event, actor, annulment)
        notes.append(
            f"pinax: event {event['id']} was annulled in {shard_path} so an "
            "unpublished record is never carried forward."
        )
    notes.append(
        f"pinax: the push to {remote_branch} was rejected after {attempts} attempts."
    )
    return SyncOutcome(
        status=PUSH_REJECTED,
        exit_code=_exit_code(PUSH_REJECTED, requires_remote),
        report=report,
        notes=notes,
        event=event,
        shard_path=shard_path,
        remote_branch=remote_branch,
        state=state,
    )


def conclude(outcome: SyncOutcome) -> None:
    """
    Report the sequence and end the command when it did not succeed.

    Notes always reach the operator. A failure also prints the machine
    readable report and ends the command with the outcome's code.
    """
    for note in outcome.notes:
        print(note, file=sys.stderr)
    if outcome.exit_code == EXIT_OK:
        return
    print(json.dumps(outcome.report, sort_keys=True, ensure_ascii=True))
    sys.exit(outcome.exit_code)
