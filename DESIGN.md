# Pinax design

Pinax keeps tracker state in an append-only JSONL event log. The current state
is a deterministic fold over that log. The Markdown board and item pages are
generated projections, committed so they remain readable without the CLI.

## Event envelope

```json
{"id":"content-hash","seq":12,"ts":"2026-08-17T12:00:00Z","actor":"alex@laptop","type":"item.created","payload":{},"prev":"","v":2}
```

`id` is a BLAKE2b hash of canonical JSON. Canonical JSON uses sorted ASCII keys
and compact separators. `prev` is the id of the actor's previous event in its
shard, or the empty string for the first.

Every event Pinax writes is a version 2 event and carries `"v":2`. The version
2 id covers `prev` and `v` as well as `seq`, `ts`, `actor`, `type`, and
`payload`, so each event's hash binds the event before it.

An event with no `v` field is a version 1 event. Its id covers `seq`, `ts`,
`actor`, `type`, and `payload` only, and it keeps verifying under that rule, so
logs written before version 2 replay unchanged. A log may hold version 1 events
followed by version 2 events, and the first version 2 event may name a version
1 event as its `prev`. An event whose `v` is present but is not the integer 2
fails verification.

The fold always sorts by `(seq, ts, actor, id)`. It deduplicates equal IDs and
chooses a deterministic body-sensitive representative for conflicting same-ID
lines. Every physical parsed line is inspected before deduplication by
`pinax verify`.

## Event log and projection

```text
.ergon/
  log/*.jsonl
  board.md
  items/<id>.md
```

The log is the source of truth. `board.md` and `items/*.md` are generated from
the fold and must never be edited by hand. State-changing commands append an
event and regenerate the projection. `pinax verify` compares a fresh generated
projection with the files on disk.

A claim and a status change are separate events, so the fold can hold a
claimed item at `queued` or `ready`. The board and the item page show such an
item as `building` while its claim is live, the same rule the status view uses.

JSONL files are LF-normalised and use Git's `union` merge driver. The fold is
order-independent and idempotent, so merge order and duplicate lines do not
change the resulting state.

## Items, claims, and dependencies

Items use `<prefix>-<short-hash>` identifiers. A prefix is readable context;
the short hash provides uniqueness and extends when needed. A sub-item is a
full item linked by a `parent-child` edge. Display numbering is derived from
the edge graph and is not an identity.

Dependency edge types are `blocks`, `parent-child`, `discovered-from`,
`related`, and `supersedes`. Only `blocks` gates the ready queue. `next` ranks
ready work from the dependency graph and any explicit priority.

Claims are reconciled during the fold. The earliest `(ts, actor, id)` claim
wins; later claims remain in the log and are reported as superseded.

A claim ends when it is released or when it expires, and the fold decides both.
`pinax release <id> --reason <text>` appends a release event; any actor may
release a live claim, because the case it serves is a worker that has stopped.
`pinax policy claim-expiry --hours N` appends a repository-level policy event.
The policy in force for a claim is the last policy event at or before it in the
claim order above, and twenty-four hours when the log carries none. A live
claim ends at the earlier of two points in that order: the first release for
its item after it, and the first event of any type after it whose timestamp is
at or past the claim's own timestamp plus that policy. The claim after that
point is live in its turn, so a released or expired item is claimable and ready
again. An ended claim sets no owner, claim timestamp or claim event id, and the
item's status is untouched either way; the release and the expiry are each
recorded as an outcome beside the superseded ones.

Both comparisons are between recorded timestamps, so the fold still reads no
clock, and a fold that stops before the ending event still shows the claim
live. A log that records nothing after a claim never expires it: `pinax doctor
--reconcile` offers a release there beside its done and park actions, and its
staleness threshold defaults to the folded policy when the log carries one.

## Owner-gate classes

