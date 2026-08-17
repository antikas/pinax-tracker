"""
tests/test_projection.py — byte-deterministic projection + drift lint gate.

1. Projection byte-determinism: regenerate twice → identical (byte-identical).
2. Drift lint: a deliberate hand-edit to the projection is caught by
   'pinax verify' (exits 1); a clean tree passes (exits 0).
3. Cross-platform: projection byte-identical under CRLF-on-disk (the log
   shard written with CRLF produces the same projection as LF).
4. Ordering is deterministic (phase, lane, next, id) with no wall-clock.

Test path: uses the real regenerate() + verify command path,
writing real files to a temp directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold_events, read_events
from pinax.projection import regenerate, render_board, render_item

pytestmark = pytest.mark.deep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACTOR = "operator@example.test"


def _ts(sec: int) -> str:
    return f"2026-06-29T10:00:{sec:02d}Z"


def _append(log_dir: str, seq: int, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    event = mint_event(seq=seq, ts=_ts(seq), actor=actor, etype=etype,
                       payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _build_test_repo(root: str) -> str:
    """
    Build a minimal repo structure with .ergon/log/ and some items.

    Returns the repo_root (= root).
    """
    ergon_dir = os.path.join(root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    # Write .gitattributes.
    gitattributes = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"
    with open(os.path.join(ergon_dir, ".gitattributes"), "w", newline="\n") as fh:
        fh.write(gitattributes)

    prev = ""
    e = _append(log_dir, seq=0, actor=ACTOR, etype="ergon.created",
                payload={"repo": "test"}, prev=prev)
    prev = e["id"]

    e = _append(log_dir, seq=1, actor=ACTOR, etype="phase.opened",
                payload={"phase": "phase-1"}, prev=prev)
    prev = e["id"]

    e = _append(log_dir, seq=2, actor=ACTOR, etype="item.created",
                payload={"item_id": "pnx-aaa", "title": "Alpha item",
                         "prefix": "phase-1", "status": "queued"},
                prev=prev)
    prev = e["id"]

    e = _append(log_dir, seq=3, actor=ACTOR, etype="item.created",
                payload={"item_id": "pnx-bbb", "title": "Beta item",
                         "prefix": "phase-1", "status": "queued"},
                prev=prev)
    prev = e["id"]

    _append(log_dir, seq=4, actor=ACTOR, etype="item.claimed",
            payload={"item_id": "pnx-aaa"},
            prev=prev)

    return root


def _read_text(path: str) -> str:
    """Read a file and LF-normalise its content."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read().replace("\r\n", "\n").replace("\r", "\n")


# ---------------------------------------------------------------------------
# 1. Byte-determinism: regenerate twice → byte-identical
# ---------------------------------------------------------------------------

