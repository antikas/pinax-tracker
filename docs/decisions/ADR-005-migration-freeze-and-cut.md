# ADR-005: migration

## Decision

Migration imports a frozen legacy board in one direction into the event log.
The importer preserves semantic item identifiers where possible, maps legacy
statuses explicitly, and verifies the imported state before the legacy board is
retired.

## Consequences

Pinax avoids a dual-write period. The event log becomes the source of current
delivery state after a successful import.
