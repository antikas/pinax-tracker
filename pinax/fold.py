"""Deterministic fold over the JSONL event log.

The reader normalises line endings, parses complete JSON records, deduplicates
by event id, and orders events by `(seq, ts, actor, id)`. Type handlers derive
items, phases, typed dependency edges, registry entries, notes, priorities,
claims, and annulment records without consulting time, randomness, locale, or
hash iteration order.

Claims are reconciled from the ordered stream. Dependencies use last-write-wins
semantics under the same total order. Readiness uses `blocks` edges and next-item
selection combines phase order, explicit priority, critical-path depth, age, and
item id.

An `event.annulled` record identifies one hash-valid event by id. Annulment
suppresses that target's handler effects and integrity warning while retaining
its original bytes in the append-only log. Invalid annulment records do not
suppress any target.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from collections import OrderedDict, defaultdict
from typing import Iterator

from .event import parse_line, valid_annulment, verify_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Determinism layer
# ---------------------------------------------------------------------------

def parse_shard_bytes(raw: bytes, shard_id: str) -> Iterator[dict]:
    """
    Parse one shard's raw bytes into event dicts.  LF-normalises; tolerates a
    torn/partial trailing line.

    Shared by _read_shard (filesystem shards) and pinax.replay's git-ref shard
    reader — the byte-parsing half of the determinism layer is the
    same regardless of whether the bytes came from a file on disk or a git
    blob at a historical ref.  Do not re-implement this parsing in replay.py;
    import and reuse (SSOT — one place knows how to turn shard bytes into
    events).

    Each yielded event dict is annotated with a private '_shard' key carrying
    the caller-supplied shard_id (a filesystem path for _read_shard; a
    repo-relative git path for the replay reader).  This is used by
    _check_prev_chain to group the chain check by shard (not by actor),
    grouping chains by shard: one actor may legitimately write multiple
    shards, each beginning with `prev=''`. The '_shard' key is
    stripped before the fold state handlers receive events.
    """
    # Split on LF after normalising CRLF.  Each element is one line's bytes
    # (without the terminator).
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    lines = normalised.split(b"\n")

    for i, line_bytes in enumerate(lines):
        if not line_bytes:
            # Empty line — could be the trailing newline split or a blank; skip.
            continue
        event = parse_line(line_bytes)
        if event is None:
            if i == len(lines) - 1:
                # Torn trailing line — the common crash-mid-append case.
                logger.warning(
                    "Torn trailing line in shard %s (line %d) - ignored.", shard_id, i + 1
                )
            else:
                logger.warning(
                    "Unparseable line in shard %s (line %d) - ignored.", shard_id, i + 1
                )
            continue
        # Annotate with shard provenance for _check_prev_chain.
        event["_shard"] = shard_id
        yield event


def _read_shard(path: str) -> Iterator[dict]:
    """
    Read one JSONL shard from the filesystem, yielding parsed event dicts.

    Thin wrapper over parse_shard_bytes — see that function for the parsing
    contract (LF-normalisation, torn-line tolerance, '_shard' annotation).
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    yield from parse_shard_bytes(raw, path)


def _sort_key(event: dict):
    """
    Total-order sort key per ADR-001: (seq, ts, actor, id).

    seq is the primary key (explicit Lamport counter).
    ts is secondary (metadata tie-break).
    actor and id are tertiary/quaternary for a complete, deterministic total order.
    Plain string comparison throughout — no locale collation.
    """
    return (event["seq"], event["ts"], event["actor"], event["id"])


def _check_id_integrity(events: list[dict], annulled_ids: frozenset[str] = frozenset()) -> None:
    """
    ADR-001 tamper-evidence: verify each event's id matches its recomputed hash.

    A mismatch means the payload (or any envelope field) was mutated after the
    event was written — a tampered or corrupted event.  Emit a WARNING for each
    mismatch so that callers (fold, replay) can detect it.  The event is NOT
    dropped — the fold still applies it; the detection is the guarantee.

    `annulled_ids` scopes the suppression to specific event ids that
    have been formally tombstoned via `event.annulled` — every other event
    (including a different, not-yet-annulled tampered event) still warns
    without affecting other integrity checks. This is never a blanket
    suppression: an id absent from annulled_ids always gets its check.
    """
    for event in events:
        if event.get("id") in annulled_ids:
            continue
        if not verify_id(event):
            logger.warning(
                "Integrity violation: event id=%s does not match recomputed hash "
                "(payload or envelope tampered). seq=%s actor=%s type=%s",
                event.get("id"),
                event.get("seq"),
                event.get("actor"),
                event.get("type"),
            )


def _check_prev_chain(events: list[dict], annulled_ids: frozenset[str] = frozenset()) -> None:
    """
    ADR-001 tamper-evidence: verify the prev-chain per (shard, actor) group.

    ``annulled_ids`` scopes suppression to specific event ids that have
    been formally tombstoned via `event.annulled` — an event whose own id is in
    annulled_ids has its dangling-prev WARNING suppressed; every other event's
    prev-chain check is unaffected.  Never a blanket suppression.

    Why set-membership:
      A real git merge=union of two same-actor branches forks the per-(shard,actor)
      chain — multiple events share the same prev (both chains branched from a common
      ancestor).  A linear walk expects each event's prev to equal exactly one
      predecessor, so it cries "Broken prev-chain" on every fork.  This is the
      common case for same-actor branches sharing one shard.

    Set-membership rule:
      Within each (shard, actor) scope, collect all event ids as a set.  An event's
      prev is LEGITIMATE if it is empty (chain anchor) OR references a known id in
      that scope's id-set.  A prev that references NO known id is a dangling ref —
      evidence of deletion or tamper — and warrants a WARNING.

    What this detects and does not detect:
      - DETECTS: a deleted-middle event (its successor's prev dangles — the deleted
        id is no longer in the id-set).
      - DETECTS: a mutated-payload event (its id changes, so its successor's prev
        references the OLD id which is no longer in the set — dangling).
      - DOES NOT DETECT: anchor tamper (deleting/altering the first event — the new
        first event self-anchors with prev=''; see DESIGN.md, "Verification and tombstones" for this v1
        limitation.)
      - DOES NOT DETECT: tip tamper (deleting/altering the last event — no successor
        to dangle; documented in DESIGN.md, "Verification and tombstones").
      - DOES NOT DETECT: seq-monotonicity violations (documented in DESIGN.md, "Verification and tombstones").
      - DOES NOT DETECT: a fork (multiple events sharing the same prev — legitimate
        after a union merge; this is intentional).

    A broken link means a log segment was deleted or a prev/payload field was
    mutated.  Emit a WARNING for each dangling prev — detectable on replay.
    """
    # Group events by (shard, actor), preserving the already-established total order.
    # Tuple key: (shard_path, actor_string).
    by_chain: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for event in events:
        shard = event.get("_shard", "<unknown>")
        actor = event.get("actor", "")
        by_chain[(shard, actor)].append(event)

    for (shard, actor), chain_events in by_chain.items():
        if not chain_events:
            continue

        # Collect all known event ids in this (shard, actor) scope.
        known_ids: set[str] = {e["id"] for e in chain_events if e.get("id")}

        for event in chain_events:
            prev = event.get("prev", "")
            # Rule: warn only when prev is non-empty AND not in known_ids AND this is
            # not the scope's first event.  The first event is the chain anchor — its
            # prev (empty or a cross-scope ref to a prior session/shard) is trusted.
            # This also covers the single-event-scope case: a lone event is its own
            # anchor, so its prev is trusted regardless of value (see DESIGN.md, "Verification and tombstones").
            if prev and prev not in known_ids:
                # suppressed (it is a formally tombstoned event) — every other
                # event in this chain is still checked normally.
                if event.get("id") in annulled_ids:
                    continue
                # Only warn if this is NOT the first event in the total-order for
                # this (shard, actor) scope — the first event's prev is an anchor ref
                # that may legitimately point outside this scope.
                if event is not chain_events[0]:
                    logger.warning(
                        "Broken prev-chain in shard=%s for actor=%s at seq=%s id=%s: "
                        "prev=%r references no known id in this (shard,actor) scope "
                        "(deleted or tampered predecessor).",
                        shard,
                        actor,
                        event.get("seq"),
                        event.get("id"),
                        prev,
                    )