`policy.process_set` (flow or strict) and `policy.owner_gate_classes_set` (a
list of classes) are repository-level events; the last of each in fold order is
in force, and a log without one runs on flow and the built-in list. An
`item.blocked` event may carry an `owner_gate` class; the fold records it on the
item while the item stays blocked. A later block without one clears it, and so
does any event that moves the item out of blocked. Every log written before the
field existed replays unchanged. The projection shows the class only under
strict: under flow the board and item files are byte-identical to those a
client without classes renders for the same log, so mixed client versions see
no drift until a project sets strict. Under strict, the publish sequence asks
`pinax.owner_gates` on the pre-append union fold and refuses a `decision` or
`destructive` block with no listed class, with exit 8 and nothing minted.
`pinax policy check` reports the blocked items for the owner that carry no valid
class, and fails only under strict.

## Decisions

`pinax decide <id> --option <key> --label <text> --effect release|park
--reason <text>` appends one `item.decided` event, a version 2 event minted
like every other, with the payload `{"item_id", "option", "label",
"effect", "reason"}`. The actor and the time are the envelope's `actor` and
`ts`. The option, label and reason are strings that are not blank and the
effect is `release` or `park`; the command refuses anything else with exit
2 before anything is minted, and the fold records no decision from a record
of any other shape. The payload's shape is stated once, in `pinax.event`,
and both the command and the fold read it there. The item must exist, under
the same target rule every item event follows, so an unknown id ends with
exit 1 and nothing appended.

With `--effect park` the command also appends an ordinary `item.parked`
event with the same reason, and both land in one commit: `item.decided`
first, then `item.parked`, with consecutive sequence numbers, one shared
timestamp, and the park's `prev` naming the decision. The publish sequence
does this in one run, with one fetch, one union fold, the target rule on
each event, one policy check, one projection, one commit whose subject is
`pinax: item.decided <id>`, and one push. Every other command still
publishes exactly one event. With `--effect release` only the decision is
appended, and the item's status, gate and owner-gate class are unchanged;
the claim follows the expiry rule above, as for any event.

The fold records on the item the latest valid `item.decided` that no valid
tombstone annuls, by fold order, as `decision`: its option, label, effect,
reason, actor, time and event id. The item page shows it in a `## Decision`
section after `## Briefing`, and `pinax status --json` carries it as an
additive `decision` object on the rows it prints for that item. Decisions
change nothing in how an item with no decision renders or serialises. A
decision is never a
note, so an item's newest note is unchanged by it.

A repeat is harmless, and it delivers what an earlier run left undone.
The repeat rule is asked on the union fold the publish sequence already
builds: the working-tree log united with the remote default branch's
committed shards when fetched. When the latest decision on the item there
has the same option, label, effect, reason and actor, the actor's host
compared in lower case, and for `park` the item is parked in that same
fold, the command appends nothing and continues into the commit and push
half of the sequence with nothing minted. When the remote default branch,
as fetched in this run, already records the decision, it makes no commit
and no push and exits 0 with `"published": true` and `"sync_status":
"already_published"`. Otherwise a decision left uncommitted by a refused
commit or a staging failure is committed as a fresh one would be, under
the subject `pinax: item.decided <id>`, and a retry after a rejected push
publishes the existing decision without appending: when the default branch
is checked out, offline mode does not apply and `HEAD` holds commits the
remote lacks, the command pushes with the same attempts, pull and
regeneration as a fresh event. Offline, with no reachable remote or with
another branch checked out it exits 0 with `"published": false` and the
status a fresh event would report there. A repeat never adds a second
`item.decided` or `item.parked`.

`policy.decision_actors_set` is a repository-level event, `{"actors":
[...]}`, written by `pinax policy decision-actors`; each listed actor is a
`role@host` handle stored with its host in lower case, and the last one in
fold order is in force. Under flow any actor may decide and the list is
recorded and not checked. Under strict, the publish sequence asks
`pinax.owner_gates` on the pre-append union fold, and a decision from an
actor not on the list, or with no list set, is refused with exit 8 and
nothing minted.

