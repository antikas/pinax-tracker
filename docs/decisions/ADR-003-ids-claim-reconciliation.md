# ADR-003: identifiers, hierarchy, and claims

## Decision

Item IDs use a readable prefix and a short content-derived suffix. The suffix
extends when needed to avoid a collision. An imported legacy identifier may be
retained as an alias.

Sub-items are normal items connected by a `parent-child` edge. Display numbers
are rendered from the graph and are never stored as identities.

Claims are reconciled during the deterministic fold. When multiple valid
claims target one item, the earliest `(ts, actor, id)` claim wins. Other claims
remain visible in the event history and are reported as superseded.

## Consequences

Concurrent clones can claim the same item without a central lock. The folded
result is deterministic and records the losing claim for review.
