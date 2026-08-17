"""
pinax.projection — byte-deterministic Markdown projection.

ADR-002 compliance:
- Regenerated atomically with every state-changing command.
- Byte-deterministic: regenerate twice from the same log → byte-identical.
- Never hand-edited: the drift lint enforces this.
- LF line endings always (not CRLF) — even on Windows where the repo
  may use CRLF for other files.  The .ergon/** text eol=lf gitattribute
  keeps Git from converting the projection on checkout.
- No wall-clock in rendered output (no "generated at <timestamp>" headers).

Projection layout (DESIGN.md):
  .ergon/board.md          — all items grouped by status lane
  .ergon/items/<id>.md     — per-item file (frontmatter + briefing + audit trail)

Ordering (DESIGN.md):
  board.md: phase order (by opened_seq, then opened_at, then opened_by),
  then `next` order (compute_next priority), then id.
  A "## Parked / needs human" section lists gate-blocked + parked items.

Per-item files:
  YAML-ish frontmatter (no YAML library — stdlib-only),
  + completion briefing (work-record) if present,
  + audit trail from item.audit_result events.
"""

from __future__ import annotations

import json
import os

from .commands.dep import VALID_EDGE_TYPES
from .fold import compute_next, compute_ready, fold_events, read_events

# Deterministic display ordering for typed edges in per-item files (alphabetical).
# Derived from the single-source enum — adding a 6th type requires only dep.py.
_ALL_EDGE_TYPES = sorted(VALID_EDGE_TYPES)


# ---------------------------------------------------------------------------
# Status lane order for board.md (deterministic, not by insertion order)
# ---------------------------------------------------------------------------

_LANE_ORDER = [
    "queued",
    "ready",
    "building",
    "blind-verify",
    "adjudicate",
    "blocked",
    "parked",
    "done",
]


def _lane_index(status: str) -> int:
    """Return the board lane index for a status string."""
    try:
        return _LANE_ORDER.index(status)
    except ValueError:
        return len(_LANE_ORDER)  # unknown statuses go last


# ---------------------------------------------------------------------------
# Phase ordering helper (mirrors compute_next phase order)
# ---------------------------------------------------------------------------

def _phase_sort_key(phase_name: str, phases: dict) -> tuple:
    """
    Sort key for a phase name: (opened_seq, opened_at, opened_by).

    Mirrors the ordering in compute_next: seq is the primary
    key (total-order); ts and actor are secondary tie-breakers.
    """
    p = phases.get(phase_name, {})
    return (
        p.get("opened_seq", 0),
        p.get("opened_at", ""),
        p.get("opened_by", ""),
    )


def _sorted_phase_names(phases: dict) -> list[str]:
    """Return phase names sorted by total-order (seq, ts, actor)."""
    return sorted(phases.keys(), key=lambda n: _phase_sort_key(n, phases))


def _phase_index_for_item(item: dict, phases: dict, sorted_phases: list[str]) -> int:
    """
    Return the phase index for an item based on its prefix field.

    An item with prefix='phase-1' belongs to the 'phase-1' phase.
    Items with no matching phase get a high index (appended last).
    """
    prefix = item.get("prefix", "")
    try:
        return sorted_phases.index(prefix)
    except ValueError:
        return len(sorted_phases)


# ---------------------------------------------------------------------------
# Item sort key for board.md
# ---------------------------------------------------------------------------

def _board_item_sort_key(item_id: str, item: dict, phases: dict,
                         sorted_phases: list[str],
                         ready_set: set, next_id: str | None) -> tuple:
    """
    Deterministic sort key for an item in board.md.

    Ordering: (phase_idx, lane_idx, next_flag, created_at, event_id, id)

    - phase_idx: phase order (opened_seq total-order)
    - lane_idx: status lane order (queued→ready→building→... → done)
    - next_flag: 0 if this is the current `next` item (pinned first), 1 otherwise
    - created_at: ISO-8601 (sorts lexicographically)
    - event_id: blake2b hash (tie-break for same-second items)
    - id: lexicographic final tie-break

    The `next_flag` brings the highest-priority ready item to the top of its
    lane, matching the display intent.
    """
    phase_idx = _phase_index_for_item(item, phases, sorted_phases)
    lane_idx = _lane_index(item.get("status", "queued"))
    next_flag = 0 if item_id == next_id else 1
    created_at = item.get("created_at", "")
    event_id_val = item.get("event_id", "")
    return (phase_idx, lane_idx, next_flag, created_at, event_id_val, item_id)


