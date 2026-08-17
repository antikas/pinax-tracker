"""Tests for recall-index exclusion of tracker operational paths.

The tests verify that `sources.toml` exclusion patterns and
`is_excluded_path` cover tracker logs, projections, archived boards, and
cycle work records while leaving ordinary knowledge and source paths visible.
They use a seeded file tree and the production classifier when a live indexer
is unavailable.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from pinax.separation import (
    EXCLUSION_PATTERNS,
    all_patterns_match_examples,
    is_excluded_path,
    generate_sources_toml_stanza,
)


# ---------------------------------------------------------------------------
# 1. Pattern coverage test — all four ADR-004 path classes
# ---------------------------------------------------------------------------

class TestExclusionPatterns:
    """The EXCLUSION_PATTERNS constant covers all four ADR-004 path classes."""

    def test_ergon_log_pattern_present(self):
        """Pattern covering .ergon/** is in EXCLUSION_PATTERNS."""
        ergon_patterns = [p for p in EXCLUSION_PATTERNS if ".ergon" in p]
        assert ergon_patterns, (
            "EXCLUSION_PATTERNS must contain a pattern for .ergon/ "
            "(the event log and committed projection)"
        )

    def test_board_pattern_present(self):
        """Pattern covering board/ is in EXCLUSION_PATTERNS."""
        board_patterns = [p for p in EXCLUSION_PATTERNS if "board" in p and "ergon" not in p]
        assert board_patterns, (
            "EXCLUSION_PATTERNS must contain a pattern for board/ "
            "(the legacy durable build board)"
        )

    def test_docs_cycles_pattern_present(self):
        """Pattern covering docs/cycles/ is in EXCLUSION_PATTERNS."""
        cycle_patterns = [p for p in EXCLUSION_PATTERNS if "cycles" in p]
        assert cycle_patterns, (
            "EXCLUSION_PATTERNS must contain a pattern for docs/cycles/ "
            "(build-cycle artefacts / work-records)"
        )

    def test_pattern_count(self):
        """All four ADR-004 path classes are covered by at least one pattern."""
        # The three unique top-level path classes:
        # 1. .ergon/** (log + projection)
        # 2. board/**  (bootstrap tracker)
    # 3. Work-record paths
        # Note: the committed projection (.ergon/board.md, .ergon/items/) is
        # a SUBSET of .ergon/**, so covered by the first pattern.
        assert len(EXCLUSION_PATTERNS) >= 3, (
            f"Expected >= 3 exclusion patterns (one per ADR-004 path class), "
            f"got {len(EXCLUSION_PATTERNS)}: {EXCLUSION_PATTERNS}"
        )


# ---------------------------------------------------------------------------
# 2. Classifier correctness — the path classifier works on all example paths
# ---------------------------------------------------------------------------

class TestPathClassifier:
    """is_excluded_path() correctly classifies all ADR-004 example paths."""

    def test_all_examples_from_oracle(self):
        """all_patterns_match_examples() oracle is fully consistent with is_excluded_path."""
        oracle = all_patterns_match_examples()
        for path, expected in oracle.items():
            result = is_excluded_path(path)
            assert result == expected, (
                f"Classifier mismatch for path {path!r}: "
                f"expected is_excluded={expected}, got is_excluded={result}"
            )

    # Parametrised spot-checks for the four path classes.

    @pytest.mark.parametrize("path", [
        ".ergon/log/operator-example.test.jsonl",
        ".ergon/board.md",
        ".ergon/items/pnx-abc123.md",
        ".ergon/.gitattributes",
        ".ergon/log/reviewer-host.jsonl",
    ])
    def test_ergon_paths_excluded(self, path: str):
        """All .ergon/ paths are excluded (log + projection)."""
        assert is_excluded_path(path), (
            f"Path {path!r} should be excluded (falls under .ergon/)"
        )

    @pytest.mark.parametrize("path", [
        "board/PNX-01.md",
        "board/PNX-05.md",
        "board/some-task.md",
    ])
    def test_board_paths_excluded(self, path: str):
        """All board/ paths are excluded (legacy durable build board)."""
        assert is_excluded_path(path), (
            f"Path {path!r} should be excluded (falls under board/)"
        )

    @pytest.mark.parametrize("path", [
        "docs/cycles/PNX-01/briefs/cycle-1-brief.md",
        "docs/cycles/PNX-05/reports/cycle-1-report.md",
        "docs/cycles/PNX-05/audit/cycle-1-audit.md",
        "docs/cycles/README.md",
    ])
    def test_docs_cycles_paths_excluded(self, path: str):
        """All docs/cycles/ paths are excluded (work-records)."""
        assert is_excluded_path(path), (
            f"Path {path!r} should be excluded (falls under docs/cycles/)"
        )

    @pytest.mark.parametrize("path", [
        "CLAUDE.md",
        "DESIGN.md",
        "docs/decisions/ADR-001-event-log-ssot-deterministic-fold.md",
        "docs/decisions/ADR-004-separation-enforcement.md",
        "pinax/__main__.py",
        "tests/test_fold_determinism.py",
        "docs/architecture/overview.md",
    ])
    def test_knowledge_paths_not_excluded(self, path: str):
        """CLAUDE.md, DESIGN.md, ADRs, and code are NOT excluded."""
        assert not is_excluded_path(path), (
            f"Path {path!r} should NOT be excluded (legitimate knowledge/code path)"
        )


# ---------------------------------------------------------------------------
# 3. Zero-chunks proof over a seeded synthetic file tree
# ---------------------------------------------------------------------------

class TestZeroChunksFromExcludedPaths:
    """
    Simulate what an indexer would produce from a seeded repo and assert
    that the classifier returns ZERO non-excluded chunks from excluded paths.

    The seeded file tree contains:
    - .ergon/log/operator-example.test.jsonl     (event log — EXCLUDED)
    - .ergon/board.md                     (projection board — EXCLUDED)
    - .ergon/items/.md            (projection item — EXCLUDED)
    - board/.md                     (bootstrap board — EXCLUDED)
    -   (work-record — EXCLUDED)
    - CLAUDE.md                           (knowledge — NOT excluded)
    - DESIGN.md                           (knowledge — NOT excluded)
    - docs/decisions/ADR-001.md           (knowledge — NOT excluded)

    We simulate the indexer by listing all files in the repo and checking
    each against the classifier.  The test asserts that ALL files under the
    four excluded path classes are classified as excluded (zero leak).
    """

    def _seed_repo(self, repo_dir: str) -> list[str]:
        """
        Seed the repo with files from the excluded and non-excluded path classes.
        Returns a list of relative paths created.
        """
        files = {
            # --- EXCLUDED paths ---
            ".ergon/log/operator-example.test.jsonl": (
                '{"id":"test","seq":0,"ts":"2026-06-30T00:00:00Z",'
                '"actor":"operator@example.test","type":"ergon.created",'
                '"payload":{"repo":"test"},"prev":""}\n'
            ),
            ".ergon/board.md": "# Board\n\n| id | title | status |\n| --- | --- | --- |\n",
            ".ergon/items/pnx-test.md": "---\nid: pnx-test\ntitle: Test item\n---\n",
            "board/PNX-01.md": "---\ntitle: Test board item\nstatus: done\n---\n",
            "docs/cycles/PNX-01/briefs/cycle-1-brief.md": (
                "# PNX-01 brief\n\nThis is a build cycle work-record.\n"
            ),
            "docs/cycles/PNX-05/reports/cycle-1-report.md": (
                "# PNX-05 report\n\nThis is a report.\n"
            ),
            # --- NOT EXCLUDED paths ---
            "CLAUDE.md": "# Pinax\n\nKnowledge doc.\n",
            "DESIGN.md": "# DESIGN\n\nKnowledge doc.\n",
            "docs/decisions/ADR-001.md": "# ADR-001\n\nKnowledge doc.\n",
        }
        created: list[str] = []
        for rel_path, content in files.items():
            full_path = os.path.join(repo_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", newline="\n", encoding="utf-8") as fh:
                fh.write(content)
            created.append(rel_path)
        return created

    def _collect_all_files(self, repo_dir: str) -> list[str]:
        """Walk the repo and return all relative paths (normalised to forward slashes)."""
        paths: list[str] = []
        for root, dirs, files in os.walk(repo_dir):
            # Skip hidden OS directories.
            dirs[:] = [d for d in dirs if not d.startswith(".git")]
            for fname in files:
                abs_path = os.path.join(root, fname)
                rel = os.path.relpath(abs_path, repo_dir).replace(os.sep, "/")
                paths.append(rel)
        return sorted(paths)

    def test_zero_chunks_from_excluded_paths(self):
        """
        Seeded repo: zero files from excluded path classes escape the classifier.

        MECHANISM-ASSERT: this proves that is_excluded_path() correctly blocks
        all four ADR-004 path classes for a realistic repo structure.
        The live koine-memory indexer is not invoked (not available in test env);
        the classifier IS the production mechanism that the indexer calls.
        """
        with tempfile.TemporaryDirectory() as repo_dir:
            created = self._seed_repo(repo_dir)
            all_files = self._collect_all_files(repo_dir)

            # Partition: which files should be excluded, which should not.
            expected_excluded = {
                p for p in created if is_excluded_path(p)
            }
            expected_not_excluded = {
                p for p in created if not is_excluded_path(p)
            }

            # Simulate indexer: for each file in the repo, check if it leaks.
            leaked: list[str] = []
            for rel_path in all_files:
                # Would the indexer include this? If it's under an excluded class
                # but the classifier doesn't block it → leak.
                # For this test: check each file under the 4 excluded path classes.
                under_excluded = any(
                    rel_path.startswith(prefix)
                    for prefix in (".ergon/", "board/", "docs/cycles/")
                )
                if under_excluded and not is_excluded_path(rel_path):
                    leaked.append(rel_path)

            assert leaked == [], (
                f"ZERO-CHUNK ASSERTION FAILED: {len(leaked)} path(s) from excluded "
                f"classes escaped the classifier:\n"
                + "\n".join(f"  {p}" for p in leaked)
                + "\n\nThese represent chunks that would appear in the recall index "
                "in violation of ADR-004."
            )

    def test_knowledge_files_not_classified_as_excluded(self):
        """
        Knowledge docs (CLAUDE.md, DESIGN.md, ADRs) are NOT blocked by the classifier.

        This is the complementary test: over-exclusion would hide legitimate
        knowledge docs from the recall index.
        """
        with tempfile.TemporaryDirectory() as repo_dir:
            self._seed_repo(repo_dir)
            all_files = self._collect_all_files(repo_dir)

            # Knowledge files that must NOT be excluded.
            knowledge_files = [p for p in all_files if not any(
                p.startswith(prefix)
                for prefix in (".ergon/", "board/", "docs/cycles/")
            )]

            over_excluded = [p for p in knowledge_files if is_excluded_path(p)]
            assert over_excluded == [], (
                f"OVER-EXCLUSION: {len(over_excluded)} knowledge path(s) were "
                f"incorrectly excluded:\n"
                + "\n".join(f"  {p}" for p in over_excluded)
            )


# ---------------------------------------------------------------------------
# 4. sources.toml stanza covers all four path classes
# ---------------------------------------------------------------------------

class TestSourcesTomlStanza:
    """The generated sources.toml stanza covers the four ADR-004 exclusion classes."""

    def test_stanza_contains_ergon_exclude(self):
        stanza = generate_sources_toml_stanza("pinax", "/workspace/pinax")
        assert ".ergon/**/*" in stanza, (
            "sources.toml stanza must exclude .ergon/**/* "
            "(the event log + committed projection)"
        )

    def test_stanza_contains_board_exclude(self):
        stanza = generate_sources_toml_stanza("pinax", "/workspace/pinax")
        assert "board/**/*" in stanza, (
            "sources.toml stanza must exclude board/**/* "
            "(the legacy durable build board)"
        )

    def test_stanza_contains_docs_cycles_exclude(self):
        stanza = generate_sources_toml_stanza("pinax", "/workspace/pinax")
        assert "docs/cycles/**/*" in stanza, (
            "sources.toml stanza must exclude docs/cycles/**/* "
            "(build-cycle artefacts / work-records)"
        )

    def test_existing_sources_toml_covers_all_four_classes(self):
        """
        The live sources.toml for the pinax source covers all four ADR-004 path classes.

        Reads the actual koine-memory sources.toml and verifies the pinax source
        entry's exclude list covers all four classes.
        """
        sources_toml_path = os.path.join(
            "/workspace/knowledge", ".koine-memory", "sources.toml"
        )
        if not os.path.isfile(sources_toml_path):
            pytest.skip("sources.toml not found at expected path — skipping live check")

        with open(sources_toml_path, "r", encoding="utf-8") as fh:
            content = fh.read()

        # Locate the pinax source stanza.
        if 'id = "pinax"' not in content:
            pytest.skip("pinax source not registered in sources.toml")

        # Extract the pinax stanza (from id = "pinax" to the next [[source]] or EOF).
        pinax_start = content.find('id = "pinax"')
        next_stanza = content.find("[[source]]", pinax_start + 1)
        pinax_stanza = content[pinax_start:next_stanza] if next_stanza != -1 else content[pinax_start:]

        assert ".ergon/**/*" in pinax_stanza, (
            "Live sources.toml pinax source must exclude .ergon/**/*"
        )
        assert "board/**/*" in pinax_stanza or '"board/' in pinax_stanza, (
            "Live sources.toml pinax source must exclude board/**/*"
        )
        assert "docs/cycles/**/*" in pinax_stanza, (
            "Live sources.toml pinax source must exclude docs/cycles/**/*"
        )
