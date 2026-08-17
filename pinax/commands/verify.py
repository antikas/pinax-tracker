"""
pinax verify — drift lint: regenerate the projection and diff against committed.

ADR-002: the projection is never hand-edited.  This command catches any drift
between the committed projection and the regenerated-from-log version.

Exit codes:
  0 — event history is intact and projection is clean
  1 — event history is invalid or projection drift is detected

Used by the pre-commit hook and by the CI gate.
"""

from __future__ import annotations

import os
import sys


def _inspect(repo_root: str) -> tuple[list[str] | None, list[dict]]:
    """
    Check event hashes and compare the projection against a fresh log fold.

    Returns ``(drifted_files, invalid_events)``. ``drifted_files`` is None
    when there is no log to verify. Formally annulled events are excluded from
    ``invalid_events`` because annulment is Pinax's audit-preserving mechanism
    for retiring a known bad event.
    """
    from ..fold import _collect_annulled_ids, finalise_events, fold_events, read_raw_events
    from ..event import valid_annulment, verify_id
    from ..projection import render_board, render_item

    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    board_path = os.path.join(ergon_dir, "board.md")
    items_dir = os.path.join(ergon_dir, "items")

    if not os.path.isdir(log_dir):
        return None, []

    # Verify the physical parsed lines before same-id deduplication.  The fold
    # still receives its canonical deduplicated representation afterwards.
    raw_events = read_raw_events(log_dir, include_invalid=True)
    annulled_ids = _collect_annulled_ids(raw_events)
    invalid_events = [
        event
        for event in raw_events
        if (
            (event.get("type") == "event.annulled" and not valid_annulment(event))
            or (event.get("type") != "event.annulled" and event.get("id") not in annulled_ids and not verify_id(event))
        )
    ]

    # Do not sort or fold a malformed physical record. Besides reporting the
    # integrity failure first, this keeps --fix from deriving a replacement
    # projection from invalid history.
    if invalid_events:
        return [], invalid_events

    events = finalise_events(raw_events)
    state = fold_events(events)
    items = state.get("items", {})

    drift_files: list[str] = []

    # Check board.md.
    generated_board = render_board(state)
    if os.path.isfile(board_path):
        with open(board_path, "r", encoding="utf-8", newline="") as fh:
            committed_board = fh.read()
        # Normalise CRLF in committed board (in case Git checked it out with CRLF).
        committed_board_lf = committed_board.replace("\r\n", "\n").replace("\r", "\n")
        if generated_board != committed_board_lf:
            drift_files.append(".ergon/board.md")
    else:
        if items:
            # Log exists and has items but board.md is missing → drift.
            drift_files.append(".ergon/board.md (missing)")

    # Check per-item files.
    for item_id, item in sorted(items.items()):
        item_path = os.path.join(items_dir, f"{item_id}.md")
        generated_item = render_item(item_id, item, state)
        if os.path.isfile(item_path):
            with open(item_path, "r", encoding="utf-8", newline="") as fh:
                committed_item = fh.read()
            committed_item_lf = committed_item.replace("\r\n", "\n").replace("\r", "\n")
            if generated_item != committed_item_lf:
                drift_files.append(f".ergon/items/{item_id}.md")
        else:
            drift_files.append(f".ergon/items/{item_id}.md (missing)")

    # Check for stale item files (present in projection but not in fold state).
    if os.path.isdir(items_dir):
        for fname in sorted(os.listdir(items_dir)):
            if fname.endswith(".md"):
                stale_id = fname[:-3]
                if stale_id not in items:
                    drift_files.append(f".ergon/items/{stale_id}.md (stale)")

    return drift_files, invalid_events


def _fail_on_invalid_events(invalid_events: list[dict]) -> None:
    """Fail closed before projection comparison can hide tampering."""
    if not invalid_events:
        return

    details = "\n".join(
        "    id={id} seq={seq} actor={actor} type={type}".format(
            id=event.get("id", "<missing>"),
            seq=event.get("seq", "<missing>"),
            actor=event.get("actor", "<missing>"),
            type=event.get("type", "<missing>"),
        )
        for event in invalid_events
    )
    print(
        "pinax verify: EVENT LOG INTEGRITY FAILURE - "
        f"{len(invalid_events)} non-annulled event(s) no longer match their "
        "stored content hash.\n"
        f"  Invalid events:\n{details}\n\n"
        "  The append-only event log was modified after these events were written.\n"
        "  'pinax verify --fix' only regenerates projections and will not rewrite\n"
        "  event history. Restore the exact original event bytes. A valid ordinary\n"
        "  event may be formally annulled before replacement; a malformed annulment\n"
        "  record itself must be restored from known-good history.",
        file=sys.stderr,
    )
    sys.exit(1)


