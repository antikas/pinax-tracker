"""
pinax done <id> --briefing <file> [--actor <actor>] [--json]

Records an item.completed event carrying the briefing content as a
work-record in the log payload, through the publish sequence (pinax.sync):
fetch, fold the union with the remote default branch, append, commit and
push.

The briefing is operational provenance - it is NOT knowledge-plane content.
It is stored in the log/fold state only.  Durable knowledge is projected to
the vault via 'capability-project' at a separate step (Discipline 12).

The briefing file content is read verbatim and stored in the payload.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from .. import sync


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def run(
    repo_root: str,
    item_id: str,
    briefing_path: str,
    actor: str | None = None,
    as_json: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax done in repo_root.

    Reads the briefing file, mints the item.completed event with the
    briefing as a work-record in the payload, and hands it to the publish
    sequence, which owns the fetch, the union fold, the append, the
    projection, the commit and the push.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(briefing_path):
        print(
            f"pinax: briefing file not found: {briefing_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    with open(briefing_path, "r", encoding="utf-8") as fh:
        briefing_content = fh.read()

    _actor = actor or _default_actor()

    outcome = sync.run_sequence(
        repo_root,
        event_type="item.completed",
        payload={"item_id": item_id, "briefing": briefing_content},
        actor=_actor,
        ts=_utc_now_iso(),
        item_id=item_id,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event

    from ..doctor import warn_if_log_ignored
    warn_if_log_ignored(repo_root)

    result = {
        "item_id": item_id,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "item.completed",
        "briefing_chars": len(briefing_content),
        "root": ergon_dir,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(f"pinax: item {item_id} marked done by {_actor} in {ergon_dir}")
        print(
            f"       briefing={len(briefing_content)} chars "
            f"event_id={event['id'][:12]}... seq={event['seq']}"
        )