# ---------------------------------------------------------------------------
# board.md renderer
# ---------------------------------------------------------------------------

def render_board(state: dict, item_sources: dict | None = None) -> str:
    """
    Render .ergon/board.md from the folded state.

    Byte-deterministic: no wall-clock, no RNG, no PYTHONHASHSEED dependence.
    Output is LF-terminated (b'\\n' joins), returned as a str.

    Layout:
      # Board
      ## <Status Lane>
      - <id> · <title> · <status> · <owner|—> · <blockers|—>
      ...
      ## Parked / needs human
      - <id> · <title> · <reason|gate>

    `item_sources` (`--all-branches` only): optional {item_id: [branch
    names]} map — when an item id is a key, its line gets a trailing
    " [from: branch1, branch2]" marker. Defaults to None, which reproduces the
    same output byte-for-byte (the committed .ergon/board.md
    projection and the plain `pinax board` path never pass this argument).
    """
    items = state.get("items", {})
    phases = state.get("phases", {})
    deps = state.get("deps", set())

    sorted_phases = _sorted_phase_names(phases)
    ready_set = set(compute_ready(state))
    next_id = compute_next(state)

    # Build a reverse blocker map: to_id → sorted list of from_ids
    blockers_of: dict[str, list[str]] = {}
    for (from_id, to_id) in deps:
        blockers_of.setdefault(to_id, []).append(from_id)
    # Sort the blocker lists for determinism.
    for to_id in blockers_of:
        blockers_of[to_id].sort()

    # Sort all items by board sort key.
    sorted_item_ids = sorted(
        items.keys(),
        key=lambda iid: _board_item_sort_key(
            iid, items[iid], phases, sorted_phases, ready_set, next_id
        ),
    )

    # Group by lane.
    lanes: dict[str, list[str]] = {}
    for iid in sorted_item_ids:
        status = items[iid].get("status", "queued")
        lanes.setdefault(status, []).append(iid)

    lines: list[str] = ["# Board", ""]

    # Emit non-parked, non-blocked lanes first (in lane order), then parked/blocked.
    parked_blocked_items: list[str] = []
    main_lanes = [s for s in _LANE_ORDER if s not in ("blocked", "parked")]
    extra_statuses = sorted(set(lanes.keys()) - set(_LANE_ORDER))

    def _item_line(iid: str) -> str:
        item = items[iid]
        owner = item.get("owner", item.get("created_by", "—")) or "—"
        blockers = blockers_of.get(iid, [])
        blocker_str = ", ".join(blockers) if blockers else "—"
        status = item.get("status", "queued")
        marker = " [next]" if iid == next_id else ""
        source_marker = ""
        if item_sources and iid in item_sources:
            source_marker = f" [from: {', '.join(item_sources[iid])}]"
        return (
            f"- {iid} · {item.get('title', '')} · {status}{marker} · "
            f"{owner} · {blocker_str}{source_marker}"
        )

    for status in main_lanes + extra_statuses:
        if status not in lanes:
            continue
        heading = status.capitalize().replace("-", " ").replace("_", " ")
        lines.append(f"## {heading}")
        lines.append("")
        for iid in lanes[status]:
            lines.append(_item_line(iid))
        lines.append("")

    # Parked / needs human section.
    for status in ("blocked", "parked"):
        if status in lanes:
            parked_blocked_items.extend(lanes[status])

    if parked_blocked_items:
        lines.append("## Parked / needs human")
        lines.append("")
        for iid in parked_blocked_items:
            item = items[iid]
            status = item.get("status", "")
            if status == "blocked":
                detail = f"gate={item.get('gate', '?')}"
            else:
                detail = item.get("park_reason", item.get("gate", "—"))
            lines.append(f"- {iid} · {item.get('title', '')} · {detail}")
        lines.append("")

    # Superseded claims warning section.
    superseded = state.get("claim_superseded", [])
    if superseded:
        lines.append("## Claim conflicts (resolved)")
        lines.append("")
        for sup in superseded:
            lines.append(
                f"- {sup['item_id']}: {sup['superseded_actor']} superseded by "
                f"{sup['winner_actor']}"
            )
        lines.append("")

    # Ensure trailing newline but no double-blank at end.
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _render_repo_sections(sorted_reports: list[dict]) -> tuple[list[str], list[str]]:
    """
    Shared per-repo section body (SSOT) for both `render_overview` (the CLI
    live view) and `render_overview_markdown` (the committed PORTFOLIO.md
    projection) — one rendering path, so the two views can never
    drift textually out of sync with each other.

    `sorted_reports` MUST already be sorted by repo id (both callers sort
    once, at their own single sort point, before calling this).

    Each repo_report dict has the shape:
      {
        "id": str, "path": str, "initialised": bool,
        "total_items": int, "by_status": {status: count},
        "next": {"id": str, "title": str} | None,
        "parked": [{"id", "title", "reason"}],
        "blocked": [{"id", "title", "gate"}],
      }
    "initialised" False means the repo has no .ergon/log yet (registered but
    not migrated) — reported explicitly, never silently omitted (mirrors the
    handling for un-migrated repos).

    Remote-sourced reports (`pinax overview --remote`) additionally
    carry:
      "url": str            — the git remote the numbers were folded from
      "sha": str | None     — the remote's PUSHED tip commit (None = nothing
                              pushed yet); rendered inline so the view itself
                              says exactly which published state it describes
                              (the PORTFOLIO.md stamp discipline, per repo)
      "error": str          — present INSTEAD of the summary fields when the
                              remote could not be fetched; rendered as an
                              "unreachable" line + a needs-attention entry —
                              reported explicitly, never silently dropped.
    Local reports never carry these keys, so their rendering is byte-for-byte
    unchanged.

    Returns (body_lines, needs_attention_lines):
      - body_lines: the `## <repo_id>` sections, one per repo, in order.
      - needs_attention_lines: the flat cross-repo parked/blocked rollup,
        ready to append as-is (callers render "(none)" themselves if empty).
    """
    lines: list[str] = []
    needs_attention: list[str] = []

    for report in sorted_reports:
        repo_id = report["id"]
        lines.append(f"## {repo_id}")
        lines.append("")

        if "error" in report:
            lines.append(f"- status: unreachable · {report['error']}")
            lines.append("")
            needs_attention.append(
                f"- {repo_id} · unreachable remote: {report.get('url', '(no url)')}"
            )
            continue

        if "url" in report:
            # numbers below describe (per-repo stamp discipline).  Only what
            # is pushed to the remote is visible here, by design.
            sha = report.get("sha")
            lines.append(
                f"- remote: {report['url']} @ {sha if sha else '(nothing pushed)'}"
            )

        if not report.get("initialised", False):
            lines.append(
                "- status: not initialised (no .ergon/log — not yet migrated to Pinax)"
            )
            lines.append("")
            continue

        by_status = report.get("by_status", {})
        status_str = " ".join(
            f"{k}={by_status[k]}" for k in sorted(by_status.keys())
        )
        total = report.get("total_items", 0)
        lines.append(f"- status: ok · {total} items · {status_str}")
        notices = report.get("notices", 0)
        if notices:
            lines.append(
                f"- notices: {notices} claim-reconciliation notice(s) - "
                "run `pinax doctor` for detail"
            )

        next_item = report.get("next")
        if next_item:
            lines.append(f"- next: {next_item['id']}  {next_item.get('title', '')}")
        else:
            lines.append("- next: (none — ready queue empty)")

        parked = report.get("parked", [])
        lines.append(f"- parked ({len(parked)}):" + ("" if parked else " (none)"))
        for p in parked:
            reason = p.get("reason", "") or "(no reason)"
            lines.append(f"  - {p['id']} · {p.get('title', '')} · reason: {reason}")
            needs_attention.append(
                f"- {repo_id}/{p['id']} · {p.get('title', '')} · parked: {reason}"
            )

        blocked = report.get("blocked", [])
        lines.append(f"- blocked ({len(blocked)}):" + ("" if blocked else " (none)"))
        for b in blocked:
            gate = b.get("gate", "") or "(no gate)"
            lines.append(f"  - {b['id']} · {b.get('title', '')} · gate: {gate}")
            needs_attention.append(
                f"- {repo_id}/{b['id']} · {b.get('title', '')} · blocked: gate={gate}"
            )

        lines.append("")

    return lines, needs_attention


