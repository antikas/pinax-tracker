# ADR-006: publishing a state change

## Decision

A mutating command does not simply append. It runs one publish sequence:
fetch `origin`, fold the union of the local log and the remote default
branch's committed shards, append the event, regenerate the projection,
commit the shard and the projection, and push the remote default branch.

The union fold reads the remote side through the git-ref reader that
`pinax replay` uses and folds it with the working tree through the one
determinism layer and the one fold. It decides the event's sequence number
and its predecessor reference, so a machine that has never seen a remote
event still numbers its own event after it. The sequence reads no clock;
the command mints the event timestamp and passes it in.

A remote that answers and publishes no default branch at all is reported as
that, and never as an unreachable remote, because the two ask the operator
for different repairs. A branch nobody has published folds as an empty
remote. A published branch
whose committed shards cannot be read is a read failure and never an empty
remote, because a silent empty read would drop every event the remote holds
from the union. The command then ends with exit 4, naming the ref and the
underlying error.

The commit runs the repository's hooks, with the resolved tracker root in
their environment as `PINAX_ROOT`. A hook is given no arguments by the
sequence, so a hook that calls Pinax back would otherwise pin its root from
whatever the caller's environment carried, and a stale value there would
refuse the command's own commit. A refused commit ends the command
with exit 7 and leaves the appended event uncommitted, named by shard and
event id, for the operator to commit once the refusal is fixed. A repository
that cannot stage the shard and the projection ends the same way and says so
in its own words, so a staging failure is never reported as a refusal. A
hook is never bypassed.

A `.gitignore` rule that swallows the event log is one of those refusals and
not an exception to them. The pre-commit hook `pinax init` installs runs
`pinax verify`, which fails while the log is ignored, so a state-changing
command in such a repository appends its event, is refused its own commit,
and ends with exit 7 carrying the hook's output. The one-line notice a
command prints about a swallowed log, when it reaches the end of its work,
reports the condition; it never makes a refused commit succeed.

The push targets exactly the remote default branch and runs only when that
branch is checked out. On any other branch the command appends and commits
locally, prints the branch it refused, and publishes nothing.

A rejected push is retried at most three times. Between attempts the
sequence pulls, written as its two halves so that no local configuration can
turn it into a rewrite, and folds again. After the third rejection the
command ends with exit 5 and a machine-readable report naming the remote
head and the local shard.

After the pull, and before the next push attempt, the sequence regenerates
the board and the item pages from the merged log and commits them. It does
so in every case, not only when git reported a textual conflict, because the
generated files describe a log that has just gained the other side's events.
A conflict in `board.md` or in `items/*.md` is resolved by exactly that
regeneration, never by merging the two sides, by hand or by a driver, as
ADR-002 requires; `.ergon/.gitattributes` assigns no driver to generated
Markdown and is unchanged. Staging the regenerated files also concludes a
merge those files conflicted in. A merge that conflicted anywhere else is
left for git to refuse: the commit that would conclude it fails, and that
refusal ends the retry with exit 5 in the words git used.

## The fold decides how a claim ends

After an accepted push the sequence fetches once more and folds again over
the state the remote now holds. That confirming fold, not the push, decides
how the command ends. The fold names every superseded claim by the id of the
event it superseded, and this sequence minted the event it is asking about,
so it recognises its own event by that id alone. No second reconciliation
rule is involved and none is needed.

A claim the confirming fold reports superseded ends with exit 3 and a
machine-readable report naming the winning event and its actor. The losing
claim stays in the log, visible and superseded, as ADR-003 requires, and is
not annulled. No command checks ownership before it appends: the fold is the
sole reconciler, before the push and after it.

The confirming fold reads the remote as it stands when the command runs. A
claim that publishes first and is only later overtaken by an earlier claim
from another machine ends with exit 0 and learns of the supersession at its
next mutation or from the board, never retroactively.

## A claim refuses a clock behind published history

On the pre-append fold, before an event is minted, appended, committed or
pushed, a claim compares the timestamp it intends to record with the
greatest timestamp over the events read from the remote shards whose ids no
valid `event.annulled` tombstone names. When the intended timestamp is
earlier than that maximum by more than a tolerance, the command ends with
exit 6 and records nothing at all.

The tolerance is five seconds, or the value of `PINAX_CLOCK_TOLERANCE_S`
when that names a number of seconds. The report carries both timestamps,
the difference between them, the event and the actor that set the maximum,
and the remedy: retire a future-dated event with `pinax annul`, which takes
it out of the maximum, or raise the tolerance for a single crossing.

At that same point, a timestamp the sequence cannot read at all ends the
command the same way and is reported apart, as a staging failure is reported
apart from a refused commit. The command minted it moments earlier in the
form every event carries, so a value that cannot be read contradicts the
sequence itself: it is an integrity refusal, it names the form it expected,
and no tolerance answers it.

Only a claim carries this guard, because only a claim asserts ownership from
a moment in time. The guard refuses a clock behind known history. It does
not decide a genuine race, where two machines claim within seconds of each
other; ADR-003's claim order stays the sole reconciler for that.

The sequence folds up to three times over one log, and every fold reports
the warnings the log still carries. The operator is told each of those
warnings once, not once per fold.

A command whose purpose is cross-machine visibility declares that it
requires the remote. Such a command ends with exit 4 when the sequence
published nothing, and when the remote cannot be reached it appends nothing
at all. When such a command has already appended and committed but cannot
publish, it annuls its own event in the same shard, so an unpublished record
is never carried forward by a later mutation.

No command rewrites published history. Nothing in the package re-applies
commits onto a new base, overwrites a remote branch, discards or rewrites a
commit, or deletes a branch.

## Consequences

A claim or a completion is visible to every other clone as soon as the
command returns zero, without a coordinator in between. A command that
returns a non-zero code says exactly what state the repository is in:
appended and published, appended and published but superseded, appended and
committed but not published, appended and not committed, or not appended at
all.

Concurrency stays where ADR-003 put it. The sequence takes no lock and
performs no ownership check of its own; it publishes and lets the fold
reconcile. The projection is written by regeneration from the log, as
ADR-002 requires, and never by hand.

A caller reads the outcome from the exit code alone: 0 published, 2 the
actor handle, 3 a published claim the fold superseded, 4 a remote a claim
needed and could not reach or read, 5 a push rejected on every attempt, 6 a
claim timestamp behind published history with nothing recorded, and 7 a
staging failure or a refused commit.
