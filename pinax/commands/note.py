"""
pinax note add <item_id> --ref <ref> [--caption <text>] [--actor <actor>] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
a note.added event, then fetch (unless offline), fold, append, commit and
push. Refuses an actor handle that is not role@host before minting
anything; see pinax.sync for the exit codes and offline rules.

ADR-004 / DESIGN.md enforcement (hard error at CLI write-time, not warning):
- ref MUST match ^(koine://|~/knowledge/|projects/|docs/) — it is a pointer to a
  knowledge-plane document, never knowledge content itself.
- caption is optional; if provided it MUST be <= 200 characters.

Rejection is a hard error: sys.exit(1) with a clear message.  A direct JSONL
append bypasses this check (by construction — the log is append-only); this is
the CLI-enforced boundary.
"""

from __future__ import annotations

import json
import os
import re
import sys

from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.

# ADR-004: the typed ref pattern — pointer to a knowledge-plane document.
_REF_PATTERN = re.compile(r"^(koine://|~/knowledge/|projects/|docs/)")

# ADR-004 / DESIGN.md: caption cap.
_CAPTION_MAX = 200


def run(
    repo_root: str,
    item_id: str,
    ref: str,
    caption: str | None,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax note add in repo_root.

    Hard-rejects at the CLI:
    - ref that does not match the typed-ref pattern (ADR-004)
    - caption that exceeds 200 characters (ADR-004 / DESIGN.md)

    On success: hands a note.added event to the publish sequence, which
    refuses an item that does not exist before appending anything
    (pinax.targets owns that rule).

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print(
            "pinax: .ergon/log/ not found - run 'pinax init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- HARD VALIDATION (ADR-004 / DESIGN.md) ---

    if not _REF_PATTERN.match(ref):
        print(
            f"pinax note add: REJECTED - ref must match "
            f"^(koine://|~/knowledge/|projects/|docs/), got: {ref!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    if caption is not None and len(caption) > _CAPTION_MAX:
        print(
            f"pinax note add: REJECTED - caption exceeds {_CAPTION_MAX} characters "
            f"({len(caption)} chars). Truncate or use a ref to a vault document.",
            file=sys.stderr,
        )
        sys.exit(1)

    # --- HAND OFF TO THE PUBLISH SEQUENCE ---

    from .. import sync

    _actor = actor or default_actor()

    payload: dict = {"item_id": item_id, "ref": ref}
    if caption is not None:
        payload["caption"] = caption

    outcome = sync.run_sequence(
        repo_root,
        event_type="note.added",
        payload=payload,
        actor=_actor,
        ts=utc_now_iso(),
        item_id=item_id,
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    if as_json:
        print(json.dumps({
            "event_id": event["id"],
            "item_id": item_id,
            "ref": ref,
            "root": ergon_dir,
        }))
    else:
        # visible the moment it happens.
        caption_str = f" ({caption!r})" if caption else ""
        print(f"pinax: note.added on {item_id} -> {ref}{caption_str} in {ergon_dir}")
