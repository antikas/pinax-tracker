"""
pinax.targets - the one rule that an event about something names something real.

An event that records a change to an existing item (a claim, a completion, a
block, a park, a priority, a release, a note, a dependency edge, a status
change) must name an item that exists, and an annulment must name an event
that exists. Appending one that does not leaves a line in the append-only log
that the fold can only ignore with a warning, forever, so the command refuses
before anything is appended.

This module is the single owner of the rule: which event types carry which
reference, and what a refusal says. pinax.sync.run_sequence applies it, over
the union fold, for every command that publishes an event; the commands that
append without the sequence (the status setter) and the one command that must
read the item before it can validate anything else (release) ask the same
function. No command keeps its own copy of the check.

item.created is not listed: it creates the id it carries. An event type not
listed here references nothing (policy, registry), so the rule has nothing to
say about it.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# Event type -> the payload keys that must each name an existing item.
ITEM_REFERENCES: Mapping[str, tuple[str, ...]] = {
    "item.status_changed": ("item_id",),
    "item.claimed": ("item_id",),
    "item.claim_released": ("item_id",),
    "item.blocked": ("item_id",),
    "item.completed": ("item_id",),
    "item.parked": ("item_id",),
    "item.priority_set": ("item_id",),
    "note.added": ("item_id",),
    "dep.added": ("from_id", "to_id"),
    "dep.removed": ("from_id", "to_id"),
}

# Event type -> the payload keys that must each name an existing event (an
# annulment addresses an event by its content-hash id, never an item).
EVENT_REFERENCES: Mapping[str, tuple[str, ...]] = {
    "event.annulled": ("target_id",),
}


def unknown_target_message(
    event_type: str,
    payload: dict,
    *,
    items: Mapping[str, object],
    events: Iterable[dict] = (),
) -> str | None:
    """
    None when every reference the event carries names something that exists;
    otherwise a caller-facing refusal naming the first unknown id.

    items is the fold state's item mapping. events is the event pool an
    annulment target is looked up in (only read for an event type that
    references events).
    """
    for key in ITEM_REFERENCES.get(event_type, ()):
        value = payload.get(key)
        if not isinstance(value, str) or value not in items:
            return (
                f"pinax: unknown item {value!r} - no item with that id exists, "
                "so nothing was appended"
            )

    event_keys = EVENT_REFERENCES.get(event_type, ())
    if event_keys:
        known = {event.get("id") for event in events}
        for key in event_keys:
            value = payload.get(key)
            if not isinstance(value, str) or value not in known:
                return (
                    f"pinax: unknown event {value!r} - no event with that id "
                    "exists in the log, so nothing was appended"
                )
    return None
