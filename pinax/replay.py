"""
pinax.replay — time-travel fold to any git reference.

ADR-001 / DESIGN.md: "Replay = fold the event log up to a git ref; no
secondary state store is needed."  This module is the git-ref-sourced sibling
of pinax.fold.read_events: it sources raw shard bytes from the git object
store AS COMMITTED at <ref> (a branch, tag, or commit sha) instead of from the
current working tree, then hands off to the identical determinism layer
(pinax.fold.finalise_events / fold_events) the live commands use.  There is
exactly one fold implementation (SSOT) — this module only changes WHERE the
bytes come from, never HOW they are sorted, deduped, or folded.

Read-only, by construction: every git operation here is a read (ls-tree,
cat-file, rev-parse) — nothing writes to the index, the working tree, or the
log.  No secondary snapshot/cache is created or persisted.

No wall-clock, no RNG, no locale-sensitive behaviour — the ref is resolved to
a commit sha up front so a single replay call is pinned to one exact
historical snapshot even if the given ref is a moving branch tip.
"""

from __future__ import annotations

import logging
import subprocess

from .fold import fold_events, parse_shard_bytes, finalise_events

logger = logging.getLogger(__name__)

_DEFAULT_LOG_SUBPATH = ".ergon/log"


class ReplayRefError(Exception):
    """Raised when <ref> does not resolve to a valid commit in repo_root."""


def _run_git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git subprocess in repo_root, capturing stdout/stderr as bytes."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
    )


def _resolve_commit(repo_root: str, ref: str) -> str:
    """
    Resolve <ref> (branch, tag, or commit sha) to a single commit sha.

    Pinning to a sha up front means the rest of replay operates on one fixed
    snapshot even if <ref> is a branch tip that could move between calls
    (there is no other caller in-process here, but this keeps the read
    atomic and matches "replay is a pure function of (repo, ref)").

    Raises ReplayRefError if <ref> does not resolve to a commit.
    """
    result = _run_git(repo_root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReplayRefError(
            f"not a valid git ref in this repo: {ref!r}"
            + (f" ({stderr})" if stderr else "")
        )
    sha = result.stdout.decode("utf-8", errors="replace").strip()
    if not sha:
        raise ReplayRefError(f"not a valid git ref in this repo: {ref!r}")
    return sha


def _list_shard_paths(repo_root: str, commit_sha: str, log_subpath: str) -> list[str]:
    """
    Return the repo-relative paths of *.jsonl shards under log_subpath as they
    existed at commit_sha.  Empty (not missing) log_subpath at that commit —
    e.g. a ref that predates 'pinax init' — yields an empty list, which folds
    to the empty base state (a valid outcome, not an error).
    """
    result = _run_git(
        repo_root,
        ["ls-tree", "-r", "--name-only", commit_sha, "--", log_subpath],
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReplayRefError(
            f"git ls-tree failed for {commit_sha}:{log_subpath}: {stderr}"
        )
    text = result.stdout.decode("utf-8", errors="replace")
    paths = [line for line in text.splitlines() if line]
    return sorted(p for p in paths if p.endswith(".jsonl"))


def _read_blob(repo_root: str, commit_sha: str, path: str) -> bytes:
    """Return the raw bytes of the blob at commit_sha:path."""
    result = _run_git(repo_root, ["cat-file", "-p", f"{commit_sha}:{path}"])
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReplayRefError(
            f"git cat-file failed for {commit_sha}:{path}: {stderr}"
        )
    return result.stdout


def read_raw_events_at_ref(
    repo_root: str,
    ref: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> list[dict]:
    """
    Git-ref-sourced RAW events at <ref> — unsorted, undeduped, no integrity
    checks applied.  This is the git-blob-sourced half of read_events_at_ref(),
    extracted so pinax.all_branches can union this raw pool with the
    filesystem-sourced pool (pinax.fold.read_raw_events) AND other branch tips'
    raw pools, before a single finalise_events call across the combined set.
    `read_events_at_ref(repo_root, ref, log_subpath)` equals
    `finalise_events(read_raw_events_at_ref(repo_root, ref, log_subpath))`.

    Raises ReplayRefError if <ref> does not resolve to a commit in repo_root.
    """
    commit_sha = _resolve_commit(repo_root, ref)
    shard_paths = _list_shard_paths(repo_root, commit_sha, log_subpath)

    raw_events: list[dict] = []
    for shard_path in shard_paths:
        raw = _read_blob(repo_root, commit_sha, shard_path)
        for event in parse_shard_bytes(raw, shard_id=shard_path):
            eid = event.get("id")
            if eid is None:
                logger.warning(
                    "Event without id in shard %s at %s - ignored.", shard_path, commit_sha
                )
                continue
            raw_events.append(event)

    return raw_events


def read_events_at_ref(
    repo_root: str,
    ref: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> list[dict]:
    """
    Determinism layer, git-ref-sourced: read all *.jsonl shards as they existed
    at <ref>, sort by total-order key, dedupe by id — identical guarantee to
    pinax.fold.read_events, sourced from git blobs instead of the filesystem.

    Raises ReplayRefError if <ref> does not resolve to a commit in repo_root.
    """
    return finalise_events(read_raw_events_at_ref(repo_root, ref, log_subpath))


def fold_at_ref(
    repo_root: str,
    ref: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> dict:
    """
    End-to-end time-travel fold: reconstruct state exactly as it existed at
    <ref> (branch, tag, or commit sha).  Read-only — never writes to the
    working tree, the log, or the projection.

    Raises ReplayRefError if <ref> does not resolve to a commit in repo_root.
    """
    events = read_events_at_ref(repo_root, ref, log_subpath)
    return fold_events(events)
