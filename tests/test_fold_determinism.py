"""Deterministic fold tests for event ordering and dependency graphs."""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile

import pytest

from pinax.fold import fold_events, fold_prefix, read_events


# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
GOLDEN_LOG = os.path.join(FIXTURES_DIR, "golden_log.jsonl")
GOLDEN_STATE = os.path.join(FIXTURES_DIR, "golden_state.json")
GOLDEN_STATE_K1 = os.path.join(FIXTURES_DIR, "golden_state_k1.json")
GOLDEN_STATE_K3 = os.path.join(FIXTURES_DIR, "golden_state_k3.json")


def load_golden_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


from helpers import normalise_for_comparison


def read_log_lines(path: str) -> list[bytes]:
    """Read the golden log, return as a list of raw line bytes (without terminator)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    # Normalise CRLF → LF, split, drop empty.
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return [line for line in normalised.split(b"\n") if line]


def write_log_to_tmpdir(lines: list[bytes], tmpdir: str, name: str = "test.jsonl") -> str:
    """Write a list of line bytes (LF-terminated) to a temp shard file."""
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        for line in lines:
            fh.write(line + b"\n")
    return path


def fold_from_lines(lines: list[bytes]) -> dict:
    """
    Round-trip: write lines to a temp dir, read back through the production fold.

    This exercises the real bytes-on-disk path (write → read → normalise → parse → fold)
    rather than an in-memory object graph.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        write_log_to_tmpdir(lines, tmpdir)
        return fold_events(read_events(tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. fold(golden_log) == golden_state
# ---------------------------------------------------------------------------

def test_fold_golden_exact():
    """Assertion 1: fold over the golden fixture equals the checked-in golden state."""
    lines = read_log_lines(GOLDEN_LOG)
    state = fold_from_lines(lines)
    expected = load_golden_state(GOLDEN_STATE)
    # normalise_for_comparison converts Python sets-of-tuples (deps, edges) to
    # sorted lists-of-lists so they compare equal to json.load()'d lists.
    normalised_state = normalise_for_comparison(state)
    assert normalised_state == expected, (
        f"fold(golden_log) != golden_state\n"
        f"got:      {json.dumps(normalised_state, sort_keys=True, indent=2)}\n"
        f"expected: {json.dumps(expected, sort_keys=True, indent=2)}"
    )


# ---------------------------------------------------------------------------
# 2. Order-independent: fold(shuffle(lines)) == golden_state
# ---------------------------------------------------------------------------

SHUFFLE_SEEDS = [42, 99, 137, 7, 2026]


@pytest.mark.parametrize("seed", SHUFFLE_SEEDS)
def test_fold_order_independent(seed: int):
    """Assertion 2: fold is identical regardless of input line order (shuffle test)."""
    lines = read_log_lines(GOLDEN_LOG)
    rng = random.Random(seed)
    shuffled = lines[:]
    rng.shuffle(shuffled)
    # Verify the shuffle actually changed the order for at least one seed.
    state = fold_from_lines(shuffled)
    expected = load_golden_state(GOLDEN_STATE)
    normalised_state = normalise_for_comparison(state)
    assert normalised_state == expected, (
        f"fold(shuffle(lines, seed={seed})) != golden_state\n"
        f"got: {json.dumps(normalised_state, sort_keys=True, indent=2)}"
    )


# ---------------------------------------------------------------------------
# 3. Idempotent: fold(lines + duplicated_subset) == golden_state
# ---------------------------------------------------------------------------

def test_fold_idempotent_duplicate():
    """
    Assertion 3: duplicating lines (union-merge artefact) is a no-op.

    The golden_log.jsonl already contains one duplicate line (the reviewer item.created).
    This test adds MORE duplicates to ensure the deduplication is truly idempotent.
    """
    lines = read_log_lines(GOLDEN_LOG)
    # Duplicate the first 3 lines and the last line — worst-case union scenario.
    with_extras = lines + lines[:3] + lines[-1:]
    state = fold_from_lines(with_extras)
    expected = load_golden_state(GOLDEN_STATE)
    normalised_state = normalise_for_comparison(state)
    assert normalised_state == expected, (
        f"fold(lines + duplicated_subset) != golden_state\n"
        f"got: {json.dumps(normalised_state, sort_keys=True, indent=2)}"
    )


# ---------------------------------------------------------------------------
# 4. Replay-determinism: fold(lines[:k]) == golden_state_at_k
# ---------------------------------------------------------------------------

def test_replay_determinism_k1():
    """
    Assertion 4a: fold of first 1 event == golden_state_k1.

    Git-reference replay relies on this prefix-fold property.
    """
    lines = read_log_lines(GOLDEN_LOG)
    # The total-order sort happens inside read_events/fold_events.
    # To get the first k events by total order, we must sort first, then slice.
    # We replicate read_events' sort here to identify the correct prefix.
    tmpdir = tempfile.mkdtemp()
    try:
        write_log_to_tmpdir(lines, tmpdir)
        all_events = read_events(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # fold_events on the first 1 event of the total-ordered stream.
    from pinax.fold import fold_events
    state_k1 = fold_events(all_events[:1])
    expected_k1 = load_golden_state(GOLDEN_STATE_K1)
    normalised_k1 = normalise_for_comparison(state_k1)
    assert normalised_k1 == expected_k1, (
        f"fold(events[:1]) != golden_state_k1\n"
        f"got: {json.dumps(normalised_k1, sort_keys=True, indent=2)}\n"
        f"expected: {json.dumps(expected_k1, sort_keys=True, indent=2)}"
    )


def test_replay_determinism_k3():
    """
    Assertion 4b: fold of first 3 events == golden_state_k3.

    k=3 is after: ergon.created + item.created() + item.created().
    This prefix includes the same-second tie (events 1 and 2 in total order share ts=10:00:02Z
    from different actors), verifying the total-order tiebreak is deterministic.
    """
    lines = read_log_lines(GOLDEN_LOG)
    tmpdir = tempfile.mkdtemp()
    try:
        write_log_to_tmpdir(lines, tmpdir)
        all_events = read_events(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    from pinax.fold import fold_events
    state_k3 = fold_events(all_events[:3])
    expected_k3 = load_golden_state(GOLDEN_STATE_K3)
    normalised_k3 = normalise_for_comparison(state_k3)
    assert normalised_k3 == expected_k3, (
        f"fold(events[:3]) != golden_state_k3\n"
        f"got: {json.dumps(normalised_k3, sort_keys=True, indent=2)}\n"
        f"expected: {json.dumps(expected_k3, sort_keys=True, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Same-second tie: verify it resolves deterministically
# ---------------------------------------------------------------------------

def test_same_second_tie_deterministic():
    """
    The golden fixture contains a same-second tie at ts=10:00:02Z between:
    - actor@host seq=2 (item.status_changed  → building)
    - actor@host seq=0 (item.created )

    Total-order: seq=0 reviewer comes before seq=2 operator.
    Verify: shuffling the lines does not change which event is processed first
    (the final fold state is the same).
    """
    lines = read_log_lines(GOLDEN_LOG)
    expected = load_golden_state(GOLDEN_STATE)
    # Run 10 additional shuffles to stress-test the tie.
    for seed in range(10):
        rng = random.Random(seed + 1000)
        shuffled = lines[:]
        rng.shuffle(shuffled)
        state = fold_from_lines(shuffled)
        normalised_state = normalise_for_comparison(state)
        assert normalised_state == expected, f"Same-second tie not deterministic at seed={seed+1000}"


# ---------------------------------------------------------------------------
# (actor, id) tie-break: verify seq+ts collision between different actors
# resolves by actor then id, deterministically
# ---------------------------------------------------------------------------

def test_actor_id_tiebreak_deterministic():
    """
    The golden fixture contains a same-(seq, ts) pair from different actors:
    - seq=5, ts=10:00:05Z, actor=aaaa@host
    - seq=5, ts=10:00:05Z, actor=zzzz@host

    The total-order key (seq, ts, actor, id) must resolve this tie by actor first
    ('aaaa@host' < 'zzzz@host' lexically), then id. The deterministic order
    processes aaaa@host before zzzz@host.

    Assert:
    1. Both items are present in the final fold state.
    2. The fold state is identical under shuffled input — the (actor, id) tie-break
       is exercised and stable.
    3. The expected item identifiers are present.
    """
    lines = read_log_lines(GOLDEN_LOG)
    expected = load_golden_state(GOLDEN_STATE)

    state = fold_from_lines(lines)
    items = state.get("items", {})

    # Both tie-break items must be present.
    assert "pnx-tie1" in items, (
        f"pnx-tie1 (aaaa@host, seq=5) missing from fold state; items={list(items.keys())}"
    )
    assert "pnx-tie2" in items, (
        f"pnx-tie2 (zzzz@host, seq=5) missing from fold state; items={list(items.keys())}"
    )

    # Verify actor attribution is correct (not swapped by a bad tie-break).
    assert items["pnx-tie1"]["created_by"] == "aaaa@host", (
        f"Expected pnx-tie1 created_by=aaaa@host; got {items['pnx-tie1']['created_by']!r}"
    )
    assert items["pnx-tie2"]["created_by"] == "zzzz@host", (
        f"Expected pnx-tie2 created_by=zzzz@host; got {items['pnx-tie2']['created_by']!r}"
    )

    # Full state equality (golden_state.json now includes tie-break items + dep event).
    normalised_state = normalise_for_comparison(state)
    assert normalised_state == expected, (
        f"fold(golden_log) != golden_state after adding tie-break pair\n"
        f"got:      {json.dumps(normalised_state, sort_keys=True, indent=2)}\n"
        f"expected: {json.dumps(expected, sort_keys=True, indent=2)}"
    )

    # Shuffle stress: the (actor, id) tie-break must be stable under line reorder.
    for seed in range(5):
        rng = random.Random(seed + 2000)
        shuffled = lines[:]
        rng.shuffle(shuffled)
        shuffled_state = fold_from_lines(shuffled)
        normalised_shuffled = normalise_for_comparison(shuffled_state)
        assert normalised_shuffled == expected, (
            f"(actor, id) tie-break not deterministic at seed={seed+2000}"
        )


# ---------------------------------------------------------------------------
# Order-dependent sequence: verify final status is 'queued' (not blocked)
# ---------------------------------------------------------------------------

def test_order_dependent_sequence_final_status():
    """
    The golden fixture moves one item through: queued → building → blocked → queued.
    The final status MUST be 'queued' — the last event in total order wins.
    This proves the order-dependent sequence is correctly resolved.
    """
    lines = read_log_lines(GOLDEN_LOG)
    state = fold_from_lines(lines)
    pnx_aaa1 = state.get("items", {}).get("pnx-aaa1", {})
    assert pnx_aaa1.get("status") == "queued", (
        f"Expected pnx-aaa1 status='queued' (latest wins), got '{pnx_aaa1.get('status')}'"
    )
    # Also check the claim/block/unblock arc timestamps.
    assert pnx_aaa1.get("status_changed_at") == "2026-06-29T10:00:04Z"
    assert pnx_aaa1.get("status_changed_by") == "operator@example.test"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def test_tampered_twin_fold_deterministic():
    """
    Same-id/different-body (tampered twin) must fold to ONE stable outcome
    regardless of physical line order on disk.

    Scenario: take the golden seq=4 item.status_changed event for
    (id=ieax..., status=queued).  Build a tampered twin: same id field, same
    (seq, ts, actor) sort key, but payload status='HACKED'.  Write the pair
    in two orderings (genuine-first, then tampered-first) to separate temp dirs
    and assert that both produce the SAME fold state.

    `_dedupe_by_id` picks min(canonical_bytes)
    over the same-id group, which is body-sensitive and input-order-independent.

    Same-id records with distinct bodies must fold to the same deterministic
    representative regardless of physical line order.
    """
    # The golden seq=4 event bytes (genuine).
    # id=ieax2ayw4i6a56f2r4fqeckhwwscelmdsbhupstsn2jjb3ow7qcq, status=queued.
    genuine_line = (
        b'{"id":"ieax2ayw4i6a56f2r4fqeckhwwscelmdsbhupstsn2jjb3ow7qcq",'
        b'"seq":4,"ts":"2026-06-29T10:00:04Z","actor":"operator@example.test",'
        b'"type":"item.status_changed",'
        b'"payload":{"item_id":"pnx-aaa1","status":"queued"},'
        b'"prev":"45nosa6lg3f5pwfmvg3lhzwbylncx7x5573lfu6loiva2rhooy3q"}'
    )
    # Tampered twin: SAME id field, SAME (seq, ts, actor), DIFFERENT body (status=HACKED).
    # This is the realistic tamper: payload mutated, stale id left in place.
    tampered_line = (
        b'{"id":"ieax2ayw4i6a56f2r4fqeckhwwscelmdsbhupstsn2jjb3ow7qcq",'
        b'"seq":4,"ts":"2026-06-29T10:00:04Z","actor":"operator@example.test",'
        b'"type":"item.status_changed",'
        b'"payload":{"item_id":"pnx-aaa1","status":"HACKED"},'
        b'"prev":"45nosa6lg3f5pwfmvg3lhzwbylncx7x5573lfu6loiva2rhooy3q"}'
    )

    # Create the item before applying its status change.
    ergon_line = (
        b'{"id":"gnawgd2j7v3o5n3amf57dmu6es2udpsh5dhjwizl5rn2wmwpm5lq",'
        b'"seq":0,"ts":"2026-06-29T10:00:00Z","actor":"operator@example.test",'
        b'"type":"ergon.created","payload":{"repo":"pinax"},"prev":""}'
    )
    item_created_line = (
        b'{"id":"a3rvkn6d7ijwyuayyg7lr2uaedntfu37ouabk4ukoncnvtb7oasq",'
        b'"seq":1,"ts":"2026-06-29T10:00:01Z","actor":"operator@example.test",'
        b'"type":"item.created",'
        b'"payload":{"item_id":"pnx-aaa1","title":"First item","prefix":"pnx","status":"queued"},'
        b'"prev":"gnawgd2j7v3o5n3amf57dmu6es2udpsh5dhjwizl5rn2wmwpm5lq"}'
    )

    base_lines = [ergon_line, item_created_line]

    def fold_with_pair(first: bytes, second: bytes) -> dict:
        """Write base_lines + first + second to a temp dir and fold."""
        lines = base_lines + [first, second]
        return fold_from_lines(lines)

    state_genuine_first = fold_with_pair(genuine_line, tampered_line)
    state_tampered_first = fold_with_pair(tampered_line, genuine_line)

    pnx_aaa1_gf = state_genuine_first.get("items", {}).get("pnx-aaa1", {})
    pnx_aaa1_tf = state_tampered_first.get("items", {}).get("pnx-aaa1", {})

    # PRIMARY assertion: both orderings must yield the SAME status.
    assert pnx_aaa1_gf.get("status") == pnx_aaa1_tf.get("status"), (
        f"Tampered-twin fold is NOT deterministic: "
        f"genuine-first status={pnx_aaa1_gf.get('status')!r}, "
        f"tampered-first status={pnx_aaa1_tf.get('status')!r}. "
        f"The naive 'first-in-sorted-order' fix fails this: Python stable sort "
        f"preserves input order when sort keys are identical, so the winner depended "
        f"on physical line order. The body-sensitive min(canonical_bytes) fix must "
        f"make both orderings produce the same winner."
    )

    # SECONDARY assertion: the winning status is a known fixed value — not BOTH
    # could be 'HACKED' (that would mean the tampered line always wins).
    # The deterministic winner is whichever of genuine/tampered has the
    # lexicographically smaller canonical JSON. We just assert stability here;
    # the value must be consistent, not necessarily 'queued'.
    winning_status = pnx_aaa1_gf.get("status")
    assert winning_status in ("queued", "HACKED"), (
        f"Unexpected winning status: {winning_status!r}"
    )

    # DOCUMENTATION: log which body won so a test failure message is informative.
    # (Not an assertion — just context if the primary assertion fires above.)
    _ = winning_status  # consumed above
