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
pinax add --title TEXT
pinax claim ITEM_ID
pinax done ITEM_ID --briefing FILE
pinax block ITEM_ID --gate scope|decision|destructive|proposal
pinax park ITEM_ID --reason TEXT
pinax dep add FROM_ID --blocks TO_ID
pinax ready [--under ITEM_ID]
pinax next [--under ITEM_ID]
pinax status [--json] [--under ITEM_ID]
pinax board [--json]
pinax report [--json]
pinax release ITEM_ID --reason TEXT
pinax policy claim-expiry --hours N
pinax verify [--fix]
pinax replay --at GIT_REF
```

`pinax verify` checks that every physical parsed event has a valid event hash
unless it is covered by a valid tombstone, then compares the generated board
and item pages with the committed projection. `--fix` regenerates only a
drifted projection. It refuses invalid event history without changing the log
or projection.

## Publishing a state change

Every command above that appends an event (add, claim, done, block, park,
priority, dep, note, annul) runs the same publish sequence. It fetches the
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
    4  claim needed the remote and could not reach or read it, or any
       command's remote answered but its committed events could not be
       read
    5  the push was rejected on every attempt
    7  the shard and the projection could not be staged, or the commit
       was refused; the appended event stays uncommitted either way

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

`next --json` and `status --json` were already JSON objects, so both
simply gain an `under` key: the ancestor id, or `null` when `--under` is
not given. `ready --json` was a bare array of item ids, and a JSON array
cannot carry a key, so it keeps that exact shape when `--under` is not
given; with `--under` it becomes `{"ready": [...], "under": "<id>"}`.
`ready --json --all-branches` was already an object before `--under`
existed, so it also just gains the `under` key.

An item with at least one `parent-child` child carries a derived
`rollup` value beside its own `status` - `done`, `building`, `blocked`,
or `queued` - summarising its children without ever changing the
item's own status or its eligibility for `ready`/`next`. `pinax board`
and `pinax status` render it alongside the status wherever it is
present; the rule the fold applies is in [DESIGN.md](DESIGN.md).

The predecessor field is a local consistency check. It can report a dangling
predecessor reference, but version 1 does not hash the predecessor field or
provide remote anchoring, signatures, or hostile-writer authentication.

The event envelope, integrity rules, and projection model are described in
[DESIGN.md](DESIGN.md). The executable event handlers and renderers are the
authoritative implementation details.
Architecture decisions are in [docs/decisions](docs/decisions).

## Licence

Pinax is available under the [MIT License](LICENSE).
