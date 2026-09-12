"""
pinax.event — event envelope: mint, hash, serialise, parse.

ADR-001 compliance:
- id = blake2b over canonical JSON of (seq, ts, actor, type, payload)
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


def event_id(seq: int, ts: str, actor: str, etype: str, payload: dict) -> str:
    """Compute the blake2b content-hash id for an event envelope (ADR-001)."""
    obj = {"seq": seq, "ts": ts, "actor": actor, "type": etype, "payload": payload}
    raw = _canonical_json(obj)
    digest = hashlib.blake2b(raw, digest_size=_DIGEST_BITS // 8).digest()
    return _b32(digest)


def mint_event(
    seq: int,
    ts: str,
    actor: str,
    etype: str,
    payload: dict,
    prev: str = "",
) -> dict:
    """
    Build a complete event envelope.

    All fields are deterministic from inputs — no wall-clock, no RNG.
    prev = '' for the first event in an actor shard (explicit sentinel).
    """
    eid = event_id(seq, ts, actor, etype, payload)
    return {
        "id": eid,
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "type": etype,
        "payload": payload,
        "prev": prev,
    }


def serialise(event: dict) -> str:
    """
    Serialise event to a canonical JSON string (no trailing newline).

    The line stored in the JSONL file is serialise(event) + '\\n'.
    Field order: id, seq, ts, actor, type, payload, prev — consistent for readability;
    the fold parses the JSON, not the field order.
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


def verify_id(event: dict) -> bool:
    """
    Recompute the event id from its envelope fields and check it matches stored id.

    Operates on LF-normalised canonical JSON — identical to mint_event.
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
    expected = event_id(
        event["seq"], event["ts"], event["actor"], event["type"], event["payload"]
    )
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