Every released Pinax before 0.4.0 predates version 2 events and reports
each version 2 event that no tombstone names as an integrity violation.
Decision events are version 2 events and are reported the same way. No
release wrote version 2 events without decisions.

## Parent-child roll-up

An item that has at least one direct child through a `parent-child` edge
carries a derived `rollup` value in the fold state, beside its own
`status` field. The roll-up reads the fold state; it never reads or
changes `status`.

The roll-up is one of four buckets, evaluated in this order:

- `done`: every direct child is done.
- `building`: any direct child is claimed (a live reconciled claim, per
  the claim rules above) or itself in a build-cycle status (being
  built, in blind review, or under adjudication).
- `blocked`: every open direct child is blocked, and at least one direct
  child is open. Open means not done and not annulled. An item whose
  children are all done or annulled, but not literally every child
  done, reads as `queued` rather than a vacuous `blocked` - blocked
  describes concrete waiting work, not the absence of any.
- `queued`: none of the above.

A direct child that itself has children is represented, in its parent's
roll-up, by its own already-computed roll-up value - never by its raw
status or claim. This is what makes a parent of parents roll up
correctly: a grandchild's state reaches its grandparent through the
child's own resolved bucket, read once. A child id that names no item
in the fold (its own creating event was itself annulled) is treated the
same as an annulled child: it cannot make its parent `done`, and it is
never counted as an open child a parent is waiting on.

An item that sits on a parent-child cycle carries no roll-up. The cycle
is already reported as a warning by the same detector that protects the
parent-child descendants walk; a roll-up computed by walking through a
cycle is not a well-defined function of a finite child set, so no value
is guessed for it.

The roll-up never changes what `ready` or `next` select, and never
changes an item's own `status`: readiness stays decided by the `blocks`
edge rule alone, exactly as described above. A parent whose roll-up
reads `building` remains, itself, a candidate for `ready`/`next`
whenever its own status and claim make it one - the roll-up is a
read-only summary of what is under an item, not a gate on the item
itself.

The fold state's JSON forms gain an additive `rollup` key: present, one
of the four bucket names, only on an item that carries one; absent on
every item with no children and on an item excluded by a cycle. The
board and the per-item projection render the same value beside the
item's status, in the same stable text form, wherever it is present;
an item with no roll-up renders exactly as it always has. Determinism
follows from the fold itself: the roll-up is a pure function of the
already-reconciled items and edges, so folding the same log twice, or
folding it through replay at a commit, produces the same roll-up bytes.

## Publishing a state change

A command that appends an event does not simply write the log and stop.
It runs one publish sequence, shared by every mutating command: fetch
`origin`, fold the union of the local log and the remote default branch's
committed shards through the one fold above, append the event, regenerate
the projection, commit the shard and the projection with the repository's
hooks running, push the remote default branch when it is checked out, and
fetch once more to fold over the state the remote now holds. The union
fold, not a clock, decides the event's sequence number and its
predecessor reference, so a machine that has never seen a remote event
still numbers its own event after it.

That last fold, not the push, decides how a claim ends. Two machines can
claim one item with no lock between them: each appends and publishes, and
the fold reconciles them by the claim order above. A claim the fold
reports superseded ends with exit 3 and a report naming the winning event
and its actor; the losing claim stays in the log, published and
superseded. Nothing checks ownership before appending, so the fold is the
only reconciler, before the push and after it. A claim that publishes
first and is only later overtaken by an earlier claim from another
machine ends normally and learns of it at its next mutation or from the
board.

A claim also refuses to record a timestamp behind published history. On
the fold before the append, and before anything is minted, it compares
the timestamp it intends to record with the greatest timestamp over the
events read from the remote shards whose ids no valid `event.annulled`
tombstone names. When the intended timestamp is earlier than that maximum
by more than a tolerance, the command ends with exit 6 and records
nothing at all. The tolerance is five seconds, or the value of
`PINAX_CLOCK_TOLERANCE_S` when that names a number of seconds. The report
carries both timestamps, the difference, the event and actor that set the
maximum, and the remedy: retire a future-dated event with `pinax annul`,
which takes it out of the maximum, or raise the tolerance for a single
crossing. This refuses a clock behind known history; it does not decide a
genuine race, which stays with the claim order.