def _collect_annulled_ids(events: list) -> frozenset[str]:
    """
    Scan an event list for `event.annulled` events and return the set of
    target event ids they tombstone.

    SSOT for "which ids are annulled" — used both by finalise_events (to scope
    the ADR-001 tamper-evidence WARNING suppression to specific ids) and by
    fold_events (to skip applying an annulled event's own payload effects).

    Pure and order-independent: a target is annulled if ANY `event.annulled`
    event anywhere in the stream names it, regardless of where in total order
    the annulment falls relative to its target. Malformed event.annulled
    events (missing target_id) are simply not counted here — the handler
    layer (_handle_event_annulled) logs its own warning for those when it runs.
    """
    annulled: set[str] = set()
    for event in events:
        if valid_annulment(event):
            annulled.add(event["payload"]["target_id"])
    return frozenset(annulled)


def _canonical_event_bytes(event: dict) -> bytes:
    """
    Return canonical JSON bytes for an event, excluding the private '_shard' key.

    Used by _dedupe_by_id to pick a body-sensitive deterministic representative
    from a same-id duplicate group.
    """
    # Build a copy without the private annotation; use canonical JSON so the
    # comparison is byte-deterministic and PYTHONHASHSEED-independent.
    clean = {k: v for k, v in event.items() if k != "_shard"}
    return json.dumps(clean, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _dedupe_by_id(sorted_events: list[dict]) -> list[dict]:
    """
    Deduplicate a total-order-sorted event list by id.

    A naive 'keep first in sorted
    order' approach is NOT body-sensitive.  When a tampered log carries two events
    that share the same id (and therefore the same sort key (seq, ts, actor, id)),
    Python's stable sort preserves input order, so 'first in sorted order' reduces
    to 'first in input order' — non-deterministic across different read orders.

    Correct approach: for each id, collect ALL events with that id, then pick the
    representative as min(canonical_json_bytes over the group).  This is:
    - Deterministic: canonical-JSON ordering is independent of input order, dict
      insertion order, and PYTHONHASHSEED.
    - Body-sensitive: two events with the same id but different payloads produce
      different canonical bytes, so the min picks a stable winner.
    - Valid-log correct: a valid log's same-id duplicates are byte-identical
      (union-merge artefact), so min of identical bytes == that one value; the
      fold output is byte-identical to the valid duplicate case.

    The output preserves the relative total-order of the deduplicated events
    (first occurrence in sorted order determines position; body-sensitive pick
    determines which event occupies that position).
    """
    # Group by id while preserving the order of first occurrence (from sorted input).
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for event in sorted_events:
        eid = event["id"]
        if eid not in groups:
            groups[eid] = []
        groups[eid].append(event)

    result: list[dict] = []
    for eid, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            # Pick the representative with the lexicographically smallest canonical bytes.
            # For byte-identical duplicates (valid log) all candidates are equal; min()
            # is stable and returns the first element — same as before.
            representative = min(group, key=_canonical_event_bytes)
            result.append(representative)
    return result


def finalise_events(raw_events: list[dict]) -> list[dict]:
    """
    Determinism layer: sort by total-order key, dedupe by id, integrity-check.

    Shared by read_events (filesystem shards) and pinax.replay's
    read_events_at_ref (git-ref shards) — this is the determinism
    guarantee and must have exactly one implementation regardless of where
    the raw shard events were sourced from (SSOT).

    Dedupe uses a body-sensitive deterministic representative per id-group (min over canonical
    JSON bytes) so that even a tampered log with same-id/different-body lines
    folds to the same winner regardless of physical line order.  Valid-log
    behaviour is byte-identical (same-id duplicates in a valid log are
    byte-identical; min of identical bytes equals that one value).

    After sorting+deduplication, performs two integrity checks (ADR-001 tamper-evidence):
    - id-verification: each event's stored id is recomputed and compared.
    - prev-chain check (set-membership): warns when an event's prev
      is non-empty and references no known id in its (shard,actor) scope (a dangling
      prev = deleted/tampered predecessor).  A fork — multiple events chaining from
      the same known id after a git merge=union — produces ZERO false warnings.
      Anchor tamper, tip tamper, and seq-monotonicity are NOT detected; documented in
      DESIGN.md, "Verification and tombstones" as v1 limitations.
    Both checks emit WARNING log messages on violation; neither drops events
    (detection is the guarantee; the fold still applies all events).

    The '_shard' annotation added by parse_shard_bytes is stripped before
    returning so fold handlers never see it.

    Returns the ordered, deduped event list — the canonical input to fold_events.
    """
    # Sort by total-order key — this is THE determinism guarantee.
    raw_events = sorted(raw_events, key=_sort_key)

    # Derive exemptions only from structurally valid, hash-valid tombstones and
    # inspect every physical parsed line before same-id deduplication.  Otherwise
    # a malformed twin could be hidden by the representative-selection step.
    annulled_ids = _collect_annulled_ids(raw_events)
    _check_id_integrity(raw_events, annulled_ids)
    _check_prev_chain(raw_events, annulled_ids)

    # Dedupe remains the deterministic fold input after physical-line checks.
    events = _dedupe_by_id(raw_events)

    # Strip the private '_shard' annotation — fold handlers must not see it.
    for event in events:
        event.pop("_shard", None)

    return events


def read_raw_events(log_dir: str, *, include_invalid: bool = False) -> list[dict]:
    """
    Read all shards in log_dir as RAW events — unsorted, undeduped, no
    integrity checks applied. By default records without an identifier are
    omitted so callers that sort the canonical event pool retain their
    established behaviour. Verification passes ``include_invalid`` to
    inspect every successfully parsed JSON object before that filtering. This
    is the filesystem-sourced half of
    read_events(), extracted so pinax.all_branches can union this
    raw pool with git-blob-sourced raw pools (pinax.replay.read_raw_events_at_ref,
    one per unmerged branch tip) BEFORE a single finalise_events call over the
    combined set. `read_events(log_dir)` equals
    `finalise_events(read_raw_events(log_dir))`.
    """
    pattern = os.path.join(log_dir, "*.jsonl")
    shards = sorted(glob.glob(pattern))  # deterministic glob order (then re-sorted anyway)

    raw_events: list[dict] = []

    for shard_path in shards:
        for event in _read_shard(shard_path):
            eid = event.get("id")
            if eid is None:
                logger.warning("Event without id in shard %s - ignored.", shard_path)
                if include_invalid:
                    raw_events.append(event)
                continue
            raw_events.append(event)

    return raw_events


def read_events(log_dir: str) -> list[dict]:
    """
    Determinism layer: read all shards in log_dir, sort by total-order key, dedupe by id.

    Sources raw events from the filesystem (log_dir/*.jsonl) via read_raw_events,
    then hands off to finalise_events for the sort/dedupe/integrity-check steps
    shared with the git-ref replay path (pinax.replay.read_events_at_ref).

    Returns the ordered, deduped event list — the canonical input to the fold handlers.
    """
    return finalise_events(read_raw_events(log_dir))


# ---------------------------------------------------------------------------
# Per-type handlers
# ---------------------------------------------------------------------------

def _handle_ergon_created(state: dict, event: dict) -> None:
    """Record the ergon initialisation marker in fold state."""
    if "ergon" not in state:
        state["ergon"] = {}
    state["ergon"]["created_at"] = event["ts"]
    state["ergon"]["actor"] = event["actor"]


def _handle_phase_opened(state: dict, event: dict) -> None:
    """Open a named phase in fold state.

     Store `opened_seq` (the event seq) alongside `opened_at`.
    compute_next sorts phases by (opened_seq, opened_at, opened_by) — the seq
    is the total-order primary key (ADR-001: timestamps are metadata, never
    the sort key).  Same-second same-seq collisions resolve by opened_at then
    opened_by for a complete total order.
    """
    phases = state.setdefault("phases", {})
    name = event["payload"].get("phase", "default")
    phases[name] = {
        "status": "open",
        "opened_seq": event["seq"],
        "opened_at": event["ts"],
        "opened_by": event["actor"],
    }


def _handle_item_created(state: dict, event: dict) -> None:
    """Record a new item in the items dict."""
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.created event missing item_id: %s", event.get("id"))
        return
    items[item_id] = {
        "id": item_id,
        "title": payload.get("title", ""),
        "prefix": payload.get("prefix", ""),
        "status": payload.get("status", "queued"),
        "created_at": event["ts"],
        "created_by": event["actor"],
        # event_id of the creating event — for chain verification
        "event_id": event["id"],
    }


def _handle_item_status_changed(state: dict, event: dict) -> None:
    """
    Apply the new status to an item — latest-by-total-order wins.

    ``pinax status`` emits item.status_changed events for this handler.
    Claim reconciliation (item.claimed) is handled separately — see
    _reconcile_claims() which runs as a post-pass after the full event stream.
    """
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning(
            "item.status_changed event missing item_id: %s", event.get("id")
        )
        return
    if item_id not in items:
        logger.warning(
            "item.status_changed for unknown item %s - ignored.", item_id
        )
        return
    # Latest-by-total-order wins: events are already sorted by (seq, ts, actor, id),
    # so we simply overwrite — the last writer in sorted order is the latest.
    items[item_id]["status"] = payload.get("status", items[item_id]["status"])
    items[item_id]["status_changed_at"] = event["ts"]
    items[item_id]["status_changed_by"] = event["actor"]


def _handle_item_claimed(state: dict, event: dict) -> None:
    """
    Record an item.claimed event.

    Collects all claim events into state["_pending_claims"][item_id] so that
    _reconcile_claims() can resolve double-claims after the full event stream
    has been folded (order-independent, idempotent — ADR-003).

    The status field is NOT updated here; _reconcile_claims() sets
    items[item_id]["owner"] and emits claim.superseded outcomes for losers.
    """
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.claimed event missing item_id: %s", event.get("id"))
        return
    # Accumulate all claim events for this item for later reconciliation.
    pending = state.setdefault("_pending_claims", {})
    pending.setdefault(item_id, []).append(event)


def _handle_item_blocked(state: dict, event: dict) -> None:
    """
    Apply item.blocked to an item's status — sets status='blocked' and gate.

    Latest-by-total-order wins (same discipline as item.status_changed).
    """
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.blocked event missing item_id: %s", event.get("id"))
        return
    if item_id not in items:
        logger.warning("item.blocked for unknown item %s - ignored.", item_id)
        return
    items[item_id]["status"] = "blocked"
    items[item_id]["gate"] = payload.get("gate", "")
    items[item_id]["status_changed_at"] = event["ts"]
    items[item_id]["status_changed_by"] = event["actor"]


def _handle_item_completed(state: dict, event: dict) -> None:
    """
    Apply item.completed — sets status='done' and stores the briefing work-record.

    The briefing is operational provenance (the work-record for this item's cycle).
    It lives in the fold state / log and is NOT projected to the knowledge plane.

    Latest-by-total-order wins.
    """
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.completed event missing item_id: %s", event.get("id"))
        return
    if item_id not in items:
        logger.warning("item.completed for unknown item %s - ignored.", item_id)
        return
    items[item_id]["status"] = "done"
    items[item_id]["status_changed_at"] = event["ts"]
    items[item_id]["status_changed_by"] = event["actor"]
    # Briefing is stored as a work-record in the fold state.
    briefing = payload.get("briefing")
    if briefing is not None:
        items[item_id]["briefing"] = briefing


def _handle_item_parked(state: dict, event: dict) -> None:
    """
    Apply item.parked — sets status='parked' and stores the reason.

    Latest-by-total-order wins.
    """
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.parked event missing item_id: %s", event.get("id"))
        return
    if item_id not in items:
        logger.warning("item.parked for unknown item %s - ignored.", item_id)
        return
    items[item_id]["status"] = "parked"
    items[item_id]["park_reason"] = payload.get("reason", "")
    items[item_id]["status_changed_at"] = event["ts"]
    items[item_id]["status_changed_by"] = event["actor"]


def _handle_item_priority_set(state: dict, event: dict) -> None:
    """
    Apply item.priority_set — sets items[item_id]["priority"].

    Latest-by-total-order wins: events are already sorted by (seq, ts,
    actor, id) before fold_events iterates them (see finalise_events), so a
    plain overwrite on each occurrence is the last writer in sorted order —
    identical discipline to _handle_item_status_changed.

    Payload: {"item_id": <item>, "priority": <int, lower = more urgent>}.
    An item with no item.priority_set event in its fold history simply has
    no "priority" key at all — compute_next's absence check keys off that,
    not off any sentinel value.
    """
    items = state.setdefault("items", {})
    payload = event["payload"]
    item_id = payload.get("item_id")
    if item_id is None:
        logger.warning("item.priority_set event missing item_id: %s", event.get("id"))
        return
    if item_id not in items:
        logger.warning("item.priority_set for unknown item %s - ignored.", item_id)
        return
    priority = payload.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool):
        logger.warning(
            "item.priority_set for %s has non-integer priority %r - ignored.",
            item_id, priority,
        )
        return
    items[item_id]["priority"] = priority
    items[item_id]["priority_changed_at"] = event["ts"]
    items[item_id]["priority_changed_by"] = event["actor"]


