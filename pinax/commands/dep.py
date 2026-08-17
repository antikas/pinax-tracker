"""
pinax dep add <item> --to <other> --type <t>  → append dep.added event
pinax dep rm  <item> --to <other> --type <t>  → append dep.removed event

where <t> is one of the valid edge types defined by VALID_EDGE_TYPES in this file
(closed enum; rejected at write-time if any other value is given).

Back-compat alias: --blocks <other> is equivalent to --to <other> --type blocks.
Existing logs and test fixtures that use --blocks continue to work unchanged.

Payload for both:
  {"from_id": <item>, "to_id": <other>, "type": <edge_type>}

Semantics: see VALID_EDGE_TYPES below for the full set; each type carries the meaning
implied by its name (blocks = readiness gate; parent-child = hierarchy; others informational).

--json prints the result as JSON (for agents).

Typed multi-edge graph: all edge types in VALID_EDGE_TYPES are first-class in
the fold. Readiness gates on `blocks` edges only.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import fold, read_events


# ---------------------------------------------------------------------------
# Closed enum of valid edge types (enforced at write-time — ADR-001).
# ---------------------------------------------------------------------------

VALID_EDGE_TYPES = frozenset({
    "blocks",
    "parent-child",
    "discovered-from",
    "related",
    "supersedes",
})


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _run_dep(
    repo_root: str,
    from_id: str,
    to_id: str,
    operation: str,   # "add" or "rm"
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Execute pinax dep add/rm in repo_root.

    Appends dep.added (operation="add") or dep.removed (operation="rm").
    Validates:
    - Both item IDs exist in the fold state.
    - from_id != to_id (no self-edges).
    - edge_type is in the closed enum (VALID_EDGE_TYPES).
    """
    # Validate edge type at write-time (before touching the log).
    if edge_type not in VALID_EDGE_TYPES:
        print(
            f"pinax: unknown edge type '{edge_type}'. "
            f"Valid types: {', '.join(sorted(VALID_EDGE_TYPES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    # Validate both item IDs exist.
    state = fold(log_dir)
    items = state.get("items", {})
    if from_id not in items:
        print(
            f"pinax: unknown item '{from_id}'. Known items: {', '.join(sorted(items))}",
            file=sys.stderr,
        )
        sys.exit(1)
    if to_id not in items:
        print(
            f"pinax: unknown item '{to_id}'. Known items: {', '.join(sorted(items))}",
            file=sys.stderr,
        )
        sys.exit(1)
    if from_id == to_id:
        print(
            "pinax: dep from_id and to_id must be different (self-dep not allowed).",
            file=sys.stderr,
        )
        sys.exit(1)

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    etype = "dep.added" if operation == "add" else "dep.removed"
    payload = {
        "from_id": from_id,
        "to_id": to_id,
        "type": edge_type,
    }
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype=etype,
        payload=payload,
        prev=prev,
    )
    append_event(log_dir, event, actor=_actor)

    # Regenerate the projection atomically after the append (ADR-002).
    from ..projection import regenerate
    regenerate(repo_root)

    from ..doctor import warn_if_log_ignored
    warn_if_log_ignored(repo_root)

    result = {
        "from_id": from_id,
        "to_id": to_id,
        "dep_type": edge_type,
        "operation": operation,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": etype,
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        verb = "added" if operation == "add" else "removed"
        print(f"pinax: dep {verb}: {from_id} --{edge_type}--> {to_id} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq} actor={_actor}")


def run_add(
    repo_root: str,
    from_id: str,
    to_id: str,
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """Execute pinax dep add."""
    _run_dep(repo_root, from_id, to_id, "add", edge_type=edge_type, actor=actor, as_json=as_json)


def run_rm(
    repo_root: str,
    from_id: str,
    to_id: str,
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """Execute pinax dep rm."""
    _run_dep(repo_root, from_id, to_id, "rm", edge_type=edge_type, actor=actor, as_json=as_json)