A rejected push is retried. Between attempts the sequence pulls, then
regenerates the board and the item pages from the merged log and commits
them before pushing again. That regeneration is also how a conflict in
the generated Markdown is resolved: those files are never merged, by hand
or by a driver, and `.ergon/.gitattributes` assigns no driver to them. A
merge that conflicted anywhere else is left for git to refuse, and the
refusal ends the retry.

The commit hands its hooks the resolved tracker root as `PINAX_ROOT`,
because the sequence gives a hook no arguments: a hook that calls Pinax
back verifies the tracker the command resolved, not a root the ambient
environment happened to pin. A hook that refuses the commit is never
bypassed. The command reports the hook's output, names the shard and the
event id, leaves the appended event uncommitted and ends with exit 7. A
`.gitignore` rule that swallows the event log is one of those refusals,
because the installed pre-commit hook's `pinax verify` fails while the log
is ignored; the repair is the `.gitignore`, then the commit.

Every command refuses an actor that is not written as `role@host`, before
it appends anything.

A write command is one of the commands that run the publish sequence: add,
claim, done, block, park, decide, release, the four policy setters,
priority, annul, dep add, dep rm and note add. Given `--json`, a write
command prints exactly one JSON object on standard output on every exit
code, and that JSON result carries `published` and `sync_status`.
`published` is true only when this run's push to the remote default
branch was accepted, or when the remote default branch already held the
event when this run fetched it. A push URL that differs from the fetch
URL is not checked. `sync_status` is the publish sequence's own status
for the run. When
the sequence fails, the result is the sequence's own report. Exit 0 also
covers a commit that stayed local, so a caller which must know a write
reached other machines reads `published`.

A write command refused before the sequence runs prints a refusal result.
Its `published` is false, and its `sync_status` names the refusal:
`invalid_argument`, `not_a_tracker`, `root_mismatch`, `prefix_refused`,
`invalid_dependency`, `unknown_target` or `internal_error`. Its `error`
holds the refusal's message, and `exit_code` and `command` name the exit
code and the subcommand. The CLI entry point parses with the output
captured and takes the mode from what argparse parsed, for the Python
running it, so an argument error, an early refusal and an uncaught
exception are all covered. A `--json` that argparse refuses, such as
`--json=1` or one before the subcommand, still asks for JSON. A refusal
is named where it is raised, never read from its message. The exit code
is the same with or without `--json`. An interrupt prints a result whose
`sync_status` is `interrupted`, unless the sequence had already printed
its report, and then ends the process by the interrupt as before. A help
or version request is the one exception: it prints its text unchanged
and exits 0.

Every command except `claim` accepts offline mode, requested with
`--offline` or the `PINAX_OFFLINE=1` environment variable: it skips the
fetch and the push, and commits locally only. A repository with no
reachable `origin`, or no published default branch, falls back to the
same local-only behaviour on its own, with a warning naming the remote
instead of a silent skip. A directory with no git repository at all falls
back the same way, appending and regenerating the projection with a
warning and no commit attempted. `claim` never goes offline: it exists to
make ownership visible on every other machine, so it always needs the
remote. Without a reachable `origin`, or without a git repository to
fetch from, it ends without appending anything, rather than recording a
claim nobody else can see.

Exit codes: 0 is committed, and published when the remote default branch
is checked out; 1 is an id that names no item or event, with nothing
appended; 2 is an actor not written as `role@host`, with nothing
appended; 3 is a published claim the fold over the pushed remote state
reports superseded; 4 is a remote `claim` needed and could not reach or
read, or any command's remote that answered but whose committed events
could not be read; 5 is a push rejected on every attempt; 6 is a claim
timestamp behind published history, or one the sequence cannot read in the
envelope's own form, reported apart with nothing appended either way; 7 is
a commit refused or a staging failure, with the appended event left
uncommitted either way; 8 is the project's policy refusing the event under
strict (a block for the owner without a listed class, or a decision from an
actor not on the decision-actor list or with no list set), with nothing
appended.

