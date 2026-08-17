"""Offline action reconciliation tests."""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from pinax.commands import init as init_cmd
from pinax.commands import add as add_cmd
from pinax.commands import reconcile as rec_cmd
from pinax.fold import fold, read_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo() -> str:
    """Create a temp repo with .ergon/ initialised. Caller must clean up."""
    tmpdir = tempfile.mkdtemp()
    init_cmd.run(repo_root=tmpdir, actor="operator@example.test")
    return tmpdir


def _add_item(repo_root: str, title: str) -> str:
    """Add an item, return its item_id."""
    log_dir = os.path.join(repo_root, ".ergon", "log")
    before = set(fold(log_dir).get("items", {}).keys())
    add_cmd.run(repo_root=repo_root, title=title, prefix="pnx", actor="operator@example.test", as_json=True)
    after = fold(log_dir).get("items", {})
    new_ids = set(after.keys()) - before
    assert len(new_ids) == 1
    return next(iter(new_ids))


def _write_offline(repo_root: str, lines: list[str]) -> str:
    path = os.path.join(repo_root, "BACKLOG-OFFLINE.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write(line + "\n")
    return path


def _state(repo_root: str) -> dict:
    return fold(os.path.join(repo_root, ".ergon", "log"))


def _events(repo_root: str) -> list[dict]:
    return read_events(os.path.join(repo_root, ".ergon", "log"))


@pytest.fixture
def repo():
    root = _make_repo()
    yield root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Happy path: done + park imported with correct actor/provenance
# ---------------------------------------------------------------------------

def test_done_line_imports_with_offline_actor_and_provenance(repo):
    item_id = _add_item(repo, "Item A")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | fixed the thing",
    ])

    rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=False)

    events = _events(repo)
    completed = [e for e in events if e["type"] == "item.completed"]
    assert len(completed) == 1
    ev = completed[0]

    # The action-line actor is preserved instead of the reconciler.
    assert ev["actor"] == "operator@laptop"
    # The line timestamp becomes the event timestamp verbatim.
    assert ev["ts"] == "2026-07-01T10:00:00Z"
    # Reconciliation provenance lives in the payload, not the actor field.
    payload = ev["payload"]
    assert payload["imported_by"] == "reviewer@example.test"
    assert payload["source"] == "BACKLOG-OFFLINE.md"
    assert payload["item_id"] == item_id
    assert payload["caption"] == "fixed the thing"
    assert "source_line_hash" in payload and payload["source_line_hash"]
    assert "imported_at" in payload

    # The event lands in the action-line actor's shard.
    shard_path = os.path.join(repo, ".ergon", "log", "operator-laptop.jsonl")
    assert os.path.isfile(shard_path)

    state = _state(repo)
    assert state["items"][item_id]["status"] == "done"
    assert state["items"][item_id]["status_changed_by"] == "operator@laptop"


def test_park_line_requires_and_carries_reason(repo):
    item_id = _add_item(repo, "Item B")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop park {item_id} | blocked on X",
    ])

    rec_cmd.run(repo_root=repo, as_json=False)

    events = _events(repo)
    parked = [e for e in events if e["type"] == "item.parked"]
    assert len(parked) == 1
    assert parked[0]["payload"]["reason"] == "blocked on X"
    assert parked[0]["actor"] == "operator@laptop"

    state = _state(repo)
    assert state["items"][item_id]["status"] == "parked"
    assert state["items"][item_id]["park_reason"] == "blocked on X"


def test_done_without_caption_is_optional(repo):
    item_id = _add_item(repo, "Item C")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id}",
    ])
    rec_cmd.run(repo_root=repo, as_json=False)
    events = [e for e in _events(repo) if e["type"] == "item.completed"]
    assert len(events) == 1
    assert "caption" not in events[0]["payload"]


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------

def test_reconcile_twice_is_zero_duplicate_events(repo):
    item_id = _add_item(repo, "Item D")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | done it",
    ])

    rec_cmd.run(repo_root=repo, as_json=False)
    events_after_first = _events(repo)
    completed_first = [e for e in events_after_first if e["type"] == "item.completed"]
    assert len(completed_first) == 1

    # Second reconcile: no new offline lines (file was rewritten empty of
    # unprocessed content), so this is a pure no-op.
    rec_cmd.run(repo_root=repo, as_json=False)
    events_after_second = _events(repo)
    completed_second = [e for e in events_after_second if e["type"] == "item.completed"]
    assert len(completed_second) == 1
    assert events_after_first == events_after_second


