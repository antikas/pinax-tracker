"""Read-only diagnosis for common tracker consistency conditions.

The doctor reports uncommitted event shards, stale claims, contradictions with
an optional legacy board, and event-log paths ignored by Git. It derives each
result from the repository and an explicit reference time, returns stable
ordering, and does not modify the log or projection.
"""
from __future__ import annotations

import datetime
import glob
import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys

from .fold import fold, read_raw_events, _sort_key
from .replay import ReplayRefError, read_raw_events_at_ref

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

DEFAULT_STALE_HOURS = 24

# Statuses that mean the claim trail is NOT dangling:
# - done/parked: terminal — the trail was closed out.
# - blocked: deliberately gated (a visible, intentional hold), not an
#   orphaned session trail.
_SETTLED_STATUSES = frozenset({"done", "parked", "blocked"})

_DEFAULT_LOG_SUBPATH = ".ergon/log"
_DEFAULT_ERGON_SUBPATH = ".ergon"


def hook_resolution_mode() -> str:
    """How the pre-commit hook would resolve Pinax in this process."""
    if shutil.which("pinax"):
        return "executable"
    if importlib.util.find_spec("pinax") is not None:
        return "module"
    return "absent"


def _editable_install_target() -> str | None:
    """
    Best-effort editable install target from direct_url.json.

    Returns None when Pinax is not installed as a distribution or when the
    install is not an editable local directory install.
    """
    try:
        dist = importlib.metadata.distribution("pinax")
    except importlib.metadata.PackageNotFoundError:
        return None
    direct = dist.read_text("direct_url.json")
    if not direct:
        return None
    try:
        data = json.loads(direct)
    except ValueError:
        return None
    if not data.get("dir_info", {}).get("editable"):
        return None
    url = data.get("url", "")
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(url)
        if parsed.netloc and parsed.path:
            return unquote(f"//{parsed.netloc}{parsed.path}")
        return unquote(parsed.path)
    return url or None


def install_health() -> dict:
    """Read-only install-health signals for `pinax doctor`."""
    stdout_errors = getattr(sys.stdout, "errors", None)
    stderr_errors = getattr(sys.stderr, "errors", None)
    return {
        "executable": shutil.which("pinax"),
        "executable_on_path": shutil.which("pinax") is not None,
        "hook_resolution_mode": hook_resolution_mode(),
        "console": {
            "stdout_encoding": getattr(sys.stdout, "encoding", None),
            "stdout_errors": stdout_errors,
            "stderr_encoding": getattr(sys.stderr, "encoding", None),
            "stderr_errors": stderr_errors,
            "replace_guard": stdout_errors == "replace" and stderr_errors == "replace",
        },
        "editable_install_target": _editable_install_target(),
    }


def parse_ts(ts: str) -> datetime.datetime | None:
    """Parse a pinax event timestamp; None when it does not match the format."""
    try:
        return datetime.datetime.strptime(ts, _TS_FMT)
    except (ValueError, TypeError):
        return None


def _run_git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a git subprocess in repo_root; None on any failure to launch."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def uncommitted_ergon_files(
    repo_root: str,
    ergon_subpath: str = _DEFAULT_ERGON_SUBPATH,
) -> list[dict] | None:
    """
    Working-tree `.ergon/` paths that differ from HEAD (modified, added, or
    untracked), from `git status --porcelain -z` — NUL-separated so paths
    containing spaces or quotable characters parse exactly.

    Returns a sorted list of {"path": <repo-relative, forward-slash>,
    "state": "untracked"|"modified"} — or None when git is unavailable /
    this is not a git repo (fail-safe: the caller reports the class as
    unavailable instead of guessing).
    """
    result = _run_git(
        repo_root, ["status", "--porcelain", "-z", "--", ergon_subpath]
    )
    if result is None or result.returncode != 0:
        return None
    fields = result.stdout.decode("utf-8", errors="replace").split("\0")
    entries: list[dict] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        if not field or len(field) < 4:
            i += 1
            continue
        code = field[:2]
        path = field[3:]
        entries.append(
            {
                "path": path,
                "state": "untracked" if code == "??" else "modified",
            }
        )
        # Rename/copy entries carry the source path as the NEXT NUL field.
        if code and code[0] in ("R", "C"):
            i += 2
        else:
            i += 1
    return sorted(entries, key=lambda e: e["path"])


