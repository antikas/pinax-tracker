"""
pinax note add <item_id> --ref <ref> [--caption <text>] [--actor <actor>] [--offline] [--json]

Runs the publish sequence (pinax.sync) exactly as claim and done do: mint
a note.added event, then fetch (unless offline), fold, append, commit and
push. Refuses an actor handle that is not role@host before minting
anything; see pinax.sync for the exit codes and offline rules.

Enforcement (hard error at CLI write-time, not warning):
- ref MUST be a repository-relative path, an http or https address, or start
  with a prefix set by 'pinax policy note-refs' (see ref_refusal). It is read
  on the pre-append union fold; a refusal ends with exit 8 (policy_refused).
- caption is optional; if provided it MUST be <= 200 characters, else exit 1.

A direct JSONL append bypasses this check (by construction — the log is
append-only); this is the CLI-enforced boundary.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from .. import refusal
from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..fold import _is_file_name

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.

# The caption cap (docs/commands.md, "pinax note add").
_CAPTION_MAX = 200


def _is_repository_path(ref: str) -> bool:
    """
    True for a repository-relative path, whether or not a file is there.

    Empty and "." segments are allowed beside at least one that is a file
    name. The file name rule is the one for item ids, and it refuses "..",
    a backslash and a drive letter's colon.
    """
    if ref.startswith("/"):
        return False
    segments = [segment for segment in ref.split("/") if segment not in ("", ".")]
    return bool(segments) and all(_is_file_name(segment) for segment in segments)


def _is_web_address(ref: str) -> bool:
    """True for a URL with scheme http or https and a host."""
    # urlsplit silently drops tabs, newlines and leading spaces.
    if any(char.isspace() or not char.isprintable() for char in ref):
        return False
    try:
        parts = urlsplit(ref)
        parts.port  # raises ValueError when the port is not a number
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def ref_refusal(state: dict, ref: str) -> str | None:
    """
    None when a note may carry ref, else the refusal message.

    state is the pre-append union fold, which holds the prefixes the
    repository accepts (event.declared_note_ref_prefixes). The process,
    flow or strict, does not change the rule.
    """
    prefixes = (state.get("note_ref_policy") or {}).get("prefixes", [])
    if (
        ref.startswith(tuple(prefixes))
        or _is_web_address(ref)
        or _is_repository_path(ref)
    ):
        return None
    return (
        f"pinax note add: the reference '{ref}' is not a repository-relative path, "
        "not an http or https address, and does not start with a prefix the "
        f"repository accepts ({', '.join(prefixes) or 'none set'}; "
        "see 'pinax policy note-refs')"
    )


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
    - ref that ref_refusal refuses, through the sequence's policy_check
    - caption that exceeds 200 characters (docs/commands.md, "pinax note add")

    On success: hands a note.added event to the publish sequence, which
    refuses an item that does not exist before appending anything
    (pinax.targets owns that rule).

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    refusal.require_tracker(log_dir)

    # --- HARD VALIDATION ---

    if caption is not None and len(caption) > _CAPTION_MAX:
        refusal.refuse(
            f"pinax note add: REJECTED - caption exceeds {_CAPTION_MAX} characters "
            f"({len(caption)} chars). Truncate or use a ref to a vault document.",
            refusal.INVALID_ARGUMENT,
        )

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
        policy_check=lambda state: ref_refusal(state, ref),
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
            **outcome.publication(),
        }))
    else:
        # visible the moment it happens.
        caption_str = f" ({caption!r})" if caption else ""
        print(f"pinax: note.added on {item_id} -> {ref}{caption_str} in {ergon_dir}")
