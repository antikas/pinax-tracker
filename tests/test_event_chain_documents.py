"""The README and the fold's own docstrings state what the event chain does not catch."""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"

UNSEEN_EDITS = "Because nothing signs the events, `verify` cannot see several kinds of edit."


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_the_readme_says_verify_cannot_see_several_kinds_of_edit():
    assert UNSEEN_EDITS in README.read_text(encoding="utf-8")


def test_an_added_unhashed_field_keeps_the_id_and_changes_no_fold():
    # Pins the documented gap: a field beside the hashed ones is outside the
    # id, and the fold reads none of it.
    from pinax.event import event_id_v2, verify_id
    from pinax.fold import fold_events

    payload = {"item_id": "pnx-aaaa", "title": "First", "prefix": "pnx", "status": "queued"}
    event = {
        "seq": 0, "ts": "2026-11-10T09:00:00Z", "actor": "coordinator@desk",
        "type": "item.created", "payload": payload, "prev": "", "v": 2,
    }
    event["id"] = event_id_v2(
        event["seq"], event["ts"], event["actor"], event["type"], payload, event["prev"]
    )
    padded = dict(event, note="added after the fact")
    assert verify_id(event) and verify_id(padded)
    assert fold_events([dict(padded)]) == fold_events([dict(event)])


def test_prev_chain_docstring_counts_the_oldest_event_as_caught():
    from pinax.fold import _check_prev_chain, broken_prev_links

    doc = _flat(_check_prev_chain.__doc__ or "")
    assert "deleting the first event" not in doc
    assert "the successor becomes the trusted first event" not in doc
    assert "a deleted event that is not its chain's newest" not in doc
    assert (
        "DETECTS: a deleted event that a later, non-annulled event names as its "
        "prev, the oldest included"
    ) in doc
    assert "the tip of each fork branch" in doc
    assert "deleting an event whose successor is annulled" in doc
    assert "DOES NOT CHECK: the content of an annulled event" in doc
    assert "WARNS ONLY: a broken link on a version 1 event, the anchor included" in doc
    rule = _flat(broken_prev_links.__doc__ or "")
    assert "for the first event of its scope (its anchor): no id anywhere in the log" in rule
    assert "There is no exemption for a prev that names the target of a tombstone." in rule
    assert "Annulment never removes" not in rule
    assert "never remove the target's line" not in rule
    assert (
        "Pinax's commands only append to the log, so they do not remove an "
        "annulled event's line"
    ) in rule
    assert "Verify does not check the content of an annulled event" in rule


def test_prev_chain_docstring_says_an_altered_event_fails_its_own_id_check():
    from pinax.fold import _check_prev_chain

    doc = _flat(_check_prev_chain.__doc__ or "")
    assert "deleting/altering the last event" not in doc
    assert "deleting/altering the first event" not in doc
    assert "fails its own id check" in doc
