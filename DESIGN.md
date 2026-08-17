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
