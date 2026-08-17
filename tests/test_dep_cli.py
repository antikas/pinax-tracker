"""
tests/test_dep_cli.py — CLI validation tests for 'pinax dep add/rm'.

  Covers three CLI rejection cases and a backward-compatible alias.

  - Neither --to nor --blocks → exit non-zero with clear message, nothing appended.
  - Both --to and --blocks    → exit non-zero with clear message, nothing appended.
  - --to without --type       → exit non-zero with clear message, nothing appended.
  - --blocks <id> alias       → works (back-compat).
  - All five --type values     → accepted.

Test path:
  Uses a real pinax repo (pinax init + pinax add) so that item IDs exist
  in the fold state — the CLI validates item IDs against the fold state before
  checking edge arguments.  The subprocess path exercises the real argparse +
  _validate_dep_args + run_add/run_rm path.

  validate-before-append invariant: on any rejection, the log line count must
  not increase (nothing is written to the JSONL shard).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env() -> dict:
    """Build environment with pinax on PYTHONPATH."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing if existing else "")
    return env


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    env = _build_env()
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True,
    )
    return r


def _pinax(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = _build_env()
    r = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo}:\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git not available on PATH"
)


_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"


def _setup_repo_with_two_items() -> tuple[str, str, str, str]:
    """
    Create a git repo with pinax init + two items.

    Returns (tmpdir, repo_root, item_x_id, item_y_id).
    Caller is responsible for shutil.rmtree(tmpdir).
    """
    tmpdir = tempfile.mkdtemp()
    repo = os.path.join(tmpdir, "repo")
    os.makedirs(repo)

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@pinax.test")
    _git(repo, "config", "user.name", "Pinax Test")
    _git(repo, "config", "core.autocrlf", "false")

    ergon_dir = os.path.join(repo, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    ga_path = os.path.join(ergon_dir, ".gitattributes")
    with open(ga_path, "w", newline="\n") as fh:
        fh.write(_GITATTRIBUTES)

    # pinax init
    _pinax(repo, "init", "--actor", "operator@example.test")

    # Add two items
    _pinax(repo, "add", "--title", "Item X", "--prefix", "pnx", "--actor", "operator@example.test")
    _pinax(repo, "add", "--title", "Item Y", "--prefix", "pnx", "--actor", "operator@example.test")

    # Discover IDs from fold state
    from pinax.fold import fold_events, read_events
    state = fold_events(read_events(log_dir))
    items = state.get("items", {})
    x_ids = [iid for iid, item in items.items() if "Item X" in item.get("title", "")]
    y_ids = [iid for iid, item in items.items() if "Item Y" in item.get("title", "")]
    assert x_ids, f"Item X not found; items={list(items.keys())}"
    assert y_ids, f"Item Y not found; items={list(items.keys())}"

    return tmpdir, repo, x_ids[0], y_ids[0]


def _count_log_lines(repo: str) -> int:
    """Return the total number of non-empty lines across all JSONL shards."""
    log_dir = os.path.join(repo, ".ergon", "log")
    total = 0
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, fname), "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        total += len([l for l in normalised.split(b"\n") if l])
    return total


# ---------------------------------------------------------------------------
# Three rejection cases
# ---------------------------------------------------------------------------

