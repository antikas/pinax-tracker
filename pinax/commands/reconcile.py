"""Import offline completion and park actions into the event log.

`pinax reconcile [--file PATH] [--actor ACTOR] [--dry-run] [--json]` accepts
one action per line:

- `<UTC ISO-8601 ts> <actor> done <item-id> [| <caption>]`
- `<UTC ISO-8601 ts> <actor> park <item-id> | <reason>`

The line timestamp is event metadata; fold order remains `(seq, ts, actor, id)`.
The line actor is preserved in the imported event and the reconciling actor is
recorded as provenance. A content-derived `source_line_hash` makes repeated
imports idempotent. Invalid item identifiers and incompatible terminal states
are reported without appending an event.

Processed lines remain in the action file under dated Reconciled or Rejected
sections. Each original line is paired with a separate detail marker, so a new
line remains distinguishable from an earlier result regardless of its content.
`--dry-run` performs no append, rewrite, or projection generation.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from typing import Optional

from ..append import append_event, _shard_name_for_actor
from ..event import mint_event, _canonical_json, _b32
from ..fold import fold, read_events


_VALID_VERBS = {"done", "park"}

_DEFAULT_FILENAME = "BACKLOG-OFFLINE.md"


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _source_line_hash(actor: str, ts: str, verb: str, item_id: str, text: str) -> str:
    """
    Blake2b (base32) over the canonical form of (actor, ts, verb, item_id,
    text) -- derived from the parsed line's content, never minted.  Same line
    always yields the same hash, regardless of when or by whom it is
    reconciled.
    """
    obj = {"actor": actor, "ts": ts, "verb": verb, "item_id": item_id, "text": text}
    raw = _canonical_json(obj)
    digest = hashlib.blake2b(raw, digest_size=32).digest()
    return _b32(digest)


def parse_offline_line(raw_line: str) -> dict:
    """
    Parse one BACKLOG-OFFLINE.md action line.

    Returns a dict:
      {"ok": True, "ts": ..., "actor": ..., "verb": ..., "item_id": ..., "text": str|None}
    or
      {"ok": False, "reason": "<why this line is rejected>"}

    Grammar:
      - <ts> <actor> done <item-id> [| <caption>]
      - <ts> <actor> park <item-id> | <reason>

    Unknown verbs and any line not matching the 4-field head are REJECTED
    with a reason, never guessed at.
    """
    line = raw_line.strip()
    if not line.startswith("- "):
        return {"ok": False, "reason": "malformed line: must start with '- '"}

    rest = line[2:].strip()
    if not rest:
        return {"ok": False, "reason": "malformed line: empty after '- '"}

    if "|" in rest:
        head, text = rest.split("|", 1)
        head = head.strip()
        text = text.strip() or None
    else:
        head = rest
        text = None

    tokens = head.split()
    if len(tokens) != 4:
        return {
            "ok": False,
            "reason": (
                "malformed line: expected '<ts> <actor> <verb> <item-id>', "
                f"got {len(tokens)} field(s)"
            ),
        }
    ts, actor, verb, item_id = tokens

    if verb not in _VALID_VERBS:
        return {
            "ok": False,
            "reason": f"unknown verb: {verb!r} (only 'done'/'park' in v1)",
        }

    if verb == "park" and not text:
        return {"ok": False, "reason": "park requires '| <reason>'"}

    return {
        "ok": True,
        "reason": None,
        "ts": ts,
        "actor": actor,
        "verb": verb,
        "item_id": item_id,
        "text": text,
    }


# A line this command itself wrote into a dated section body is followed
# on the NEXT physical line by a standalone marker of this exact shape (see
# _rewrite_offline_file) -- never appended onto the same line as the
# original text.  Used by _split_file to recognise an already-processed
# entry by the (original line, following marker line) PAIR, never by
# pattern-matching the original line's own content.
# above for why a same-line suffix is not collision-proof.
_MARKER_LINE_RE = re.compile(r"^  => .+$")
_HEADER_RE = re.compile(r"^## ")


def _split_file(content: str) -> tuple[list[str], list[str]]:
    """Return candidate and preserved lines without matching marker text inline.

    A processed entry is an original line immediately followed by a standalone
    detail marker. Headers and blank section spacers are also preserved. Any
    other line remains a candidate, including a new line below an earlier
    dated section.
    """
    lines = content.split("\n")
    n = len(lines)
    candidates: list[str] = []
    preserved: list[str] = []
    in_section = False
    i = 0
    while i < n:
        line = lines[i]
        if _HEADER_RE.match(line):
            in_section = True
            preserved.append(line)
            i += 1
            continue
        if in_section and line.strip() == "":
            preserved.append(line)
            i += 1
            continue
        if in_section and i + 1 < n and _MARKER_LINE_RE.match(lines[i + 1]):
            # This line, paired with the standalone marker line right after
            # it, is an entry a prior pass of THIS command wrote. Preserve
            # both, regardless of what the original line's own text is.
            preserved.append(line)
            preserved.append(lines[i + 1])
            i += 2
            continue
        in_section = False
        candidates.append(line)
        i += 1
    return candidates, preserved


def _prev_event_id_for_actor(all_events: list[dict], actor: str) -> str:
    matching = [e["id"] for e in all_events if e.get("actor") == actor]
    return matching[-1] if matching else ""


def run(
    repo_root: str,
    file_path: Optional[str] = None,
    actor: Optional[str] = None,
    dry_run: bool = False,
    as_json: bool = False,
) -> None:
    """
    Execute pinax reconcile in repo_root.

    Imports done/park lines from the offline file into .ergon events,
    then, unless --dry-run, regenerates the projection and rewrites the
    offline file in place.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    offline_path = file_path if file_path is not None else os.path.join(repo_root, _DEFAULT_FILENAME)

    if not os.path.isfile(offline_path):
        result = {
            "file": offline_path,
            "dry_run": dry_run,
            "imported": [],
            "already_imported": [],
            "rejected": [],
            "skipped": [],
        }
        if as_json:
            print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        else:
            print(f"pinax reconcile: no offline file at {offline_path} - nothing to reconcile.")
        return

    with open(offline_path, "r", encoding="utf-8", newline="") as fh:
        raw_content = fh.read()
    normalised = raw_content.replace("\r\n", "\n").replace("\r", "\n")

    candidate_lines, preserved_lines = _split_file(normalised)
    candidates = [(i, l) for i, l in enumerate(candidate_lines) if l.strip()]

    state = fold(log_dir)
    items = state.get("items", {})

    all_events = read_events(log_dir)
    hash_index: dict[str, str] = {}
    for ev in all_events:
        h = ev.get("payload", {}).get("source_line_hash")
        if h:
            hash_index[h] = ev["id"]

    _reconciler = actor or _default_actor()
    imported_at = _utc_now_iso()

    next_seq = (max(e["seq"] for e in all_events) + 1) if all_events else 0
    actor_prev: dict[str, str] = {}

    def _prev_for(line_actor: str) -> str:
        if line_actor not in actor_prev:
            actor_prev[line_actor] = _prev_event_id_for_actor(all_events, line_actor)
        return actor_prev[line_actor]

    # Local overlay so several lines for the same item within ONE file are
    # resolved against each other's outcome this pass, not just the on-disk
    # fold at the start of this invocation.
    status_overlay: dict[str, str] = {}

    imported: list[dict] = []
    already_imported: list[dict] = []
    rejected: list[dict] = []
    skipped: list[dict] = []

    # index -> "=> <detail>" outcome, split into result buckets.
    outcomes: dict[int, tuple[str, str]] = {}  # idx -> (bucket, detail)

    for idx, raw_line in candidates:
        stripped = raw_line.strip()
        parsed = parse_offline_line(raw_line)

        if not parsed["ok"]:
            reason = parsed["reason"]
            rejected.append({"line": stripped, "reason": reason})
            outcomes[idx] = ("rejected", reason)
            continue

        ts = parsed["ts"]
        line_actor = parsed["actor"]
        verb = parsed["verb"]
        item_id = parsed["item_id"]
        text = parsed["text"]

        h = _source_line_hash(line_actor, ts, verb, item_id, text or "")

        if h in hash_index:
            event_id = hash_index[h]
            already_imported.append({"line": stripped, "event_id": event_id})
            outcomes[idx] = ("reconciled", event_id)
            continue

        if item_id not in items:
            reason = f"unknown item-id: {item_id}"
            rejected.append({"line": stripped, "reason": reason})
            outcomes[idx] = ("rejected", reason)
            continue

        current_status = status_overlay.get(item_id, items[item_id].get("status"))

        if verb == "done" and current_status == "done":
            reason = f"skipped: item {item_id} already done"
            skipped.append({"line": stripped, "reason": reason})
            outcomes[idx] = ("rejected", reason)
            continue

        if verb == "park" and current_status == "done":
            reason = f"skipped: item {item_id} already done, cannot park"
            skipped.append({"line": stripped, "reason": reason})
            outcomes[idx] = ("rejected", reason)
            continue

        if dry_run:
            imported.append({
                "line": stripped, "item_id": item_id, "verb": verb, "actor": line_actor,
            })
            outcomes[idx] = ("reconciled", "(dry-run, not appended)")
            status_overlay[item_id] = "done" if verb == "done" else "parked"
            continue

        payload = {
            "item_id": item_id,
            "imported_by": _reconciler,
            "source": _DEFAULT_FILENAME,
            "source_line_hash": h,
            "imported_at": imported_at,
        }
        if verb == "done":
            etype = "item.completed"
            if text:
                payload["caption"] = text
        else:
            etype = "item.parked"
            payload["reason"] = text

        prev_id = _prev_for(line_actor)
        event = mint_event(
            seq=next_seq, ts=ts, actor=line_actor, etype=etype, payload=payload, prev=prev_id,
        )
        append_event(log_dir, event, actor=line_actor)
        actor_prev[line_actor] = event["id"]
        next_seq += 1
        status_overlay[item_id] = "done" if verb == "done" else "parked"
        hash_index[h] = event["id"]

        imported.append({
            "line": stripped, "event_id": event["id"], "item_id": item_id,
            "verb": verb, "actor": line_actor,
        })
        outcomes[idx] = ("reconciled", event["id"])

    if not dry_run and candidates:
        from ..projection import regenerate
        if imported:
            regenerate(repo_root)
            # .gitignore rule.
            from ..doctor import warn_if_log_ignored
            warn_if_log_ignored(repo_root)
        _rewrite_offline_file(offline_path, candidates, outcomes, preserved_lines, _reconciler)

    result = {
        "file": offline_path,
        "dry_run": dry_run,
        "imported": imported,
        "already_imported": already_imported,
        "rejected": rejected,
        "skipped": skipped,
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return

    mode = " (dry-run)" if dry_run else ""
    print(f"pinax reconcile{mode}: {offline_path}")
    print(f"  imported         : {len(imported)}")
    print(f"  already_imported : {len(already_imported)}")
    print(f"  skipped          : {len(skipped)}")
    print(f"  rejected         : {len(rejected)}")
    for entry in imported:
        marker = entry.get("event_id", "(dry-run)")
        print(f"    + {entry['verb']} {entry['item_id']} (actor={entry['actor']}) -> {marker}")
    for entry in skipped:
        print(f"    ~ {entry['line']} -- {entry['reason']}")
    for entry in rejected:
        print(f"    x {entry['line']} -- {entry['reason']}")


def _rewrite_offline_file(
    offline_path: str,
    candidates: list[tuple[int, str]],
    outcomes: dict[int, tuple[str, str]],
    preserved_lines: list[str],
    reconciler: str,
) -> None:
    """
    Never delete. Every candidate line processed this pass is moved into
    a dated Reconciled or Rejected section; prior dated sections and any
    other already-processed content (preserved_lines, position-independent
    per _split_file) are carried forward untouched.  Nothing stays
    unprocessed after a run that touched it -- new lines appended anywhere
    (including below existing sections -- the natural append-only workflow)
    naturally remain candidates for the next pass if not resolved this one.

    Each processed entry is written as TWO physical lines: the original line
    completely VERBATIM (never mutated, never suffixed -- so nothing about
    its own text, including an embedded "  => ", is ever altered), followed
    by a standalone "  => <detail>" marker line.  _split_file recognises
    this exact pair on a later pass regardless of the original line's shape
    (see its docstring for the pairing rule). A dated header for a
    bucket that has nothing filed under it THIS pass is never emitted -- no
    empty "## Reconciled"/"## Rejected" stubs.
    """
    reconciled_lines: list[str] = []
    rejected_lines: list[str] = []
    for idx, raw_line in candidates:
        bucket, detail = outcomes[idx]
        stripped = raw_line.strip()
        pair = [stripped, f"  => {detail}"]
        if bucket == "reconciled":
            reconciled_lines.extend(pair)
        else:
            rejected_lines.extend(pair)

    today = _utc_today()
    new_sections: list[str] = []
    if reconciled_lines:
        new_sections.append(f"## Reconciled {today} (by {reconciler})")
        new_sections.extend(reconciled_lines)
        new_sections.append("")
    if rejected_lines:
        new_sections.append(f"## Rejected {today}")
        new_sections.extend(rejected_lines)
        new_sections.append("")

    out_lines = new_sections + preserved_lines
    content = "\n".join(out_lines).strip("\n") + "\n"

    with open(offline_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