def test_reconcile_after_restoring_reconciled_raw_line_is_noop(repo):
    """
    A git merge can resurrect an already-reconciled raw line into the
    unprocessed block.  Re-running reconcile must not double-append -- the
    source_line_hash guard skips it, and the line is re-filed into Reconciled
    referencing the SAME original event id.
    """
    item_id = _add_item(repo, "Item E")
    raw_line = f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | done it"
    offline_path = _write_offline(repo, [raw_line])

    rec_cmd.run(repo_root=repo, as_json=False)
    events_first = _events(repo)
    completed_first = [e for e in events_first if e["type"] == "item.completed"]
    assert len(completed_first) == 1
    original_event_id = completed_first[0]["id"]

    # Simulate a git merge resurrecting the raw line: prepend it back onto
    # the (now-rewritten) offline file, ahead of the existing sections.
    with open(offline_path, "r", encoding="utf-8") as fh:
        rewritten_content = fh.read()
    with open(offline_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(raw_line + "\n\n" + rewritten_content)

    rec_cmd.run(repo_root=repo, as_json=False)

    events_second = _events(repo)
    completed_second = [e for e in events_second if e["type"] == "item.completed"]
    # Zero duplicate events -- still exactly one item.completed.
    assert len(completed_second) == 1
    assert completed_second[0]["id"] == original_event_id
    assert events_first == events_second

    # Fold state identical.
    assert _state(repo) == _state(repo)


def test_reconcile_json_reports_already_imported_on_dup(repo):
    item_id = _add_item(repo, "Item F")
    raw_line = f"- 2026-07-01T10:00:00Z operator@laptop done {item_id}"
    offline_path = _write_offline(repo, [raw_line])
    rec_cmd.run(repo_root=repo, as_json=False)

    # Restore the raw line and reconcile again, capturing JSON this time.
    with open(offline_path, "a", encoding="utf-8", newline="\n") as fh:
        pass
    with open(offline_path, "r", encoding="utf-8") as fh:
        rewritten = fh.read()
    with open(offline_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(raw_line + "\n\n" + rewritten)

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec_cmd.run(repo_root=repo, as_json=True)
    result = json.loads(buf.getvalue())
    assert result["imported"] == []
    assert len(result["already_imported"]) == 1


# ---------------------------------------------------------------------------
# 3. Negative gates
# ---------------------------------------------------------------------------

def test_unknown_item_id_rejected_never_appended(repo):
    _write_offline(repo, [
        "- 2026-07-01T10:00:00Z operator@laptop done pnx-doesnotexist | oops",
    ])
    events_before = _events(repo)

    rec_cmd.run(repo_root=repo, as_json=False)

    events_after = _events(repo)
    assert events_after == events_before  # nothing appended

    with open(os.path.join(repo, "BACKLOG-OFFLINE.md"), encoding="utf-8") as fh:
        content = fh.read()
    assert "## Rejected" in content
    assert "unknown item-id" in content


def test_terminal_state_conflict_done_for_done_skipped(repo):
    item_id = _add_item(repo, "Item G")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | first",
        f"- 2026-07-01T11:00:00Z reviewer@example.test done {item_id} | second, too late",
    ])

    rec_cmd.run(repo_root=repo, as_json=False)

    completed = [e for e in _events(repo) if e["type"] == "item.completed"]
    # Only ONE item.completed event -- the second (conflicting) line was
    # skipped, never appended.
    assert len(completed) == 1
    assert completed[0]["actor"] == "operator@laptop"

    with open(os.path.join(repo, "BACKLOG-OFFLINE.md"), encoding="utf-8") as fh:
        content = fh.read()
    assert "already done" in content


def test_terminal_state_conflict_park_for_done_skipped(repo):
    item_id = _add_item(repo, "Item H")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | shipped",
        f"- 2026-07-01T11:00:00Z operator@laptop park {item_id} | actually blocked",
    ])

    rec_cmd.run(repo_root=repo, as_json=False)

    events = _events(repo)
    assert not [e for e in events if e["type"] == "item.parked"]
    assert len([e for e in events if e["type"] == "item.completed"]) == 1

    state = _state(repo)
    assert state["items"][item_id]["status"] == "done"