def _dep_event_key(event: dict) -> tuple:
    """
    Total-order sort key for a dep event: (seq, ts, actor, id).

    Matches ADR-001's fold total-order key.  Used by _handle_dep_added /
    _handle_dep_removed to record the last-by-total-order operation for each
    (type, from_id, to_id) triple so that _resolve_dep_ops() can apply
    last-write-wins per (type, from, to) independently.
    """
    return (event.get("seq", 0), event.get("ts", ""), event.get("actor", ""), event.get("id", ""))


def _handle_dep_added(state: dict, event: dict) -> None:
    """
    Record a dep.added event.

    Payload: {"from_id": <item>, "to_id": <item>, "type": <edge_type>}
    Semantics: from_id has a typed edge to to_id of the given type.

    All five typed edges are handled as first-class. The edge store key is
    (type, from_id, to_id)
    so add/rm on one type is fully independent of another type on the same
    (from, to) pair.

    Last-write-wins by total-order key (seq, ts, actor, id): both dep.added and
    dep.removed write their total-order key into state["_dep_ops"][(type, pair)].
    After the full fold, _resolve_dep_ops() builds state["edges"] from the
    last-written action per (type, from_id, to_id).

      add → rm → add  : last op is add → triple is in edges[type]
      rm  → add       : last op is add → triple is in edges[type]
      add → rm        : last op is rm  → triple is NOT in edges[type]
      add (only)      : last op is add → triple is in edges[type]

    Because fold_events() receives events already sorted by (seq, ts, actor, id),
    the last handler called for a given triple always holds the highest total-order
    key.  This is order-independent: the result is the same regardless of log-line
    order on disk, because read_events() always sorts before folding.
    """
    payload = event["payload"]
    edge_type = payload.get("type", "blocks")
    from_id = payload.get("from_id")
    to_id = payload.get("to_id")
    if not from_id or not to_id:
        logger.warning(
            "dep.added event missing from_id or to_id: %s", event.get("id")
        )
        return
    # Key includes the edge type: (type, from_id, to_id) — fully independent per type.
    triple_key = (edge_type, from_id, to_id)
    ops: dict = state.setdefault("_dep_ops", {})
    current = ops.get(triple_key)
    key = _dep_event_key(event)
    if current is None or key > current["key"]:
        ops[triple_key] = {"action": "add", "key": key}