def uncommitted_events(
    repo_root: str,
    log_dir: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> list[dict]:
    """
    Events present in the working-tree log shards but NOT reachable from
    HEAD's committed shards — the orphaned-trail event set.

    Computed as an id-set difference between two raw pools read through the
    SAME parsing layer (fold.parse_shard_bytes via read_raw_events /
    read_raw_events_at_ref) — never by diffing text.  An unborn HEAD (or a
    ref that predates `pinax init`) means nothing is committed yet, so every
    working-tree event is uncommitted — a valid outcome, not an error.

    Returns per-event summaries sorted by the fold's total-order key
    (seq, ts, actor, id), deduped by id:
      {"id", "seq", "ts", "actor", "type", "item_id", "shard"}
    """
    fs_raw = read_raw_events(log_dir)
    try:
        head_ids = {
            e["id"]
            for e in read_raw_events_at_ref(repo_root, "HEAD", log_subpath)
            if e.get("id")
        }
    except ReplayRefError:
        head_ids = set()

    seen: set[str] = set()
    out: list[dict] = []
    for event in sorted(fs_raw, key=_sort_key):
        eid = event.get("id")
        if eid is None or eid in head_ids or eid in seen:
            continue
        seen.add(eid)
        shard = event.get("_shard", "")
        if shard:
            shard = os.path.relpath(shard, repo_root).replace(os.sep, "/")
        out.append(
            {
                "id": eid,
                "seq": event.get("seq"),
                "ts": event.get("ts", ""),
                "actor": event.get("actor", ""),
                "type": event.get("type", ""),
                "item_id": event.get("payload", {}).get("item_id"),
                "shard": shard,
            }
        )
    return out


def stale_claims(
    state: dict,
    now: datetime.datetime,
    stale_hours: float = DEFAULT_STALE_HOURS,
) -> list[dict]:
    """
    Claim-without-done diagnosis: items with an owner (a winning
    item.claimed), a status that is neither terminal (done/parked) nor a
    deliberate hold (blocked), whose claim is older than `stale_hours`
    relative to the caller-supplied `now`.

    A claim whose claimed_at timestamp does not parse is ALWAYS flagged
    (age_hours None) — an unverifiable age remains a diagnostic finding.

    Pure function of (state, now, stale_hours) — no wall-clock here.
    Returns findings sorted by item id.
    """
    findings: list[dict] = []
    items = state.get("items", {})
    for item_id in sorted(items):
        item = items[item_id]
        owner = item.get("owner")
        if not owner:
            continue
        if item.get("status") in _SETTLED_STATUSES:
            continue
        claimed_at = item.get("claimed_at", "")
        parsed = parse_ts(claimed_at)
        if parsed is None:
            age_hours: float | None = None
        else:
            age_hours = round((now - parsed).total_seconds() / 3600.0, 1)
            if age_hours < stale_hours:
                continue
        findings.append(
            {
                "item_id": item_id,
                "owner": owner,
                "claimed_at": claimed_at,
                "age_hours": age_hours,
                "status": item.get("status", ""),
            }
        )
    return findings


_LOG_IGNORE_PROBE_NAME = "pinax-doctor-probe.jsonl"


def log_tracking_status(
    repo_root: str,
    log_dir: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> dict:
    """
    Diagnose class 4: is the event log currently being swallowed
    by a .gitignore rule?

    Two independent, fail-safe git probes:
      - `git check-ignore` against a SYNTHETIC probe filename inside the log
        directory (a filename that need not exist on disk) — this is the
        relevant condition: would a new shard file be
        silently git-ignored right now, regardless of whether any shard
        happens to exist yet.  This is the loud-fail signal (`ignored`).
      - `git ls-files` against the REAL on-disk `*.jsonl` shard files —
        informational only (the printed report's "N/M tracked" line).  A
        shard being merely uncommitted-but-not-ignored is class 1's
        territory, not this one's.

    Returns:
      {"available": bool, "ignored": bool, "probe_path": <repo-relative,
       forward-slash>, "shards_on_disk": int, "shards_tracked": int}

    Fail-safe: any git failure (no git binary, not a repo, or
    check-ignore's fatal exit 128 e.g. outside a work tree) degrades to
    {"available": False, "ignored": False, ...} — never a crash, matching
    every other doctor class.  `ignored` is only ever True when `available`
    is True.
    """
    probe_rel = f"{log_subpath}/{_LOG_IGNORE_PROBE_NAME}"
    shard_paths = sorted(glob.glob(os.path.join(log_dir, "*.jsonl")))

    probe = _run_git(repo_root, ["check-ignore", "-q", "--", probe_rel])
    # check-ignore exit codes: 0 = ignored, 1 = not ignored, 128 = fatal
    # (e.g. not a git repo at all) — anything else is "unavailable".
    if probe is None or probe.returncode not in (0, 1):
        return {
            "available": False,
            "ignored": False,
            "probe_path": probe_rel,
            "shards_on_disk": len(shard_paths),
            "shards_tracked": 0,
        }

    tracked = 0
    ls = _run_git(repo_root, ["ls-files", "-z", "--", log_subpath])
    if ls is not None and ls.returncode == 0:
        tracked = len(
            [p for p in ls.stdout.decode("utf-8", errors="replace").split("\0") if p]
        )

    return {
        "available": True,
        "ignored": probe.returncode == 0,
        "probe_path": probe_rel,
        "shards_on_disk": len(shard_paths),
        "shards_tracked": tracked,
    }


def warn_if_log_ignored(
    repo_root: str,
    log_dir: str | None = None,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
    stream=None,
) -> bool:
    """
    Print a one-line stderr warning if the event log
    would currently be swallowed by a .gitignore rule; return True iff it
    warned.

    Called once at the end of every state-changing command (add/claim/
    done/block/park/note/dep/annul/priority/dispatch/status/reconcile),
    immediately after the projection regenerate() — the natural "once per
    command invocation" point, matching the granularity of the read-side
    unmerged-branch warning.

    Fail-safe by construction (delegates to log_tracking_status): a git
    failure means "no warning", never an exception — this decorates
    ordinary state-changing commands and must never block or crash them.
    """
    _log_dir = log_dir if log_dir is not None else os.path.join(repo_root, *log_subpath.split("/"))
    status = log_tracking_status(repo_root, _log_dir, log_subpath)
    if not status["available"] or not status["ignored"]:
        return False
    out = stream if stream is not None else sys.stderr
    print(
        "pinax: WARNING - the event log is being swallowed by a .gitignore "
        f"rule ({status['probe_path']} would be git-ignored right now). "
        "This event is local-only and invisible to other worktrees/branches "
        "until fixed. Run 'pinax init' again to (re)install the "
        ".ergon/.gitignore negation, or fix your repo's .gitignore, then "
        "'pinax doctor' for the full diagnosis.",
        file=out,
    )
    return True


def _parse_frontmatter(text: str) -> dict:
    """
    Minimal frontmatter parse for legacy board files: the block between the
    leading '---' fence and the next '---' fence, split on the first ':' per
    line.  Values are stripped of surrounding quotes.  Anything that is not
    a well-formed fence-opened file returns {} (not a legacy board entry).
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fm
        if ":" in line:
            key, value = line.split(":", 1)
            fm[key.strip().lower()] = value.strip().strip('"').strip("'")
    return {}  # no closing fence — not a frontmatter block


def legacy_contradictions(
    repo_root: str,
    state: dict,
    legacy_board: str,
) -> list[dict]:
    """
    Cross-check a frozen legacy board (a directory of *.md files, or one
    file) against the pinax fold on migrated items.

    Matching: a legacy file whose frontmatter `id` equals a fold item id
    case-insensitively is a migrated item.  Contradiction rule (the observed
    failure class, kept deliberately narrow so vocabulary drift between the
    two systems — 'todo' vs 'queued' etc. — is never a false positive): the
    two sides disagree on DONE-ness.  Legacy status 'done' while pinax says
    anything else, or legacy anything-else while pinax says 'done'.

    Legacy files are frozen archives — this is diagnosis only; no
    ever writes to them.

    Returns findings sorted by (item_id, file).
    """
    paths: list[str] = []
    if os.path.isfile(legacy_board):
        paths = [legacy_board]
    elif os.path.isdir(legacy_board):
        paths = sorted(glob.glob(os.path.join(legacy_board, "*.md")))
    if not paths:
        return []

    items = state.get("items", {})
    by_lower = {iid.lower(): iid for iid in items}

    findings: list[dict] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        legacy_id = fm.get("id", "")
        legacy_status = fm.get("status", "").lower()
        if not legacy_id or not legacy_status:
            continue
        item_id = by_lower.get(legacy_id.lower())
        if item_id is None:
            continue  # not a migrated item — nothing to contradict
        pinax_status = items[item_id].get("status", "")
        if (legacy_status == "done") != (pinax_status == "done"):
            rel = os.path.relpath(path, repo_root).replace(os.sep, "/")
            findings.append(
                {
                    "item_id": item_id,
                    "legacy_id": legacy_id,
                    "legacy_status": legacy_status,
                    "pinax_status": pinax_status,
                    "file": rel,
                }
            )
    findings.sort(key=lambda f: (f["item_id"], f["file"]))
    return findings


def diagnose(
    repo_root: str,
    log_dir: str,
    now: datetime.datetime,
    stale_hours: float = DEFAULT_STALE_HOURS,
    legacy_board: str | None = None,
) -> dict:
    """
    Run all three diagnosis classes; read-only.

    `legacy_board`: explicit path (file or dir) enables class 3; when None,
    the conventional frozen legacy location `<repo_root>/board` is checked
    if it exists, else class 3 is skipped (checked=False).

    Returns:
      {
        "now": <ISO ts of the reference time used>,
        "stale_hours": <threshold>,
        "uncommitted": {"available": bool, "files": [...], "events": [...]},
        "stale_claims": [...],
        "legacy": {"checked": bool, "path": str|None, "contradictions": [...]},
        "log_tracking": {"available": bool, "ignored": bool, "probe_path": str,
                          "shards_on_disk": int, "shards_tracked": int},
        "findings": <total finding count across all four classes>,
      }
    """
    state = fold(log_dir)

    files = uncommitted_ergon_files(repo_root)
    if files is None:
        uncommitted = {"available": False, "files": [], "events": []}
    else:
        events = uncommitted_events(repo_root, log_dir) if files else []
        uncommitted = {"available": True, "files": files, "events": events}

    claims = stale_claims(state, now, stale_hours)

    log_tracking = log_tracking_status(repo_root, log_dir)

    legacy_path = legacy_board
    if legacy_path is None:
        default_board = os.path.join(repo_root, "board")
        if os.path.isdir(default_board):
            legacy_path = default_board
    if legacy_path is not None:
        legacy = {
            "checked": True,
            "path": os.path.relpath(legacy_path, repo_root).replace(os.sep, "/"),
            "contradictions": legacy_contradictions(repo_root, state, legacy_path),
        }
    else:
        legacy = {"checked": False, "path": None, "contradictions": []}

    findings = (
        len(uncommitted["files"])
        + len(claims)
        + len(legacy["contradictions"])
        + (1 if log_tracking["available"] and log_tracking["ignored"] else 0)
    )
    return {
        "now": now.strftime(_TS_FMT),
        "stale_hours": stale_hours,
        "install_health": install_health(),
        "uncommitted": uncommitted,
        "stale_claims": claims,
        "legacy": legacy,
        "log_tracking": log_tracking,
        "findings": findings,
    }