@requires_git
def test_dep_add_neither_to_nor_blocks_rejected():
    """
    'pinax dep add <from> -- (no --to, no --blocks)' → exit non-zero, nothing appended.

    This is the 'neither --to nor --blocks' rejection case.
    """
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        # No --to, no --blocks → must be rejected.
        r = _pinax(repo, "dep", "add", x_id, "--actor", "operator@example.test", check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when neither --to nor --blocks given; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        # Error message must be present on stderr.
        assert r.stderr.strip(), (
            "Expected an error message on stderr; got nothing."
        )
        # Nothing must have been appended.
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection — "
            "validate-before-append invariant broken."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_git
def test_dep_rm_neither_to_nor_blocks_rejected():
    """'pinax dep rm <from>' (no --to, no --blocks) → exit non-zero, nothing appended."""
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "rm", x_id, "--actor", "operator@example.test", check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when neither --to nor --blocks given; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_git
def test_dep_add_both_to_and_blocks_rejected():
    """
    'pinax dep add <from> --to <y> --blocks <y>' → exit non-zero, nothing appended.

    This is the 'both --to and --blocks' rejection case.
    """
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "add", x_id,
                   "--to", y_id, "--type", "blocks",
                   "--blocks", y_id,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when both --to and --blocks given; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_git
def test_dep_rm_both_to_and_blocks_rejected():
    """'pinax dep rm <from> --to <y> --blocks <y>' → exit non-zero, nothing appended."""
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "rm", x_id,
                   "--to", y_id, "--type", "blocks",
                   "--blocks", y_id,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when both --to and --blocks given; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_git
def test_dep_add_to_without_type_rejected():
    """
    'pinax dep add <from> --to <y>' (no --type) → exit non-zero, nothing appended.

    This is the '--to without --type' rejection case.  No silent default to blocks.
    """
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "add", x_id,
                   "--to", y_id,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when --to given without --type; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}\n"
            "The silent default-to-blocks footgun must be removed."
        )
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@requires_git
def test_dep_rm_to_without_type_rejected():
    """'pinax dep rm <from> --to <y>' (no --type) → exit non-zero, nothing appended."""
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "rm", x_id,
                   "--to", y_id,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode != 0, (
            "Expected non-zero exit when --to given without --type; got 0.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        lines_after = _count_log_lines(repo)
        assert lines_after == lines_before, (
            f"Log grew by {lines_after - lines_before} line(s) despite rejection."
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Back-compat: --blocks alias still works
# ---------------------------------------------------------------------------

@requires_git
def test_dep_add_blocks_alias_works():
    """
    'pinax dep add <from> --blocks <y>' → accepted, appends dep.added blocks edge.

    The --blocks alias is back-compat for --to <y> --type blocks.
    """
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        lines_before = _count_log_lines(repo)

        r = _pinax(repo, "dep", "add", x_id,
                   "--blocks", y_id,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode == 0, (
            f"Expected exit 0 for --blocks alias; got {r.returncode}.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
        lines_after = _count_log_lines(repo)
        assert lines_after > lines_before, (
            "Expected at least one new log line after dep add --blocks; none appended."
        )

        # Verify the edge was recorded as blocks type.
        from pinax.fold import fold_events, read_events
        state = fold_events(read_events(os.path.join(repo, ".ergon", "log")))
        pair = (x_id, y_id)
        assert pair in state.get("deps", set()), (
            f"blocks edge ({x_id}, {y_id}) not in state['deps'] after --blocks alias.\n"
            f"deps = {sorted(state.get('deps', set()))}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# All five --type values are accepted
# ---------------------------------------------------------------------------

@requires_git
@pytest.mark.parametrize("edge_type", [
    "blocks",
    "parent-child",
    "discovered-from",
    "related",
    "supersedes",
])
def test_dep_add_all_five_types_accepted(edge_type: str):
    """
    'pinax dep add <from> --to <y> --type <t>' → accepted for all five edge types.
    """
    tmpdir, repo, x_id, y_id = _setup_repo_with_two_items()
    try:
        r = _pinax(repo, "dep", "add", x_id,
                   "--to", y_id,
                   "--type", edge_type,
                   "--actor", "operator@example.test",
                   check=False)

        assert r.returncode == 0, (
            f"Expected exit 0 for --type {edge_type!r}; got {r.returncode}.\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )

        # Verify the edge was recorded with the correct type.
        from pinax.fold import fold_events, read_events
        state = fold_events(read_events(os.path.join(repo, ".ergon", "log")))
        pair = (x_id, y_id)
        edges = state.get("edges", {})
        assert pair in edges.get(edge_type, set()), (
            f"edge ({x_id}, {y_id}) of type {edge_type!r} not in fold state after dep add.\n"
            f"edges[{edge_type!r}] = {sorted(edges.get(edge_type, set()))}"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# SSOT: verify no second literal list of five types in __main__.py or projection.py
# ---------------------------------------------------------------------------

def test_no_second_literal_type_list_in_main():
    """
    SSOT guard: __main__.py must not contain a second hard-coded list of the five
    edge types.  It must import from dep.VALID_EDGE_TYPES and derive from it.
    """
    main_py = os.path.join(_PINAX_SRC, "pinax", "__main__.py")
    with open(main_py, "r", encoding="utf-8") as fh:
        content = fh.read()
    # The canonical list is in dep.py.  __main__.py must import VALID_EDGE_TYPES.
    assert "VALID_EDGE_TYPES" in content, (
        "__main__.py does not import VALID_EDGE_TYPES from dep — SSOT broken."
    )
    # It must NOT contain a hardcoded list literal with all five type names.
    # We check for the telltale pattern of a Python list or frozenset containing
    # all five types inline.
    forbidden_patterns = [
        '"blocks", "parent-child", "discovered-from", "related", "supersedes"',
        '"blocks","parent-child","discovered-from","related","supersedes"',
        "'blocks', 'parent-child', 'discovered-from', 'related', 'supersedes'",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in content, (
            f"Found a second hard-coded type list in __main__.py: {pattern!r}\n"
            "Only dep.py should contain the canonical VALID_EDGE_TYPES frozenset."
        )


def test_no_second_literal_type_list_in_projection():
    """
    SSOT guard: projection.py must not contain a second hard-coded list of the five
    edge types.  It must import VALID_EDGE_TYPES from dep and use the module-level
    _ALL_EDGE_TYPES derived from it.
    """
    proj_py = os.path.join(_PINAX_SRC, "pinax", "projection.py")
    with open(proj_py, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "VALID_EDGE_TYPES" in content, (
        "projection.py does not import VALID_EDGE_TYPES from dep — SSOT broken."
    )
    # Must not contain the old hardcoded list literal with all five types.
    forbidden_patterns = [
        '"blocks",\n        "discovered-from",\n        "parent-child",',
        '["blocks",',
        'sorted([\n        "blocks"',
    ]
    for pattern in forbidden_patterns:
        assert pattern not in content, (
            f"Found a second hard-coded type list in projection.py: {pattern!r}\n"
            "Only dep.py should contain the canonical VALID_EDGE_TYPES frozenset."
        )
