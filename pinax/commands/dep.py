"""
pinax dep add <item> --to <other> --type <t> [--offline]  -> append dep.added event
pinax dep rm  <item> --to <other> --type <t> [--offline]  -> append dep.removed event

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

Both operations run the publish sequence (pinax.sync) exactly as claim and
done do: mint the event, then fetch (unless offline), fold, append, commit
and push. Refuses an actor handle that is not role@host before minting
anything; see pinax.sync for the exit codes and offline rules.
"""

from __future__ import annotations

import json
import os
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..fold import fold

# pinax.sync imports pinax.projection, which imports this module for
# VALID_EDGE_TYPES at its own top level -- a top-level "from .. import
# sync" here would close that back into a circular import when this
# module is the first of the pair to load. The import is deferred to
# _run_dep instead, same discipline as ..projection's own deferred import
# below.


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


def _run_dep(
    repo_root: str,
    from_id: str,
    to_id: str,
    operation: str,   # "add" or "rm"
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax dep add/rm in repo_root.

    Hands dep.added (operation="add") or dep.removed (operation="rm") to
    the publish sequence. Validates first:
    - Both item IDs exist in the fold state.
    - from_id != to_id (no self-edges).
    - edge_type is in the closed enum (VALID_EDGE_TYPES).

    runner is injectable for tests only; the CLI passes none.
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

    from .. import sync

    _actor = actor or default_actor()
    etype = "dep.added" if operation == "add" else "dep.removed"
    payload = {
        "from_id": from_id,
        "to_id": to_id,
        "type": edge_type,
    }

    outcome = sync.run_sequence(
        repo_root,
        event_type=etype,
        payload=payload,
        actor=_actor,
        ts=utc_now_iso(),
        item_id=from_id,
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = {
        "from_id": from_id,
        "to_id": to_id,
        "dep_type": edge_type,
        "operation": operation,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": etype,
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        verb = "added" if operation == "add" else "removed"
        print(f"pinax: dep {verb}: {from_id} --{edge_type}--> {to_id} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']} actor={_actor}")


def run_add(
    repo_root: str,
    from_id: str,
    to_id: str,
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """Execute pinax dep add."""
    _run_dep(
        repo_root, from_id, to_id, "add", edge_type=edge_type, actor=actor,
        as_json=as_json, offline=offline, runner=runner,
    )


def run_rm(
    repo_root: str,
    from_id: str,
    to_id: str,
    edge_type: str = "blocks",
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """Execute pinax dep rm."""
    _run_dep(
        repo_root, from_id, to_id, "rm", edge_type=edge_type, actor=actor,
        as_json=as_json, offline=offline, runner=runner,
    )
