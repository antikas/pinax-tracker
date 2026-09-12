# ADR-007: claim release and deterministic expiry

## Context

A claim holds an item on every machine that folds it. The fold reconciles
concurrent claims by the claim order of ADR-003 and the readiness rule keeps a
claimed item out of the ready set until the claim is settled. Nothing in the
log ever ends a claim, so a machine that stops mid-build holds its item for
good, and the only way back is to tombstone the claim event.

Two things were missing: an explicit release, and an expiry the fold applies
by itself. Neither may read a clock, because ADR-001 makes the fold a pure
function of the events it reads and the same log must fold to the same state
on every machine and at every replayed commit.

## Decision

Two event types carry the whole rule.

`item.claim_released` carries the item and a reason. `pinax release <id>
--reason <text>` mints it and hands it to the publish sequence of ADR-006,
like every other mutating command; it accepts offline mode as every command
but a claim does.

`policy.claim_expiry_set` carries a positive number of hours and names no
item: it is a repository-level event, and its commit subject names only the
event type. `pinax policy claim-expiry --hours N` mints it through the same
sequence with the same offline rule.

### Who may release

Any actor may release any live claim, with a reason. The case the release
exists for is a worker that has stopped: the actor who can say so is never the
actor holding the claim. Restricting the release to the owner would leave the
one case it was built for unserved. The reason is required and the releasing
actor is recorded in the event, so the act is attributable to whoever
performed it, and the released claim stays in the log.

### The policy in force

The policy in force for a claim is the last `policy.claim_expiry_set` at or
before that claim in claim order `(ts, actor, id)`, the order ADR-003 already
uses for claims. A claim with no policy event at or before it runs on
twenty-four hours. Ordering a policy event against a claim is a comparison of
recorded fields, so no clock is involved and a policy set today does not
change how a claim from last week already folded.

### Release and expiry in the fold

The fold walks each item's claims in claim order and keeps one live claim at a
time. A claim that arrives while another is live is superseded, exactly as
before. A live claim ends at the earlier of two points in claim order:

- the first `item.claim_released` for that item after it, or
- the first event of any type after it whose timestamp is at or past the
  claim's own timestamp plus the policy in force for that claim.

The claim that arrives after that point is live in its turn, so an item whose
claim was released or expired is claimable again and its next claim wins. An
ended claim sets no owner, no claim timestamp and no claim event id, and the
item's status is untouched: ending a claim says who may pick the work up, not
what state the work reached.

The expiry compares one recorded timestamp with another recorded timestamp.
The log's own events are its clock: a claim expires because the log has moved
past the deadline, not because the machine folding it has. A log that records
nothing after a claim never expires it, however long ago it was made; the
diagnosis command's guided reconciliation is the way out of that, and it now
offers a release beside its completion and park actions.

Every outcome is recorded. A released claim and an expired claim each leave an
entry naming the claim, the actor that held it, and the event that ended it,
beside the superseded entries the fold already keeps.

### Replay

Because an expiry is attributed to one specific later event, a fold that stops
before that event still shows the claim live. `pinax replay --at <ref>` reads
the log as committed at that ref and folds it through the same fold, so the
state it shows is the state that ref actually held. An expiry never appears
retroactively in a fold of an earlier prefix.

### Refusals

A release refuses, before anything is minted, an item the log does not know,
an item carrying no live claim, and an empty reason. A policy refuses a value
that is not a positive number of hours. Each refusal names its one reason and
appends nothing. The check reads the local fold before the sequence runs,
the way the dependency and priority commands already validate their item, and
it decides nothing about ownership: two machines racing to claim are still
reconciled by the fold alone, before the push and after it, as ADR-006
requires.

### The staleness threshold

The diagnosis command's staleness threshold defaults to the folded policy when
the log carries one, and to twenty-four hours otherwise. An explicit threshold
on the command line still wins. The threshold and the expiry therefore read
the same number, and a claim the fold has already expired carries no owner, so
what the diagnosis reports is the set of claims still live in a quiet log.

## Consequences

A stopped worker no longer holds an item for good. The item returns to the
ready set either when a human releases the claim or when the log's own events
pass the policy, and every machine that folds the log agrees on which happened
and when, because both are derived from the events alone.

Annulling a claim event remains available and remains a different act: it
retires an event that should never have counted, while a release records that
a claim that did count has ended.

The fold still reads no clock. The two comparisons the expiry adds are between
event timestamps, and the claim order they use is the one ADR-003 established.

A log that dates an event further ahead of a claim than the policy in force now
folds that claim as ended. Any construction whose subject is a live claim
therefore has to keep its later events inside the policy window, or set a policy
wide enough to cover the span it records; a span that used to be arbitrary is
now part of what the log means.
