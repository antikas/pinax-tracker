# ADR-001: event log and deterministic fold

## Decision

Pinax stores tracker truth as append-only JSONL events. Current state is a
pure fold over all parsed events.

Event IDs are BLAKE2b hashes of canonical JSON for `(seq, ts, actor, type,
payload)`. The fold sorts by `(seq, ts, actor, id)` and deduplicates by ID.
This makes the result independent of file order and duplicate lines.

Event parsing normalises CRLF and LF input. The fold never depends on wall
clock time, locale, random values, or Python's built-in `hash()`.

`prev` is a stored predecessor reference used for a local dangling-reference
check. It is outside the version 1 event hash. The format therefore does not
prove complete history or defend against a hostile writer.

## Consequences

Git can union-merge concurrent appends. Replaying a Git ref uses the same fold
as a working tree. Hash-valid tombstones can retire known bad events without
rewriting their raw bytes.
