# ADR-001: event log and deterministic fold

## Decision

Pinax stores tracker truth as append-only JSONL events. Current state is a
pure fold over all parsed events.

Event IDs are BLAKE2b hashes of canonical JSON. A version 2 event carries
`"v":2` and its ID covers `(seq, ts, actor, type, payload, prev, v)`. A
version 1 event has no `v` field and its ID covers `(seq, ts, actor, type,
payload)`; it keeps verifying under that rule. Pinax writes only version 2
events, and a log may mix both versions. The fold sorts by `(seq, ts, actor,
id)` and deduplicates by ID.
This makes the result independent of file order and duplicate lines.

Event parsing normalises CRLF and LF input. The fold never depends on wall
clock time, locale, random values, or Python's built-in `hash()`.

`prev` is the ID of the actor's previous event in its shard. A version 2 event
fails `pinax verify` when its `prev` names no known ID in its (shard, actor)
scope, or, for that scope's first event, no ID anywhere in the log; for a
version 1 event this is a fold warning only. Changing a hashed field (`seq`,
`ts`, `actor`, `type`, `payload`, `prev`, `v`) of a version 2 event without
recomputing its ID is caught by that ID failing to verify, whatever event
follows it. Deleting or repointing a version 2 event, or editing it and
recomputing its ID, is caught when a later version 2 event names it as its
`prev` and is left unchanged and not annulled. This holds for a chain's oldest
event, and when the later events are moved byte for byte into another shard
file. `pinax verify` does not check the content of an annulled event, only that
some line carries its ID.

There are no signing keys and no outside anchor. Git history is the anchor for
what the log cannot show on its own: a rewrite that also recomputes every later
event of the chain up to the newest, deletion of a chain's newest events, since no later event names them,
deletion of any event no later event names, which after two clones wrote to the
same chain includes the newest event of each branch, deletion of an event whose
successor is annulled, replacing an annulled event's line with any line that
keeps its ID, deletion
of a version 2 event whose next event is a version 1 event written by an
earlier client during an upgrade, or an edit to such an event that also
recomputes its ID, either of which is a fold warning only and passes
`pinax verify`, any edit to a version 1 event's `prev`, and a field added to an
event beside the hashed fields, which the fold does not read. Once the events
were committed, the commit that removes them shows them as lines deleted from
the shard file, and a clone or hub that already fetched them still holds them.
Events deleted before any commit leave no trace in Git history. Pinax does not
compare the log with Git history itself. The format does not defend against a
hostile writer.

## Consequences

Git can union-merge concurrent appends. Replaying a Git ref uses the same fold
as a working tree. Hash-valid tombstones can retire known bad events without
rewriting their raw bytes.