def render_overview(repo_reports: list[dict]) -> str:
    """
    Render the portfolio rollup (`pinax overview`) from a list of per-repo
    summary dicts.  Pure function — no filesystem access, no wall-clock.

    Sorted by repo id (deterministic regardless of input order — callers may
    pass an unsorted list; this function is the single sort point, SSOT).

    Byte-deterministic: same input list -> same output string, always.
    """
    lines: list[str] = ["# Pinax portfolio overview", ""]

    sorted_reports = sorted(repo_reports, key=lambda r: r["id"])
    body_lines, needs_attention = _render_repo_sections(sorted_reports)
    lines.extend(body_lines)

    lines.append("## Needs attention (cross-repo)")
    lines.append("")
    if needs_attention:
        lines.extend(needs_attention)
    else:
        lines.append("(none)")
    lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def render_overview_markdown(repo_reports: list[dict], stamp: dict) -> str:
    """
    Render the committed PORTFOLIO.md projection — rung 2 of the view ladder
    (docs/portfolio-views.md). Renders the same
    per-repo body `render_overview` renders (SSOT via `_render_repo_sections`
    above), plus a stamp footer that bounds and makes visible how stale the
    committed file is.

    `stamp` shape (passed in, not computed here — this renderer stays a pure
    function of its inputs, the same discipline as every other renderer in
    this module; the one wall-clock read + the per-repo `git rev-parse HEAD`
    calls live one layer up, in pinax/commands/overview.py's `run()`):
      {
        "generated_at": "<ISO-8601 UTC timestamp, seconds precision,
                          e.g. 2026-07-04T12:00:00Z>",
        "shas": {repo_id: "<git HEAD sha>" | None, ...},
      }
    A `None` sha renders as "(no git)" — reported explicitly, never silently
    omitted (the same "report, don't drop" discipline `render_overview`
    already applies to un-initialised repos).

    Sorted by repo id throughout (bodies and the SHA list both use the same
    sort key) — byte-deterministic given identical (repo_reports, stamp).
    """
    lines: list[str] = [
        "# Pinax Portfolio",
        "",
        "_Generated by `pinax overview --markdown`. Do not hand-edit — "
        "regeneration is the only writer (see `hooks/post-commit`)._",
        "",
    ]

    sorted_reports = sorted(repo_reports, key=lambda r: r["id"])
    body_lines, needs_attention = _render_repo_sections(sorted_reports)
    lines.extend(body_lines)

    lines.append("## Needs attention (cross-repo)")
    lines.append("")
    if needs_attention:
        lines.extend(needs_attention)
    else:
        lines.append("(none)")
    lines.append("")

    # Stamp footer (docs/portfolio-views.md): bounds
    # staleness and makes it visible, never silently trusted.
    lines.append("---")
    lines.append("")
    lines.append(f"_Generated: {stamp.get('generated_at', '')}_")
    lines.append("")
    lines.append("Source SHAs:")
    shas: dict = stamp.get("shas", {})
    for repo_id in sorted(shas.keys()):
        sha = shas[repo_id]
        lines.append(f"- {repo_id}: {sha if sha else '(no git)'}")
    lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