`registry`, `reconcile`, and `init` never run this sequence; they keep
their own local behaviour, described in README.md, because none of them
needs cross-machine visibility the moment it runs.

## Verification and tombstones

`pinax verify` validates event identifiers before comparing projections. A
valid `event.annulled` tombstone has its own valid hash, a non-empty target and
reason, and a target different from its own ID. It permits a known bad target
to remain in the append-only audit trail while suppressing that target's fold
effects. A malformed, forged, or self-annulling tombstone has no exemption.

The predecessor check looks for broken links per shard file and actor. For an
event after the first of its shard and actor, a non-empty `prev` is broken when
it names no id in that scope. For the first event, a non-empty `prev` is broken
when it names no id anywhere in the log: a chain may continue from another
shard file, as in a log written before the shard choice took `prev` only from
the chosen shard, but the event it names must exist. For a version 2 event that
is not covered by a valid tombstone, `pinax verify` fails and names the event.
For a version 1 event, the first of its shard included, the fold warns and
`pinax verify` passes.

A tombstone covers only the event it names, and the annulled event has no
effect on the fold. `pinax verify` does not check the content of an annulled
event, only that some line in the log carries its id. There is no exemption for
a `prev` that names an annulled event: Pinax's commands only append to the
log, so they do not remove an annulled event's line, and a `prev` naming it
finds that id in the log. An
annulled event whose line was deleted, with no line left carrying its id,
leaves its successor's `prev` broken, and `pinax verify` fails.

Together these rules catch a change to a hashed field (`seq`, `ts`, `actor`,
`type`, `payload`, `prev`, `v`) of a version 2 event that leaves its recorded
id as it was, whatever event follows it: the edited event's id no
longer verifies. They also catch a deletion of, a repointed `prev` on, or an
edit that recomputes the id of a version 2 event that a later version 2 event
names as its `prev`, while that later event is left unchanged and is not
covered by a valid tombstone: the later event's `prev` no longer names a known
event, or a repointed event's id no longer verifies. This holds for a chain's
oldest event too, whatever its new place in sort order, and when the later
events are moved byte for byte into another shard file of the same actor or
into another actor's shard file.

Some changes are not caught by the log itself:

- a rewrite that also recomputes every later event of the chain, up to the
  newest;
- deletion of a chain's newest events, since no later event names them;
- deletion of any event that no later event names as its `prev`: after two
  clones wrote to the same chain and were merged, this includes the newest
  event of each branch, even while later events of the chain stay unchanged;
- deletion of an event whose successor is covered by a valid tombstone, since
  the successor's broken `prev` is exempt with it;
- replacing an annulled event's line with any line that keeps its id, in place
  or in another shard file, since an annulled event's content is not checked;
- deletion of a version 2 event whose next event is a version 1 event written
  by an earlier client during an upgrade: the broken link sits on the version 1
  event, so the fold warns and `pinax verify` passes;
- an edit to a version 2 event that also recomputes its own id, when its next
  event is a version 1 event written by an earlier client during an upgrade:
  the broken link sits on the version 1 event, so the fold warns and
  `pinax verify` passes;
- any edit to a version 1 event's `prev`, which is outside the version 1 hash;
- a field added to an event beside the hashed fields, which is outside the hash
  and which the fold does not read.

Git history is the anchor for these. Once the events were committed, the commit
that removes them shows them as lines deleted from the shard file, and a clone
or hub that already fetched them still holds them. Events deleted before any
commit leave no trace in Git history. Pinax does not compare the log with Git
history itself. Pinax has no signing keys and no outside
anchor, and does not authenticate a hostile writer.

## Operational boundary

Pinax records delivery work. A note stores a typed reference and a short
caption, not an unrestricted document body. The tracker does not index or
synchronise an external knowledge base.