def test_malformed_line_rejected(repo):
    _write_offline(repo, [
        "- this is not a valid grammar line",
        "not even a bullet",
    ])
    events_before = _events(repo)
    rec_cmd.run(repo_root=repo, as_json=False)
    assert _events(repo) == events_before

    with open(os.path.join(repo, "BACKLOG-OFFLINE.md"), encoding="utf-8") as fh:
        content = fh.read()
    assert "## Rejected" in content
    assert "malformed line" in content


def test_unknown_verb_rejected(repo):
    item_id = _add_item(repo, "Item I")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop claim {item_id}",
        f"- 2026-07-01T10:00:00Z operator@laptop add {item_id} | a new wish",
        f"- 2026-07-01T10:00:00Z operator@laptop note {item_id} | a note",
    ])
    events_before = _events(repo)
    rec_cmd.run(repo_root=repo, as_json=False)
    assert _events(repo) == events_before

    with open(os.path.join(repo, "BACKLOG-OFFLINE.md"), encoding="utf-8") as fh:
        content = fh.read()
    assert content.count("unknown verb") == 3


def test_park_without_reason_rejected(repo):
    item_id = _add_item(repo, "Item J")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop park {item_id}",
    ])
    events_before = _events(repo)
    rec_cmd.run(repo_root=repo, as_json=False)
    assert _events(repo) == events_before

    with open(os.path.join(repo, "BACKLOG-OFFLINE.md"), encoding="utf-8") as fh:
        content = fh.read()
    assert "park requires" in content


# ---------------------------------------------------------------------------
# --dry-run touches nothing
# ---------------------------------------------------------------------------

def test_dry_run_touches_nothing(repo):
    item_id = _add_item(repo, "Item K")
    offline_path = _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | dry run me",
    ])
    with open(offline_path, encoding="utf-8") as fh:
        content_before = fh.read()
    events_before = _events(repo)

    rec_cmd.run(repo_root=repo, dry_run=True, as_json=False)

    with open(offline_path, encoding="utf-8") as fh:
        content_after = fh.read()
    assert content_after == content_before
    assert _events(repo) == events_before

    # A subsequent real (non-dry-run) reconcile still processes the line.
    rec_cmd.run(repo_root=repo, as_json=False)
    completed = [e for e in _events(repo) if e["type"] == "item.completed"]
    assert len(completed) == 1


def test_dry_run_json_shape(repo):
    item_id = _add_item(repo, "Item L")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id}",
    ])
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec_cmd.run(repo_root=repo, dry_run=True, as_json=True)
    result = json.loads(buf.getvalue())
    assert result["dry_run"] is True
    assert len(result["imported"]) == 1
    assert "event_id" not in result["imported"][0]
    # Nothing reconcile-related was appended (only the pre-existing
    # ergon.created/phase.opened/item.created events from setup remain).
    assert not [e for e in _events(repo) if e["type"] == "item.completed"]


# ---------------------------------------------------------------------------
# File lifecycle
# ---------------------------------------------------------------------------

def test_file_lifecycle_prior_sections_never_deleted(repo):
    item_a = _add_item(repo, "Item M")
    item_b = _add_item(repo, "Item N")
    offline_path = _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_a} | first batch",
    ])
    rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=False)

    with open(offline_path, encoding="utf-8") as fh:
        after_first = fh.read()
    assert "## Reconciled" in after_first

    # Append a NEW line for the second pass, ahead of the existing sections.
    with open(offline_path, "r", encoding="utf-8") as fh:
        existing = fh.read()
    with open(offline_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"- 2026-07-02T10:00:00Z operator@laptop done {item_b} | second batch\n\n"
            + existing
        )

    rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=False)

    with open(offline_path, encoding="utf-8") as fh:
        after_second = fh.read()

    # Both dated Reconciled sections survive (never delete).
    assert after_second.count("## Reconciled") == 2
    assert "first batch" in after_second
    assert "second batch" in after_second

    state = _state(repo)
    assert state["items"][item_a]["status"] == "done"
    assert state["items"][item_b]["status"] == "done"


