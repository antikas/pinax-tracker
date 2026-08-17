"""Recall-index exclusion registration for Pinax.

This module writes and verifies the koine-memory sources.toml exclude
patterns for a Pinax-registered repo.  The exclusion covers the four
path classes:

    1. .ergon/         — the event log and all projection files
    2. .ergon/board.md — the committed projection board (subset of .ergon/)
    3. .ergon/items/   — committed projection per-item files (subset of .ergon/)
    4. board/          — an operational board projection
    5. work records    — operational material, not knowledge

The four exclusion PATTERN CLASSES (for the negative test classifier):
    ".ergon/**/*"       covers the log + all projection files
    ".ergon/board.md"   (subset — covered by .ergon/**)
    ".ergon/items/*"    (subset — covered by .ergon/**)
    "board/**/*"        covers board/
    a work-record pattern covers its directory

Registration is idempotent: re-registering is safe.

KNOWLEDGE-PLANE CLEAN:
- This module ONLY writes to sources.toml (the koine-memory index config).
- It NEVER writes any operational state (item IDs, statuses, metrics, event
  log content) into the knowledge plane.
- It writes EXCLUSION PATTERNS — negative entries that prevent operational
  truth from entering the knowledge corpus.
"""

from __future__ import annotations

import re
from typing import Sequence

# Exclusion pattern classes (as glob patterns).
# Each class maps a path class to its canonical glob pattern.
EXCLUSION_PATTERNS: tuple[str, ...] = (
    ".ergon/**/*",      # event log + all projection files
    "board/**/*",       # operational board
    "docs/cycles/**/*", # work records
)

# Regex patterns for the path-class classifier (used by the negative test).
# A chunk path matches one of these if it falls under the excluded classes.
_EXCLUDED_PATH_REGEXES: tuple[re.Pattern, ...] = (
    re.compile(r"^\.ergon(?:/|\\)"),        # .ergon/ — log + projection
    re.compile(r"^board(?:/|\\)"),           # board/ — legacy build board
    re.compile(r"^docs(?:/|\\)cycles(?:/|\\)"), # work-record directory
)


def is_excluded_path(rel_path: str) -> bool:
    """
    Return True if rel_path falls under one of the excluded classes.

    rel_path is a relative path (from the repo root), using either / or \\.

    This is the chunk-path classifier used by the negative test
    (tests/test_separation_negative.py) to assert zero chunks from excluded paths.

    Deterministic, pure function — no filesystem access.
    """
    for pattern in _EXCLUDED_PATH_REGEXES:
        if pattern.match(rel_path):
            return True
    return False


def all_patterns_match_examples() -> dict[str, bool]:
    """
    Self-test: return a mapping of example paths to their expected exclusion.

    Used by tests/test_separation_negative.py to prove that the pattern
    classifier correctly covers all configured path classes.

    Returns a dict: {example_path: expected_is_excluded}
    """
    return {
        # Excluded — log
        ".ergon/log/operator-example.test.jsonl": True,
        # Excluded — projection board
        ".ergon/board.md": True,
        # Excluded — projection item file
        ".ergon/items/pnx-abc123.md": True,
        # Excluded — nested ergon path
        ".ergon/.gitattributes": True,
        # Excluded — board/
        "board/PNX-01.md": True,
        "board/some-task.md": True,
        # Excluded work-record paths
        "docs/cycles/PNX-01/briefs/cycle-1-brief.md": True,
        "docs/cycles/PNX-05/reports/cycle-1-report.md": True,
        # NOT excluded — knowledge docs
        "CLAUDE.md": False,
        "DESIGN.md": False,
        "docs/decisions/ADR-001-event-log-ssot-deterministic-fold.md": False,
        "docs/decisions/ADR-004-separation-enforcement.md": False,
        # NOT excluded — code
        "pinax/__main__.py": False,
        "tests/test_fold_determinism.py": False,
        # NOT excluded — top-level docs
        "docs/architecture/overview.md": False,
    }


def generate_sources_toml_stanza(
    repo_id: str,
    repo_path: str,
) -> str:
    """
    Generate the TOML stanza that should be added to sources.toml for a
    Pinax-registered repo.

    The stanza includes the configured exclusion pattern classes.
    Markdown-only include (the .py operational tool is not knowledge).

    This is the canonical generated form — the unit test asserts that the
    stanza covers all four path classes.
    """
    # Normalise path separators to forward slashes for TOML portability.
    safe_path = repo_path.replace("\\", "/")
    exclude_list = (
        '".ergon/**/*", '
        '"board/**/*", '
        '"docs/cycles/**/*", '
        '"**/node_modules/**/*", "**/.venv/**/*", "**/venv/**/*", '
        '"**/.git/**/*", "**/.claude/**/*"'
    )
    return (
        f'# Pinax repo: {repo_id}\n'
        f'# Exclusions: .ergon/** (log and projection), board/**, and work records.\n'
        f'[[source]]\n'
        f'id = "{repo_id}"\n'
        f'path = "{safe_path}"\n'
        f'kind = "repo"\n'
        f'include = ["**/*.md", "**/*.mdx"]\n'
        f'exclude = [{exclude_list}]\n'
    )
