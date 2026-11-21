# Pinax

Pinax is a Git-native tracker for software delivery work. It stores an
append-only JSONL event log in the repository, derives the current board from
that log, and commits the generated Markdown board alongside the code.

The tracker gives a team one durable record of items, dependencies, ownership,
status, and completion briefings. Git carries the record between clones and
branches. There is no service to host, database to operate, or account to
create.

## Install

Pinax requires Python 3.10 or later and Git.

```bash
pipx install pinax-tracker
pinax --help
```

The same package installs with pip:

```bash
python -m pip install pinax-tracker
```

To work from a checkout:

```bash
git clone https://github.com/antikas/pinax-tracker.git
cd pinax-tracker
python -m pip install -e .
```

## Start tracking a repository

Run `pinax init` at the root of the repository you want to track. It creates
`.ergon/`, records the initial events, configures the log's merge attributes,
and installs a pre-commit verification hook when Git allows it.

```bash
pinax init --actor alex@laptop
pinax add --title "Ship the widget" --actor alex@laptop
pinax claim <item-id> --actor alex@laptop
pinax done <item-id> --briefing completion.md --actor alex@laptop
pinax status
```

`completion.md` is a short work record. It stays with the item in the
generated projection.

## Everyday commands

```text
pinax --version
pinax add --title TEXT
pinax claim ITEM_ID
pinax done ITEM_ID --briefing FILE
pinax block ITEM_ID --gate scope|decision|destructive|proposal [--owner-gate CLASS]
pinax park ITEM_ID --reason TEXT
pinax decide ITEM_ID --option KEY --label TEXT --effect release|park --reason TEXT
pinax dep add FROM_ID --blocks TO_ID
pinax ready [--under ITEM_ID]
pinax next [--under ITEM_ID]
pinax status [--json] [--under ITEM_ID]
pinax board [--json]
pinax report [--json]
pinax release ITEM_ID --reason TEXT
pinax policy claim-expiry --hours N
pinax policy process --mode flow|strict
pinax policy owner-gates --class TEXT [--class TEXT ...]
pinax policy decision-actors --decider ROLE@HOST [--decider ROLE@HOST ...]
pinax policy check [--json]
pinax verify [--fix]
pinax replay --at GIT_REF
```

`pinax verify` checks that every physical parsed event has a valid event hash
unless it is covered by a valid tombstone, then compares the generated board
and item pages with the committed projection. `--fix` regenerates only a
drifted projection. It refuses invalid event history without changing the log
or projection.

## Owner-gate classes

A block with gate `decision` or `destructive` is a block for the owner: the
item waits until the owner decides or approves. `scope` and `proposal` are
not, and never need a class.

Each project chooses a process with `pinax policy process --mode flow|strict`.
The last such event in the log is the one in force, and a log without one
runs on `flow`.

Before you set `strict`, upgrade every clone, and every older `pinax` on
PATH that a pre-commit hook could run, to 0.4.0 or later.

- `flow`: nothing is checked. `--owner-gate CLASS` on `pinax block` records
  the class in the event and nowhere else, so the board and item files are
  the same as an earlier Pinax writes for the same log. A block without the
  option is the same event as before. A class that matches a listed one,
  whatever its case and spacing, is stored in the list's spelling; any other
  class is stored as given with its spaces collapsed.
- `strict`: a block for the owner must name a class with `--owner-gate`,
  and any class given must be on the project's list. A block with no class,
  or with a class not on the list, is refused, nothing is appended, and the
  command ends with exit 8. The check runs on the union with the remote, so
  a process set on another clone already binds. With `--offline` there is no
  remote check, so a strict process set on another clone and not yet pulled
  does not bind. The refusal is one line that shows the list, for example:

  ```text
  pinax: strict process: 'because I said so' is not an owner-gate class. Give one of these with --owner-gate: production deployment; publication; ... Nothing was appended.
  pinax: strict process: a block with gate decision needs an owner-gate class. Give one of these with --owner-gate: production deployment; publication; ... Nothing was appended.
  ```

Under strict, while the item is blocked, the class shows on its board line
(`gate=decision · owner gate: publication`) and as `owner_gate:` in its
item file. When the item leaves blocked, the class goes with the block.

