"""
pinax.visibility — unmerged-branch tracker awareness.

The fold is branch-scoped truth: board/report/ready fold ONLY the .ergon log
as it exists in the current working tree. Tracker events committed on OTHER
local branches (including linked worktrees) are invisible to that fold.

This module detects that condition and warns — on stderr, never stdout, so
the fold output (human or --json) stays byte-identical and deterministic.
The warning is environment-dependent advisory context (it depends on what
other branches exist locally), which is exactly why it is NOT part of the
fold result and never affects exit codes.

Fail-safe by construction: any git failure (no git binary, not a git repo,
unborn HEAD) degrades to "no warning", never to a broken fold.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TextIO

_DEFAULT_LOG_SUBPATH = ".ergon/log"


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


def _git_lines(repo_root: str, args: list[str]) -> list[str] | None:
    """stdout lines of a git command, or None if git failed in any way."""
    result = _run_git(repo_root, args)
    if result is None or result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def unmerged_tracker_refs(
    repo_root: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
) -> list[tuple[str, int]]:
    """
    Local branches whose committed .ergon log carries events NOT reachable
    from HEAD, as (branch_name, added_event_count) sorted by branch name.

    Detection is two-step per branch, cheapest first:
    1. `git rev-list --count HEAD..<branch> -- <log_subpath>` — does the
       branch have any commit touching the log that HEAD lacks?
    2. If yes, `git diff --numstat HEAD...<branch> -- <log_subpath>` — count
       added shard lines since the merge base. Events are one-per-line,
       append-only jsonl, so added lines = events outside this fold.

    Branches checked out in other worktrees are ordinary local branches and
    are therefore covered. Returns [] on any git failure (advisory helper —
    it must never break the fold it decorates).
    """
    branches = _git_lines(
        repo_root, ["for-each-ref", "refs/heads", "--format=%(refname:short)"]
    )
    if not branches:
        return []

    head_lines = _git_lines(repo_root, ["rev-parse", "--abbrev-ref", "HEAD"])
    current = head_lines[0] if head_lines else "HEAD"

    unmerged: list[tuple[str, int]] = []
    for branch in sorted(branches):
        if branch == current:
            continue
        count_lines = _git_lines(
            repo_root,
            ["rev-list", "--count", f"HEAD..{branch}", "--", log_subpath],
        )
        if not count_lines or count_lines[0] == "0":
            continue
        added = 0
        numstat = _git_lines(
            repo_root,
            ["diff", "--numstat", f"HEAD...{branch}", "--", log_subpath],
        )
        for line in numstat or []:
            fields = line.split("\t")
            if fields and fields[0].isdigit():
                added += int(fields[0])
        unmerged.append((branch, added))
    return unmerged


def warn_unmerged(
    repo_root: str,
    log_subpath: str = _DEFAULT_LOG_SUBPATH,
    stream: TextIO | None = None,
) -> list[tuple[str, int]]:
    """
    Print the branch-scoped-truth warning to stderr (or `stream`) if any
    local branch carries tracker events outside the current fold; return the
    offending (branch, added_events) list either way.
    """
    refs = unmerged_tracker_refs(repo_root, log_subpath)
    if refs:
        out = stream if stream is not None else sys.stderr
        n = len(refs)
        plural = "es" if n != 1 else ""
        print(
            f"pinax: WARNING - {n} unmerged branch{plural} carr"
            f"{'y' if n != 1 else 'ies'} tracker events not in this view:",
            file=out,
        )
        for branch, added in refs:
            suffix = f" (+{added} event{'s' if added != 1 else ''})" if added else ""
            print(f"         {branch}{suffix}", file=out)
        print(
            "       This fold is branch-scoped: it reflects only the current "
            "branch's committed .ergon log.",
            file=out,
        )
    return refs
