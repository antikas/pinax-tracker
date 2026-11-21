"""The design documents state what the event chain does not catch."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "DESIGN.md"
ADR = ROOT / "docs" / "decisions" / "ADR-001-event-log-ssot-deterministic-fold.md"

NEWEST = "deletion of a chain's newest events, since no later event names them"
V1_SUCCESSOR = (
    "deletion of a version 2 event whose next event is a version 1 event "
    "written by an earlier client during an upgrade"
)
GIT_DELETED_LINES = (
    "Once the events were committed, the commit that removes them shows them "
    "as lines deleted from the shard file, and a clone or hub that already "
    "fetched them still holds them."
)


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _design_section(heading: str) -> str:
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index(f"## {heading}")
    end = text.index("\n## ", start + 1)
    return _flat(text[start:end])


def _adr() -> str:
    return _flat(ADR.read_text(encoding="utf-8"))


def test_design_names_deleting_the_newest_events_as_not_caught():
    section = _design_section("Verification and tombstones")
    not_caught = section[section.index("Some changes are not caught by the log itself"):]
    assert NEWEST in not_caught


def test_design_names_the_version_one_successor_case_as_not_caught():
    section = _design_section("Verification and tombstones")
    not_caught = section[section.index("Some changes are not caught by the log itself"):]
    assert V1_SUCCESSOR in not_caught
    assert "the fold warns and `pinax verify` passes" in not_caught


def test_design_names_git_history_deleted_lines_as_what_catches_them():
    section = _design_section("Verification and tombstones")
    assert "Git history is the anchor for these." in section
    assert GIT_DELETED_LINES in section


def test_design_no_longer_claims_any_version_two_deletion_is_caught():
    section = _design_section("Verification and tombstones")
    old = (
        "Together these rules catch an edit to, a deletion of, or a repointed "
        "`prev` on a version 2 event, as long as the later events of its chain "
        "are left unchanged"
    )
    assert old not in section
    assert DESIGN_EDIT_KEPT_ID in section
    assert DESIGN_SUCCESSOR_CONDITION in section


def test_adr_names_both_cases_among_what_git_history_anchors():
    text = _adr()
    anchored = text[text.index("Git history is the anchor for"):]
    assert NEWEST in anchored
    assert V1_SUCCESSOR in anchored
    assert GIT_DELETED_LINES in anchored


def test_adr_limits_the_caught_claim_to_an_unchanged_version_two_successor():
    text = _adr()
    assert "is caught as long as the later events of its chain are left unchanged" not in text
    assert ADR_EDIT_KEPT_ID in text
    assert ADR_SUCCESSOR_CONDITION in text


DESIGN_EDIT_KEPT_ID = (
    "Together these rules catch a change to a hashed field (`seq`, `ts`, "
    "`actor`, `type`, `payload`, `prev`, `v`) of a version 2 event that leaves "
    "its recorded id as it was, whatever event follows it: the edited event's id "
    "no longer verifies."
)
DESIGN_SUCCESSOR_CONDITION = (
    "They also catch a deletion of, a repointed `prev` on, or an edit that "
    "recomputes the id of a version 2 event that a later version 2 event names "
    "as its `prev`, while that later event is left unchanged and is not covered "
    "by a valid tombstone"
)
RECOMPUTED_BEFORE_V1 = (
    "an edit to a version 2 event that also recomputes its own id, when its "
    "next event is a version 1 event written by an earlier client during an "
    "upgrade"
)
ADR_EDIT_KEPT_ID = (
    "Changing a hashed field (`seq`, `ts`, `actor`, `type`, `payload`, `prev`, "
    "`v`) of a version 2 event without recomputing its ID is caught by that ID "
    "failing to verify, whatever event follows it."
)
ADR_SUCCESSOR_CONDITION = (
    "Deleting or repointing a version 2 event, or editing it and recomputing "
    "its ID, is caught when a later version 2 event names it as its `prev` and "
    "is left unchanged and not annulled."
)
ADR_RECOMPUTED_BEFORE_V1 = "or an edit to such an event that also recomputes its ID"


def test_design_splits_the_caught_claim_by_kind():
    section = _design_section("Verification and tombstones")
    assert DESIGN_EDIT_KEPT_ID in section
    assert DESIGN_SUCCESSOR_CONDITION in section


def test_design_names_the_recomputed_id_edit_before_a_version_one_successor():
    section = _design_section("Verification and tombstones")
    not_caught = section[section.index("Some changes are not caught by the log itself"):]
    assert RECOMPUTED_BEFORE_V1 in not_caught


def test_adr_splits_the_caught_claim_by_kind():
    text = _adr()
    assert ADR_EDIT_KEPT_ID in text
    assert ADR_SUCCESSOR_CONDITION in text


def test_adr_names_the_recomputed_id_edit_among_what_git_history_anchors():
    text = _adr()
    anchored = text[text.index("Git history is the anchor for"):]
    assert ADR_RECOMPUTED_BEFORE_V1 in anchored
    assert V1_SUCCESSOR + ", " + ADR_RECOMPUTED_BEFORE_V1 in anchored


# The behaviour behind each sentence below is pinned in
# tests/test_event_chain_v2.py:
#   oldest and moved-oldest: test_removing_a_chains_oldest_events_fails_verify,
#     test_oldest_event_moved_after_its_successor_with_a_recomputed_id_fails_verify;
#   later events moved to another shard file:
#     test_middle_change_with_later_events_moved_to_another_shard_fails_verify;
#   anchor continuing from another shard file:
#     test_a_chain_continued_in_another_shard_before_the_shard_choice_fix_verifies;
#   version 1 anchor: test_a_moved_version_one_anchor_warns_and_passes_verify;
#   tombstones: test_a_tombstone_for_the_deleted_event_does_not_excuse_the_moved_successor.
OLD_NOT_CAUGHT = (
    "removal of a chain's oldest events",
    "moves it after its successor in sort order",
    "trusted as its anchor",
    "the trusted first event",
    "is caught only while",
)
DESIGN_ANCHOR_RULE = (
    "For the first event, a non-empty `prev` is broken when it names no id "
    "anywhere in the log: a chain may continue from another shard file, as in a "
    "log written before the shard choice took `prev` only from the chosen "
    "shard, but the event it names must exist."
)
DESIGN_V1_ANCHOR = (
    "For a version 1 event, the first of its shard included, the fold warns "
    "and `pinax verify` passes."
)
DESIGN_TOMBSTONE = (
    "A tombstone covers only the event it names, and the annulled event has no "
    "effect on the fold. `pinax verify` does not check the content of an "
    "annulled event, only that some line in the log carries its id. There is no "
    "exemption for a `prev` that names an annulled event: Pinax's commands only "
    "append to the log, so they do not remove an annulled event's line, and a "
    "`prev` naming it finds that id in the log. An annulled event whose line was "
    "deleted, with no line left "
    "carrying its id, leaves its successor's `prev` broken, and `pinax verify` "
    "fails."
)
# Pinned by test_event_chain_v2.py: fork tip by
# test_deleting_a_fork_tip_passes_verify; successor tombstone by
# test_a_tombstone_covers_a_deleted_version_two_link; annulled stub by
# test_an_annulled_event_replaced_by_a_line_keeping_its_id_passes_verify.
DESIGN_NOT_CAUGHT_NEW = (
    "deletion of any event that no later event names as its `prev`: after two "
    "clones wrote to the same chain and were merged, this includes the newest "
    "event of each branch, even while later events of the chain stay unchanged;",
    "deletion of an event whose successor is covered by a valid tombstone, since "
    "the successor's broken `prev` is exempt with it;",
    "replacing an annulled event's line with any line that keeps its id, in "
    "place or in another shard file, since an annulled event's content is not "
    "checked;",
)
ADR_NOT_CAUGHT_NEW = (
    "deletion of any event no later event names, which after two clones wrote "
    "to the same chain includes the newest event of each branch, deletion of an "
    "event whose successor is annulled, replacing an annulled event's line with "
    "any line that keeps its ID,"
)
ADR_ANNULLED_CONTENT = (
    "`pinax verify` does not check the content of an annulled event, only that "
    "some line carries its ID."
)
OLD_BYTES_CLAIM = "annulment never removes the annulled event's bytes"


def test_design_lists_the_fork_tip_successor_tombstone_and_annulled_stub_as_not_caught():
    section = _design_section("Verification and tombstones")
    assert OLD_BYTES_CLAIM not in section
    not_caught = section[section.index("Some changes are not caught by the log itself"):]
    for sentence in DESIGN_NOT_CAUGHT_NEW:
        assert sentence in not_caught, sentence


def test_adr_lists_the_fork_tip_successor_tombstone_and_annulled_stub_as_not_caught():
    text = _adr()
    assert ADR_ANNULLED_CONTENT in text
    anchored = text[text.index("Git history is the anchor for"):]
    assert ADR_NOT_CAUGHT_NEW in anchored
DESIGN_OLDEST_AND_MOVED = (
    "This holds for a chain's oldest event too, whatever its new place in sort "
    "order, and when the later events are moved byte for byte into another "
    "shard file of the same actor or into another actor's shard file."
)
ADR_ANCHOR_RULE = (
    "A version 2 event fails `pinax verify` when its `prev` names no known ID "
    "in its (shard, actor) scope, or, for that scope's first event, no ID "
    "anywhere in the log; for a version 1 event this is a fold warning only."
)
ADR_OLDEST_AND_MOVED = (
    "This holds for a chain's oldest event, and when the later events are "
    "moved byte for byte into another shard file."
)


def test_design_states_the_anchor_rule_and_the_tombstone_scope():
    section = _design_section("Verification and tombstones")
    assert DESIGN_ANCHOR_RULE in section
    assert DESIGN_V1_ANCHOR in section
    assert DESIGN_TOMBSTONE in section
    assert "never remove an annulled event's line" not in section


def test_design_counts_the_oldest_event_and_moved_events_as_caught():
    section = _design_section("Verification and tombstones")
    caught = section[: section.index("Some changes are not caught by the log itself")]
    assert DESIGN_OLDEST_AND_MOVED in caught
    for phrase in OLD_NOT_CAUGHT:
        assert phrase not in section, phrase


def test_adr_states_the_anchor_rule_and_counts_the_oldest_event_as_caught():
    text = _adr()
    assert ADR_ANCHOR_RULE in text
    caught = text[: text.index("Git history is the anchor for")]
    assert ADR_OLDEST_AND_MOVED in caught
    for phrase in OLD_NOT_CAUGHT:
        assert phrase not in text, phrase


README = ROOT / "README.md"
README_OVERCLAIM = (
    "`pinax verify` fails when an event is edited, deleted, or repointed while "
    "the later events of its chain stay as they were."
)
README_CAUGHT = (
    "`pinax verify` fails when a hashed field of such an event (`seq`, `ts`, "
    "`actor`, `type`, `payload`, `prev`, `v`) is changed and the event keeps its "
    "recorded identifier. It also fails when a new event that a later new event names as "
    "its predecessor is deleted, repointed, or edited with a recomputed "
    "identifier, while that later event stays as it was and is not annulled, "
    "even when the later events are moved into another shard file."
)
README_NOT_COVERED = (
    "These are not covered by the checks: deleting, or editing with a "
    "recomputed identifier, an event no later event names as its predecessor, "
    "which includes the newest events of a chain and, after two clones wrote to "
    "the same chain, the newest event of each branch; deleting an event whose "
    "successor is annulled; the content of an annulled event, of which only the "
    "identifier is checked; a rewrite that recomputes an event and every later "
    "event of its chain; deleting, or editing with a recomputed identifier, an "
    "event followed by one an earlier Pinax wrote; any edit to the predecessor "
    "field of an event an earlier Pinax wrote; and a field added to an event "
    "beside the hashed fields, which the fold does not read. Git history is the "
    "anchor for them."
)
README_OLD_NOT_COVERED = (
    "These are not covered by the checks: an event no later event names",
    "each branch; an event whose successor is annulled;",
    "a rewrite of every later event of a chain;",
    "an event followed by one an earlier Pinax wrote; and the predecessor field",
)
README_EARLIER_PINAX = (
    "An earlier Pinax checks every event under the old rule, so it reports each "
    "newly written event that no tombstone names as an integrity violation, its "
    "`pinax verify` fails, and its pre-commit hook refuses commits."
)
ADDED_FIELD_DESIGN = (
    "a field added to an event beside the hashed fields, which is outside the "
    "hash and which the fold does not read."
)
ADDED_FIELD_ADR = (
    "and a field added to an event beside the hashed fields, which the fold "
    "does not read."
)


def test_design_and_adr_list_an_added_unhashed_field_as_not_caught():
    assert ADDED_FIELD_DESIGN in _design_section("Verification and tombstones")
    assert ADDED_FIELD_ADR in _adr()


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


def test_readme_limits_the_chain_claim_to_what_verify_catches():
    text = _flat(README.read_text(encoding="utf-8"))
    assert README_OVERCLAIM not in text
    assert README_CAUGHT in text
    assert README_NOT_COVERED in text
    for phrase in README_OLD_NOT_COVERED:
        assert phrase not in text, phrase
    assert "The oldest and the newest event of a chain" not in text
    assert "other than the newest of its chain" not in text


def test_readme_limits_what_an_earlier_pinax_reports_to_events_no_tombstone_names():
    # An earlier Pinax skips the id check for an event a valid tombstone names.
    text = _flat(README.read_text(encoding="utf-8"))
    assert README_EARLIER_PINAX in text
    assert "it reports each newly written event as an integrity violation" not in text


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
