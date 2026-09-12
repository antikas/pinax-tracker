# Pinax design

Pinax keeps tracker state in an append-only JSONL event log. The current state
is a deterministic fold over that log. The Markdown board and item pages are
generated projections, committed so they remain readable without the CLI.

## Event envelope

```json
{"id":"content-hash","seq":12,"ts":"2026-08-17T12:00:00Z","actor":"alex@laptop","type":"item.created","payload":{},"prev":""}
```

`id` is a BLAKE2b hash of canonical JSON for `seq`, `ts`, `actor`, `type`, and
`payload`. Canonical JSON uses sorted ASCII keys and compact separators. `prev`
is stored as a predecessor reference but is outside the version 1 hash.

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
is checked out; 2 is an actor not written as `role@host`, with nothing
appended; 3 is a published claim the fold over the pushed remote state
reports superseded; 4 is a remote `claim` needed and could not reach or
read, or any command's remote that answered but whose committed events
could not be read; 5 is a push rejected on every attempt; 6 is a claim
timestamp behind published history, or one the sequence cannot read in the
envelope's own form, reported apart with nothing appended either way; 7 is
a commit refused or a staging failure, with the appended event left
uncommitted either way.

`registry`, `reconcile`, and `init` never run this sequence; they keep
their own local behaviour, described in README.md, because none of them
needs cross-machine visibility the moment it runs.

## Verification and tombstones

`pinax verify` validates event identifiers before comparing projections. A
valid `event.annulled` tombstone has its own valid hash, a non-empty target and
reason, and a target different from its own ID. It permits a known bad target
to remain in the append-only audit trail while suppressing that target's fold
effects. A malformed, forged, or self-annulling tombstone has no exemption.

The predecessor check identifies dangling references within a shard and actor.
Version 1 cannot prove complete history: `prev` is not in the event hash, and
there is no remote anchor, signing, or hostile-writer authentication.

## Operational boundary

Pinax records delivery work. A note stores a typed reference and a short
caption, not an unrestricted document body. The tracker does not index or
synchronise an external knowledge base.
