"""
Pure status-view contract for `pinax status`.

This module is the CLI-free live push-down for the later integration adapter.
It reads the Pinax event log and local git visibility data at call time; it
never writes a projection, cache, or knowledge-plane status copy.
"""

from __future__ import annotations

import datetime
import os
import subprocess

from .fold import (
    BUILDING_STATUSES,
    claim_age_hours,
    compute_next,
    compute_ready,
    descendants,
    fold,
)
from .visibility import unmerged_tracker_refs

_SCHEMA = "pinax.status.v1"
# Statuses where a claim is no longer "live" for display purposes even if
# state["items"][id]["owner"] is still set - the item has moved past being
# actively worked (done) or is explicitly gated pending a human (parked,
# blocked); either way it belongs in its own bucket below, not "building".
_CLAIM_SETTLED_STATUSES = frozenset({"done", "parked", "blocked"})
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_ts(ts: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(ts, _TS_FMT)
    except (TypeError, ValueError):
        return None


def _coerce_now(now: datetime.datetime | str | None) -> datetime.datetime:
    if now is None:
        return datetime.datetime.utcnow()
    if isinstance(now, datetime.datetime):
        if now.tzinfo is not None:
            return now.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return now
    parsed = _parse_ts(now)
    if parsed is None:
        raise ValueError("now must be a UTC ISO timestamp like YYYY-MM-DDTHH:MM:SSZ")
    return parsed


def find_pinax_repo_root(start: str | None = None) -> str | None:
    """
    Walk upward from start (or cwd) until a directory containing .ergon/ is
    found.  This is deliberately .ergon-based, not .git-based: repo
    subdirectories count as Pinax repo scope.
    """
    candidate = os.path.abspath(os.path.expanduser(start or os.getcwd()))
    if os.path.isfile(candidate):
        candidate = os.path.dirname(candidate)
    while True:
        if os.path.isdir(os.path.join(candidate, ".ergon")):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent


def _git_branch(repo_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _repo_id(repo_root: str) -> str:
    return os.path.basename(os.path.normpath(os.path.abspath(repo_root))) or "repo"


def _notice_count(state: dict) -> int:
    superseded = state.get("claim_superseded")
    if isinstance(superseded, list):
        return len(superseded)
    warnings = state.get("report", {}).get("warnings", [])
    return sum(1 for w in warnings if isinstance(w, str) and w.startswith("claim.superseded:"))


def _branch_warnings(repo_root: str) -> list[str]:
    warnings: list[str] = []
    for branch, added in unmerged_tracker_refs(repo_root):
        suffix = f" (+{added} event{'s' if added != 1 else ''})" if added else ""
        warnings.append(
            f"unmerged branch {branch}{suffix}: current view is branch-scoped"
        )
    return warnings


def _item_title(items: dict, item_id: str | None) -> str:
    if not item_id:
        return ""
    return items.get(item_id, {}).get("title", "")


def _summarise_repo(
    repo_id: str,
    repo_root: str,
    *,
    since_days: int | None,
    all_branches: bool,
    now: datetime.datetime,
    under: str | None = None,
) -> dict:
    root = os.path.abspath(os.path.expanduser(repo_root))
    log_dir = os.path.join(root, ".ergon", "log")

    base = {
        "id": repo_id,
        "root": root,
        "branch": _git_branch(root),
        "building": [],
        "shipped_recent": [],
        "shipped_earlier_count": 0,
        "parked": [],
        "next": None,
        "queue_depth": 0,
        "warnings": [],
        "notices": 0,
    }
    if not os.path.isdir(log_dir):
        base["initialised"] = False
        base["warnings"] = ["not initialised: .ergon/log not found"]
        return base

    if all_branches:
        from .all_branches import compute_all_branches_fold

        folded = compute_all_branches_fold(root, log_dir)
        state = folded["state"]
        base["all_branches"] = True
        base["source_branches"] = folded.get("source_branches", [])
    else:
        state = fold(log_dir)
        base["warnings"] = _branch_warnings(root)

    items: dict = state.get("items", {})
    base["notices"] = _notice_count(state)

    # `under`, when given, restricts every list below (building, shipped,
    # parked) and the next/queue_depth figures to the transitive
    # parent-child descendants of that item. Raises ValueError on an
    # unknown ancestor - propagated to status_view()'s caller, which
    # already turns a ValueError into a message and a non-zero exit.
    scope: set[str] | None = descendants(state, under) if under is not None else None

    building = []
    shipped_recent = []
    shipped_earlier = 0
    parked = []

    threshold = None if since_days is None else now - datetime.timedelta(days=since_days)

    for item_id in sorted(items):
        if scope is not None and item_id not in scope:
            continue
        item = items[item_id]
        status = item.get("status", "queued")
        title = item.get("title", "")
        owner = item.get("owner")
        changed_at = item.get("status_changed_at", item.get("created_at", ""))

        # A live claim puts the item in the building list even before its
        # status field itself moves to 'building' - a claim and a status
        # change are two different events (see fold.compute_ready).
        claimed_active = bool(owner) and status not in _CLAIM_SETTLED_STATUSES
        if status in BUILDING_STATUSES or claimed_active:
            claimed_at = item.get("claimed_at", "")
            # An owned row is labelled by when the claim was made, not by
            # status_changed_at/created_at: age_hours is computed from the
            # same claimed_at, so the row reads one way instead of pairing
            # a creation time with a claim age. A building row with no
            # owner (status forced to 'building' without ever being
            # claimed) keeps the status/creation fallback -- it has no
            # claimed_at to show.
            row = {
                "id": item_id,
                "title": title,
                "stage": status,
                "owner": owner or "",
                "since": claimed_at if owner else changed_at,
                "age_hours": claim_age_hours(claimed_at, now) if owner else None,
            }
            # rollup is additive: present only on an item that carries one
            # (see fold._compute_rollups) - a leaf item's row is unchanged.
            if item.get("rollup"):
                row["rollup"] = item["rollup"]
            building.append(row)
        elif status == "done":
            entry = {
                "id": item_id,
                "title": title,
                "done_at": changed_at,
                "actor": item.get("status_changed_by", ""),
            }
            if item.get("rollup"):
                entry["rollup"] = item["rollup"]
            parsed = _parse_ts(changed_at)
            if threshold is None or (parsed is not None and parsed >= threshold):
                shipped_recent.append(entry)
            else:
                shipped_earlier += 1
        elif status in ("parked", "blocked"):
            row = {
                "id": item_id,
                "title": title,
                "kind": status,
                "reason": (
                    item.get("park_reason", "")
                    if status == "parked"
                    else item.get("gate", "")
                ),
            }
            if item.get("rollup"):
                row["rollup"] = item["rollup"]
            parked.append(row)

    building.sort(key=lambda it: (it.get("since", ""), it["id"]))
    shipped_recent.sort(key=lambda it: (it.get("done_at", ""), it["id"]), reverse=True)
    parked.sort(key=lambda it: (it["kind"], it["id"]))

    next_id = compute_next(state, under=under)
    ready = compute_ready(state, under=under)

    base["initialised"] = True
    base["building"] = building
    base["shipped_recent"] = shipped_recent
    base["shipped_earlier_count"] = shipped_earlier
    base["parked"] = parked
    base["next"] = {"id": next_id, "title": _item_title(items, next_id)} if next_id else None
    base["queue_depth"] = len(ready)
    return base


def _portfolio_repos(start: str | None) -> list[tuple[str, str]]:
    from .commands.overview import (
        _dedupe_physical_path,
        _dedupe_worktrees,
        _discover_repos,
        _resolve_max_depth,
        _resolve_roots,
        _scan_roots_for_ergon,
    )

    roots = _resolve_roots(None)
    depth = _resolve_max_depth(None)
    candidates = _dedupe_physical_path(_dedupe_worktrees(_scan_roots_for_ergon(roots, depth)))
    if not candidates:
        return []

    hub_root = find_pinax_repo_root(start) or candidates[0]
    registry: dict = {}
    hub_log = os.path.join(hub_root, ".ergon", "log")
    if os.path.isdir(hub_log):
        registry = fold(hub_log).get("registry", {})
    return _discover_repos(hub_root, registry, roots=roots, max_depth=depth)


def status_view(
    repo_root: str | None = None,
    scope: str = "auto",
    since_days: int | None = 7,
    all_branches: bool = False,
    now: datetime.datetime | str | None = None,
    under: str | None = None,
) -> dict:
    """
    Return the versioned status-view payload.

    scope:
      - "repo": repo_root (or cwd) must resolve upward to a Pinax repo.
      - "portfolio": scan configured roots and summarise each local Pinax repo.
      - "auto": repo scope when cwd/repo_root is inside a Pinax repo, else
        portfolio scope.

    `under`, when given, restricts the repo view's building/shipped/parked
    lists and its next/queue_depth figures to the transitive `parent-child`
    descendants of that item (see pinax.fold.descendants); an id naming no
    item in the resolved repo's fold raises ValueError. `under` requires
    repo scope: combining it with portfolio scope, explicit or
    auto-resolved, also raises ValueError, since a portfolio spans more
    than one tracker and an ancestor id is only meaningful inside one of
    them.
    """
    if scope not in {"auto", "repo", "portfolio"}:
        raise ValueError("scope must be one of: auto, repo, portfolio")

    start = repo_root or os.getcwd()
    pinned_now = _coerce_now(now)
    repo_match = find_pinax_repo_root(start)

    is_repo_scope = scope == "repo" or (scope == "auto" and repo_match is not None)

    if under is not None and not is_repo_scope:
        raise ValueError("--under requires repo scope, not --portfolio")

    if is_repo_scope:
        root = repo_match
        if root is None:
            raise ValueError(
                "No Pinax repo found. Use 'pinax status --portfolio' or run from a repo with .ergon/."
            )
        return {
            "schema": _SCHEMA,
            "scope": "repo",
            "under": under,
            "repo": _summarise_repo(
                _repo_id(root),
                root,
                since_days=since_days,
                all_branches=all_branches,
                now=pinned_now,
                under=under,
            ),
        }

    repos = _portfolio_repos(start)
    payload = {
        "schema": _SCHEMA,
        "scope": "portfolio",
        "under": None,
        "repos": [
            _summarise_repo(
                repo_id,
                path,
                since_days=since_days,
                all_branches=False,
                now=pinned_now,
            )
            for repo_id, path in repos
        ],
    }
    if not repos:
        payload["warnings"] = [
            "No Pinax repos discovered from PINAX_ROOTS or the default roots.",
            "Run from a repo with .ergon/ or set PINAX_ROOTS to portfolio roots.",
        ]
    return payload
