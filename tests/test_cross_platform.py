"""
tests/test_cross_platform.py — CRLF/LF cross-platform test.

Proves: event ids and the fold are byte-identical whether the log bytes on disk
use LF or CRLF.

Test path: writes a real CRLF file to a temp directory, reads it
back through the production fold path (read_events → fold_events), and asserts
the result is identical to the LF fold.  This is not an in-memory text-mode test.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from pinax.fold import fold_events, read_events
from pinax.event import parse_line
from helpers import normalise_for_comparison

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN_LOG = os.path.join(FIXTURES_DIR, "golden_log.jsonl")
GOLDEN_STATE = os.path.join(FIXTURES_DIR, "golden_state.json")


def _read_raw_lines(path: str) -> list[bytes]:
    with open(path, "rb") as fh:
        raw = fh.read()
    lf_normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return [line for line in lf_normalised.split(b"\n") if line]


def _fold_from_file(filepath: str) -> dict:
    """Fold a single JSONL file through the production path."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Copy the file into a temp log dir as a shard.
        dest = os.path.join(tmpdir, "test.jsonl")
        with open(filepath, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        return fold_events(read_events(tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test: CRLF file produces identical fold to LF file
# ---------------------------------------------------------------------------

def test_crlf_fold_identical_to_lf():
    """
    Write a CRLF variant of the golden fixture, fold both, assert identical state.

    The fold is byte-identical regardless of whether the shard uses LF or CRLF endings.
    """
    lines = _read_raw_lines(GOLDEN_LOG)

    # Write LF file.
    lf_dir = tempfile.mkdtemp()
    crlf_dir = tempfile.mkdtemp()
    try:
        lf_path = os.path.join(lf_dir, "test.jsonl")
        crlf_path = os.path.join(crlf_dir, "test.jsonl")

        with open(lf_path, "wb") as fh:
            for line in lines:
                fh.write(line + b"\n")

        with open(crlf_path, "wb") as fh:
            for line in lines:
                fh.write(line + b"\r\n")

        lf_state = fold_events(read_events(lf_dir))
        crlf_state = fold_events(read_events(crlf_dir))

        assert lf_state == crlf_state, (
            f"LF fold != CRLF fold\n"
            f"LF:   {json.dumps(lf_state, sort_keys=True, indent=2)}\n"
            f"CRLF: {json.dumps(crlf_state, sort_keys=True, indent=2)}"
        )
    finally:
        shutil.rmtree(lf_dir, ignore_errors=True)
        shutil.rmtree(crlf_dir, ignore_errors=True)


def test_crlf_fold_equals_golden():
    """CRLF fold equals the checked-in golden state (not just 'same as LF', but correct)."""
    lines = _read_raw_lines(GOLDEN_LOG)

    tmpdir = tempfile.mkdtemp()
    try:
        crlf_path = os.path.join(tmpdir, "test.jsonl")
        with open(crlf_path, "wb") as fh:
            for line in lines:
                fh.write(line + b"\r\n")

        crlf_state = fold_events(read_events(tmpdir))

        with open(GOLDEN_STATE, "r", encoding="utf-8") as fh:
            expected = json.load(fh)

        normalised = normalise_for_comparison(crlf_state)
        assert normalised == expected, (
            f"CRLF fold != golden_state\n"
            f"got: {json.dumps(normalised, sort_keys=True, indent=2)}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_event_ids_identical_lf_vs_crlf():
    """
    The event ids parsed from LF lines and CRLF lines are byte-identical, AND each
    stored id matches the blake2b recomputed from the envelope fields.

    Recompute the blake2b id from the envelope
    fields for LF and CRLF bytes and assert equal -- not just compare the stored id
    field (which would only prove the stored value survived the round-trip, not that
    the hash is platform-independent).

    Proves:
    1. parse_line(lf_bytes)["id"] == parse_line(crlf_bytes)["id"]  (stored ids match)
    2. event_id(envelope fields) == parse_line(lf_bytes)["id"]  (recomputed == stored)
    3. event_id(envelope fields) == parse_line(crlf_bytes)["id"]  (recomputed == stored for CRLF)
    ADR-001: hashes are performed on LF-normalised, terminator-excluded bytes.
    """
    from pinax.event import event_id as compute_event_id

    lines = _read_raw_lines(GOLDEN_LOG)

    lf_ids = []
    crlf_ids = []
    recomputed_ids = []

    for line in lines:
        lf_event = parse_line(line + b"\n")
        crlf_event = parse_line(line + b"\r\n")
        if lf_event and crlf_event:
            lf_ids.append(lf_event["id"])
            crlf_ids.append(crlf_event["id"])
            # Recompute the id from the envelope fields -- independent of line bytes.
            recomputed = compute_event_id(
                lf_event["seq"],
                lf_event["ts"],
                lf_event["actor"],
                lf_event["type"],
                lf_event["payload"],
            )
            recomputed_ids.append(recomputed)

    assert lf_ids == crlf_ids, (
        f"Event ids differ between LF and CRLF parsing\n"
        f"LF ids:   {lf_ids}\n"
        f"CRLF ids: {crlf_ids}"
    )

    assert lf_ids == recomputed_ids, (
        f"Stored event ids do not match recomputed blake2b ids -- "
        f"hash is NOT platform-independent (ADR-001 violation)\n"
        f"stored:     {lf_ids}\n"
        f"recomputed: {recomputed_ids}"
    )

    # Sanity: we parsed all 11 lines (7 original + 2 tie-break events + 1 phase.opened
    # deduplicated by _read_raw_lines since it returns raw lines).
    assert len(lf_ids) == 11, (
        f"Expected 11 lines in fixture (7 original + 2 tie-break + 1 phase.opened "
        f"+ 1 dep.added), got {len(lf_ids)}"
    )
