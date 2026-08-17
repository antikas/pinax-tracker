"""Read event logs from repository remotes for `pinax overview --remote`.

The module obtains remote `.ergon` shards through Git or, for GitHub-hosted
repositories, the contents API fallback. It passes their bytes to the shared
Pinax parser and fold implementation. Remote reads use temporary local storage
and never push or modify a remote repository.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request

from .fold import finalise_events, parse_shard_bytes
from .replay import (
    ReplayRefError,
    _list_shard_paths,
    _read_blob,
    _resolve_commit,
)

_LOG_SUBPATH = ".ergon/log"

# Generous ceiling for one shallow clone — a hung transport must fail loudly,
# never hang the whole portfolio render.
_CLONE_TIMEOUT_SECONDS = 300

# Per-request ceiling for the contents-API fallback.
_HTTP_TIMEOUT_SECONDS = 30

_GITHUB_URL_PATTERNS = (
    # https://github.com/owner/repo[.git][/]
    re.compile(r"^https?://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"),
    # git@github.com:owner/repo[.git]
    re.compile(r"^git@github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?$"),
    # ssh://git@github.com/owner/repo[.git]
    re.compile(r"^ssh://git@github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"),
)


class RemoteFetchError(Exception):
    """Raised when a remote's events cannot be fetched (either transport)."""


def parse_github_url(url: str) -> tuple[str, str] | None:
    """
    Return (owner, repo) if `url` is a GitHub remote URL (https / ssh / scp
    form), else None.  None means "not GitHub" — the contents-API fallback
    only exists for GitHub-hosted remotes.
    """
    for pattern in _GITHUB_URL_PATTERNS:
        m = pattern.match(url.strip())
        if m:
            return m.group("owner"), m.group("repo")
    return None


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def _ls_remote_heads(url: str) -> tuple[str | None, dict[str, str]]:
    """
    `git ls-remote --symref <url> HEAD refs/heads/*` — discover the remote's
    default branch (its HEAD symref target) and the published branch tips,
    without fetching anything.

    Returns (head_symref_target | None, {refname: sha} for refs/heads/*).
    Raises RemoteFetchError when the remote cannot be contacted at all.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--symref", "--", url, "HEAD", "refs/heads/*"],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteFetchError(f"git ls-remote failed for {url!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RemoteFetchError(
            f"git ls-remote failed for {url!r}"
            + (f": {stderr}" if stderr else "")
        )

    symref: str | None = None
    heads: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        left, right = parts
        if left.startswith("ref: ") and right == "HEAD":
            symref = left[len("ref: "):].strip()
        elif right.startswith("refs/heads/"):
            heads[right] = left.strip()
    return symref, heads


def _pick_remote_branch(symref: str | None, heads: dict[str, str]) -> str | None:
    """
    The branch the remote fold follows: the remote's default branch (HEAD
    symref) when it resolves to a published branch; else a DOCUMENTED
    deterministic fallback for a misconfigured/dangling remote HEAD —
    refs/heads/main, then refs/heads/master, then the alphabetically-first
    published branch (same documented-tie-break discipline as the worktree
    dedup rule in DESIGN.md). None = nothing published at all (empty remote).
    """
    if not heads:
        return None
    if symref and symref in heads:
        return symref
    for candidate in ("refs/heads/main", "refs/heads/master"):
        if candidate in heads:
            return candidate
    return sorted(heads.keys())[0]


def fetch_remote_git(url: str, scratch_dir: str) -> dict:
    """
    Fetch a remote's published-tip `.ergon/log` events via git.

    Resolves the branch to follow via `git ls-remote --symref` (the remote's
    default branch; deterministic fallback per `_pick_remote_branch` when the
    remote HEAD dangles), then clones shallow (--depth=1), blob-filtered
    (--filter=blob:none, gracefully ignored by servers without partial-clone
    support) and --no-checkout into `scratch_dir`/clone, and reads the shard
    blobs through pinax.replay's git-blob readers — the same determinism
    layer `pinax replay --at` uses.

    Returns {"sha": <tip sha> | None, "has_log": bool, "events": [...]}.
    An EMPTY remote (no branch pushed yet) returns sha=None, has_log=False,
    events=[] — honestly "nothing published", not an error.

    Raises RemoteFetchError when the remote cannot be contacted or cloned.
    """
    symref, heads = _ls_remote_heads(url)
    branch_ref = _pick_remote_branch(symref, heads)
    if branch_ref is None:
        # Reachable remote, but nothing published yet.
        return {"sha": None, "has_log": False, "events": []}
    branch = branch_ref[len("refs/heads/"):]

    clone_dir = os.path.join(scratch_dir, "clone")
    try:
        result = subprocess.run(
            [
                "git", "clone",
                "--depth=1",
                "--filter=blob:none",
                "--no-checkout",
                "--quiet",
                "--branch", branch,
                "--", url, clone_dir,
            ],
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteFetchError(f"git clone failed for {url!r}: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RemoteFetchError(
            f"git clone failed for {url!r}"
            + (f": {stderr}" if stderr else "")
        )

    try:
        sha = _resolve_commit(clone_dir, "HEAD")
    except ReplayRefError as exc:
        raise RemoteFetchError(
            f"cloned {url!r} but could not resolve branch {branch!r} tip: {exc}"
        ) from exc

    shard_paths = _list_shard_paths(clone_dir, sha, _LOG_SUBPATH)
    raw_events: list[dict] = []
    for shard_path in shard_paths:
        raw = _read_blob(clone_dir, sha, shard_path)
        for event in parse_shard_bytes(raw, shard_id=shard_path):
            if event.get("id") is None:
                continue  # same id-required guard as replay.read_events_at_ref
            raw_events.append(event)

    return {
        "sha": sha,
        "has_log": bool(shard_paths),
        "events": finalise_events(raw_events),
    }


# ---------------------------------------------------------------------------
# Transport 2: GitHub contents API (read-only fallback, GitHub remotes only)
# ---------------------------------------------------------------------------

def _default_http_get(url: str) -> tuple[int, bytes]:
    """
    One HTTP GET.  Returns (status, body_bytes); HTTP error statuses are
    RETURNED (not raised) so callers can decide (a 404 on `.ergon/log` means
    "not initialised", not a failure).  Network-level errors raise
    RemoteFetchError.  Unauthenticated by default; a GITHUB_TOKEN env var is
    honoured if present (lifts the unauthenticated rate limit — never
    required).
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "pinax-overview-remote",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RemoteFetchError(f"HTTP GET failed for {url!r}: {exc}") from exc


