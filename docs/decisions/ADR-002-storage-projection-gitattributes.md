# ADR-002: storage and generated projections

## Decision

The event log is stored in `.ergon/log/*.jsonl`. The generated Markdown board
and item pages are stored in `.ergon/board.md` and `.ergon/items/`.

JSONL and generated text use LF line endings. `.gitattributes` assigns the
`union` merge driver to JSONL shards. Appends take an operating-system file
lock, while the fold remains tolerant of a torn trailing line.

Every state-changing command regenerates the projection. `pinax verify`
renders the projection in memory and compares it with the working files.

## Consequences

The log is the sole state store. The projection is readable in any clone but
must not be edited by hand. A merge conflict in generated output is resolved
by regeneration from the merged log.