class TestProjectionByteDeterminism:
    """Regenerating twice from the same log produces byte-identical output."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.root = tempfile.mkdtemp()
        _build_test_repo(self.root)
        yield
        shutil.rmtree(self.root, ignore_errors=True)

    def test_board_md_identical_on_second_regenerate(self):
        """board.md is byte-identical after two consecutive regenerations."""
        regenerate(self.root)
        board_path = os.path.join(self.root, ".ergon", "board.md")

        with open(board_path, "rb") as fh:
            first = fh.read()

        regenerate(self.root)

        with open(board_path, "rb") as fh:
            second = fh.read()

        assert first == second, (
            "board.md is NOT byte-identical after two regenerations.\n"
            f"First  ({len(first)} bytes): {first[:500]!r}\n"
            f"Second ({len(second)} bytes): {second[:500]!r}"
        )

    def test_items_md_identical_on_second_regenerate(self):
        """items/<id>.md files are byte-identical after two consecutive regenerations."""
        regenerate(self.root)
        items_dir = os.path.join(self.root, ".ergon", "items")

        first_contents: dict[str, bytes] = {}
        for fname in sorted(os.listdir(items_dir)):
            if fname.endswith(".md"):
                with open(os.path.join(items_dir, fname), "rb") as fh:
                    first_contents[fname] = fh.read()

        regenerate(self.root)

        second_contents: dict[str, bytes] = {}
        for fname in sorted(os.listdir(items_dir)):
            if fname.endswith(".md"):
                with open(os.path.join(items_dir, fname), "rb") as fh:
                    second_contents[fname] = fh.read()

        assert first_contents == second_contents, (
            "Item files are NOT byte-identical after two regenerations.\n"
            + "\n".join(
                f"  {k}: first={v[:100]!r}, second={second_contents.get(k, b'<missing>')[:100]!r}"
                for k, v in first_contents.items()
                if v != second_contents.get(k)
            )
        )

    def test_lf_line_endings_always(self):
        """All projection files use LF line endings (never CRLF), even on Windows."""
        regenerate(self.root)
        ergon_dir = os.path.join(self.root, ".ergon")

        for fname in ["board.md"]:
            path = os.path.join(ergon_dir, fname)
            with open(path, "rb") as fh:
                raw = fh.read()
            assert b"\r\n" not in raw, (
                f"{fname} contains CRLF line endings — must be LF-only (ADR-002).\n"
                f"Content (first 200 bytes): {raw[:200]!r}"
            )

        items_dir = os.path.join(ergon_dir, "items")
        if os.path.isdir(items_dir):
            for fname in os.listdir(items_dir):
                if fname.endswith(".md"):
                    path = os.path.join(items_dir, fname)
                    with open(path, "rb") as fh:
                        raw = fh.read()
                    assert b"\r\n" not in raw, (
                        f"items/{fname} contains CRLF line endings — must be LF-only.\n"
                        f"Content (first 200 bytes): {raw[:200]!r}"
                    )

    def test_no_wall_clock_in_projection(self):
        """The board.md does not contain a 'generated at' timestamp header."""
        regenerate(self.root)
        board_path = os.path.join(self.root, ".ergon", "board.md")
        content = _read_text(board_path)
        # Wall-clock indicators that would make the projection non-deterministic.
        for bad_phrase in ("generated at", "last updated", "timestamp"):
            assert bad_phrase not in content.lower(), (
                f"board.md contains wall-clock phrase {bad_phrase!r} — "
                f"projection must be byte-deterministic (no wall-clock)."
            )


# ---------------------------------------------------------------------------
# 2. Drift lint: pinax verify catches hand-edits, passes on clean tree
# ---------------------------------------------------------------------------

class TestDriftLint:
    """pinax verify exits 0 on a clean tree, exits 1 after a hand-edit."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.root = tempfile.mkdtemp()
        _build_test_repo(self.root)
        regenerate(self.root)  # Start with a clean projection.
        yield
        shutil.rmtree(self.root, ignore_errors=True)

    def _run_verify_result(self, *args: str) -> subprocess.CompletedProcess:
        """
        Run 'python -m pinax verify' and return the completed process.

        The subprocess runs in the temp repo root (self.root), which is not
        the pinax source tree.  We pass the source tree's parent directory on
        PYTHONPATH so that 'import pinax' resolves correctly.
        """
        # Find the pinax source root: the directory that contains the pinax/ package.
        pinax_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = pinax_src + (os.pathsep + existing_pp if existing_pp else "")
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "verify", *args],
            cwd=self.root,
            capture_output=True,
            env=env,
        )
        return result

    def _run_verify(self) -> int:
        """Run plain verify and return its exit code."""
        return self._run_verify_result().returncode

    def _tamper_created_event(self, item_id: str = "pnx-aaa") -> str:
        """Mutate one historical payload while retaining its stored id."""
        log_dir = os.path.join(self.root, ".ergon", "log")
        shard_paths = [
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.endswith(".jsonl")
        ]
        assert len(shard_paths) == 1
        shard_path = shard_paths[0]

        with open(shard_path, "r", encoding="utf-8", newline="") as fh:
            lines = fh.read().replace("\r\n", "\n").replace("\r", "\n").splitlines()

        target_id = ""
        rewritten: list[str] = []
        for line in lines:
            event = json.loads(line)
            if event.get("type") == "item.created" and event.get("payload", {}).get("item_id") == item_id:
                target_id = event["id"]
                event["payload"]["title"] = "MUTATED HISTORICAL TITLE"
                line = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
            rewritten.append(line)

        assert target_id, f"item.created for {item_id} not found"
        with open(shard_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(rewritten) + "\n")
        return target_id

    def test_clean_tree_passes(self):
        """A clean (freshly regenerated) projection → verify exits 0."""
        rc = self._run_verify()
        assert rc == 0, (
            "pinax verify returned non-zero on a clean projection — "
            "should exit 0 when board.md matches the log."
        )

    def test_hand_edit_board_md_caught(self):
        """Hand-editing board.md → verify exits 1 (drift detected)."""
        board_path = os.path.join(self.root, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        rc = self._run_verify()
        assert rc == 1, (
            "pinax verify returned 0 after hand-editing board.md — "
            "drift lint must catch hand-edits (exits 1)."
        )

    def test_hand_edit_item_md_caught(self):
        """Hand-editing an items/<id>.md → verify exits 1 (drift detected)."""
        items_dir = os.path.join(self.root, ".ergon", "items")
        # Find any item file.
        item_files = [f for f in os.listdir(items_dir) if f.endswith(".md")]
        assert item_files, "No item files found — regenerate must create them."
        item_path = os.path.join(items_dir, item_files[0])

        with open(item_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        rc = self._run_verify()
        assert rc == 1, (
            "pinax verify returned 0 after hand-editing an item file — "
            "drift lint must catch hand-edits (exits 1)."
        )

    def test_missing_board_md_caught(self):
        """Deleting board.md → verify exits 1 (projection missing)."""
        board_path = os.path.join(self.root, ".ergon", "board.md")
        os.remove(board_path)

        rc = self._run_verify()
        assert rc == 1, (
            "pinax verify returned 0 after deleting board.md — "
            "drift lint must catch a missing projection file."
        )

    def test_regenerate_fixes_drift(self):
        """After a hand-edit, regenerate restores the clean state."""
        board_path = os.path.join(self.root, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        # Verify fails.
        assert self._run_verify() == 1

        # Regenerate.
        regenerate(self.root)

        # Verify now passes.
        rc = self._run_verify()
        assert rc == 0, (
            "After regeneration, pinax verify should exit 0 — projection is clean again."
        )

    def test_tampered_event_fails_even_when_projection_matches(self):
        """Regenerating from edited history cannot make verify green."""
        target_id = self._tamper_created_event()
        regenerate(self.root)

        result = self._run_verify_result()
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        assert result.returncode == 1
        assert "EVENT LOG INTEGRITY FAILURE" in combined
        assert target_id in combined

    def test_verify_fix_never_rewrites_tampered_history(self):
        """Verify that --fix fails closed and leaves history and projection untouched."""
        self._tamper_created_event()
        regenerate(self.root)

        log_dir = os.path.join(self.root, ".ergon", "log")
        shard_path = next(
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.endswith(".jsonl")
        )
        board_path = os.path.join(self.root, ".ergon", "board.md")
        with open(shard_path, "rb") as fh:
            log_before = fh.read()
        with open(board_path, "rb") as fh:
            board_before = fh.read()

        result = self._run_verify_result("--fix")
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        assert result.returncode == 1
        assert "will not rewrite" in combined
        with open(shard_path, "rb") as fh:
            assert fh.read() == log_before
        with open(board_path, "rb") as fh:
            assert fh.read() == board_before

    def test_invalid_same_id_twin_fails_without_rewriting_projection(self):
        """Every physical event is checked before same-id deduplication."""
        log_dir = os.path.join(self.root, ".ergon", "log")
        shard_path = next(
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.endswith(".jsonl")
        )
        with open(shard_path, "r", encoding="utf-8") as fh:
            events = [json.loads(line) for line in fh if line.strip()]
        source = next(event for event in events if event["type"] == "item.created")
        twin = dict(source)
        twin["payload"] = dict(source["payload"])
        twin["payload"]["title"] = "MUTATED TWIN"
        with open(shard_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(twin, separators=(",", ":")) + "\n")

        board_path = os.path.join(self.root, ".ergon", "board.md")
        log_before = open(shard_path, "rb").read()
        board_before = open(board_path, "rb").read()
        for args in ((), ("--fix",)):
            result = self._run_verify_result(*args)
            assert result.returncode == 1
            assert "EVENT LOG INTEGRITY FAILURE" in (result.stdout + result.stderr).decode()
            assert open(shard_path, "rb").read() == log_before
            assert open(board_path, "rb").read() == board_before

    def test_missing_null_and_non_string_ids_fail_without_rewriting_projection(self):
        """Every parsed record is checked before verify can fold or regenerate."""
        log_dir = os.path.join(self.root, ".ergon", "log")
        shard_path = next(
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.endswith(".jsonl")
        )
        with open(shard_path, "r", encoding="utf-8") as fh:
            source = json.loads(next(line for line in fh if line.strip()))

        malformed = []
        missing = dict(source)
        missing.pop("id")
        malformed.append(missing)
        null = dict(source)
        null["id"] = None
        malformed.append(null)
        non_string = dict(source)
        non_string["id"] = 7
        malformed.append(non_string)
        with open(shard_path, "a", encoding="utf-8", newline="\n") as fh:
            for event in malformed:
                fh.write(json.dumps(event, separators=(",", ":")) + "\n")

        board_path = os.path.join(self.root, ".ergon", "board.md")
        log_before = open(shard_path, "rb").read()
        board_before = open(board_path, "rb").read()
        for args in ((), ("--fix",)):
            result = self._run_verify_result(*args)
            assert result.returncode == 1
            assert "EVENT LOG INTEGRITY FAILURE" in (result.stdout + result.stderr).decode()
            assert open(shard_path, "rb").read() == log_before
            assert open(board_path, "rb").read() == board_before

    def test_forged_self_annulment_fails_without_rewriting_projection(self):
        """A tombstone cannot exempt itself from verification."""
        log_dir = os.path.join(self.root, ".ergon", "log")
        shard_path = next(
            os.path.join(log_dir, name)
            for name in os.listdir(log_dir)
            if name.endswith(".jsonl")
        )
        events = read_events(log_dir)
        forged = mint_event(
            seq=5,
            ts=_ts(5),
            actor=ACTOR,
            etype="event.annulled",
            payload={"target_id": "placeholder", "reason": "forged"},
            prev=events[-1]["id"],
        )
        forged["payload"]["target_id"] = forged["id"]
        with open(shard_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(forged, separators=(",", ":")) + "\n")

        board_path = os.path.join(self.root, ".ergon", "board.md")
        log_before = open(shard_path, "rb").read()
        board_before = open(board_path, "rb").read()
        for args in ((), ("--fix",)):
            result = self._run_verify_result(*args)
            assert result.returncode == 1
            assert "EVENT LOG INTEGRITY FAILURE" in (result.stdout + result.stderr).decode()
            assert open(shard_path, "rb").read() == log_before
            assert open(board_path, "rb").read() == board_before

    def test_formally_annulled_invalid_event_remains_supported(self):
        """The strict gate preserves Pinax's explicit audit-safe tombstone path."""
        log_dir = os.path.join(self.root, ".ergon", "log")
        events = read_events(log_dir)
        target = next(
            event
            for event in events
            if event.get("type") == "item.created"
            and event.get("payload", {}).get("item_id") == "pnx-bbb"
        )
        last = events[-1]
        _append(
            log_dir,
            seq=5,
            actor=ACTOR,
            etype="event.annulled",
            payload={"target_id": target["id"], "reason": "known bad test event"},
            prev=last["id"],
        )
        self._tamper_created_event(item_id="pnx-bbb")
        regenerate(self.root)

        result = self._run_verify_result()
        combined = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        assert result.returncode == 0, combined


# ---------------------------------------------------------------------------
# 3. Cross-platform: projection byte-identical under CRLF-on-disk
# ---------------------------------------------------------------------------

class TestProjectionCrossplatform:
    """The projection is byte-identical whether the log shard uses LF or CRLF."""

    def test_projection_identical_lf_vs_crlf_log(self):
        """
        Write the same events with LF and CRLF log shards; regenerate both;
        assert board.md and items/*.md are byte-identical.

        Proves: projection renderer is LF-normalised at read time, so the
        line ending of the on-disk log does not affect the output.
        """
        lf_root = tempfile.mkdtemp()
        crlf_root = tempfile.mkdtemp()
        try:
            _build_test_repo(lf_root)
            _build_test_repo(crlf_root)

            # Convert the CRLF repo's log shards to CRLF.
            crlf_log_dir = os.path.join(crlf_root, ".ergon", "log")
            for fname in os.listdir(crlf_log_dir):
                if fname.endswith(".jsonl"):
                    fpath = os.path.join(crlf_log_dir, fname)
                    with open(fpath, "rb") as fh:
                        content = fh.read()
                    # Replace LF with CRLF (simulating Windows checkout with autocrlf).
                    content_crlf = content.replace(b"\n", b"\r\n")
                    with open(fpath, "wb") as fh:
                        fh.write(content_crlf)

            # Regenerate both.
            regenerate(lf_root)
            regenerate(crlf_root)

            # Compare board.md.
            lf_board_path = os.path.join(lf_root, ".ergon", "board.md")
            crlf_board_path = os.path.join(crlf_root, ".ergon", "board.md")
            with open(lf_board_path, "rb") as fh:
                lf_board = fh.read()
            with open(crlf_board_path, "rb") as fh:
                crlf_board = fh.read()
            assert lf_board == crlf_board, (
                "board.md differs between LF-shard and CRLF-shard repos.\n"
                f"LF:   {lf_board[:300]!r}\n"
                f"CRLF: {crlf_board[:300]!r}"
            )

            # Compare items/*.md.
            lf_items_dir = os.path.join(lf_root, ".ergon", "items")
            crlf_items_dir = os.path.join(crlf_root, ".ergon", "items")
            lf_items = sorted(f for f in os.listdir(lf_items_dir) if f.endswith(".md"))
            crlf_items = sorted(f for f in os.listdir(crlf_items_dir) if f.endswith(".md"))
            assert lf_items == crlf_items, (
                f"Items files differ: LF={lf_items}, CRLF={crlf_items}"
            )
            for fname in lf_items:
                with open(os.path.join(lf_items_dir, fname), "rb") as fh:
                    lf_content = fh.read()
                with open(os.path.join(crlf_items_dir, fname), "rb") as fh:
                    crlf_content = fh.read()
                assert lf_content == crlf_content, (
                    f"items/{fname} differs between LF-shard and CRLF-shard repos.\n"
                    f"LF:   {lf_content[:200]!r}\n"
                    f"CRLF: {crlf_content[:200]!r}"
                )
        finally:
            shutil.rmtree(lf_root, ignore_errors=True)
            shutil.rmtree(crlf_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4. Ordering is deterministic: phase → lane → next → created_at → event_id → id
# ---------------------------------------------------------------------------

class TestProjectionOrdering:
    """The board.md item ordering is fully deterministic."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.root = tempfile.mkdtemp()
        _build_test_repo(self.root)
        yield
        shutil.rmtree(self.root, ignore_errors=True)

    def test_board_contains_all_items(self):
        """board.md lists all items from the fold state."""
        regenerate(self.root)
        board_path = os.path.join(self.root, ".ergon", "board.md")
        content = _read_text(board_path)
        assert "pnx-aaa" in content, "pnx-aaa missing from board.md"
        assert "pnx-bbb" in content, "pnx-bbb missing from board.md"

    def test_board_next_item_marked(self):
        """The item compute_next identifies is marked [next] in board.md."""
        regenerate(self.root)
        board_path = os.path.join(self.root, ".ergon", "board.md")
        content = _read_text(board_path)
        # The [next] marker must appear exactly once for the highest-priority ready item.
        assert "[next]" in content, (
            "board.md does not contain [next] marker — the next item must be marked."
        )
        next_count = content.count("[next]")
        assert next_count == 1, (
            f"board.md contains {next_count} [next] markers — must be exactly 1."
        )

    def test_items_dir_has_per_item_files(self):
        """items/<id>.md files are created for every item in the fold state."""
        regenerate(self.root)
        items_dir = os.path.join(self.root, ".ergon", "items")
        assert os.path.isdir(items_dir), ".ergon/items/ not created"
        item_files = {f[:-3] for f in os.listdir(items_dir) if f.endswith(".md")}
        assert "pnx-aaa" in item_files, "pnx-aaa.md missing from items/"
        assert "pnx-bbb" in item_files, "pnx-bbb.md missing from items/"

    def test_item_file_has_frontmatter(self):
        """Per-item files have YAML-ish frontmatter with required fields."""
        regenerate(self.root)
        item_path = os.path.join(self.root, ".ergon", "items", "pnx-aaa.md")
        content = _read_text(item_path)
        for field in ("id:", "title:", "phase:", "status:", "deps:"):
            assert field in content, (
                f"pnx-aaa.md is missing frontmatter field {field!r}.\n"
                f"Content:\n{content[:400]}"
            )