def _api_json(http_get, url: str) -> dict | list:
    """GET + parse a GitHub API JSON response; typed errors for the caller."""
    status, body = http_get(url)
    if status in (403, 429):
        raise RemoteFetchError(
            f"GitHub API rate limit / forbidden ({status}) for {url!r} — "
            "unauthenticated requests are limited; set GITHUB_TOKEN to lift "
            "the limit, or use a git-reachable remote URL."
        )
    if status == 404:
        raise _ApiNotFound(url)
    if status != 200:
        raise RemoteFetchError(f"GitHub API returned {status} for {url!r}")
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RemoteFetchError(f"GitHub API returned unparseable JSON for {url!r}") from exc


class _ApiNotFound(Exception):
    """Internal: a 404 from the contents API (path or repo absent)."""


def fetch_remote_github_api(url: str, http_get=None) -> dict:
    """
    Fetch a GitHub-hosted remote's tip `.ergon/log` events via the contents
    API (read-only; no git required).  Same return shape as fetch_remote_git:
    {"sha": <tip sha> | None, "has_log": bool, "events": [...]}.

    The tip sha is resolved explicitly (repo default branch → tip commit) so
    the rendered portfolio is byte-identical to the git-transport render.
    The `.ergon/log` listing is pinned to that sha (`?ref=`) — one atomic
    snapshot, even if the remote advances mid-read.

    A 404 on `.ergon/log` folds to has_log=False ("not initialised" — the
    path, or the whole repo, is not published there); rate-limit responses
    raise RemoteFetchError with a plain explanation.

    `http_get` is injectable for tests (the real API is never hit by the
    test suite); it must return (status, body_bytes) and raise
    RemoteFetchError on network failure.
    """
    parsed = parse_github_url(url)
    if parsed is None:
        raise RemoteFetchError(f"not a GitHub URL (contents-API fallback unavailable): {url!r}")
    owner, repo = parsed
    get = http_get if http_get is not None else _default_http_get
    base = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        repo_info = _api_json(get, base)
    except _ApiNotFound:
        raise RemoteFetchError(f"GitHub repo not found (or private): {owner}/{repo}")
    default_branch = ""
    if isinstance(repo_info, dict):
        default_branch = repo_info.get("default_branch") or ""
    if not default_branch:
        # A repo with no commits has no meaningful default-branch tip.
        return {"sha": None, "has_log": False, "events": []}

    try:
        commit_info = _api_json(get, f"{base}/commits/{default_branch}")
    except _ApiNotFound:
        # Branch exists in metadata but has no commit — nothing published.
        return {"sha": None, "has_log": False, "events": []}
    sha = commit_info.get("sha") if isinstance(commit_info, dict) else None
    if not sha:
        raise RemoteFetchError(f"GitHub API returned no tip sha for {owner}/{repo}")

    try:
        listing = _api_json(get, f"{base}/contents/{_LOG_SUBPATH}?ref={sha}")
    except _ApiNotFound:
        return {"sha": sha, "has_log": False, "events": []}
    if not isinstance(listing, list):
        raise RemoteFetchError(
            f"GitHub contents API returned a non-directory for {_LOG_SUBPATH} in {owner}/{repo}"
        )

    shard_entries = sorted(
        (e for e in listing
         if isinstance(e, dict) and str(e.get("name", "")).endswith(".jsonl")),
        key=lambda e: str(e.get("name", "")),
    )
    raw_events: list[dict] = []
    for entry in shard_entries:
        download_url = entry.get("download_url")
        if not download_url:
            raise RemoteFetchError(
                f"GitHub contents API entry without download_url in {owner}/{repo}: "
                f"{entry.get('name')!r}"
            )
        status, body = get(download_url)
        if status != 200:
            raise RemoteFetchError(
                f"GitHub raw download returned {status} for {entry.get('name')!r} "
                f"in {owner}/{repo}"
            )
        shard_id = f"{_LOG_SUBPATH}/{entry.get('name')}"
        for event in parse_shard_bytes(body, shard_id=shard_id):
            if event.get("id") is None:
                continue
            raw_events.append(event)

    return {
        "sha": sha,
        "has_log": bool(shard_entries),
        "events": finalise_events(raw_events),
    }


# ---------------------------------------------------------------------------
# The one entry point `pinax overview --remote` calls per manifest entry
# ---------------------------------------------------------------------------

def fetch_remote_events(
    url: str,
    scratch_dir: str,
    http_get=None,
    git_fetch=None,
) -> dict:
    """
    Fetch one remote's published `.ergon/log` events: git transport first,
    GitHub contents-API fallback second (GitHub URLs only).

    Returns {"sha": <tip sha> | None, "has_log": bool, "events": [...]}.
    Raises RemoteFetchError when neither transport can serve the remote.

    `git_fetch` / `http_get` are injectable for tests only — production
    callers pass neither.
    """
    _git_fetch = git_fetch if git_fetch is not None else fetch_remote_git
    try:
        return _git_fetch(url, scratch_dir)
    except RemoteFetchError as git_err:
        if parse_github_url(url) is None:
            raise
        try:
            return fetch_remote_github_api(url, http_get=http_get)
        except RemoteFetchError as api_err:
            raise RemoteFetchError(
                f"git transport failed ({git_err}); "
                f"GitHub contents-API fallback failed ({api_err})"
            ) from api_err
