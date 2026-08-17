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
pipx install "git+https://github.com/antikas/pinax-tracker.git@v0.1.1"
pinax --help
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
pinax ready
pinax next
pinax status [--json]
pinax board [--json]
pinax report [--json]
pinax verify [--fix]
pinax replay --at GIT_REF
```

`pinax verify` checks that every physical parsed event has a valid event hash
unless it is covered by a valid tombstone, then compares the generated board
and item pages with the committed projection. `--fix` regenerates only a
drifted projection. It refuses invalid event history without changing the log
or projection.

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

The predecessor field is a local consistency check. It can report a dangling
predecessor reference, but version 1 does not hash the predecessor field or
provide remote anchoring, signatures, or hostile-writer authentication.

The event envelope, integrity rules, and projection model are described in
[DESIGN.md](DESIGN.md). The executable event handlers and renderers are the
authoritative implementation details.
Architecture decisions are in [docs/decisions](docs/decisions).

## Licence

Pinax is available under the [MIT License](LICENSE).
