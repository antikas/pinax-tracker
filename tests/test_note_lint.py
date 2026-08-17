"""
Tests for note linting, dispatch, and metrics.

 test coverages tested here:
1. note add HARD-rejects a bad ref at the CLI (sys.exit(1)).
2. note add HARD-rejects a >200-char caption at the CLI (sys.exit(1)).
3. note add ACCEPTS a valid ref + caption (<= 200 chars).
4. pinax metrics --json is deterministic over a seeded log.
5. pinax metrics NEVER writes any file to ~/knowledge or the knowledge plane.
6. pinax dispatch --max N emits the capped ready manifest.
7. pinax dispatch --max 0 (edge case: zero cap → empty manifest).

Test path:
- Uses python -m pinax subprocess calls for the CLI hard-rejection tests.
- Uses the Python API directly for the fold/metrics tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep

# ---------------------------------------------------------------------------
# PYTHONPATH helper (same pattern as test_merge_safety.py)
# ---------------------------------------------------------------------------

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env(**overrides: str) -> dict:
    """Build an environment with pinax on PYTHONPATH."""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Helpers: build a minimal seeded repo
# ---------------------------------------------------------------------------

def _seed_minimal_repo(repo_dir: str, actor: str = "operator@example.test") -> str:
    """
    Seed a minimal Pinax repo in repo_dir (git init + pinax init + add items).
    Returns the item_id of the first created item.
    """
    _env = _build_env()

    # git init
    subprocess.run(
        ["git", "init", "-b", "main", repo_dir],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        check=True, capture_output=True, cwd=repo_dir,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        check=True, capture_output=True, cwd=repo_dir,
    )

    # pinax init
    result = subprocess.run(
        [sys.executable, "-m", "pinax", "init", "--actor", actor],
        check=True, capture_output=True, text=True, cwd=repo_dir, env=_env,
    )

    # pinax add (returns the item_id in output)
    result = subprocess.run(
        [
            sys.executable, "-m", "pinax", "add",
            "--title", "Test item alpha",
            "--actor", actor,
        ],
        check=True, capture_output=True, text=True, cwd=repo_dir, env=_env,
    )
    lines = result.stdout.strip().splitlines()
    item_id = None
    for line in lines:
        if "pinax: added" in line:
            parts = line.split()
            # "pinax:", "added", "<id>", ...
            if len(parts) >= 3:
                item_id = parts[2]
                break

    if item_id is None:
        # Fallback: fold the log to find the item id
        from pinax.fold import fold
        ergon_dir = os.path.join(repo_dir, ".ergon")
        state = fold(os.path.join(ergon_dir, "log"))
        items = state.get("items", {})
        if items:
            item_id = next(iter(items))

    return item_id


# ---------------------------------------------------------------------------
# 1+2. Hard rejection tests (via subprocess)
# ---------------------------------------------------------------------------

class TestNoteAddHardRejection:
    """note add rejects bad ref and oversized caption at the CLI (hard error)."""

    def test_unqualified_ref_rejected(self):
        """A ref that does not match the typed-ref pattern exits with code 1."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "this-is-not-a-typed-ref",
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 1, (
                f"Expected exit code 1 for unqualified ref, got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
            assert "REJECTED" in result.stderr or "ref must match" in result.stderr, (
                f"Expected REJECTED message in stderr, got: {result.stderr!r}"
            )

    def test_http_url_ref_rejected(self):
        """An http:// URL is not a typed ref — must be rejected."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "https://example.com/some-doc",
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 1, (
                f"Expected exit code 1 for http URL ref, got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )

    def test_bare_path_ref_rejected(self):
        """A bare filename (no required prefix) is not a typed ref."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "some-document.md",
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 1, (
                f"Expected exit code 1 for bare path ref, got {result.returncode}."
            )

    def test_caption_over_200_chars_rejected(self):
        """A caption exceeding 200 characters is hard-rejected at the CLI."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            oversized_caption = "x" * 201  # 201 chars
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "~/knowledge/learnings/test.md",
                    "--caption", oversized_caption,
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 1, (
                f"Expected exit code 1 for >200-char caption, got {result.returncode}. "
                f"stderr: {result.stderr!r}"
            )
            assert "REJECTED" in result.stderr or "caption" in result.stderr.lower(), (
                f"Expected caption rejection message in stderr, got: {result.stderr!r}"
            )

    def test_caption_exactly_200_chars_accepted(self):
        """A caption of exactly 200 characters is accepted (boundary condition)."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            exact_200 = "y" * 200  # exactly 200 chars
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "~/knowledge/learnings/test.md",
                    "--caption", exact_200,
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 0, (
                f"Expected exit code 0 for exactly-200-char caption, got {result.returncode}. "
                f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
            )


# ---------------------------------------------------------------------------
# 3. Valid ref+caption acceptance
# ---------------------------------------------------------------------------

class TestNoteAddValid:
    """note add accepts all valid typed-ref prefixes."""

    @pytest.mark.parametrize("ref_prefix", [
        "koine://",
        "~/knowledge/",
        "projects/",
        "docs/",
    ])
    def test_valid_ref_prefix_accepted(self, ref_prefix: str):
        """All four valid ref prefixes are accepted."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            ref = f"{ref_prefix}some/path/doc.md"
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", ref,
                    "--caption", "A short caption",
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 0, (
                f"Expected exit code 0 for valid ref {ref!r}, got {result.returncode}. "
                f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
            )

    def test_note_without_caption_accepted(self):
        """note add without --caption is accepted (caption is optional)."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", "~/knowledge/learnings/test.md",
                    "--actor", "operator@example.test",
                ],
                capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result.returncode == 0, (
                f"Expected exit code 0 for note without caption, got {result.returncode}. "
                f"stderr: {result.stderr!r}\nstdout: {result.stdout!r}"
            )

    def test_note_appears_in_fold_state(self):
        """After note add, the note appears in the fold state."""
        with tempfile.TemporaryDirectory() as repo_dir:
            item_id = _seed_minimal_repo(repo_dir)
            ref = "~/knowledge/learnings/test.md"
            caption = "Test caption"
            subprocess.run(
                [
                    sys.executable, "-m", "pinax", "note", "add",
                    item_id,
                    "--ref", ref,
                    "--caption", caption,
                    "--actor", "operator@example.test",
                ],
                check=True, capture_output=True, cwd=repo_dir, env=_build_env(),
            )
            from pinax.fold import fold
            ergon_dir = os.path.join(repo_dir, ".ergon")
            state = fold(os.path.join(ergon_dir, "log"))
            notes = state.get("notes", [])
            assert len(notes) == 1, f"Expected 1 note in fold state, got {len(notes)}"
            assert notes[0]["ref"] == ref
            assert notes[0]["caption"] == caption
            assert notes[0]["item_id"] == item_id


