"""
pinax dispatch [--max N] [--claim] [--json]

Ready-item manifest with a concurrency cap.

Behaviour:
- Emits the ready-set manifest (from compute_ready / compute_next).
- --max N caps the manifest at N items (concurrency cap).
- --claim automatically claims each manifest item on behalf of the actor.
- --json outputs the manifest as a JSON array for agent/workflow consumption.

This command is the boundary between Pinax and an external executor.
The manifest is a list of item dicts ordered by compute_next priority
(phase order, age, id).
"""

from __future__ import annotations

import json
import os
import sys
import datetime

from ..fold import fold, compute_ready, compute_next
from ..append import append_event
from ..doctor import warn_if_log_ignored
from ..event import mint_event
from ..projection import regenerate


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _max_seq_in_log(log_dir: str) -> int:
    """Return the current maximum seq value in the log, or -1 if empty."""
    import glob
    import pinax.event as ev_mod
    max_seq = -1
    for shard_path in sorted(glob.glob(os.path.join(log_dir, "*.jsonl"))):
        try:
            with open(shard_path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        for line in normalised.split(b"\n"):
            if not line:
                continue
            event = ev_mod.parse_line(line)
            if event is None:
                continue
            seq = event.get("seq", -1)
            if isinstance(seq, int) and seq > max_seq:
                max_seq = seq
    return max_seq


def _last_event_id_in_shard(shard_path: str) -> str:
    """Return the id of the last event in a shard, or '' if empty/absent."""
    import pinax.event as ev_mod
    events = []
    try:
        with open(shard_path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    for line in normalised.split(b"\n"):
        if not line:
            continue
        event = ev_mod.parse_line(line)
        if event is not None:
            events.append(event)
    for event in reversed(events):
        eid = event.get("id", "")
        if eid:
            return eid
    return ""


def run(
    repo_root: str,
    max_items: int | None = None,
    claim: bool = False,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax dispatch in repo_root.

    Computes the ready manifest (compute_ready, ordered by compute_next priority),
    caps it at --max if specified, optionally claims each item.

    NEVER writes to the knowledge plane.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print(
            "pinax: .ergon/log/ not found - run 'pinax init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    state = fold(log_dir)
    items = state.get("items", {})

    # Build the manifest: ready items ordered by compute_next priority.
    # compute_ready returns a sorted list; we pick them off using compute_next
    # ordering by repeatedly finding the next item.
    ready_ids = compute_ready(state)

    # Build a priority-ordered manifest.
    # compute_next returns the single best item; to get an ordered list we
    # simulate removing items one at a time.  This is efficient for the
    # expected manifest sizes (single-digit items).
    manifest_ids: list[str] = []
    remaining_state = state
    remaining_ready = list(ready_ids)  # copy

    while remaining_ready:
        next_id = compute_next(remaining_state)
        if next_id is None or next_id not in remaining_ready:
            # Fallback: just take the first ready id (lexicographic).
            next_id = min(remaining_ready)
        manifest_ids.append(next_id)
        remaining_ready.remove(next_id)
        # Remove the item from state items so compute_next skips it next round.
        # We don't need to mutate the real state — build a shallow copy.
        fake_items = {k: v for k, v in remaining_state.get("items", {}).items() if k != next_id}
        remaining_state = {**remaining_state, "items": fake_items}

    # Apply --max cap.
    if max_items is not None and max_items > 0:
        manifest_ids = manifest_ids[:max_items]

    # Build manifest dicts.
    manifest: list[dict] = []
    for item_id in manifest_ids:
        item = items.get(item_id, {})
        manifest.append({
            "id": item_id,
            "title": item.get("title", ""),
            "status": item.get("status", ""),
            "owner": item.get("owner", ""),
            "created_at": item.get("created_at", ""),
        })

    # --claim: append item.claimed events for each manifest item.
    if claim and manifest:
        _actor = actor or _default_actor()
        ts = _utc_now_iso()
        seq = _max_seq_in_log(log_dir) + 1

        shard_safe = _actor.replace("@", "-").replace("/", "-").replace("\\", "-").replace(" ", "-")
        shard_path = os.path.join(log_dir, f"{shard_safe}.jsonl")
        prev_id = _last_event_id_in_shard(shard_path)

        for item_id in manifest_ids:
            payload = {"item_id": item_id, "actor": _actor}
            event = mint_event(
                seq=seq,
                ts=ts,
                actor=_actor,
                etype="item.claimed",
                payload=payload,
                prev=prev_id,
            )
            append_event(log_dir, event, actor=_actor)
            prev_id = event["id"]
            seq += 1

        # Regenerate projection atomically (ADR-002).
        regenerate(repo_root)

        warn_if_log_ignored(repo_root)

    if as_json:
        print(json.dumps(manifest, sort_keys=True, ensure_ascii=True))
        return

    # Human-readable output.
    if not manifest:
        print("pinax dispatch: ready queue is empty - nothing to dispatch.")
        return

    cap_str = f" (capped at {max_items})" if max_items is not None else ""
    claim_str = " [claimed]" if claim else ""
    print(f"pinax dispatch: {len(manifest)} item(s){cap_str}{claim_str}")
    for i, entry in enumerate(manifest, 1):
        status_label = f"[{entry['status']}]" if entry['status'] else ""
        print(f"  {i}. {entry['id']}  {status_label}  {entry['title']}")