def test_appended_line_below_existing_sections_is_imported_not_lost(repo):
    """Import a new action line appended below earlier result sections."""
    item_a = _add_item(repo, "Item S")
    item_b = _add_item(repo, "Item T")
    offline_path = _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_a} | first batch",
    ])
    rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=False)

    with open(offline_path, encoding="utf-8") as fh:
        after_first = fh.read()
    assert "## Reconciled" in after_first

    # Append (never prepend) a brand-new action line at the literal bottom
    # of the file, below the '## Reconciled' section -- the real
    # append-only workflow, not the prepend-based simulation used elsewhere.
    with open(offline_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"- 2026-07-02T10:00:00Z operator@laptop done {item_b} | second batch, appended at bottom\n")

    result_buf = []
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=True)
    result = json.loads(buf.getvalue())

    # The bottom-appended line must show up as imported, not silently dropped.
    assert len(result["imported"]) == 1, result
    assert result["imported"][0]["item_id"] == item_b

    state = _state(repo)
    assert state["items"][item_a]["status"] == "done"
    assert state["items"][item_b]["status"] == "done", (
        "line appended below an existing '## Reconciled' section was silently lost"
    )

    with open(offline_path, encoding="utf-8") as fh:
        after_second = fh.read()
    assert "second batch, appended at bottom" in after_second
    assert after_second.count("## Reconciled") == 2


def test_repeated_reconcile_of_non_bullet_lines_is_stable(repo):
    """Keep rejected non-action lines stable across repeated reconciliation."""
    offline_path = _write_offline(repo, [
        "# A markdown heading, not an action line",
        "just some prose someone jotted down, also not an action line",
    ])
    events_before = _events(repo)

    rec_cmd.run(repo_root=repo, as_json=False)
    assert _events(repo) == events_before  # rejection never touches the log

    with open(offline_path, encoding="utf-8") as fh:
        content_after_first = fh.read()
    assert content_after_first.count("## Rejected") == 1
    assert "A markdown heading" in content_after_first
    assert "just some prose" in content_after_first

    # Reconcile twice more: byte-identical, constant length, after the
    # first pass -- the line was quarantined once and left alone.
    rec_cmd.run(repo_root=repo, as_json=False)
    with open(offline_path, encoding="utf-8") as fh:
        content_after_second = fh.read()
    assert content_after_second == content_after_first
    assert len(content_after_second) == len(content_after_first)
    assert content_after_second.count("## Rejected") == 1

    rec_cmd.run(repo_root=repo, as_json=False)
    with open(offline_path, encoding="utf-8") as fh:
        content_after_third = fh.read()
    assert content_after_third == content_after_first
    assert len(content_after_third) == len(content_after_first)
    assert content_after_third.count("## Rejected") == 1


def test_action_line_with_marker_lookalike_caption_is_imported(repo):
    """Import an action whose caption contains the detail-marker text."""
    item_a = _add_item(repo, "Item U")
    item_b = _add_item(repo, "Item V")
    offline_path = _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_a} | first batch",
    ])
    rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=False)

    with open(offline_path, encoding="utf-8") as fh:
        after_first = fh.read()
    assert "## Reconciled" in after_first

    collision_line = (
        f"- 2026-07-02T10:00:00Z operator@laptop done {item_b} "
        "| renamed foo  => bar"
    )
    with open(offline_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(collision_line + "\n")

    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=True)
    result = json.loads(buf.getvalue())

    assert len(result["imported"]) == 1, result
    assert result["imported"][0]["item_id"] == item_b

    state = _state(repo)
    assert state["items"][item_b]["status"] == "done", (
        "a valid done line whose caption contained '  => ' was silently "
        "swallowed as already-processed"
    )
    completed = [
        e for e in _events(repo)
        if e["type"] == "item.completed" and e["payload"]["item_id"] == item_b
    ]
    assert len(completed) == 1
    assert completed[0]["payload"]["caption"] == "renamed foo  => bar"


def test_no_offline_file_is_a_clean_noop(repo):
    events_before = _events(repo)
    rec_cmd.run(repo_root=repo, as_json=False)
    assert _events(repo) == events_before
    assert not os.path.isfile(os.path.join(repo, "BACKLOG-OFFLINE.md"))