# ---------------------------------------------------------------------------
# Per-item file renderer
# ---------------------------------------------------------------------------

def render_item(item_id: str, item: dict, state: dict) -> str:
    """
    Render .ergon/items/<id>.md from the folded item dict.

    Byte-deterministic: no wall-clock, no RNG.
    Output is LF-terminated, returned as a str.

    Layout:
      ---
      id: <id>
      title: <title>
      phase: <prefix>
      owner: <owner>
      status: <status>
      gate: <gate>
      deps: [<blockers>]
      cycle_home: <cycle-home if set>
      ---

      ## Briefing

      <briefing text if present>

      ## Edges

      ### blocks
      - from: <from_id>   (for edges where this item is to_id)
      - to: <to_id>       (for edges where this item is from_id)
      ...


      <audit result lines if present>

    Typed edges (all five types) are surfaced in the ## Edges section,
    grouped by type in deterministic order.  The board's blocker column stays
    blocks-only.  Readiness is blocks-only (compute_ready unchanged).
    """
    phases = state.get("phases", {})
    deps = state.get("deps", set())
    edges = state.get("edges", {})

    # Build blocker list for this item (blocks-only, for frontmatter compatibility).
    blockers = sorted(from_id for (from_id, to_id) in deps if to_id == item_id)

    # YAML-ish frontmatter (stdlib-only — no PyYAML).
    def _fm(key: str, value: str) -> str:
        # Simple scalar frontmatter line; no multiline values.
        # Quote values that contain ': ' or start with special YAML chars,
        # so the brain indexer's YAML parser does not reject the file.
        # We use double-quote wrapping with minimal escaping (only " and \).
        if value and (":" in value or value[0] in ('"', "'", "[", "]", "{", "}", ">", "|", "!", "&", "*", "#", "?", "|", "-", "<", "=", "%", "@", "`")):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'{key}: "{escaped}"'
        return f"{key}: {value}"

    owner = item.get("owner", "")
    gate = item.get("gate", "")
    cycle_home = item.get("cycle_home", "")
    prefix = item.get("prefix", "")

    fm_lines = [
        "---",
        _fm("id", item_id),
        _fm("title", item.get("title", "")),
        _fm("phase", prefix),
        _fm("owner", owner),
        _fm("status", item.get("status", "queued")),
    ]
    if gate:
        fm_lines.append(_fm("gate", gate))
    if blockers:
        fm_lines.append(f"deps: [{', '.join(blockers)}]")
    else:
        fm_lines.append("deps: []")
    if cycle_home:
        fm_lines.append(_fm("cycle_home", cycle_home))
    fm_lines.append("---")
    fm_lines.append("")

    lines: list[str] = fm_lines

    # Briefing section.
    briefing = item.get("briefing", "")
    lines.append("## Briefing")
    lines.append("")
    if briefing:
        lines.append(briefing.rstrip("\n"))
        lines.append("")
    else:
        lines.append("_(no briefing yet)_")
        lines.append("")

    # Edge type display order is derived from the single-source VALID_EDGE_TYPES enum
    # (module-level _ALL_EDGE_TYPES, sorted alphabetically).
    # For each type, list edges where this item is `from_id` (outgoing) or `to_id`
    # (incoming), each clearly labelled.
    # Only emit the section if any typed edges involve this item.

    has_any_edge = False
    edge_sections: list[str] = []
    for etype in _ALL_EDGE_TYPES:
        type_set = edges.get(etype, set())
        # Outgoing edges: this item is from_id.
        outgoing = sorted(to_id for (fid, to_id) in type_set if fid == item_id)
        # Incoming edges: this item is to_id.
        incoming = sorted(fid for (fid, tid) in type_set if tid == item_id)
        if outgoing or incoming:
            has_any_edge = True
            edge_sections.append(f"### {etype}")
            for tid in outgoing:
                edge_sections.append(f"- to: {tid}")
            for fid in incoming:
                edge_sections.append(f"- from: {fid}")
            edge_sections.append("")

    lines.append("## Edges")
    lines.append("")
    if has_any_edge:
        lines.extend(edge_sections)
    else:
        lines.append("_(no typed edges yet)_")
        lines.append("")

    audit_results = item.get("audit_results", [])
    lines.append("## Audit trail")
    lines.append("")
    if audit_results:
        for entry in audit_results:
            lines.append(f"- {entry}")
        lines.append("")
    else:
        lines.append("_(no audit results yet)_")
        lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


