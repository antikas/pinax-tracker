"""Concurrent append tests using isolated event logs."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import tempfile

import pytest

from pinax.event import mint_event


# ---------------------------------------------------------------------------
# Worker function (must be module-level for multiprocessing pickling on Windows)
# ---------------------------------------------------------------------------

def _worker_variable_length(args: tuple[str, str, int, list[int]]) -> list[str]:
    """
    Variable-line-length worker: append events whose payload sizes vary by a
    caller-supplied list of lengths (e.g. [1, 100, 4000, 50, ...]).

    This stresses the byte-range lock: the msvcrt.locking range is computed from
    len(line), so different writers hold different-sized lock ranges.  If the lock
    range is wrong (too small or misaligned) a partial-overlap write would corrupt
    or lose a line.  The test asserts zero loss and zero corruption.
    """
    log_dir, actor, start_seq, lengths = args
    from pinax.append import _append_locked_windows, _append_locked_posix
    from pinax.event import mint_event, serialise

    ids = []
    prev = ""
    for i, payload_len in enumerate(lengths):
        seq = start_seq + i
        ts = f"2026-06-29T10:00:{seq % 60:02d}Z"
        # Build a payload whose title is exactly payload_len chars of 'x'.
        title = "x" * payload_len
        event = mint_event(
            seq=seq,
            ts=ts,
            actor=actor,
            etype="item.created",
            payload={"item_id": f"{actor}-{seq}", "title": title, "prefix": "pnx",
                     "status": "queued"},
            prev=prev,
        )

        shard_path = os.path.join(log_dir, "shared_varlen.jsonl")
        line_str = serialise(event)
        line_bytes = line_str.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8") + b"\n"

        if os.name == "nt":
            _append_locked_windows(shard_path, line_bytes)
        else:
            _append_locked_posix(shard_path, line_bytes)

        prev = event["id"]
        ids.append(event["id"])

    return ids


def _worker(args: tuple[str, str, int, int]) -> list[str]:
    """
    Worker: append events_per_proc events to the shared shard and return the ids.

    Each worker uses a unique actor so events have distinct ids (but the SHARD
    is shared — all workers write to the same file, which is the conflict point).
    """
    log_dir, actor, start_seq, count = args
    from pinax.append import append_event

    ids = []
    prev = ""
    for i in range(count):
        seq = start_seq + i
        ts = f"2026-06-29T10:00:{seq:02d}Z" if seq < 60 else f"2026-06-29T10:01:{seq-60:02d}Z"
        event = mint_event(
            seq=seq,
            ts=ts,
            actor=actor,
            etype="item.created",
            payload={"item_id": f"{actor}-{seq}", "title": f"item {seq}", "prefix": "pnx",
                     "status": "queued"},
            prev=prev,
        )
        # Override the shard: all workers write to 'shared.jsonl' regardless of actor.
        # We do this by passing a custom actor mapped to the same shard filename.
        # Hack: rename the actor to a fixed handle so _shard_name_for_actor yields shared.jsonl
        from pinax.append import _shard_name_for_actor, _append_locked_windows, _append_locked_posix
        from pinax.event import serialise

        shard_path = os.path.join(log_dir, "shared.jsonl")
        line_str = serialise(event)
        line_bytes = line_str.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8") + b"\n"

        if os.name == "nt":
            _append_locked_windows(shard_path, line_bytes)
        else:
            _append_locked_posix(shard_path, line_bytes)

        prev = event["id"]
        ids.append(event["id"])

    return ids


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "fork"
    and sys.platform == "win32",
    reason="fork start method not available on Windows (uses spawn)",
)
def test_concurrent_same_shard_no_lost_events():
    """
    >= 4 real OS processes each append N events to one shared shard.
    After all finish: total lines == procs * N, every line parses, every id present.

    The test verifies that all concurrent appends are retained. POSIX uses
    append mode and flock; Windows acquires the file lock before seeking.
    """
    NUM_PROCS = 4
    EVENTS_PER_PROC = 30  # 4 * 30 = 120 total events — large enough to expose the race

    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        # Pre-create the shared shard file to avoid a creation race.
        shard_path = os.path.join(log_dir, "shared.jsonl")
        open(shard_path, "ab").close()

        # Build worker arguments: each worker has a unique actor, sequential seqs.
        worker_args = [
            (log_dir, f"worker-{p}@host", p * EVENTS_PER_PROC, EVENTS_PER_PROC)
            for p in range(NUM_PROCS)
        ]

        # Run all workers concurrently in separate processes.
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=NUM_PROCS) as pool:
            results = pool.map(_worker, worker_args)

        # Collect all expected ids.
        expected_ids: set[str] = set()
        for worker_ids in results:
            expected_ids.update(worker_ids)

        total_expected = NUM_PROCS * EVENTS_PER_PROC
        assert len(expected_ids) == total_expected, (
            f"Expected {total_expected} unique ids from workers, "
            f"got {len(expected_ids)} — duplicate events from workers?"
        )

        # Read the shard back and verify.
        with open(shard_path, "rb") as fh:
            raw = fh.read()

        # LF-normalise and split.
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = [line for line in normalised.split(b"\n") if line]

        total_lines = len(lines)
        assert total_lines == total_expected, (
            f"Expected {total_expected} lines on disk, got {total_lines}. "
            f"Lost {total_expected - total_lines} events. "
            f"The lock-before-seek invariant failed: "
            f"_append_locked_windows captured EOF before acquiring the lock, "
            f"so concurrent writers overwrote each other. "
            "The EOF read must occur while the file lock is held."
        )

        # Every line must be valid JSON.
        parsed_ids: set[str] = set()
        for i, line in enumerate(lines):
            try:
                event = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                pytest.fail(
                    f"Line {i+1} is corrupt/interleaved (not valid JSON): {exc!r}\n"
                    f"raw: {line!r}"
                )
            eid = event.get("id")
            assert eid is not None, f"Line {i+1} has no 'id' field: {event}"
            parsed_ids.add(eid)

        # Every expected id must be present.
        missing = expected_ids - parsed_ids
        assert not missing, (
            f"{len(missing)} event(s) lost from the shard "
            f"Lock-before-seek failure: {sorted(missing)[:5]}"
        )

        extra = parsed_ids - expected_ids
        assert not extra, (
            f"{len(extra)} unexpected event id(s) found on disk: {sorted(extra)[:5]}"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.skipif(
    multiprocessing.get_start_method(allow_none=True) == "fork"
    and sys.platform == "win32",
    reason="fork start method not available on Windows (uses spawn)",
)
def test_concurrent_variable_line_length_no_lost_events():
    """
    Variable-line-length variant of the concurrency zero-loss test.

    Each worker appends events whose serialised lines vary widely in length (1–4000 char
    title payloads → lines range from ~150 to ~4200 bytes).  This stresses the byte-range
    lock: the msvcrt.locking range is len(line), so different writers hold different-sized
    lock ranges.  A partial-overlap write (wrong range size or misaligned seek) would corrupt
    or lose a line.

    Fixed-size payloads produce identical lock ranges. Variable payload lengths
    exercise range-size sensitivity as well.

    Asserts: zero lost events, zero corrupt lines — same guarantees as the fixed-length test.
    """
    NUM_PROCS = 4
    # Vary lengths across workers: each worker appends 8 events with different payload sizes.
    # Lengths chosen to span small, medium, and large line sizes.
    PAYLOAD_LENGTHS_PER_WORKER = [
        [1, 100, 4000, 50, 2000, 10, 3999, 500],   # worker 0: mix
        [4000, 1, 500, 3000, 100, 4000, 1, 200],   # worker 1: heavy then light
        [250, 250, 250, 250, 250, 250, 250, 250],   # worker 2: uniform medium
        [1, 2, 4, 8, 16, 32, 64, 128],              # worker 3: doubling
    ]

    tmpdir = tempfile.mkdtemp()
    try:
        log_dir = os.path.join(tmpdir, "log")
        os.makedirs(log_dir)

        # Pre-create the shared shard file to avoid a creation race.
        shard_path = os.path.join(log_dir, "shared_varlen.jsonl")
        open(shard_path, "ab").close()

        # Build worker arguments: each worker has a unique actor and its own length list.
        # seq offsets are spaced so no two workers share a (seq, actor) combination.
        EVENTS_PER_PROC = len(PAYLOAD_LENGTHS_PER_WORKER[0])
        worker_args = [
            (log_dir, f"varworker-{p}@host", p * EVENTS_PER_PROC, PAYLOAD_LENGTHS_PER_WORKER[p])
            for p in range(NUM_PROCS)
        ]

        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(processes=NUM_PROCS) as pool:
            results = pool.map(_worker_variable_length, worker_args)

        expected_ids: set[str] = set()
        for worker_ids in results:
            expected_ids.update(worker_ids)

        total_expected = NUM_PROCS * EVENTS_PER_PROC
        assert len(expected_ids) == total_expected, (
            f"Expected {total_expected} unique ids from variable-length workers, "
            f"got {len(expected_ids)} — duplicate events?"
        )

        with open(shard_path, "rb") as fh:
            raw = fh.read()

        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = [line for line in normalised.split(b"\n") if line]

        total_lines = len(lines)
        assert total_lines == total_expected, (
            f"Expected {total_expected} lines on disk (variable-length), "
            f"got {total_lines}. Lost {total_expected - total_lines} events. "
            f"Partial-overlap write hazard: the byte-range lock size varies by "
            f"line length; a misaligned range allows a concurrent writer to "
            f"overwrite part of another writer's line."
        )

        parsed_ids: set[str] = set()
        for i, line in enumerate(lines):
            try:
                event = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                pytest.fail(
                    f"Variable-length line {i+1} is corrupt/interleaved: {exc!r}\n"
                    f"raw (first 200 bytes): {line[:200]!r}"
                )
            eid = event.get("id")
            assert eid is not None, f"Variable-length line {i+1} has no 'id' field: {event}"
            parsed_ids.add(eid)

        missing = expected_ids - parsed_ids
        assert not missing, (
            f"{len(missing)} variable-length event(s) lost: {sorted(missing)[:5]}"
        )

        extra = parsed_ids - expected_ids
        assert not extra, (
            f"{len(extra)} unexpected variable-length event id(s) found: {sorted(extra)[:5]}"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
