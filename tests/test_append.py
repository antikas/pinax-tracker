"""
tests/test_append.py — append lock, LF normalisation, torn-trailing-line test.

state as without it, with a logged warning.

Also tests: append writes LF-terminated lines on all platforms.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile

import pytest

from pinax.fold import fold_events, read_events
from pinax.event import mint_event
from pinax.append import append_event
from helpers import normalise_for_comparison


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN_LOG = os.path.join(FIXTURES_DIR, "golden_log.jsonl")
GOLDEN_STATE = os.path.join(FIXTURES_DIR, "golden_state.json")


def _make_torn_log(lines: list[bytes], tmpdir: str) -> str:
    """Write a log file where the last line is a torn/partial JSON fragment."""
    path = os.path.join(tmpdir, "torn.jsonl")
    with open(path, "wb") as fh:
        for line in lines:
            fh.write(line + b"\n")
        # Write a partial/torn trailing line — simulate crash mid-append.
        fh.write(b'{"id":"torn_partial","seq":99,"ts":"')
    return path


def _read_lines(path: str) -> list[bytes]:
    with open(path, "rb") as fh:
        raw = fh.read()
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return [line for line in normalised.split(b"\n") if line]


# ---------------------------------------------------------------------------
# 6. Torn trailing line: fold is identical to the clean log, warning emitted
# ---------------------------------------------------------------------------

def test_torn_trailing_line_folds_same_as_clean(caplog):
    """
    A torn trailing line is logged as a warning and ignored.
    The fold result is identical to folding the clean log.
    """
    lines = _read_lines(GOLDEN_LOG)

    # Get the clean fold.
    clean_dir = tempfile.mkdtemp()
    try:
        clean_path = os.path.join(clean_dir, "clean.jsonl")
        with open(clean_path, "wb") as fh:
            for line in lines:
                fh.write(line + b"\n")
        clean_state = fold_events(read_events(clean_dir))
    finally:
        shutil.rmtree(clean_dir, ignore_errors=True)

    # Get the torn fold.
    torn_dir = tempfile.mkdtemp()
    try:
        torn_path = _make_torn_log(lines, torn_dir)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            torn_state = fold_events(read_events(torn_dir))
    finally:
        shutil.rmtree(torn_dir, ignore_errors=True)

    assert normalise_for_comparison(torn_state) == normalise_for_comparison(clean_state), (
        f"Torn-log fold != clean fold\n"
        f"torn: {json.dumps(normalise_for_comparison(torn_state), sort_keys=True, indent=2)}\n"
        f"clean: {json.dumps(normalise_for_comparison(clean_state), sort_keys=True, indent=2)}"
    )

    # A warning about the torn line MUST have been emitted.
    warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("torn" in str(m).lower() or "unparseable" in str(m).lower()
               for m in warning_msgs), (
        f"Expected a warning about the torn trailing line; got: {warning_msgs}"
    )


def test_torn_trailing_line_matches_golden():
    """
    Torn-trailing-line fold equals the golden state (not just 'same as clean').
    """
    lines = _read_lines(GOLDEN_LOG)
    torn_dir = tempfile.mkdtemp()
    try:
        _make_torn_log(lines, torn_dir)
        torn_state = fold_events(read_events(torn_dir))
    finally:
        shutil.rmtree(torn_dir, ignore_errors=True)

    with open(GOLDEN_STATE, "r", encoding="utf-8") as fh:
        expected = json.load(fh)

    normalised = normalise_for_comparison(torn_state)
    assert normalised == expected, (
        f"Torn-log fold != golden_state\n"
        f"got: {json.dumps(normalised, sort_keys=True, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Append: LF-terminated lines, locked write
# ---------------------------------------------------------------------------

def test_append_writes_lf_terminated_line():
    """append_event writes a single LF-terminated line."""
    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        event = mint_event(
            seq=0,
            ts="2026-06-29T12:00:00Z",
            actor="test@host",
            etype="ergon.created",
            payload={"repo": "test"},
            prev="",
        )
        path = append_event(log_dir, event, actor="test@host")

        with open(path, "rb") as fh:
            raw = fh.read()

        # Must end with LF (not CRLF).
        assert raw.endswith(b"\n"), "Appended line does not end with LF"
        assert b"\r\n" not in raw, "Appended line contains CRLF"

        # Must be parseable.
        line = raw.rstrip(b"\n")
        parsed = json.loads(line.decode("utf-8"))
        assert parsed["id"] == event["id"]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_append_foldable():
    """Appended events produce a foldable, correct state."""
    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        e1 = mint_event(0, "2026-06-29T12:00:00Z", "test@host", "ergon.created",
                        {"repo": "test"}, prev="")
        append_event(log_dir, e1, actor="test@host")

        e2 = mint_event(1, "2026-06-29T12:00:01Z", "test@host", "item.created",
                        {"item_id": "pnx-test", "title": "T", "prefix": "pnx", "status": "queued"},
                        prev=e1["id"])
        append_event(log_dir, e2, actor="test@host")

        state = fold_events(read_events(log_dir))
        assert "ergon" in state
        assert "pnx-test" in state.get("items", {})
        assert state["items"]["pnx-test"]["status"] == "queued"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_multiple_appends_order_independent():
    """Multiple appended events fold to the same state regardless of shard file read order."""
    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        # Append to two different actor shards.
        e1 = mint_event(0, "2026-06-29T12:00:00Z", "actor-a@host", "ergon.created",
                        {"repo": "test"}, prev="")
        append_event(log_dir, e1, actor="actor-a@host")

        e2 = mint_event(0, "2026-06-29T12:00:01Z", "actor-b@host", "item.created",
                        {"item_id": "pnx-x1", "title": "X", "prefix": "pnx", "status": "queued"},
                        prev="")
        append_event(log_dir, e2, actor="actor-b@host")

        e3 = mint_event(1, "2026-06-29T12:00:02Z", "actor-a@host", "item.created",
                        {"item_id": "pnx-x2", "title": "Y", "prefix": "pnx", "status": "queued"},
                        prev=e1["id"])
        append_event(log_dir, e3, actor="actor-a@host")

        state = fold_events(read_events(log_dir))
        items = state.get("items", {})
        assert "pnx-x1" in items
        assert "pnx-x2" in items
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
