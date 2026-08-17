"""Portfolio discovery and read-only reporting.

The command folds each discovered repository's `.ergon` log for every
invocation. It reads other repositories but writes only the optional
`PORTFOLIO.md` projection in the hub repository.

Repository discovery scans configured roots for `.ergon` directories. Registry
entries are optional: they can add repositories outside those roots or supply
an explicit identifier. Linked worktrees, symlink aliases, and bare repositories
are excluded or deduplicated so each repository appears once.

`--remote` reads the published tips of URL-bearing registry entries into a
temporary directory and folds their `.ergon` logs using the same deterministic
reader. It performs no remote writes. `--remote --markdown` is rejected because
`PORTFOLIO.md` is a local-fold projection.

`--json` emits repository summaries. `--markdown` writes `PORTFOLIO.md` in the
hub repository after computing the same local portfolio view.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from ..fold import fold, fold_events, compute_next
from ..projection import render_overview, render_overview_markdown

# Bounded-depth scan: enough to reach a repo nested one extra level under its
# root (e.g. ~/src/group/project/) without walking the whole disk.
_DEFAULT_MAX_DEPTH = 3

# Directories never worth descending into while scanning for `.ergon/` —
# git internals, Python/Node build/venv noise.  `.ergon` itself is excluded
# because a repo's own `.ergon/` subtree is never itself a nested repo.
_NOISE_DIRNAMES = {
    ".git",
    ".ergon",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
}


def _summarise_repo(repo_id: str, repo_path: str) -> dict:
    """
    Pure read: fold repo_path's .ergon/log (if present) into a portfolio
    summary dict.  Never writes anything, in this repo or any other.
    """
    log_dir = os.path.join(repo_path, ".ergon", "log")

    if not os.path.isdir(log_dir):
        return {"id": repo_id, "path": repo_path, "initialised": False}

    summary = _summarise_state(repo_id, fold(log_dir))
    summary["path"] = repo_path
    return summary


def _summarise_state(repo_id: str, state: dict) -> dict:
    """
    Pure function: summarise an already-folded state dict into the portfolio
    report shape (`_render_repo_sections`' input contract).  Shared by the
    local path (`_summarise_repo`, filesystem-folded) and the remote path
    (`_summarise_remote`, remote-tip-folded) — SSOT: one summary shape, one
    place that computes it.
    """
    items: dict = state.get("items", {})
    superseded = state.get("claim_superseded")
    if isinstance(superseded, list):
        notices = len(superseded)
    else:
        warnings = state.get("report", {}).get("warnings", [])
        notices = sum(
            1 for w in warnings
            if isinstance(w, str) and w.startswith("claim.superseded:")
        )

    by_status: dict[str, int] = {}
    for item in items.values():
        status = item.get("status", "queued")
        by_status[status] = by_status.get(status, 0) + 1

    next_id = compute_next(state)
    next_item = None
    if next_id:
        it = items.get(next_id, {})
        next_item = {"id": next_id, "title": it.get("title", "")}

    parked = sorted(
        (
            {"id": it["id"], "title": it.get("title", ""), "reason": it.get("park_reason", "")}
            for it in items.values()
            if it.get("status") == "parked"
        ),
        key=lambda x: x["id"],
    )
    blocked = sorted(
        (
            {"id": it["id"], "title": it.get("title", ""), "gate": it.get("gate", "")}
            for it in items.values()
            if it.get("status") == "blocked"
        ),
        key=lambda x: x["id"],
    )

    return {
        "id": repo_id,
        "initialised": True,
        "total_items": len(items),
        "by_status": by_status,
        "next": next_item,
        "parked": parked,
        "blocked": blocked,
        "notices": notices,
    }


def _summarise_remote(repo_id: str, url: str, scratch_dir: str, fetcher=None) -> dict:
    """
    Fetch one remote's published-tip events (Git transport, GitHub
    contents-API fallback — pinax.remote.fetch_remote_events) and fold them
    through the same fold as everything else.  Network READ only; the only
    local write is inside the per-remote scratch subdirectory the caller
    deletes after the run.

    Report shape: the `_summarise_state` shape plus {"url", "sha"} — the sha
    is the remote's published tip, so the render itself shows exactly which
    pushed state the numbers describe (the same bounded-and-visible-freshness
    discipline as PORTFOLIO.md's stamp).  An unreachable remote returns
    {"id", "url", "error"} — reported explicitly, never silently dropped.

    `scratch_dir` is this remote's own subdirectory of the per-run scratch
    (allocated by index in `run()`, never derived from the repo_id — a folded
    repo_id is not a path-validated value).  `fetcher` is injectable for
    tests only.
    """
    from ..remote import RemoteFetchError, fetch_remote_events

    _fetch = fetcher if fetcher is not None else fetch_remote_events
    os.makedirs(scratch_dir, exist_ok=True)
    try:
        fetched = _fetch(url, scratch_dir)
    except RemoteFetchError as exc:
        return {"id": repo_id, "url": url, "error": str(exc)}

    if not fetched.get("has_log"):
        return {
            "id": repo_id,
            "url": url,
            "sha": fetched.get("sha"),
            "initialised": False,
        }

    summary = _summarise_state(repo_id, fold_events(fetched["events"]))
    summary["url"] = url
    summary["sha"] = fetched.get("sha")
    return summary


def _remote_manifest(registry: dict) -> list[tuple[str, str]]:
    """
    The manifest of remotes `overview --remote` folds: every url-bearing
    registry entry, as a deterministic (repo_id, url) list sorted by repo_id.
    Path-only entries (local overrides) are not remotes and are excluded.
    """
    return [
        (repo_id, registry[repo_id]["url"])
        for repo_id in sorted(registry.keys())
        if registry[repo_id].get("url")
    ]


def _has_ergon(path: str) -> bool:
    return os.path.isdir(os.path.join(path, ".ergon"))


def _scan_dir(path: str, max_depth: int, found: list[str], depth: int) -> None:
    """
    Depth-bounded recursive scan for `.ergon/` directories.  A directory that
    itself has `.ergon/` is recorded and NOT recursed into further (a repo's
    own internal subtree is never itself a nested repo).
    Sorted os.listdir() at every level so the walk order (and hence the
    result order) is filesystem-listing-independent — a load-bearing part of
    the determinism contract.  Symlinked directories are not followed (avoids
    cycles from a symlinked root back into itself).
    """
    if _has_ergon(path):
        found.append(path)
        return
    if depth >= max_depth:
        return
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return
    for entry in entries:
        if entry in _NOISE_DIRNAMES:
            continue
        child = os.path.join(path, entry)
        if os.path.islink(child):
            continue
        if os.path.isdir(child):
            _scan_dir(child, max_depth, found, depth + 1)


def _scan_roots_for_ergon(roots: list[str], max_depth: int) -> list[str]:
    """Scan every root for `.ergon/`-containing directories, bounded depth."""
    found: list[str] = []
    for root in roots:
        abs_root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(abs_root):
            continue
        _scan_dir(abs_root, max_depth, found, depth=0)
    return found


def _git_query(path: str, *args: str) -> str | None:
    """Run a read-only `git -C <path> <args>`; None on any failure (no git,
    not a repo, unsupported flag on an old git version — all treated as
    'no signal', never fatal to the scan)."""
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_abs_path_query(path: str, rev_parse_flag: str) -> str | None:
    """
    `git rev-parse --path-format=absolute <flag>`, with a fallback for git
    versions that predate --path-format (git < 2.31): re-resolve a relative
    result against `path` ourselves.
    """
    val = _git_query(path, "rev-parse", "--path-format=absolute", rev_parse_flag)
    if val is None:
        val = _git_query(path, "rev-parse", rev_parse_flag)
        if val is None:
            return None
        if not os.path.isabs(val):
            val = os.path.normpath(os.path.join(path, val))
    return os.path.normcase(os.path.normpath(val))


def _is_bare_repo(path: str) -> bool:
    return _git_query(path, "rev-parse", "--is-bare-repository") == "true"


def _repo_head_sha(path: str) -> str | None:
    """
    Read-only `git rev-parse HEAD` for a discovered repo's PORTFOLIO.md stamp
    entry. Returns None if
    `path` is not a git repo, has no commits yet, or git is unavailable —
    rendered explicitly as "(no git)" by render_overview_markdown, never
    silently dropped.  Reuses `_git_query` (SSOT: one git-invocation helper).
    """
    return _git_query(path, "rev-parse", "HEAD")


def _physical_path(path: str) -> str:
    """
    Resolve `path` to its case-normalised RESOLVED PHYSICAL path -- the
    on-disk identity a directory junction or POSIX symlink ultimately points
    at. This
    is the grouping key `_dedupe_physical_path` and the cross-stage
    `seen_paths` check in `_discover_repos` use instead of a plain
    `normcase(abspath)`, so a repo reached two different ways (through an
    alias and directly) collapses to one identity.

    `os.path.realpath()` is the right primitive here, NOT `os.path.islink()`:
    verified empirically on this Python/Windows combination (mklink /J
    fixture) that `os.path.islink()` returns False for an NTFS junction --
    junctions use the IO_REPARSE_TAG_MOUNT_POINT reparse tag, not
    IO_REPARSE_TAG_SYMLINK, so the symlink-specific check misses them --
    while `os.path.realpath()` correctly resolves a junction to its target
    (via GetFinalPathNameByHandle under the hood).  realpath is also correct
    on POSIX symlinks, and a safe no-op (beyond normalisation) on a plain
    unaliased path, so it is the single primitive used on both platforms --
    no `os.path.samefile` fallback is needed (samefile requires both paths to
    already exist and is inherently pairwise/O(n^2) for a candidate list;
    realpath gives an O(1)-per-path groupable key).
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _dedupe_physical_path(candidates: list[str]) -> list[str]:
    """
    Fold candidate paths that resolve to the SAME on-disk directory -- a
    directory-junction or symlink alias of the same physical repo to one
    entry. Mirrors
    `_dedupe_worktrees`'s grouping-key-plus-tie-break shape one layer up:
    physical on-disk identity instead of git-common-dir identity.

    Rule (documented, since none is dictated -- same discipline as
    `_dedupe_worktrees`):
    - Grouping key: `_physical_path()` -- the case-normalised,
      junction/symlink-resolved absolute path.
    - Within a group, the survivor is the alphabetically-first candidate by
      `os.path.normcase(os.path.abspath(path))` -- the SCANNED (pre-resolve)
      path string, not the resolved one, so the surviving entry is one of the
      actually-scanned candidate paths -- exact parity with
      `_dedupe_worktrees`'s deterministic tie-break.
    - The output list is itself sorted by that same key, so the result (which
      candidate survives per group, AND the order they come back in) is
      independent of the order `candidates` was passed in -- same input set,
      same output, regardless of scan order.
    """
    groups: dict[str, list[str]] = {}
    for path in candidates:
        groups.setdefault(_physical_path(path), []).append(path)

    survivors = [
        sorted(paths, key=lambda p: os.path.normcase(os.path.abspath(p)))[0]
        for paths in groups.values()
    ]
    return sorted(survivors, key=lambda p: os.path.normcase(os.path.abspath(p)))


def _dedupe_worktrees(candidates: list[str]) -> list[str]:
    """
    Fold linked git worktrees of the same repo to ONE entry (the primary/main
    worktree). A naive scan would otherwise double-count them.

    Rule (documented, since none is dictated):
    - A bare repo (`git rev-parse --is-bare-repository` == "true") is
      excluded outright — a bare repo has no working tree, so a `.ergon/`
      directory physically present under one is a scratch/degenerate
      artefact, never a portfolio entry.
    - Grouping key: `git rev-parse --git-common-dir` — identical for the
      main worktree and every linked worktree of the same repo.
    - Within a group, the PRIMARY is the worktree whose `--git-dir` equals
      the common dir (the main worktree).  If none of the scanned candidates
      IS the main worktree (only linked worktrees were found under the
      scanned roots), the alphabetically-first candidate path wins — a
      deterministic, documented tie-break.
    - A candidate that is not a git repo at all (no signal from
      --git-common-dir) passes through untouched: Pinax does not require an
      `.ergon/`-holding directory to be a git repo.
    """
    groups: dict[str, list[str]] = {}
    passthrough: list[str] = []

    for path in candidates:
        if _is_bare_repo(path):
            continue
        common = _git_abs_path_query(path, "--git-common-dir")
        if common is None:
            passthrough.append(path)
            continue
        groups.setdefault(common, []).append(path)

    result: list[str] = list(passthrough)
    for common, paths in groups.items():
        if len(paths) == 1:
            result.append(paths[0])
            continue
        ordered = sorted(paths, key=lambda p: os.path.normcase(os.path.abspath(p)))
        primary = None
        for candidate in ordered:
            if _git_abs_path_query(candidate, "--git-dir") == common:
                primary = candidate
                break
        result.append(primary if primary is not None else ordered[0])

    return result


def _repo_id_for_path(path: str, used_ids: set) -> str:
    """
    Derive a deterministic repo id from a scanned path's basename.  Collision
    with an id already in use (two scanned repos sharing a basename under
    different parents) is resolved by qualifying with the parent directory
    name, then a numeric suffix — always the same result for the same input,
    since callers process candidates in a fixed sorted order.
    """
    norm = os.path.normpath(os.path.abspath(path))
    base = os.path.basename(norm) or "repo"
    if base not in used_ids:
        return base
    parent = os.path.basename(os.path.dirname(norm))
    qualified = f"{parent}-{base}" if parent else base
    if qualified not in used_ids:
        return qualified
    n = 2
    candidate = f"{qualified}-{n}"
    while candidate in used_ids:
        n += 1
        candidate = f"{qualified}-{n}"
    return candidate


def _discover_repos(
    repo_root: str,
    registry: dict,
    roots: list[str] | None = None,
    max_depth: int | None = None,
) -> list[tuple[str, str]]:
    """
    Build the deterministic (id, path) list to fold:
      1. the hub repo, always first;
      2. registry entries (explicit override/extra-roots — an id chosen here
         wins over an auto-derived scan id on a path collision);
      3. root-scan discovery (skipped entirely when `roots` is
         None/empty — callers that want the production default must resolve
         it explicitly via `_resolve_roots()`, keeping this function a pure,
         fully-deterministic function of its explicit arguments).
    All three stages dedupe by resolved PHYSICAL path (`_physical_path()`,
    junction/symlink-resolved: a directory-junction alias must not double-count a
    repo reached both ways, whether the collision is hub-vs-registry,
    hub-vs-scan, or registry-vs-scan); stage 3 additionally dedupes linked
    git worktrees to their primary (`_dedupe_worktrees`) and physically
    -aliased scan hits to one candidate (`_dedupe_physical_path`), and drops
    bare repos.
    """
    hub_id = os.path.basename(os.path.normpath(os.path.abspath(repo_root))) or "hub"
    hub_abs = _physical_path(repo_root)

    seen_paths = {hub_abs}
    repos: list[tuple[str, str]] = [(hub_id, repo_root)]
    used_ids = {hub_id}

    for repo_id in sorted(registry.keys()):
        entry = registry[repo_id]
        path = entry.get("path", "")
        if not path:
            # (folded by `overview --remote`), not a local repo — nothing to
            # fold here, and abspath("") would mis-resolve to the CWD.
            continue
        abs_path = _physical_path(path)
        if abs_path in seen_paths:
            continue
        seen_paths.add(abs_path)
        repos.append((repo_id, path))
        used_ids.add(repo_id)

    if roots:
        depth = max_depth if max_depth is not None else _DEFAULT_MAX_DEPTH
        candidates = _scan_roots_for_ergon(roots, depth)
        candidates = _dedupe_worktrees(candidates)
        candidates = _dedupe_physical_path(candidates)
        for path in sorted(candidates, key=lambda p: os.path.normcase(os.path.abspath(p))):
            abs_path = _physical_path(path)
            if abs_path in seen_paths:
                continue
            seen_paths.add(abs_path)
            repo_id = _repo_id_for_path(path, used_ids)
            used_ids.add(repo_id)
            repos.append((repo_id, path))

    return repos


def _default_roots() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, "src")]