# ---------------------------------------------------------------------------
# Atomic projection regeneration
# ---------------------------------------------------------------------------

def regenerate(repo_root: str) -> None:
    """
    Regenerate the full projection atomically from the event log.

    Writes:
      .ergon/board.md
      .ergon/items/<id>.md  for every item in fold state

    All files are written with LF line endings (explicit newline='' +
    '\\n' in the content string), regardless of platform settings.
    The .ergon/** text eol=lf gitattribute keeps Git from altering them.

    Atomic semantics: both board.md and items/*.md are (over)written in
    the same Python invocation, so the working tree is never partially
    updated.  (True file-level atomicity would require temp-and-rename;
    the current approach is "same-invocation", which is sufficient for
    the single-process CLI use case.)

    Byte-deterministic: calling regenerate twice from the same log
    produces byte-identical output.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    items_dir = os.path.join(ergon_dir, "items")

    # Read and fold.
    events = read_events(log_dir)
    state = fold_events(events)

    items = state.get("items", {})

    # Ensure items/ directory exists.
    os.makedirs(items_dir, exist_ok=True)

    # Write board.md with LF line endings.
    board_path = os.path.join(ergon_dir, "board.md")
    board_content = render_board(state)
    with open(board_path, "w", newline="", encoding="utf-8") as fh:
        fh.write(board_content)

    # Write per-item files with LF line endings.
    for item_id, item in items.items():
        item_path = os.path.join(items_dir, f"{item_id}.md")
        item_content = render_item(item_id, item, state)
        with open(item_path, "w", newline="", encoding="utf-8") as fh:
            fh.write(item_content)

    # Remove stale item files (items that were in the projection but no longer
    # in the fold state — e.g. if an item was somehow deleted from the log,
    # which should not happen in practice but we keep the projection clean).
    if os.path.isdir(items_dir):
        for fname in os.listdir(items_dir):
            if fname.endswith(".md"):
                item_id_from_file = fname[:-3]
                if item_id_from_file not in items:
                    os.remove(os.path.join(items_dir, fname))
