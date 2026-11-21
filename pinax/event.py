"""
pinax.event — event envelope: mint, hash, serialise, parse.

ADR-001 compliance:
- version 2 (envelope field "v": 2): id = blake2b over canonical JSON of
  (seq, ts, actor, type, payload, prev, v)
- version 1 (no "v" field): id = blake2b over canonical JSON of
  (seq, ts, actor, type, payload)
- canonical JSON: sort_keys=True, ensure_ascii=True, separators=(',',':')
- hashes always on LF-normalised, terminator-excluded bytes
- prev = id of prior event in this actor's shard (sentinel '' for first)
- NO builtin hash(), NO wall-clock, NO PYTHONHASHSEED dependence
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

# pinax.owner_gates owns the set of valid processes. It imports nothing from
# pinax, so this import cannot form a cycle.
from .owner_gates import PROCESSES


_DIGEST_BITS = 256  # blake2b truncated to 256 bits = 32 bytes = 52 chars base32


def _canonical_json(obj: Any) -> bytes:
    r"""Stable, deterministic JSON bytes -- sort_keys, no trailing space, ASCII-safe.

    Encoded as UTF-8 for symmetry with parse_line's utf-8 decode.  Functionally
    identical to ascii under ensure_ascii=True (all non-ASCII codepoints are
    \\uXXXX-escaped by json.dumps), but removes the latent foot-gun where a caller
    might compare _canonical_json output against utf-8-decoded bytes.
    """
    text = json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    # Always return LF-normalised bytes (no line terminators in a single object,
    # but apply the rule uniformly so callers never need to think about it).
    return text.encode("utf-8")


def _b32(digest: bytes) -> str:
    """base32-encode digest bytes, strip padding, lowercase."""
    import base64
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()


EVENT_VERSION = 2


def _digest_id(obj: dict) -> str:
    digest = hashlib.blake2b(_canonical_json(obj), digest_size=_DIGEST_BITS // 8).digest()
    return _b32(digest)


def event_id(seq: int, ts: str, actor: str, etype: str, payload: dict) -> str:
    """Compute the version 1 id: the hash leaves prev out (ADR-001)."""
    return _digest_id({"seq": seq, "ts": ts, "actor": actor, "type": etype, "payload": payload})


def event_id_v2(seq: int, ts: str, actor: str, etype: str, payload: dict, prev: str) -> str:
    """Compute the version 2 id: the hash covers prev and the version (ADR-001)."""
    return _digest_id({
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "type": etype,
        "payload": payload,
        "prev": prev,
        "v": EVENT_VERSION,
    })


def mint_event(
    seq: int,
    ts: str,
    actor: str,
    etype: str,
    payload: dict,
    prev: str = "",
) -> dict:
    """
    Build a complete version 2 event envelope.

    All fields are deterministic from inputs — no wall-clock, no RNG.
    prev = '' for the first event in an actor shard (explicit sentinel).
    """
    eid = event_id_v2(seq, ts, actor, etype, payload, prev)
    return {
        "id": eid,
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "type": etype,
        "payload": payload,
        "prev": prev,
        "v": EVENT_VERSION,
    }


def serialise(event: dict) -> str:
    """
    Serialise event to a canonical JSON string (no trailing newline).

    The line stored in the JSONL file is serialise(event) + '\\n'.
    Field order: id, seq, ts, actor, type, payload, prev, then v when present;
    the fold parses the JSON, not the field order. A version 1 event (no v)
    keeps its original bytes.
    """
    # Emit in a fixed, human-readable field order by building an ordered dict.
    ordered = {
        "id": event["id"],
        "seq": event["seq"],
        "ts": event["ts"],
        "actor": event["actor"],
        "type": event["type"],
        "payload": event["payload"],
        "prev": event["prev"],
    }
    if "v" in event:
        ordered["v"] = event["v"]
    return json.dumps(ordered, sort_keys=False, ensure_ascii=True, separators=(",", ":"))


def parse_line(raw_bytes: bytes) -> dict | None:
    """
    Parse one LF-normalised, terminator-excluded line of bytes into an event dict.

    Returns None if the line is empty or unparseable (torn trailing line tolerance).
    Does NOT validate the id — callers that need verification call verify_id().
    """
    # Normalise CRLF → LF, strip the line terminator.
    line = raw_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n").rstrip(b"\n")
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def event_version(event: dict) -> int | None:
    """Return 1 for an event without "v", 2 for "v" equal to the integer 2, else None."""
    if "v" not in event:
        return 1
    v = event["v"]
    if type(v) is int and v == EVENT_VERSION:
        return EVENT_VERSION
    return None


def verify_id(event: dict) -> bool:
    """
    Recompute the event id from its envelope fields and check it matches stored id.

    The rule follows the version: no "v" field is version 1 (prev outside the
    hash); "v" equal to the integer 2 is version 2 (prev and v hashed). Any
    other "v" value fails.
    """
    if not isinstance(event, dict):
        return False
    if not isinstance(event.get("id"), str) or not event["id"]:
        return False
    if isinstance(event.get("seq"), bool) or not isinstance(event.get("seq"), int):
        return False
    if not isinstance(event.get("ts"), str):
        return False
    if not isinstance(event.get("actor"), str):
        return False
    if not isinstance(event.get("type"), str):
        return False
    if not isinstance(event.get("payload"), dict):
        return False
    if not isinstance(event.get("prev"), str):
        return False
    version = event_version(event)
    if version == 1:
        expected = event_id(
            event["seq"], event["ts"], event["actor"], event["type"], event["payload"]
        )
    elif version == EVENT_VERSION:
        expected = event_id_v2(
            event["seq"], event["ts"], event["actor"], event["type"], event["payload"],
            event["prev"],
        )
    else:
        return False
    return event["id"] == expected


CLAIM_RELEASED = "item.claim_released"
CLAIM_EXPIRY_SET = "policy.claim_expiry_set"


def released_item(event: dict) -> str | None:
    """
    The item id a well-formed item.claim_released event names, else None.

    The one place the release payload's shape is stated: an item id and a
    reason, both non-empty strings. A release without a reason says nothing
    about why an item came free, so it is not a release at all. The command
    refuses the same shape before it mints anything; this is what the fold
    reads, so a hand-written or foreign record cannot end a claim by
    accident.
    """
    if not isinstance(event, dict) or event.get("type") != CLAIM_RELEASED:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    item_id = payload.get("item_id")
    reason = payload.get("reason")
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    return item_id


def declared_expiry_hours(event: dict) -> float | None:
    """
    The hours a well-formed policy.claim_expiry_set event declares, else None.

    The one place the policy payload's shape is stated: a finite, positive
    number of hours, and never a boolean (True would otherwise read as one
    hour). A record that declares anything else sets no policy, so the
    claims it would have governed keep the default.
    """
    if not isinstance(event, dict) or event.get("type") != CLAIM_EXPIRY_SET:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    hours = payload.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, (int, float)):
        return None
    if not math.isfinite(hours) or hours <= 0:
        return None
    return float(hours)


PROCESS_SET = "policy.process_set"
OWNER_GATE_CLASSES_SET = "policy.owner_gate_classes_set"


def declared_process(event: dict) -> str | None:
    """
    The process a well-formed policy.process_set event declares, else None.

    The one place that payload's shape is stated: one of PROCESSES (flow
    or strict). A record that declares anything else sets no process, so
    the project keeps the one in force before it.
    """
    if not isinstance(event, dict) or event.get("type") != PROCESS_SET:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    process = payload.get("process")
    return process if process in PROCESSES else None


def declared_owner_gate_classes(event: dict) -> list[str] | None:
    """
    The classes a well-formed policy.owner_gate_classes_set event declares.

    The one place that payload's shape is stated: a non-empty list of
    non-empty strings, each kept with its whitespace collapsed. Anything
    else sets no list, so the project keeps the one in force before it.
    """
    if not isinstance(event, dict) or event.get("type") != OWNER_GATE_CLASSES_SET:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    listed = payload.get("classes")
    if not isinstance(listed, list) or not listed:
        return None
    cleaned = []
    for entry in listed:
        if not isinstance(entry, str) or not entry.strip():
            return None
        cleaned.append(" ".join(entry.split()))
    return cleaned


ITEM_DECIDED = "item.decided"
DECISION_ACTORS_SET = "policy.decision_actors_set"
DECISION_EFFECTS = ("release", "park")
DECISION_FIELDS = ("option", "label", "reason")


def decision_payload_problem(payload: object) -> str | None:
    """
    None when payload is a well-formed item.decided payload, else what is wrong.

    The one place the decision payload's shape is stated: an item id, an
    option, a label and a reason, each a string that is not blank, and an
    effect that is release or park. The command refuses the same shape
    before it mints anything, and the fold records no decision from a
    record of any other shape.
    """
    if not isinstance(payload, dict):
        return "the payload is not an object"
    item_id = payload.get("item_id")
    if not isinstance(item_id, str) or not item_id:
        return "the item id is missing"
    for key in DECISION_FIELDS:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"the {key} is blank"
    if payload.get("effect") not in DECISION_EFFECTS:
        return f"the effect must be one of {', '.join(DECISION_EFFECTS)}"
    return None


def decided_item(event: dict) -> dict | None:
    """
    The payload of a well-formed item.decided event, else None.

    The returned payload holds exactly item_id, option, label, effect and
    reason; any other key in the record is not part of the decision.
    """
    if not isinstance(event, dict) or event.get("type") != ITEM_DECIDED:
        return None
    payload = event.get("payload")
    if decision_payload_problem(payload) is not None:
        return None
    return {
        key: payload[key]
        for key in ("item_id", "option", "label", "effect", "reason")
    }


def declared_decision_actors(event: dict) -> list[str] | None:
    """
    The actors a well-formed policy.decision_actors_set event lists, else None.

    The one place that payload's shape is stated: a non-empty list of
    strings, each a role@host handle with no whitespace. Anything else
    sets no list, so the project keeps the one in force before it.
    """
    if not isinstance(event, dict) or event.get("type") != DECISION_ACTORS_SET:
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    listed = payload.get("actors")
    if not isinstance(listed, list) or not listed:
        return None
    for entry in listed:
        if not isinstance(entry, str):
            return None
        role, sep, host = entry.partition("@")
        if not sep or not role or not host or "@" in host or len(entry.split()) != 1:
            return None
    return list(listed)


def valid_annulment(event: dict) -> bool:
    """Return whether event is a hash-valid, non-self-annulling tombstone."""
    if not verify_id(event) or event.get("type") != "event.annulled":
        return False
    payload = event["payload"]
    target_id = payload.get("target_id")
    reason = payload.get("reason")
    return (
        isinstance(target_id, str)
        and bool(target_id)
        and target_id != event["id"]
        and isinstance(reason, str)
        and bool(reason)
    )