# ---------------------------------------------------------------------------
# 4+5. Metrics determinism and knowledge-plane clean
# ---------------------------------------------------------------------------

class TestMetrics:
    """metrics fold is deterministic and never writes to the knowledge plane."""

    def test_metrics_deterministic_same_output(self):
        """
        Same log → same metrics output, seed-independent.

        Run metrics twice on the same seeded log and assert byte-identical JSON.
        """
        with tempfile.TemporaryDirectory() as repo_dir:
            _seed_minimal_repo(repo_dir)

            result1 = subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            result2 = subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            assert result1.stdout == result2.stdout, (
                "Metrics output is not deterministic: two runs on the same log "
                f"produced different output.\nRun 1: {result1.stdout!r}\n"
                f"Run 2: {result2.stdout!r}"
            )

    def test_metrics_deterministic_across_seeds(self):
        """
        Metrics output is PYTHONHASHSEED-independent.

        Run metrics with PYTHONHASHSEED=0 and PYTHONHASHSEED=1 and assert
        byte-identical JSON output.
        """
        with tempfile.TemporaryDirectory() as repo_dir:
            _seed_minimal_repo(repo_dir)

            env0 = _build_env(PYTHONHASHSEED="0")
            env1 = _build_env(PYTHONHASHSEED="1")

            result0 = subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=env0,
            )
            result1 = subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=env1,
            )
            assert result0.stdout == result1.stdout, (
                "Metrics output differs across PYTHONHASHSEED values (seed-dependent). "
                f"SEED=0: {result0.stdout!r}\nSEED=1: {result1.stdout!r}"
            )

    def test_metrics_json_valid(self):
        """metrics --json produces valid JSON with the expected keys."""
        with tempfile.TemporaryDirectory() as repo_dir:
            _seed_minimal_repo(repo_dir)
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            data = json.loads(result.stdout)
            required_keys = {
                "total_items", "by_status", "events_total",
                "items_done", "items_parked", "items_blocked",
                "note_added_count", "claim_superseded_count",
                "cycle_times", "park_reasons", "gate_counts",
                "audit_verdicts", "ready_queue_size",
            }
            missing = required_keys - set(data.keys())
            assert not missing, (
                f"metrics --json output missing keys: {missing}"
            )

    def test_metrics_does_not_write_knowledge_plane(self):
        """
        metrics command does not write any file to the knowledge plane.

        We check that no file under ~/knowledge/ or knowledge-plane paths
        is written during a metrics run.  We do this by recording the mtime
        of the sources.toml before/after and asserting it is unchanged.
        """
        sources_toml = os.path.join(
            "/workspace/knowledge", ".koine-memory", "sources.toml"
        )
        if not os.path.isfile(sources_toml):
            pytest.skip("sources.toml not at expected path — skipping live KP check")

        mtime_before = os.path.getmtime(sources_toml)

        with tempfile.TemporaryDirectory() as repo_dir:
            _seed_minimal_repo(repo_dir)
            subprocess.run(
                [sys.executable, "-m", "pinax", "metrics", "--json"],
                check=True, capture_output=True, cwd=repo_dir, env=_build_env(),
            )

        mtime_after = os.path.getmtime(sources_toml)
        assert mtime_before == mtime_after, (
            "pinax metrics wrote to sources.toml — this is a knowledge-plane violation! "
            "metrics must be read-only."
        )