The list is project policy too: a project sets its own list with
`pinax policy owner-gates --class TEXT [--class TEXT ...]`. Until it does,
the list is:

- production deployment
- publication
- real personal data
- irreversible or destructive act
- material change of scope, cost or architecture
- login-bound act or owner-held input
- start signal for a material plan

`pinax policy check` lists every blocked item for the owner with no class,
or with a class not on the list. Under `flow` it only reports and exits 0.
Under `strict` it exits 1 when it lists any item. It reads the local log
and changes nothing, so pull before you run it.

## Decisions

`pinax decide ITEM_ID --option KEY --label TEXT --effect release|park
--reason TEXT` records a decision on an item as one `item.decided` event
holding the option, its label, the effect and the reason; the actor and the
time are the event's own. The option, label and reason must not be blank,
and the effect is `release` or `park`; anything else ends with exit 2 and
nothing appended. An item that does not exist ends with exit 1.

- `release`: only the decision is appended. The item's status, gate and
  owner-gate class stay as they were, and the command says so, for example
  `pinax: decision recorded on ITEM_ID; status unchanged: blocked (gate
  scope)`. A decision does not release a claim; like any event, one
  recorded past a claim's expiry deadline marks that claim expired.
- `park`: the decision and an ordinary `item.parked` event with the same
  reason are appended in one commit, decision first, so the item is parked
  exactly as `pinax park` would park it.

A decision is never written as a note, so an item's newest note stays what
it was. The item page shows the latest decision in a `## Decision` section,
and `pinax status --json` carries it as a `decision` object on every row it
prints for that item. `pinax status --json` prints no row for a queued or
ready item without a live claim, so such an item's decision shows on its
item page and in the fold state only. Decisions change nothing in how an
item with no decision renders or serialises.