def _handle_note_added(state: dict, event: dict) -> None:
    """
    Record a note.added event in the fold state.

    ADR-004 / DESIGN.md: note.added carries a typed ref (pointing to a
    knowledge-plane document) + an optional caption (capped at 200 chars,
    enforced at CLI write-time — not re-validated here).

    The note is recorded in state["notes"] as a list of note records.
    Notes are recorded but do not change item status.
    """
    payload = event["payload"]
    item_id = payload.get("item_id")
    ref = payload.get("ref", "")
    caption = payload.get("caption")

    notes = state.setdefault("notes", [])
    note_record = {
        "event_id": event["id"],
        "item_id": item_id,
        "ref": ref,
        "ts": event["ts"],
        "actor": event["actor"],
    }
    if caption is not None:
        note_record["caption"] = caption
    notes.append(note_record)


def _handle_dep_removed(state: dict, event: dict) -> None:
    """
    Record a dep.removed event — cancels an earlier dep.added.

    The handler operates on the (type, from_id, to_id) triple, so a removal of
    one edge type has NO effect on any other edge type between the same pair.
    For example, removing (related, A, B) leaves (blocks, A, B) intact.

    "Earlier" is by total-order key (seq, ts, actor, id): if the dep.removed
    event has a higher total-order key than the latest dep.added for this
    triple, the removal wins.  If a later dep.added follows (re-add), the
    dep.added wins.

    See _handle_dep_added for the full last-write-wins semantics.
    """
    payload = event["payload"]
    edge_type = payload.get("type", "blocks")
    from_id = payload.get("from_id")
    to_id = payload.get("to_id")
    if not from_id or not to_id:
        logger.warning(
            "dep.removed event missing from_id or to_id: %s", event.get("id")
        )
        return
    # Key includes the edge type: (type, from_id, to_id) — fully independent per type.
    triple_key = (edge_type, from_id, to_id)
    ops: dict = state.setdefault("_dep_ops", {})
    current = ops.get(triple_key)
    key = _dep_event_key(event)
    if current is None or key > current["key"]:
        ops[triple_key] = {"action": "remove", "key": key}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _handle_event_annulled(state: dict, event: dict) -> None:
    """
    Record an event.annulled tombstone in fold state.

    Payload: {"target_id": <id of the annulled event>, "reason": <text>}

    This handler ONLY records bookkeeping (state["annulled"][target_id]) for
    audit/report purposes — it does NOT suppress the target's own warning or
    handler application; that happens earlier via _collect_annulled_ids, which
    fold_events() and finalise_events() both consult directly from the raw
    event stream (so suppression works correctly regardless of total-order
    position of this event relative to its target).

    Last-write-wins per target_id by total-order position (events arrive here
    already sorted): re-annulling the same target with a second event.annulled
    is idempotent in effect (the target stays suppressed either way) and this
    dict simply records the latest annulling event/reason/actor.
    """
    if not valid_annulment(event):
        logger.warning("invalid event.annulled tombstone: %s", event.get("id"))
        return
    payload = event["payload"]
    target_id = payload.get("target_id")
    if not target_id:
        logger.warning("event.annulled event missing target_id: %s", event.get("id"))
        return
    annulled = state.setdefault("annulled", {})
    annulled[target_id] = {
        "event_id": event["id"],
        "reason": payload.get("reason", ""),
        "actor": event["actor"],
        "ts": event["ts"],
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _registry_event_key(event: dict) -> tuple:
    """
    Total-order sort key for a registry event: (seq, ts, actor, id).

    Identical shape to _dep_event_key — same last-write-wins discipline,
    applied per repo_id instead of per (type, from_id, to_id) triple.
    """
    return (event.get("seq", 0), event.get("ts", ""), event.get("actor", ""), event.get("id", ""))


def _handle_registry_repo_added(state: dict, event: dict) -> None:
    """
    Record a registry.repo_added event.

    Payload: {"repo_id": <slug>, "path": <repo path, forward-slash normalised>}
    — plus an optional "url": <git remote URL>. At least one
    of path/url must be present.  A path-bearing entry is a LOCAL override for
    `pinax overview` discovery; a url-bearing entry is a REMOTE-manifest entry
    for `pinax overview --remote` (the manifest of remotes IS the registry —
    same event, same log, no parallel schema).  An entry may carry both.

    Last-write-wins by total-order key, staged in state["_registry_ops"] and
    resolved by _resolve_registry_ops() after the full event stream is folded
    (mirrors _handle_dep_added / _resolve_dep_ops exactly).
    """
    payload = event["payload"]
    repo_id = payload.get("repo_id")
    path = payload.get("path")
    url = payload.get("url")
    if not repo_id or (not path and not url):
        logger.warning(
            "registry.repo_added event missing repo_id or path/url: %s", event.get("id")
        )
        return
    ops: dict = state.setdefault("_registry_ops", {})
    key = _registry_event_key(event)
    current = ops.get(repo_id)
    if current is None or key > current["key"]:
        op = {
            "action": "add",
            "key": key,
            "actor": event["actor"],
            "ts": event["ts"],
        }
        if path:
            op["path"] = path
        if url:
            op["url"] = url
        ops[repo_id] = op


def _handle_registry_repo_removed(state: dict, event: dict) -> None:
    """
    Record a registry.repo_removed event — cancels an earlier registry.repo_added.

    Payload: {"repo_id": <slug>}

    Same last-write-wins semantics as _handle_dep_removed: a removal only wins
    if its total-order key is higher than the latest add for this repo_id; a
    later re-add wins over an earlier remove.
    """
    payload = event["payload"]
    repo_id = payload.get("repo_id")
    if not repo_id:
        logger.warning(
            "registry.repo_removed event missing repo_id: %s", event.get("id")
        )
        return
    ops: dict = state.setdefault("_registry_ops", {})
    key = _registry_event_key(event)
    current = ops.get(repo_id)
    if current is None or key > current["key"]:
        ops[repo_id] = {"action": "remove", "key": key}


def _resolve_registry_ops(state: dict) -> None:
    """
    Build state["registry"] from state["_registry_ops"] via last-write-wins.

    Mirrors _resolve_dep_ops: for each repo_id, the LAST (by total-order key)
    registry event decides membership.  Order-independent (read_events()
    always sorts before folding) and idempotent (re-running on the same ops
    produces the same registry dict).

    Output: state["registry"] -> {repo_id: {"path"?, "url"?, "added_by",
    "added_at"}} — "path" / "url" present only when the winning add carried
    them. A URL-only entry is a remote-manifest entry; a path-only entry is
    a local registry entry.
    Only set when at least one registry event was seen (preserves golden-state
    compatibility — a log with no registry events produces no "registry" key).
    """
    ops: dict = state.pop("_registry_ops", {})
    if not ops:
        return

    registry: dict = {}
    for repo_id in sorted(ops.keys()):
        entry = ops[repo_id]
        if entry["action"] == "add":
            resolved = {
                "added_by": entry["actor"],
                "added_at": entry["ts"],
            }
            if "path" in entry:
                resolved["path"] = entry["path"]
            if "url" in entry:
                resolved["url"] = entry["url"]
            registry[repo_id] = resolved
    state["registry"] = registry


# Map type string → handler function.
_HANDLERS = {
    "ergon.created": _handle_ergon_created,
    "phase.opened": _handle_phase_opened,
    "item.created": _handle_item_created,
    "item.status_changed": _handle_item_status_changed,
    "item.claimed": _handle_item_claimed,
    "item.blocked": _handle_item_blocked,
    "item.completed": _handle_item_completed,
    "item.parked": _handle_item_parked,
    "item.priority_set": _handle_item_priority_set,
    "dep.added": _handle_dep_added,
    "dep.removed": _handle_dep_removed,
    "note.added": _handle_note_added,
    "registry.repo_added": _handle_registry_repo_added,
    "registry.repo_removed": _handle_registry_repo_removed,
    "event.annulled": _handle_event_annulled,
}


# ---------------------------------------------------------------------------
# Claim reconciliation (fold-time, ADR-003)
# ---------------------------------------------------------------------------

def _claim_sort_key(event: dict) -> tuple:
    """
    Sort key for claim reconciliation per ADR-003: (ts, actor, id).

    ts is the primary key (wall-clock-first — ADR-003 is explicit that claim
    is wall-clock-first, NOT seq-first).  actor and id are tiebreakers.
    This is DIFFERENT from the fold total-order key (seq, ts, actor, id).
    """
    return (event["ts"], event["actor"], event["id"])


def _reconcile_claims(state: dict) -> None:
    """
    Fold-time claim reconciliation (ADR-003).

    After the full event stream has been folded, resolve any double-claims.
    For each item with multiple item.claimed events:
    - The earliest by (ts, actor, id) wins → sets items[id]["owner"].
    - Every later claim produces a claim.superseded outcome stored in
      state["claim_superseded"] (a list of dicts, each with item_id,
      superseded_event_id, superseded_actor, winner_event_id, winner_actor).
    - A report warning is added for each superseded claim.

    This is:
    - ORDER-INDEPENDENT: we sort the accumulated claim events by (ts, actor, id)
      regardless of the order they appeared in the event stream.
    - IDEMPOTENT: the input is the already-deduped event stream; re-running
      _reconcile_claims on the same state produces the same result.
    - PURE: no wall-clock, no RNG — deterministic from the event content alone.

    Items with exactly one claim: owner set, no superseded.
    Items with zero claims: no owner field set (unowned).
    """
    pending = state.pop("_pending_claims", {})
    if not pending:
        return

    items = state.setdefault("items", {})
    superseded_list = state.setdefault("claim_superseded", [])
    warnings = state.setdefault("report", {}).setdefault("warnings", [])

    for item_id, claim_events in pending.items():
        if item_id not in items:
            logger.warning(
                "item.claimed for unknown item %s - claim ignored.", item_id
            )
            continue

        # Sort by (ts, actor, id) — ADR-003 claim order.
        sorted_claims = sorted(claim_events, key=_claim_sort_key)

        winner = sorted_claims[0]
        items[item_id]["owner"] = winner["actor"]
        items[item_id]["claimed_at"] = winner["ts"]
        items[item_id]["claim_event_id"] = winner["id"]

        # Every subsequent claim is superseded.
        for loser in sorted_claims[1:]:
            superseded_entry = {
                "item_id": item_id,
                "superseded_event_id": loser["id"],
                "superseded_actor": loser["actor"],
                "winner_event_id": winner["id"],
                "winner_actor": winner["actor"],
            }
            superseded_list.append(superseded_entry)
            warning_msg = (
                f"claim.superseded: item {item_id} claimed by "
                f"{loser['actor']} (event {loser['id'][:12]}...) superseded by "
                f"earlier claim from {winner['actor']} (event {winner['id'][:12]}...) - "
                f"earliest (ts={winner['ts']},actor={winner['actor']}) wins per ADR-003"
            )
            warnings.append(warning_msg)

    # Clean up empty report if no warnings were added by other code.
    # (We leave report.warnings as an empty list if no warnings — the field is
    # expected by the double-claim test even when there are superseded claims.)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _resolve_dep_ops(state: dict) -> None:
    """
    Build state["edges"] and state["deps"] from state["_dep_ops"] via
    last-write-wins by total-order key, keyed per (type, from_id, to_id).

    `_dep_ops` is keyed by (edge_type, from_id, to_id) so that
    add/rm on one edge type is COMPLETELY INDEPENDENT of add/rm on another
    edge type for the same (from, to) pair.  For example, removing the
    (related, A, B) triple leaves the (blocks, A, B) triple untouched.

    After the full event stream is folded, _dep_ops holds — for each
    (type, from_id, to_id) triple — the total-order key and action of the LAST
    dep event that touched it.  This function computes the definitive edge sets:

      - If the last operation for a triple was "add"    → the pair is in edges[type].
      - If the last operation for a triple was "remove" → the pair is NOT in edges[type].

    Correct under all orderings:
      add(seq=3) → rm(seq=4) → add(seq=5) : last key=(5,…) action=add  → IN edges[type]
      rm(seq=3)  → add(seq=4)             : last key=(4,…) action=add  → IN edges[type]
      add(seq=3) → rm(seq=4)              : last key=(4,…) action=rm   → NOT in edges[type]
      add(seq=3)  (only)                  : last key=(3,…) action=add  → IN edges[type]

    Outputs:
      state["edges"][type] → set of (from_id, to_id) pairs for each edge type seen.
      state["deps"]        → alias for state["edges"]["blocks"] (readiness gate source).
                            Always present after this call (may be empty set).

    Order-independent: because fold_events() processes events in total-order
    (after read_events() sorts), the "last" entry in _dep_ops for each triple is
    always the total-order winner regardless of log-line shuffle on disk.

    Idempotent: re-running on the same _dep_ops produces the same edge sets.
    Pure: no wall-clock, no RNG, no PYTHONHASHSEED dependence.
    """
    ops: dict = state.pop("_dep_ops", {})
    if not ops:
        # No dep events seen — leave deps/edges absent (preserves golden-state
        # compatibility: a log with no dep events produces no edges or deps key).
        return

    edges: dict = state.setdefault("edges", {})

    for triple_key, entry in ops.items():
        edge_type, from_id, to_id = triple_key
        type_set: set = edges.setdefault(edge_type, set())
        pair = (from_id, to_id)
        if entry["action"] == "add":
            type_set.add(pair)
        else:
            type_set.discard(pair)

    # state["deps"] is the canonical alias for blocks edges (readiness gate).
    # Only set it when dep events were seen (preserves golden-state compatibility).
    # compute_ready/compute_next use state.get("deps", set()) so absence = empty set.
    state["deps"] = edges.get("blocks", set())


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _detect_dep_cycles(deps: set, items: dict) -> list[list[str]]:
    """
    Detect cycles in the blocks dependency graph (pure function).

    Uses iterative DFS with a per-path visited set (no recursion depth issue).
    Returns a list of cycle node-lists (each cycle starts+ends at the same node).

    An empty return means the graph is a DAG.

    This is called at fold time (inside compute_ready/compute_next), not stored
    in the fold state — it is a pure derivation from the current deps set.
    Does NOT raise; callers log warnings and proceed with the acyclic subset.
    """
    # Build adjacency list: from_id → set of to_ids (B blocks A means A depends on B).
    # blocks edge: from_id BLOCKS to_id → to_id cannot start until from_id is done.
    # For cycle detection we traverse the full blocks graph.
    successors: dict[str, set[str]] = defaultdict(set)
    all_nodes: set[str] = set()
    for (from_id, to_id) in deps:
        successors[from_id].add(to_id)
        all_nodes.add(from_id)
        all_nodes.add(to_id)

    cycles: list[list[str]] = []
    visited_global: set[str] = set()

    for start in sorted(all_nodes):  # sorted for determinism
        if start in visited_global:
            continue
        # Iterative DFS: stack entries are (node, path_set, path_list, child_iter)
        stack: list = [(start, {start}, [start], iter(sorted(successors.get(start, set()))))]
        visited_global.add(start)

        while stack:
            node, path_set, path_list, children = stack[-1]
            try:
                child = next(children)
                if child in path_set:
                    # Found a cycle — record it.
                    cycle_start = path_list.index(child)
                    cycles.append(path_list[cycle_start:] + [child])
                elif child not in visited_global:
                    visited_global.add(child)
                    new_path_set = path_set | {child}
                    new_path_list = path_list + [child]
                    stack.append((
                        child, new_path_set, new_path_list,
                        iter(sorted(successors.get(child, set())))
                    ))
            except StopIteration:
                stack.pop()

    return cycles


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _detect_parent_child_cycles(parent_child_edges: set) -> list[list[str]]:
    """
    Detect cycles in the parent-child graph (pure function).

    Mirrors _detect_dep_cycles (blocks detector) exactly, operating on the
    parent-child edge set instead of the blocks dep set.  The algorithm is
    identical: iterative DFS with a per-path visited set.

    Returns a list of cycle node-lists (each cycle starts+ends at the same node).
    An empty return means the parent-child graph is a DAG.

    Scope guard: warn-only at fold time.  Does NOT change readiness or touch
    any other edge type.  The blocks cycle detector (_detect_dep_cycles /
    compute_ready) remains unchanged.

    Called by fold_events() after _resolve_dep_ops().  Warnings are surfaced
    in state["report"]["warnings"] (same channel as blocks cycle warnings).
    """
    successors: dict[str, set[str]] = defaultdict(set)
    all_nodes: set[str] = set()
    for (from_id, to_id) in parent_child_edges:
        successors[from_id].add(to_id)
        all_nodes.add(from_id)
        all_nodes.add(to_id)

    cycles: list[list[str]] = []
    visited_global: set[str] = set()

    for start in sorted(all_nodes):  # sorted for determinism
        if start in visited_global:
            continue
        stack: list = [(start, {start}, [start], iter(sorted(successors.get(start, set()))))]
        visited_global.add(start)

        while stack:
            node, path_set, path_list, children = stack[-1]
            try:
                child = next(children)
                if child in path_set:
                    cycle_start = path_list.index(child)
                    cycles.append(path_list[cycle_start:] + [child])
                elif child not in visited_global:
                    visited_global.add(child)
                    new_path_set = path_set | {child}
                    new_path_list = path_list + [child]
                    stack.append((
                        child, new_path_set, new_path_list,
                        iter(sorted(successors.get(child, set())))
                    ))
            except StopIteration:
                stack.pop()

    return cycles


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _strongly_connected_cycle_nodes(deps: set) -> set[str]:
    """
    Return the FULL set of nodes that sit on ANY cycle of the `blocks` graph,
    via iterative Tarjan's SCC — not the partial set _detect_dep_cycles finds.

    _detect_dep_cycles runs a SINGLE DFS per unvisited
    node and records only the simple back-edge cycle each DFS happens to
    close over — it is a cycle-EXISTENCE check (compute_ready only needs to
    know "cyclic or not" per node, via the same detector), not a full SCC
    membership computation.  A node that sits in a true strongly-connected
    component but is not on the one back-edge path the DFS recorded is
    MISSED.  Reproducer: blocks {n5->n1, n3->n0, n3->n2, n0->n4, n0->n3,
    n4->n3, n1->n4} (+ an unrelated m->k).  The true SCC is {n0, n3, n4}
    (n0->n3->n4->n3 and n0->n4->n3->n0 are both cycles through n4), but
    _detect_dep_cycles' single DFS from n0 only records the back-edge
    n0->n3->n0 and returns cycle_nodes={n0, n3} — n4 survives into
    _compute_critical_path_depths' "DAG" as an ordinary node, its edge
    n1->n4 is kept, and n5's depth is over-counted (2 instead of the
    correct 1), which flips compute_next's dispatch choice.

    This function computes the TRUE cyclic-node set via iterative Tarjan's
    algorithm (no recursion — stdlib only, deterministic): every node whose
    strongly-connected component has size > 1, plus any node with a direct
    self-loop (from_id == to_id), is a cyclic node.  Used ONLY by
    _compute_critical_path_depths to decide which edges to exclude from the
    depth-walk's DAG; compute_ready()/_detect_dep_cycles are UNCHANGED (same
    behaviour, warnings, and ready set remain unchanged — this is a
    narrowly-scoped correction to the depth walk's cycle exclusion,
    not a readiness-semantics change).

    Iterative Tarjan's (Nuutila/Desmet-style explicit stack, avoids Python's
    recursion limit on large/adversarial graphs): each stack frame tracks a
    node, its child iterator, and its low-link value; low-links propagate to
    the parent frame on pop, matching the classic recursive algorithm state
    for state.  Deterministic: nodes and their successors are iterated in
    sorted() order throughout, so index/low-link assignment (and hence which
    nodes group into which SCC) is bit-for-bit reproducible.

    Pure: no wall-clock, no RNG, no PYTHONHASHSEED, no dict/set-order
    dependence — all iteration order is explicit sorted().
    """
    successors: dict[str, set[str]] = defaultdict(set)
    all_nodes: set[str] = set()
    self_loop_nodes: set[str] = set()
    for (from_id, to_id) in deps:
        successors[from_id].add(to_id)
        all_nodes.add(from_id)
        all_nodes.add(to_id)
        if from_id == to_id:
            self_loop_nodes.add(from_id)

    index_counter = [0]
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    tarjan_stack: list[str] = []
    sccs: list[list[str]] = []

    for root in sorted(all_nodes):
        if root in indices:
            continue

        # Explicit work-stack: each frame is [node, sorted_child_iter].
        work: list[list] = [[root, iter(sorted(successors.get(root, set())))]]
        indices[root] = index_counter[0]
        lowlinks[root] = index_counter[0]
        index_counter[0] += 1
        tarjan_stack.append(root)
        on_stack[root] = True

        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in indices:
                    indices[child] = index_counter[0]
                    lowlinks[child] = index_counter[0]
                    index_counter[0] += 1
                    tarjan_stack.append(child)
                    on_stack[child] = True
                    work.append([child, iter(sorted(successors.get(child, set())))])
                    advanced = True
                    break
                elif on_stack.get(child, False):
                    lowlinks[node] = min(lowlinks[node], indices[child])
            if advanced:
                continue

            # No more children — finalise this node, propagate low-link up.
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])

            if lowlinks[node] == indices[node]:
                # Root of an SCC — pop it off the Tarjan stack.
                scc: list[str] = []
                while True:
                    member = tarjan_stack.pop()
                    on_stack[member] = False
                    scc.append(member)
                    if member == node:
                        break
                sccs.append(scc)

    cycle_nodes: set[str] = set(self_loop_nodes)
    for scc in sccs:
        if len(scc) > 1:
            cycle_nodes.update(scc)

    return cycle_nodes


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

