# ADR-004: operational boundary

## Decision

Pinax records operational delivery state: items, dependencies, claims, status,
briefings, and evidence references. It does not index or synthesise a separate
knowledge base.

`note.added` stores a typed reference and a caption capped by the CLI. The
event log and generated projections are excluded from content-recall sources.

## Consequences

Tracker data remains current because it is read from its event log. Durable
knowledge belongs in the system chosen by the team and can be referenced from
an item without copying the body into Pinax.