def _resolve_roots(roots: list[str] | None) -> list[str]:
    """
    CLI flag > PINAX_ROOTS env var > built-in default (~/src).  `roots=[]`
    (explicitly empty, as opposed to None) is
    honoured as "no scan roots" — used by callers that want registry/hub-only
    discovery (e.g. isolated tests) without falling through to the default.
    """
    if roots is not None:
        return roots
    env_val = os.environ.get("PINAX_ROOTS")
    if env_val:
        return [r.strip() for r in env_val.split(",") if r.strip()]
    return _default_roots()


def _resolve_max_depth(max_depth: int | None) -> int:
    if max_depth is not None:
        return max_depth
    env_val = os.environ.get("PINAX_ROOTS_MAX_DEPTH")
    if env_val:
        try:
            return int(env_val)
        except ValueError:
            pass
    return _DEFAULT_MAX_DEPTH


def run(
    repo_root: str,
    as_json: bool = False,
    as_markdown: bool = False,
    roots: list[str] | None = None,
    max_depth: int | None = None,
    remote: bool = False,
    remote_fetcher=None,
) -> None:
    """
    Execute pinax overview in repo_root (the hub repo).  Pure read fold over
    every discovered repo's log; the ONLY filesystem write this command ever
    performs is `PORTFOLIO.md` at repo_root itself, and only under
    `as_markdown=True` — it never writes into any discovered
    repo's working tree, in either mode.

    Repo discovery: root-scan first (`roots`/`max_depth` resolved
    CLI-flag > env-var > default, see `_resolve_roots`/`_resolve_max_depth`),
    the registry is an additive override/extra-roots supplement.  Pass
    `roots=[]` explicitly to disable scanning (registry/hub-only).

    `remote=True`: fold the url-bearing registry entries' PUSHED
    remote tips instead of any local clone — see the module docstring for the
    manifest/determinism/freshness contract.  Only what is pushed is visible;
    the hub's own local log participates solely as the manifest source.
    No root scan happens in remote mode (`roots`/`max_depth` are ignored).
    The per-run scratch clones live in a temp dir removed before returning —
    no local cache can bleed into a later render.  `remote_fetcher` is
    injectable for tests only.

    Flag precedence:
    `as_json` composes with `remote`; `as_json` is checked first and wins
    over `as_markdown`; `remote` + `as_markdown` is REJECTED with exit 1 —
    PORTFOLIO.md is the committed local-fold projection
    (docs/portfolio-views.md), and
    silently re-pointing it at remote state would change what the committed
    file means.
    """
    if remote and as_markdown:
        print(
            "pinax: --remote cannot be combined with --markdown - PORTFOLIO.md "
            "is the committed local-fold projection (docs/portfolio-views.md); the "
            "remote fold renders to stdout (plain or --json) only.",
            file=sys.stderr,
        )
        sys.exit(1)

    log_dir = os.path.join(repo_root, ".ergon", "log")
    if not os.path.isdir(log_dir):
        print(
            "pinax: .ergon/log/ not found - run 'pinax init' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    hub_state = fold(log_dir)
    registry: dict = hub_state.get("registry", {})

    if remote:
        manifest = _remote_manifest(registry)
        if not manifest:
            print(
                "pinax: no remotes registered - add one with "
                "'pinax registry add --id <id> --url <git remote url>'.",
                file=sys.stderr,
            )
        scratch_root = tempfile.mkdtemp(prefix="pinax-remote-")
        try:
            reports = [
                _summarise_remote(
                    repo_id, url,
                    os.path.join(scratch_root, f"r{index}"),
                    fetcher=remote_fetcher,
                )
                for index, (repo_id, url) in enumerate(manifest)
            ]
        finally:
            shutil.rmtree(scratch_root, ignore_errors=True)

        if as_json:
            print(json.dumps({"repos": reports}, sort_keys=True, ensure_ascii=True))
            return
        sys.stdout.write(render_overview(reports))
        return

    resolved_roots = _resolve_roots(roots)
    resolved_depth = _resolve_max_depth(max_depth)
    repos = _discover_repos(repo_root, registry, roots=resolved_roots, max_depth=resolved_depth)
    reports = [_summarise_repo(repo_id, path) for repo_id, path in repos]

    if as_json:
        print(json.dumps({"repos": reports}, sort_keys=True, ensure_ascii=True))
        return

    if as_markdown:
        # The one wall-clock read + one git query per repo for this command
        # stays a function of its (repo_reports, stamp) inputs only.
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        shas = {repo_id: _repo_head_sha(path) for repo_id, path in repos}
        stamp = {"generated_at": generated_at, "shas": shas}
        content = render_overview_markdown(reports, stamp)

        out_path = os.path.join(repo_root, "PORTFOLIO.md")
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            fh.write(content)

        print(
            f"pinax: wrote PORTFOLIO.md ({len(reports)} repo(s), "
            f"generated {generated_at})"
        )
        return

    sys.stdout.write(render_overview(reports))