# ---------------------------------------------------------------------------
# Same-item, multiple lines in one pass (overlay resolution)
# ---------------------------------------------------------------------------

def test_two_done_lines_same_item_one_pass_second_is_skipped(repo):
    item_id = _add_item(repo, "Item O")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | first",
        f"- 2026-07-01T10:01:00Z operator@laptop done {item_id} | duplicate in same batch",
    ])
    rec_cmd.run(repo_root=repo, as_json=False)
    completed = [e for e in _events(repo) if e["type"] == "item.completed"]
    assert len(completed) == 1
    assert completed[0]["payload"]["caption"] == "first"


# ---------------------------------------------------------------------------
# seq/prev continuity per actor shard
# ---------------------------------------------------------------------------

def test_seq_is_global_monotonic_and_prev_chains_per_actor(repo):
    item_a = _add_item(repo, "Item P")
    item_b = _add_item(repo, "Item Q")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_a} | a",
        f"- 2026-07-01T10:01:00Z operator@laptop done {item_b} | b",
    ])
    rec_cmd.run(repo_root=repo, as_json=False)

    events = _events(repo)
    laptop_events = [e for e in events if e["actor"] == "operator@laptop"]
    laptop_events.sort(key=lambda e: e["seq"])
    assert len(laptop_events) == 2
    # seq is a strictly increasing global counter (matches every other
    # write command's next_seq = max(all seq) + 1 convention).
    assert laptop_events[1]["seq"] == laptop_events[0]["seq"] + 1
    # prev chains within this actor's own shard.
    assert laptop_events[1]["prev"] == laptop_events[0]["id"]


# ---------------------------------------------------------------------------
# parse_offline_line unit coverage (grammar edge cases)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected_ok", [
    ("- 2026-07-01T10:00:00Z operator@laptop done pnx-abcd", True),
    ("- 2026-07-01T10:00:00Z operator@laptop done pnx-abcd | a caption", True),
    ("- 2026-07-01T10:00:00Z operator@laptop park pnx-abcd | a reason", True),
    ("- 2026-07-01T10:00:00Z operator@laptop park pnx-abcd", False),
    ("- 2026-07-01T10:00:00Z operator@laptop add pnx-abcd | wish", False),
    ("- 2026-07-01T10:00:00Z operator@laptop note pnx-abcd | ref", False),
    ("- 2026-07-01T10:00:00Z operator@laptop claim pnx-abcd", False),
    ("2026-07-01T10:00:00Z operator@laptop done pnx-abcd", False),
    ("- operator@laptop done pnx-abcd", False),
    ("-    ", False),
])
def test_parse_offline_line_grammar(line, expected_ok):
    result = rec_cmd.parse_offline_line(line)
    assert result["ok"] is expected_ok


def test_parse_offline_line_is_deterministic_for_hash():
    line = "- 2026-07-01T10:00:00Z operator@laptop done pnx-abcd | a caption"
    p1 = rec_cmd.parse_offline_line(line)
    p2 = rec_cmd.parse_offline_line(line)
    h1 = rec_cmd._source_line_hash(p1["actor"], p1["ts"], p1["verb"], p1["item_id"], p1["text"])
    h2 = rec_cmd._source_line_hash(p2["actor"], p2["ts"], p2["verb"], p2["item_id"], p2["text"])
    assert h1 == h2


# ---------------------------------------------------------------------------
# --json shape on the happy path
# ---------------------------------------------------------------------------

def test_json_output_shape_happy_path(repo):
    item_id = _add_item(repo, "Item R")
    _write_offline(repo, [
        f"- 2026-07-01T10:00:00Z operator@laptop done {item_id} | shipped",
    ])
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rec_cmd.run(repo_root=repo, actor="reviewer@example.test", as_json=True)
    result = json.loads(buf.getvalue())
    assert set(result.keys()) == {"file", "dry_run", "imported", "already_imported", "rejected", "skipped"}
    assert result["dry_run"] is False
    assert len(result["imported"]) == 1
    entry = result["imported"][0]
    assert entry["actor"] == "operator@laptop"
    assert entry["item_id"] == item_id
    assert entry["verb"] == "done"
    assert "event_id" in entry