# Statuses that make an item ineligible for the ready set.
_INELIGIBLE_STATUSES = frozenset({"blocked", "parked", "building", "done",
                                   "blind-verify", "adjudicate"})

# Statuses that qualify an item as a candidate for ready.
_CANDIDATE_STATUSES = frozenset({"queued", "ready"})


def compute_ready(state: dict) -> list[str]:
    """
    Compute the deterministic set of ready item IDs.

    An item is ready iff:
    1. Its status is 'queued' or 'ready'.
    2. It is NOT in any _INELIGIBLE_STATUSES (redundant with #1 but explicit).
    3. All items that BLOCK it (i.e. from_id blocks it) are status='done'.
       If from_id is not known to the fold, it is treated as NOT done (conservative).
    4. The dep graph has no cycles that involve this item (cycles are warned and
       the item is excluded from the ready set).

    Returns a SORTED list of item IDs (sorted for determinism — no set-iteration
    order dependence).  The sort is by id string (lexicographic); callers that
    need a different order apply their own sort (e.g. compute_next).

    Side effect: if cycles are detected, a WARNING is emitted via the logger and
    a "deps_cycle_warning" list is added to state["report"]["warnings"].  The
    function NEVER hangs — cycle detection is finite (iterative DFS).

    Pure: no wall-clock, no RNG, no PYTHONHASHSEED dependence.
    """
    items = state.get("items", {})
    deps: set = state.get("deps", set())

    # Detect cycles — warn but do not hang.
    cycles = _detect_dep_cycles(deps, items)
    cycle_nodes: set[str] = set()
    if cycles:
        warnings = state.setdefault("report", {}).setdefault("warnings", [])
        for cycle in cycles:
            cycle_str = " -> ".join(cycle)
            msg = f"dep cycle detected: {cycle_str}"
            logger.warning(msg)
            warnings.append(msg)
            cycle_nodes.update(cycle)

    # Build a reverse map: to_id → set of from_ids (items that block to_id).
    # "from_id blocks to_id" means to_id is blocked by from_id.
    blockers_of: dict[str, set[str]] = defaultdict(set)
    for (from_id, to_id) in deps:
        blockers_of[to_id].add(from_id)

    ready_ids: list[str] = []
    for item_id, item in items.items():
        status = item.get("status", "queued")
        if status not in _CANDIDATE_STATUSES:
            continue
        if item_id in cycle_nodes:
            # Exclude items involved in a dep cycle.
            continue
        # Check all blockers are done.
        blockers = blockers_of.get(item_id, set())
        all_done = all(
            items.get(b, {}).get("status") == "done"
            for b in blockers
        )
        if all_done:
            ready_ids.append(item_id)

    return sorted(ready_ids)