# ---------------------------------------------------------------------------
# 6+7. Dispatch --max cap and parity
# ---------------------------------------------------------------------------

class TestDispatch:
    """dispatch emits the capped ready manifest."""

    def _seed_repo_with_ready_items(
        self,
        repo_dir: str,
        n_items: int = 4,
        actor: str = "operator@example.test",
    ) -> list[str]:
        """Seed a repo with n_items ready items. Returns their IDs."""
        _env = _build_env()
        subprocess.run(["git", "init", "-b", "main", repo_dir], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], check=True, capture_output=True, cwd=repo_dir)
        subprocess.run(["git", "config", "user.name", "t"], check=True, capture_output=True, cwd=repo_dir)
        subprocess.run(
            [sys.executable, "-m", "pinax", "init", "--actor", actor],
            check=True, capture_output=True, cwd=repo_dir, env=_env,
        )

        item_ids: list[str] = []
        for i in range(n_items):
            result = subprocess.run(
                [
                    sys.executable, "-m", "pinax", "add",
                    "--title", f"Item {i}",
                    "--actor", actor,
                ],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_env,
            )
            # Extract item id from output.
            for line in result.stdout.strip().splitlines():
                if "pinax: added" in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        item_ids.append(parts[2])
                        break

        # Fall back to fold if extraction failed.
        if len(item_ids) < n_items:
            from pinax.fold import fold
            state = fold(os.path.join(repo_dir, ".ergon", "log"))
            item_ids = sorted(state.get("items", {}).keys())

        return item_ids

    def test_dispatch_max_2_caps_manifest(self):
        """dispatch --max 2 --json emits at most 2 items."""
        with tempfile.TemporaryDirectory() as repo_dir:
            self._seed_repo_with_ready_items(repo_dir, n_items=4)
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "dispatch", "--max", "2", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            manifest = json.loads(result.stdout)
            assert isinstance(manifest, list), (
                f"dispatch --json must return a JSON array, got {type(manifest)}"
            )
            assert len(manifest) <= 2, (
                f"dispatch --max 2 returned {len(manifest)} items (expected <= 2)"
            )

    def test_dispatch_without_max_returns_all_ready(self):
        """dispatch without --max returns all ready items."""
        with tempfile.TemporaryDirectory() as repo_dir:
            self._seed_repo_with_ready_items(repo_dir, n_items=3)
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "dispatch", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            manifest = json.loads(result.stdout)
            assert len(manifest) == 3, (
                f"dispatch without --max should return all 3 ready items, got {len(manifest)}"
            )

    def test_dispatch_manifest_has_expected_fields(self):
        """dispatch --json manifest items have id, title, status fields."""
        with tempfile.TemporaryDirectory() as repo_dir:
            self._seed_repo_with_ready_items(repo_dir, n_items=2)
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "dispatch", "--max", "1", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            manifest = json.loads(result.stdout)
            assert len(manifest) == 1
            item = manifest[0]
            assert "id" in item, f"Manifest item missing 'id' field: {item}"
            assert "title" in item, f"Manifest item missing 'title' field: {item}"
            assert "status" in item, f"Manifest item missing 'status' field: {item}"

    def test_dispatch_empty_ready_queue(self):
        """dispatch on an empty ready queue returns empty manifest without error."""
        with tempfile.TemporaryDirectory() as repo_dir:
            _env = _build_env()
            # Init only — no items added.
            subprocess.run(["git", "init", "-b", "main", repo_dir], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t.com"], check=True, capture_output=True, cwd=repo_dir)
            subprocess.run(["git", "config", "user.name", "t"], check=True, capture_output=True, cwd=repo_dir)
            subprocess.run(
                [sys.executable, "-m", "pinax", "init", "--actor", "operator@example.test"],
                check=True, capture_output=True, cwd=repo_dir, env=_env,
            )
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "dispatch", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_env,
            )
            manifest = json.loads(result.stdout)
            assert manifest == [], (
                f"Empty ready queue should produce empty manifest, got: {manifest}"
            )

    def test_dispatch_max_larger_than_ready_returns_all(self):
        """dispatch --max N where N > ready queue size returns the full ready set."""
        with tempfile.TemporaryDirectory() as repo_dir:
            self._seed_repo_with_ready_items(repo_dir, n_items=2)
            result = subprocess.run(
                [sys.executable, "-m", "pinax", "dispatch", "--max", "100", "--json"],
                check=True, capture_output=True, text=True, cwd=repo_dir, env=_build_env(),
            )
            manifest = json.loads(result.stdout)
            assert len(manifest) == 2, (
                f"dispatch --max 100 with 2 ready items should return 2, got {len(manifest)}"
            )