def _check_log_tracking(repo_root: str) -> dict:
    """Check whether a .gitignore rule currently swallows the log."""
    from ..doctor import log_tracking_status

    log_dir = os.path.join(repo_root, ".ergon", "log")
    return log_tracking_status(repo_root, log_dir)


def run(repo_root: str, fix: bool = False) -> None:
    """
    Verify the committed projection matches the log, and that
    the log itself is not being swallowed by a .gitignore rule.

    Regenerates the projection in-memory and diffs it against the committed
    version.  A non-empty diff means the projection was hand-edited or is
    out of date — exits 1.

    With fix=True: on drift, regenerate the projection on disk via
    ``pinax.projection.regenerate`` — the same atomic regeneration path
    every state-changing command (add/done/claim/note/...) already uses —
    then re-verify to confirm it cleared the drift.

    The gitignore-swallow check is independent of drift and of --fix (there
    is no "fix" for a swallowed log other than 'pinax init' or editing the
    consumer's own .gitignore) — it FAILS LOUDLY (exit 1) whenever the log
    would currently be git-ignored, even when the projection itself is
    drift-free.
    """
    drift_files, invalid_events = _inspect(repo_root)

    if drift_files is None:
        print("pinax verify: .ergon/log/ not found - nothing to verify.", file=sys.stderr)
        sys.exit(0)

    # This is deliberately before drift handling and --fix. A projection can
    # be regenerated from mutated history and appear byte-clean; event hashes
    # are the independent append-only boundary that must fail first.
    _fail_on_invalid_events(invalid_events)

    tracking = _check_log_tracking(repo_root)
    swallowed = bool(tracking.get("available") and tracking.get("ignored"))
    if swallowed:
        print(
            "pinax verify: LOG SWALLOWED BY GITIGNORE - "
            f"{tracking.get('probe_path')} would be git-ignored right now.\n"
            "  A .gitignore rule is shadowing the event log shard directory: "
            "new shard files\n"
            "  will be silently local-only, never committed, invisible "
            "across worktrees/\n"
            "  branches because Git ignores it. Run 'pinax init' again to "
            "(re)install the\n"
            "  .ergon/.gitignore negation, or fix your repo's .gitignore.",
            file=sys.stderr,
        )

    if not drift_files:
        if swallowed:
            sys.exit(1)
        print("pinax verify: OK - projection matches log.")
        return

    if not fix:
        print(
            f"pinax verify: DRIFT DETECTED - projection differs from log.\n"
            f"  Drifted files:\n"
            + "\n".join(f"    {f}" for f in drift_files)
            + "\n\n"
            "  Run 'pinax verify --fix' to regenerate, or run any state-changing\n"
            "  command (which regenerates atomically).\n"
            "  The projection must never be hand-edited (ADR-002).",
            file=sys.stderr,
        )
        sys.exit(1)

    # --fix: regenerate via the canonical projection-regeneration path
    # (the same function every state-changing command calls), then
    # re-verify to confirm the drift actually cleared.
    from ..projection import regenerate

    print(
        f"pinax verify: DRIFT DETECTED - regenerating projection (--fix)...\n"
        f"  Drifted files:\n"
        + "\n".join(f"    {f}" for f in drift_files),
        file=sys.stderr,
    )

    regenerate(repo_root)

    remaining, remaining_invalid = _inspect(repo_root)
    _fail_on_invalid_events(remaining_invalid)
    if remaining:
        print(
            "pinax verify --fix: regeneration did not clear all drift.\n"
            "  Still drifted:\n"
            + "\n".join(f"    {f}" for f in remaining),
            file=sys.stderr,
        )
        sys.exit(1)

    if swallowed:
        sys.exit(1)

    print("pinax verify --fix: OK - projection regenerated and matches log.")