def _compute_critical_path_depths(state: dict) -> dict[str, int]:
    """
    Compute the critical-path depth of every item, over `blocks` edges ONLY.

    The critical-path depth of item X is the
    edge-count of the longest chain of `blocks` edges rooted at X, walking
    forward through "X blocks Y" edges, counting only NOT-done successors
    (a `done` item terminates the chain — it is not remaining work).  An
    item that blocks nothing not-done has depth 0.

    Only the `blocks` edge type feeds this computation — parent-child /
    discovered-from / related / supersedes are graph metadata read from
    state["edges"] but never consulted here.  Reads state["deps"] (the
    single blocks-edge alias), the same source
    compute_ready() uses — no second edge notion is introduced.

    Cycle-safe: longest-path-under-
    a-per-path-cycle-guard is NOT memoisable across DFS roots in general — a
    node reached via a non-cyclic path from one root can also be reached via
    a path that runs through a cyclic region from another root, and
    memoising the FIRST computed (possibly path-guard-truncated) depth would
    under-count the second.  Rather than cache a path-dependent value, this
    function excludes every edge touching a cyclic node from the graph the
    depth walk runs over, so what remains is a TRUE DAG over which a
    per-node memoised longest-path is sound (a DAG node's longest path does
    not depend on which root reached it).

    A cyclic node here is
    computed via _strongly_connected_cycle_nodes (full iterative Tarjan's
    SCC) — NOT via _detect_dep_cycles.  _detect_dep_cycles is a single-DFS
    cycle-EXISTENCE check (sufficient for compute_ready(), which only needs
    "is this node cyclic, yes/no") and its returned node-list is only the
    ONE back-edge path each DFS happened to close over — on a graph where a
    true strongly-connected component is larger than that one back-edge
    (e.g. an extra chord edge folds a 4th node into what looks like a
    2-node cycle), _detect_dep_cycles under-reports cycle membership and a
    node that is genuinely on a cycle survives into this function's "DAG" as
    an ordinary node, silently corrupting depth counts and, downstream,
    compute_next's dispatch choice.  _strongly_connected_cycle_nodes closes
    that gap: every node in ANY strongly-connected component (size > 1, or a
    self-loop) is excluded, matching the graph-theoretic definition of
    "cyclic" exactly, so what remains really is a DAG (not "believed to be
    one because the existence-check found nothing more").

    Nodes excluded this way already cannot appear in the ready set
    (compute_ready's cycle exclusion via _detect_dep_cycles already excludes
    every node it reports;
    _strongly_connected_cycle_nodes here is a strict superset for nodes
    on a cycle _detect_dep_cycles missed, so this function only ever
    excludes MORE nodes than compute_ready does, never fewer — no ready
    item's depth can be corrupted by an edge into a truly-cyclic node it
    wrongly kept).  A cyclic node's own (unreachable-from-ready) depth is
    approximated as 0, which is never consulted by compute_next.  This
    function does not raise and does not hang on any input graph, cyclic or
    not (Tarjan's is itself an iterative, finite, linear-time algorithm).

    Pure: no wall-clock, no RNG, no PYTHONHASHSEED, no dict/set-order
    dependence (successors are consulted via sorted() wherever iteration
    order could matter to output, though depth itself is an integer sum
    that is order-independent by construction).

    Returns a dict item_id -> depth for every node that appears in the
    blocks edge set (as a from_id or to_id).  Items with no blocks edges
    at all are simply absent — callers must default missing ids to 0.
    """
    deps: set = state.get("deps", set())
    items = state.get("items", {})

    # remaining graph is a true DAG, so per-node memoisation is sound (see
    # cycle_nodes), NOT the partial _detect_dep_cycles existence-check set —
    # see docstring for why the partial set is unsound here.  compute_ready()
    # itself still uses _detect_dep_cycles; this depth walk uses the full SCC.
    cycle_nodes: set[str] = _strongly_connected_cycle_nodes(deps)

    successors: dict[str, set[str]] = defaultdict(set)
    all_nodes: set[str] = set()
    for (from_id, to_id) in deps:
        if from_id in cycle_nodes or to_id in cycle_nodes:
            # Drop any edge touching a cyclic node — acyclic-subset walk only.
            continue
        successors[from_id].add(to_id)
        all_nodes.add(from_id)
        all_nodes.add(to_id)

    depth_memo: dict[str, int] = {}

    def _depth_of(start: str) -> int:
        if start in depth_memo:
            return depth_memo[start]

        # Iterative post-order DFS: stack entries are
        # (node, path_set, child_iter, best_depth_so_far).  `successors` was
        # The walk graph is a DAG, so memoisation across roots is sound. The
        # path guard also keeps the traversal finite for any caller input.
        stack: list = [(start, {start}, iter(sorted(successors.get(start, set()))), 0)]

        while stack:
            node, path_set, children, best = stack[-1]
            advanced = False
            for child in children:
                if child in path_set:
                    # Defence-in-depth only (see above) — should be
                    # unreachable now that successors excludes cycle nodes.
                    continue
                item = items.get(child, {})
                if item.get("status") == "done":
                    # A done item terminates the chain — not remaining work.
                    continue
                if child in depth_memo:
                    best = max(best, 1 + depth_memo[child])
                    continue
                # Descend into child — push a new frame and re-enter the loop.
                stack[-1] = (node, path_set, children, best)
                stack.append((
                    child, path_set | {child},
                    iter(sorted(successors.get(child, set()))),
                    0,
                ))
                advanced = True
                break
            if advanced:
                continue
            # No more unresolved children — finalise this node's depth.
            stack.pop()
            depth_memo[node] = best
            if stack:
                # Fold this node's result into the parent frame's running best.
                parent_node, parent_path, parent_children, parent_best = stack[-1]
                stack[-1] = (parent_node, parent_path, parent_children,
                             max(parent_best, 1 + best))

        return depth_memo[start]

    for node in sorted(all_nodes):
        if node not in depth_memo:
            _depth_of(node)

    return depth_memo