Running the same decision again is harmless, and it finishes what an
earlier run left undone. When the latest decision on the item, in the
local log together with the remote default branch, has the same option,
label, effect, reason and actor (the actor's host compared in lower case),
and for `park` the item is still parked, nothing is appended and the JSON
says `"appended": false`. If the remote already has the decision, nothing
is committed or pushed and the JSON says `"published": true` and
`"sync_status": "already_published"`. Otherwise a decision left
uncommitted by a refused commit is committed, and a decision left local by
a rejected push is pushed, so a retry after a rejected push publishes the
existing decision without appending a second copy. `published` then says
whether it reached the remote.

Under `flow` any actor may decide. Under `strict` only an actor on the
project's decision-actor list may: set it with `pinax policy
decision-actors --decider ROLE@HOST [--decider ROLE@HOST ...]`, where
`--actor` stays the writer of the policy event. The last such event is in
force. Under strict, a decision from an actor not on the list, or with no
list set, ends with exit 8 and nothing appended.

Before the first decision is written, every clone, and every older `pinax`
a pre-commit hook could run, must run this version. Every released Pinax
before 0.4.0 predates version 2 events and reports each version 2 event
that no tombstone names as an integrity violation. Decision events are
version 2 events and are reported the same way.

## Publishing a state change

Every command above that appends an event (add, claim, done, block, park,
decide, release, the four policy setters, priority, dep, note, annul) runs
the same publish sequence. It fetches the
remote named `origin`, folds the union of the local log and the remote
default branch's committed shards, appends the event, regenerates the
board and item pages, commits the shard and the projection with the
repository's hooks running, and pushes the remote default branch when that
branch is checked out. This is what makes a claim or a completion visible
to every other clone as soon as the command returns.

The commit carries the resolved tracker root in its environment, as
`PINAX_ROOT`. The sequence gives a hook no arguments, so a hook that calls
Pinax back, such as the pre-commit hook `pinax init` installs and the
`pinax verify` it runs, checks the tracker the command resolved rather
than whatever root the surrounding environment happened to name.

Every one of these commands also refuses an actor that is not written as
`role@host`, before it appends anything.

A write command is one of the commands that run the publish sequence. Given
`--json`, a write command prints exactly one JSON object on standard output,
whatever the exit code, and that JSON result carries `published` and
`sync_status`. `published` is true only when this run's push to the remote
default branch was accepted, or when the remote default branch already
held the event when this run fetched it. `sync_status` names what the
sequence did (for example
`pushed`, `committed_local` or `remote_unreachable`). When the sequence
fails, the result is the sequence's own report. Exit 0 also covers a commit
that stayed local, so a caller which must know a write reached other
machines reads `published`.

A write command refused before the sequence runs prints a refusal result.
Its `published` is false, and its `sync_status` names the refusal:
`invalid_argument`, `not_a_tracker`, `root_mismatch`, `prefix_refused`,
`invalid_dependency`, `unknown_target` or `internal_error`. Its `error`
holds the refusal's message, and `exit_code` and `command` name the exit
code and the subcommand. The exit code is the same with or without
`--json`, and the message still goes to standard error. An interrupt
prints a result whose `sync_status` is `interrupted`, unless the sequence
had already printed its report, and then ends the process by the
interrupt as before. A help or version request is the one exception: it
prints its text unchanged and exits 0.

All of them except `claim` accept `--offline`, or the `PINAX_OFFLINE=1`
environment variable, to skip the fetch and the push and commit locally
only. A repository with no reachable `origin`, or no published default
branch, behaves the same way on its own: the command commits locally and
prints a warning naming the remote, instead of failing. A directory that
is not a git repository at all falls back the same way, appending and
regenerating the projection with a warning and no commit attempted, since
there is nothing to commit into. `claim` is the one exception. Its whole
purpose is telling every other machine an item is taken, so it always
needs the remote: without a reachable `origin`, or without a git
repository to fetch from, it ends without appending anything at all,
rather than recording a claim nobody else can see.

The exit codes these commands share are:

    0  the event is committed, and published when the remote default
       branch is checked out
    1  the command names an item that does not exist (an annulment: an
       event that does not exist); nothing was appended
    2  the actor is not written as role@host; nothing was appended
    3  a published claim that the fold over the pushed remote state reports
       superseded
    4  claim needed the remote and could not reach or read it, or any
       command's remote answered but its committed events could not be
       read
    5  the push was rejected on every attempt
    6  a claim's timestamp is behind published history, or cannot be read;
       nothing was appended
    7  the shard and the projection could not be staged, or the commit
       was refused; the appended event stays uncommitted either way
    8  the project's policy refuses the event: under the strict process,
       a block for the owner without a listed owner-gate class, or a
       decide from an actor not on the decision-actor list or with no
       list set; nothing was appended

A hook that refuses the commit is never worked around. The command prints
the hook's output, names the shard and the event id, leaves the appended
event uncommitted, and ends with exit 7; fix what the hook objected to and
commit the shard and the projection. A `.gitignore` rule that swallows
`.ergon/log/` is one of these refusals: the installed hook's `pinax verify`
fails while the log is ignored, so a state-changing command ends with exit
7 instead of committing an event the repository would silently keep to
itself. Run `pinax init` again to reinstall the `.ergon/.gitignore`
negation, or fix the repository's own `.gitignore`, then commit the shard.

`registry`, `reconcile`, and `init` do not run this sequence. They keep
their own local behaviour: `registry` records and lists remote
repositories for `pinax overview --remote`, `reconcile` imports
already-completed offline work from a text file into ordinary events, and
`init` creates `.ergon/` itself. None of the three needs cross-machine
visibility the moment it runs, so none of them fetches or pushes.

`pinax doctor` reports two separate things a git-native tracker can be
behind on: shards committed to `HEAD` but not yet on the remote default
branch (unsynced, a normal state for offline work waiting to be pushed),
and events appended to the working tree but not yet committed at all
(uncommitted, an orphaned trail worth investigating). They are listed in
distinct sections because they call for different next steps: push one,
commit the other.

## Storage model

```text
.ergon/
  log/*.jsonl       append-only event shards
  board.md           generated project board
  items/<id>.md      generated item pages
```

Each event has a content-derived identifier. The fold sorts events by
`(seq, ts, actor, id)`, deduplicates by identifier, and applies the resulting
stream deterministically. Git's union merge driver preserves concurrent JSONL
appends; a duplicated line is a no-op in the fold.

Claims resolve during the fold. If two claims name one item, the earliest
`(ts, actor, id)` claim wins and the other becomes a reported supersession.
Dependency edges drive `ready` and `next`.

A live claim also makes its item ineligible for `ready` and `next`, on
every clone, from the moment the claim reaches that clone's log - a claim
and a status change are two different events, so a claimed item can still
show status `queued`. `pinax status` renders such an item in its building
list with the owner and how many hours ago the claim was made, labelled by
the claim itself rather than the item's creation time, even before its
status field ever moves to `building`. A held claim ends when it is
released with `pinax release ITEM_ID --reason TEXT`, or when it expires:
the policy in force is the last `pinax policy claim-expiry --hours N`
event at or before the claim, and twenty-four hours by default when the
log carries none before it. A quiet log - one that records nothing after
the claim - never expires it, so a claim only ends when the log itself
records the release or a later event past the deadline.

`--under ITEM_ID` on `ready`, `next`, and `status` restricts the view to
the transitive `parent-child` descendants of that item - the ancestor's
own row is never included, only its descendants; an id that names no item
in the log is refused, nothing is appended. `status --under` requires
repo scope; it is refused together with `--portfolio` and with the
two-argument setter form. It scopes the building/shipped/parked lists and
the next/queue_depth figures; the repo-wide claim-reconciliation notice
count is unaffected.

`next --json` and `status --json` are JSON objects with an `under` key:
the ancestor id, or `null` when `--under` is not given. `ready --json` is
a bare array of item ids when `--under` is not given; with `--under` it is
`{"ready": [...], "under": "<id>"}`. `ready --json --all-branches` is an
object that also carries the `under` key.

An item with at least one `parent-child` child carries a derived
`rollup` value beside its own `status` - `done`, `building`, `blocked`,
or `queued` - summarising its children without ever changing the
item's own status or its eligibility for `ready`/`next`. `pinax board`
and `pinax status` render it alongside the status wherever it is
present; the rule the fold applies is in [DESIGN.md](DESIGN.md).

Each new event's identifier covers the event before it in its chain.
`pinax verify` fails when a hashed field of such an event (`seq`, `ts`,
`actor`, `type`, `payload`, `prev`, `v`) is changed and the event keeps its
recorded identifier. It also fails when a new event that a later new event names as its
predecessor is deleted, repointed, or edited with a recomputed identifier, while
that later event stays as it was and is not annulled, even when the later events
are moved into another shard file. These are not covered by the checks:
deleting, or editing with a recomputed identifier, an event no later event
names as its predecessor, which includes the newest events of a chain and,
after two clones wrote to the same chain, the newest event of each branch;
deleting an event whose successor is annulled; the content of an annulled
event, of which only the identifier is checked; a rewrite that recomputes an
event and every later event of its chain; deleting, or editing with a
recomputed identifier, an event followed by one an earlier Pinax wrote; any
edit to the predecessor field of an event an earlier Pinax wrote; and a field
added to an event beside the hashed fields, which the fold does not read. Git
history is the anchor for them.
Events written by an earlier
Pinax have no version field and keep verifying under the rule they were written
with. There are no signatures and no outside anchor: Git history is the record
of a rewrite the log cannot show on its own. [DESIGN.md](DESIGN.md) lists what
each rule catches.

An earlier Pinax checks every event under the old rule, so it reports each
newly written event that no tombstone names as an integrity violation, its
`pinax verify` fails, and its pre-commit hook refuses commits. Every clone, and every older `pinax` on
PATH that a pre-commit hook could run, must run this version before the first
version 2 event is written.

Decisions follow the same order: every clone, and every older `pinax` a
pre-commit hook could run, must run this version before the first decision
is written. The Decisions section above says what an earlier Pinax does
with a log that holds them.

The event envelope, integrity rules, and projection model are described in
[DESIGN.md](DESIGN.md). The executable event handlers and renderers are the
authoritative implementation details.
Architecture decisions are in [docs/decisions](docs/decisions).

## Licence

Pinax is available under the [MIT License](LICENSE).
