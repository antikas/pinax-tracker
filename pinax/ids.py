"""
pinax.ids — item ID minting per ADR-003.

ID = <prefix>-<short base32 blake2b of (seq, title, actor, worktree_id, nonce)>
- nonce = timestamp_ns + title + actor + worktree_id (ensures two agents creating
  an identical-title item at the same millisecond on different worktrees get
  different nonces and therefore different IDs).
- Auto-extends the short suffix on collision against the current fold state
  (git short-hash discipline).
- The full content-hash is the identity; the short form is a display convenience.
- NO builtin hash(), NO PYTHONHASHSEED dependence.
"""

from __future__ import annotations

import hashlib
import time
from typing import Iterable

from .event import _b32, _canonical_json


_SHORT_MIN = 4   # minimum suffix length (characters)


def _worktree_id() -> str:
    """
    A stable-enough worktree identifier for collision avoidance.

    Uses the hostname.  For tests, callers pass worktree_id explicitly.
    """
    import socket
    return socket.gethostname()


def _full_item_hash(
    seq: int,
    title: str,
    actor: str,
    worktree_id: str,
    nonce: str,
) -> str:
    """
    Full base32 blake2b hash over (seq, title, actor, worktree_id, nonce).

    Returns the full hash string (no truncation).
    """
    obj = {
        "seq": seq,
        "title": title,
        "actor": actor,
        "worktree_id": worktree_id,
        "nonce": nonce,
    }
    raw = _canonical_json(obj)
    digest = hashlib.blake2b(raw, digest_size=32).digest()
    return _b32(digest)


def mint_item_id(
    seq: int,
    title: str,
    actor: str,
    prefix: str,
    existing_ids: Iterable[str],
    *,
    worktree_id: str | None = None,
    nonce: str | None = None,
) -> str:
    """
    Mint a new item ID: <prefix>-<short_hash>.

    Short suffix starts at _SHORT_MIN characters and auto-extends until it is
    unique among existing_ids.

    worktree_id defaults to the machine hostname.
    nonce defaults to time.time_ns() as a string.
    """
    wid = worktree_id if worktree_id is not None else _worktree_id()
    nc = nonce if nonce is not None else str(time.time_ns())

    full_hash = _full_item_hash(seq, title, actor, wid, nc)

    # Build a set of existing short hashes (the part after the prefix dash).
    existing_set = set(existing_ids)

    for length in range(_SHORT_MIN, len(full_hash) + 1):
        short = full_hash[:length]
        candidate = f"{prefix}-{short}"
        if candidate not in existing_set:
            return candidate

    # Unreachable in practice (full hash is globally unique), but satisfy the type.
    return f"{prefix}-{full_hash}"