def compute_next(state: dict) -> str | None:
    """Return the highest-ranked ready item, or None when none is ready.

    Items are ordered by phase opening sequence, explicit item priority,
    negated `blocks`-path depth, creation metadata, and id. Readiness itself
    is determined separately by `compute_ready` and uses only `blocks` edges.
    """
    ready_ids = compute_ready(state)
    if not ready_ids:
        return None

    items = state.get("items", {})

    # Derive phase ordering from state["phases"] sorted by
    # (opened_seq, opened_at, opened_by) — seq is the total-order primary key.
    # Missing sequence values use zero for a deterministic fallback.
    phases = state.get("phases", {})
    sorted_phase_names = sorted(
        phases.keys(),
        key=lambda name: (
            phases[name].get("opened_seq", 0),
            phases[name].get("opened_at", ""),
            phases[name].get("opened_by", ""),
        ),
    )
    phase_idx_map: dict[str, int] = {
        name: idx for idx, name in enumerate(sorted_phase_names)
    }
    _no_phase_idx = len(sorted_phase_names)  # items with no matching phase → last

    # Depths are memoised within _compute_critical_path_depths.
    depths = _compute_critical_path_depths(state)

    def _sort_key(item_id: str) -> tuple:
        item = items.get(item_id, {})
        # Phase lookup: use item's 'prefix' field as the phase affinity.
        # An item created with prefix='phase-1' belongs to the 'phase-1' phase.
        item_prefix = item.get("prefix", "")
        phase_idx = phase_idx_map.get(item_prefix, _no_phase_idx)
        # priority_tier 0 (has an explicit priority) always beats tier 1
        # (no priority event ever seen for this item) regardless of depth.
        # priority_rank only matters within tier 0 (lower = more urgent);
        # it is a constant 0 for every tier-1 item, so it cannot perturb
        # their relative order when no priority has been set at all.
        raw_priority = item.get("priority")
        if raw_priority is None:
            priority_tier, priority_rank = 1, 0
        else:
            priority_tier, priority_rank = 0, raw_priority
        # Critical-path depth: greatest depth first within a phase, hence negated
        # for use with min().  Missing from the depths map (no blocks edges at
        # all touching this item) defaults to 0.
        neg_depth = -depths.get(item_id, 0)
        # Age proxy: (created_at, event_id) — ISO-8601 and base32 sort correctly as strings.
        created_at = item.get("created_at", "")
        event_id = item.get("event_id", "")
        return (
            phase_idx, priority_tier, priority_rank, neg_depth,
            created_at, event_id, item_id,
        )

    return min(ready_ids, key=_sort_key)


