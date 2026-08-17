"""Event-chain merge tests for deterministic log validation."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from pinax.event import mint_event, serialise
from pinax.fold import fold_events, read_events

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _setup_git_repo(tmpdir: str) -> str:
    """
    Initialise a fresh git repo in tmpdir/repo with:
    - user.name and user.email configured (required for commit)
    - .gitattributes: *.jsonl text eol=lf merge=union

    Returns the repo path.
    """
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo)

    _git(["init", "-b", "main"], cwd=repo)
    _git(["config", "user.name", "test-actor"], cwd=repo)
    _git(["config", "user.email", "test@test.local"], cwd=repo)
    # Disable autocrlf for the test repo to avoid CRLF contamination on Windows.
    _git(["config", "core.autocrlf", "false"], cwd=repo)

    # Write .gitattributes with the union merge driver for JSONL files.
    ga_path = os.path.join(repo, ".gitattributes")
    with open(ga_path, "w", newline="\n") as fh:
        fh.write("*.jsonl text eol=lf merge=union\n")
    _git(["add", ".gitattributes"], cwd=repo)
    _git(["commit", "-m", "init: gitattributes"], cwd=repo)

    return repo


def _write_shard_line(repo: str, shard_rel: str, event: dict) -> None:
    """Append one event line to a shard file in the repo (create if needed)."""
    shard_path = os.path.join(repo, shard_rel)
    os.makedirs(os.path.dirname(shard_path), exist_ok=True)
    line = serialise(event) + "\n"
    with open(shard_path, "a", newline="\n", encoding="utf-8") as fh:
        fh.write(line)


def _commit_shard(repo: str, shard_rel: str, message: str) -> None:
    """Stage and commit the shard file."""
    _git(["add", shard_rel], cwd=repo)
    _git(["commit", "-m", message], cwd=repo)


# ---------------------------------------------------------------------------
# 1. Legitimate same-actor merge: ZERO false warnings
# ---------------------------------------------------------------------------

def test_real_git_same_actor_merge_no_false_warnings(caplog):
    """
    A real two-branch git merge=union of same-actor branches forks the
    per-(shard,actor) chain.  The set-membership check must produce ZERO
    false warnings and the fold must include all items from both branches.

    Scenario:
      base:     ergon.created (actor@host, seq=0, prev='')
      branch-a: item.created  (actor@host, seq=1, prev=base.id)
      branch-b: item.created  (actor@host, seq=2, prev=base.id)   ← FORK

    After 'git merge branch-b' on branch-a:
      The shard contains 3 events.  Two share prev=base.id.
      This is the fork the linear walk complained about.

    Asserts:
      1. ZERO broken-prev-chain warnings.
      2. Both items present in fold state.

    The chain check accepts both branch events because their `prev` values
    reference known records in the shard.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        repo = _setup_git_repo(tmpdir)
        log_dir_rel = "log"
        shard_rel = os.path.join(log_dir_rel, "operator-example.test.jsonl")
        os.makedirs(os.path.join(repo, log_dir_rel), exist_ok=True)

        # ---- Base commit (common ancestor) ----
        base = mint_event(
            seq=0, ts="2026-06-29T10:00:00Z",
            actor="operator@example.test", etype="ergon.created",
            payload={"repo": "test"}, prev="",
        )
        _write_shard_line(repo, shard_rel, base)
        _commit_shard(repo, shard_rel, "base: ergon.created")

        # ---- Branch-a ----
        _git(["checkout", "-b", "branch-a"], cwd=repo)
        item_a = mint_event(
            seq=1, ts="2026-06-29T10:00:01Z",
            actor="operator@example.test", etype="item.created",
            payload={"item_id": "pnx-merge-a", "title": "Merge item A",
                     "prefix": "pnx", "status": "queued"},
            prev=base["id"],
        )
        _write_shard_line(repo, shard_rel, item_a)
        _commit_shard(repo, shard_rel, "branch-a: item-a created")

        # ---- Branch-b (forked from main/base, not from branch-a) ----
        _git(["checkout", "main"], cwd=repo)
        _git(["checkout", "-b", "branch-b"], cwd=repo)
        item_b = mint_event(
            seq=2, ts="2026-06-29T10:00:02Z",
            actor="operator@example.test", etype="item.created",
            payload={"item_id": "pnx-merge-b", "title": "Merge item B",
                     "prefix": "pnx", "status": "queued"},
            prev=base["id"],  # FORK: same prev as item_a — both branch from base
        )
        _write_shard_line(repo, shard_rel, item_b)
        _commit_shard(repo, shard_rel, "branch-b: item-b created")

        # ---- Merge branch-b into branch-a (produces the fork in the shard) ----
        _git(["checkout", "branch-a"], cwd=repo)
        merge_result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", "merge: union of branch-a and branch-b",
             "branch-b"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        # A union merge of two JSONL files should succeed without conflict.
        assert merge_result.returncode == 0, (
            f"git merge failed (expected clean union merge):\n"
            f"stdout: {merge_result.stdout}\nstderr: {merge_result.stderr}"
        )

        # ---- Read the merged shard through the production fold ----
        log_dir = os.path.join(repo, log_dir_rel)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            events = read_events(log_dir)

        # ---- Assertion 1: ZERO broken-prev-chain warnings ----
        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("broken" in r.message.lower() or "prev" in r.message.lower()
                 or "chain" in r.message.lower())
        ]
        assert chain_warnings == [], (
            f"Expected ZERO false broken-prev-chain warnings for a legitimate "
            f"real git merge=union same-actor fork; got:\n"
            + "\n".join(r.message for r in chain_warnings)
            + "\n\n"
            "Each branch event references the shared base event, which is a "
            "known predecessor in the merged shard."
        )

        # ---- Assertion 2: all events present in fold state ----
        state = fold_events(events)
        items = state.get("items", {})
        assert "pnx-merge-a" in items, (
            f"Expected pnx-merge-a in fold state after merge; "
            f"items present: {list(items.keys())}"
        )
        assert "pnx-merge-b" in items, (
            f"Expected pnx-merge-b in fold state after merge; "
            f"items present: {list(items.keys())}"
        )
        # 3 unique events (base + a + b); the union merge may duplicate lines
        # but dedupe makes count = 3.
        assert len(events) == 3, (
            f"Expected 3 events after dedupe; got {len(events)}: "
            f"{[e['id'] for e in events]}"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Genuine within-merge tamper: WARNING still fires
# ---------------------------------------------------------------------------

def test_real_git_merge_with_tamper_still_warns(caplog):
    """
    After a real git merge=union, manually introduce a dangling prev (simulating
    a deleted-middle event) and assert the WARNING still fires.

    This preserves tamper detection while tolerating
    legitimate forks, not genuine deletions.

    Scenario:
      base:     ergon.created  (prev='')
      branch-a: item-a created (prev=base.id)   — kept
      branch-b: item-b created (prev=base.id)   — kept
      tamper:   item-c created (prev=GARBAGE)   — added directly to merged shard

    After fold:
      item-c.prev = GARBAGE ∉ known_ids AND item-c is NOT the first event in the
      (shard, actor) scope → WARNING expected.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        repo = _setup_git_repo(tmpdir)
        log_dir_rel = "log"
        shard_rel = os.path.join(log_dir_rel, "operator-example.test.jsonl")
        os.makedirs(os.path.join(repo, log_dir_rel), exist_ok=True)

        # ---- Base ----
        base = mint_event(
            seq=0, ts="2026-06-29T10:00:00Z",
            actor="operator@example.test", etype="ergon.created",
            payload={"repo": "test"}, prev="",
        )
        _write_shard_line(repo, shard_rel, base)
        _commit_shard(repo, shard_rel, "base: ergon.created")

        # ---- Branch-a ----
        _git(["checkout", "-b", "branch-a"], cwd=repo)
        item_a = mint_event(
            seq=1, ts="2026-06-29T10:00:01Z",
            actor="operator@example.test", etype="item.created",
            payload={"item_id": "pnx-ta", "title": "Tamper item A",
                     "prefix": "pnx", "status": "queued"},
            prev=base["id"],
        )
        _write_shard_line(repo, shard_rel, item_a)
        _commit_shard(repo, shard_rel, "branch-a: item-a")

        # ---- Branch-b ----
        _git(["checkout", "main"], cwd=repo)
        _git(["checkout", "-b", "branch-b"], cwd=repo)
        item_b = mint_event(
            seq=2, ts="2026-06-29T10:00:02Z",
            actor="operator@example.test", etype="item.created",
            payload={"item_id": "pnx-tb", "title": "Tamper item B",
                     "prefix": "pnx", "status": "queued"},
            prev=base["id"],
        )
        _write_shard_line(repo, shard_rel, item_b)
        _commit_shard(repo, shard_rel, "branch-b: item-b")

        # ---- Merge ----
        _git(["checkout", "branch-a"], cwd=repo)
        merge_result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", "merge: branch-a + branch-b", "branch-b"],
            cwd=repo, capture_output=True, text=True,
        )
        assert merge_result.returncode == 0, (
            f"git merge failed:\nstdout: {merge_result.stdout}\nstderr: {merge_result.stderr}"
        )

        # ---- Inject a tampered event: prev points to a non-existent id ----
        # item-c.prev = GARBAGE ← simulates a deleted predecessor in the merged log.
        item_c = mint_event(
            seq=3, ts="2026-06-29T10:00:03Z",
            actor="operator@example.test", etype="item.created",
            payload={"item_id": "pnx-tc", "title": "Tamper item C (dangling prev)",
                     "prefix": "pnx", "status": "queued"},
            prev="GARBAGE_DANGLING_PREV_FOR_TAMPER_TEST",
        )
        _write_shard_line(repo, shard_rel, item_c)
        # Do NOT commit — we're testing the fold directly on the working-tree bytes.

        # ---- Read through the production fold ----
        log_dir = os.path.join(repo, log_dir_rel)
        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            read_events(log_dir)

        # ---- Assert WARNING was emitted for the dangling prev ----
        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and ("broken" in r.message.lower() or "prev" in r.message.lower()
                 or "chain" in r.message.lower())
        ]
        assert chain_warnings, (
            "Expected a broken-prev-chain WARNING for item-c's dangling prev; got none. "
            "The set-membership fix must not disable tamper detection — only legitimate "
            "forks (prev ∈ known_ids) are tolerated; a dangling prev must still warn."
        )
        warning_text = " ".join(r.message for r in chain_warnings)
        assert "GARBAGE_DANGLING_PREV" in warning_text or item_c["id"] in warning_text, (
            f"Expected the dangling prev or tampered event id in warning; got: {warning_text}"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