# ---------------------------------------------------------------------------
# Public fold API
# ---------------------------------------------------------------------------

def fold_events(events: list[dict]) -> dict:
    """
    Fold an ordered, deduped event list into state.

    Accepts the output of read_events().  Applies per-type handlers in
    total-order sequence.  Unknown event types are recorded in state["unknown_events"]
    but do not mutate any other state key.

    After the handler pass, runs _reconcile_claims() as a pure
    post-pass to resolve any double-claims (ADR-003).

    Before claim reconciliation, runs
    _resolve_dep_ops() to build state["deps"] via last-write-wins by total-order
    key.  Private keys (_dep_ops, _pending_claims) are consumed by their
    respective post-passes and are not present in the returned state.
    compute_ready() and compute_next() derive all ordering from the public fold
    state (state["phases"], state["items"], state["deps"]) — no private keys.

    Events whose id is named by a valid `event.annulled` tombstone have
    their own type handler SKIPPED entirely (their payload effects are never
    applied — e.g. an annulled item.completed never sets status=done and never
    logs "unknown item").  The event.annulled event itself is exempt from this
    skip (an annulment always runs its own handler, _handle_event_annulled, so
    the tombstone is itself recorded).  This is a pure derivation over the
    already-sorted, already-deduped stream (_collect_annulled_ids) — it does
    not matter where the annulling event sits relative to its target.
    """
    state: dict = {}
    annulled_ids = _collect_annulled_ids(events)

    for event in events:
        etype = event.get("type", "")
        if etype != "event.annulled" and event.get("id") in annulled_ids:
            # Tombstoned — suppress this event's payload effects silently.
            # Raw bytes stay in the shard untouched; only fold-time application
            # of THIS specific event's handler is skipped.
            continue
        handler = _HANDLERS.get(etype)
        if handler:
            handler(state, event)
        else:
            # Forward-compatible: unknown types are accepted without error.
            unknown = state.setdefault("unknown_events", [])
            unknown.append(event["id"])

    _resolve_dep_ops(state)

    _resolve_registry_ops(state)

    # readiness or touch the blocks cycle detector / compute_ready path).
    pc_edges = state.get("edges", {}).get("parent-child", set())
    if pc_edges:
        pc_cycles = _detect_parent_child_cycles(pc_edges)
        if pc_cycles:
            warnings = state.setdefault("report", {}).setdefault("warnings", [])
            for cycle in pc_cycles:
                cycle_str = " -> ".join(cycle)
                msg = f"parent-child cycle detected: {cycle_str}"
                logger.warning(msg)
                warnings.append(msg)

    _reconcile_claims(state)

    return state


def fold(log_dir: str) -> dict:
    """
    End-to-end fold: read all shards, dedupe, sort, fold.

    This is the single public entry point for computing current state from a
    log directory.  It composes the determinism layer with the handler layer.
    """
    events = read_events(log_dir)
    return fold_events(events)


def fold_prefix(log_dir: str, n: int) -> dict:
    """
    Fold the first n events (by total order) from log_dir.

    Used by tests to prove the replay-determinism property: the fold of a
    prefix of events equals the state at that point in the log.
    Replay uses this same property.
    """
    events = read_events(log_dir)
    return fold_events(events[:n])


def state_to_json_safe(state: object) -> object:
    """
    Convert fold state to a JSON-comparable/serialisable form.

    fold_events returns Python sets of tuples for edge collections
    (state["deps"], state["edges"][type]):
        {('', ''), ...}
    JSON has no set type — these become sorted lists of 2-element lists:
        [[from_id, to_id], ...]

    This is THE single production conversion (SSOT) between fold state and a
    JSON-safe structure.  Used by:
    - pinax replay --at <ref> --json to emit fold state over the wire.
    - tests/helpers.py normalise_for_comparison, which delegates here so the
      golden-fixture tests exercise the identical conversion the CLI ships.

    All other value types (str, int, float, bool, None, dict, list) pass
    through unchanged (dicts/lists are walked recursively for nested sets).
    """
    if isinstance(state, (set, frozenset)):
        return sorted(
            [
                list(x) if isinstance(x, tuple) else state_to_json_safe(x)
                for x in state
            ]
        )
    if isinstance(state, dict):
        return {k: state_to_json_safe(v) for k, v in state.items()}
    if isinstance(state, list):
        return [state_to_json_safe(x) for x in state]
    if isinstance(state, tuple):
        return [state_to_json_safe(x) for x in state]
    return state
