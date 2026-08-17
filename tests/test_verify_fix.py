"""Tests for `pinax verify --fix` projection regeneration."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.projection import regenerate

pytestmark = pytest.mark.deep


ACTOR = "operator@example.test"


def _ts(sec: int) -> str:
    return f"2026-07-07T10:00:{sec:02d}Z"


def _append(log_dir: str, seq: int, actor: str, etype: str,
            payload: dict, prev: str = "") -> dict:
    event = mint_event(seq=seq, ts=_ts(seq), actor=actor, etype=etype,
                        payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _build_test_repo(root: str) -> str:
    """Minimal .ergon/log/ repo with a couple of items. Returns repo_root."""
    ergon_dir = os.path.join(root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

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

    _append(log_dir, seq=3, actor=ACTOR, etype="item.created",
            payload={"item_id": "pnx-bbb", "title": "Beta item",
                     "prefix": "phase-1", "status": "queued"},
            prev=prev)

    return root


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _run_cli(root: str, *args: str) -> subprocess.CompletedProcess:
    """Run 'python -m pinax <args>' in root, with pinax importable via PYTHONPATH."""
    pinax_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = pinax_src + (os.pathsep + existing_pp if existing_pp else "")
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture()
def repo():
    root = tempfile.mkdtemp()
    _build_test_repo(root)
    regenerate(root)  # start clean
    yield root
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (a) --fix is accepted by the parser
# ---------------------------------------------------------------------------

class TestFixFlagAccepted:
    def test_fix_flag_no_longer_unrecognized(self, repo):
        """'verify --fix' must not error with 'unrecognized arguments: --fix'."""
        result = _run_cli(repo, "verify", "--fix")
        combined = (result.stdout or "") + (result.stderr or "")
        assert "unrecognized arguments" not in combined, (
            "pinax verify --fix still rejects --fix as an unrecognized argument:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_fix_flag_on_clean_tree_exits_zero(self, repo):
        """On an already-clean projection, `--fix` is a no-op that exits 0."""
        result = _run_cli(repo, "verify", "--fix")
        assert result.returncode == 0, (
            f"verify --fix on a clean tree should exit 0.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_plain_verify_still_has_no_fix_flag_requirement(self, repo):
        """'pinax verify' (no --fix) still works exactly as before."""
        result = _run_cli(repo, "verify")
        assert result.returncode == 0, (
            f"plain verify on a clean tree should exit 0.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# (b) verify --fix actually clears drift, via the canonical regenerate() path
# ---------------------------------------------------------------------------

class TestFixClearsDrift:
    def test_fix_clears_hand_edited_board(self, repo):
        """A hand-edited board.md is drift; `verify --fix` regenerates it."""
        board_path = os.path.join(repo, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        # Plain verify still detects the drift.
        pre = _run_cli(repo, "verify")
        assert pre.returncode == 1, "expected drift to be detected before --fix"

        # --fix regenerates and reports success.
        fixed = _run_cli(repo, "verify", "--fix")
        assert fixed.returncode == 0, (
            f"verify --fix should exit 0 after clearing drift.\n"
            f"stdout: {fixed.stdout}\nstderr: {fixed.stderr}"
        )

        # A follow-up plain verify now passes.
        post = _run_cli(repo, "verify")
        assert post.returncode == 0, (
            "drift persisted after 'verify --fix' — regeneration did not clear it.\n"
            f"stdout: {post.stdout}\nstderr: {post.stderr}"
        )

    def test_fix_clears_missing_item_file(self, repo):
        """A deleted items/<id>.md is drift; `verify --fix` recreates it."""
        item_path = os.path.join(repo, ".ergon", "items", "pnx-aaa.md")
        assert os.path.isfile(item_path)
        os.remove(item_path)

        assert _run_cli(repo, "verify").returncode == 1

        fixed = _run_cli(repo, "verify", "--fix")
        assert fixed.returncode == 0
        assert os.path.isfile(item_path), "verify --fix did not recreate the missing item file"
        assert _run_cli(repo, "verify").returncode == 0

    def test_fix_clears_stale_item_file(self, repo):
        """A stale items/<id>.md is drift; `verify --fix` removes it."""
        items_dir = os.path.join(repo, ".ergon", "items")
        stale_path = os.path.join(items_dir, "pnx-zzz.md")
        with open(stale_path, "w", newline="\n", encoding="utf-8") as fh:
            fh.write("stale content\n")

        assert _run_cli(repo, "verify").returncode == 1

        fixed = _run_cli(repo, "verify", "--fix")
        assert fixed.returncode == 0
        assert not os.path.isfile(stale_path), "verify --fix did not remove the stale item file"

    def test_fix_output_byte_identical_to_direct_regenerate(self, repo):
        """
        verify -- use the SAME regeneration path as every state-changing
        command (pinax.projection.regenerate) -- not a parallel implementation.
        Proof: hand-edit, then  CLI, then compare against calling
        regenerate() directly on an independently-drifted-then-cleaned copy.
        """
        board_path = os.path.join(repo, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        _run_cli(repo, "verify", "--fix")
        fixed_via_cli = _read_bytes(board_path)

        # Independently regenerate straight from the log (the canonical path).
        regenerate(repo)
        fixed_via_direct_call = _read_bytes(board_path)

        assert fixed_via_cli == fixed_via_direct_call, (
            "verify --fix output differs from pinax.projection.regenerate() output — "
            "suggests --fix is not reusing the canonical regeneration path (SSOT violation)."
        )

    def test_fix_is_byte_deterministic_across_repeated_runs(self, repo):
        """Running -- in a row on a drifted tree yields byte-identical board.md."""
        board_path = os.path.join(repo, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        _run_cli(repo, "verify", "--fix")
        first = _read_bytes(board_path)

        # Re-drift and fix again.
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT AGAIN -->\n")
        _run_cli(repo, "verify", "--fix")
        second = _read_bytes(board_path)

        assert first == second, (
            "verify --fix is not byte-deterministic across repeated fix cycles.\n"
            f"First:  {first[:300]!r}\nSecond: {second[:300]!r}"
        )


# ---------------------------------------------------------------------------
# (c) plain drift message names a command that genuinely works
# ---------------------------------------------------------------------------

class TestDriftMessageNamesWorkingCommand:
    def test_message_mentions_fix_flag_that_actually_works(self, repo):
        """
        The plain (no --fix) drift message tells the user to run
        'pinax verify --fix'.  Confirm that exact command actually clears
        the drift it is complaining about (no more advertised-but-broken flag).
        """
        board_path = os.path.join(repo, ".ergon", "board.md")
        with open(board_path, "a", newline="\n", encoding="utf-8") as fh:
            fh.write("\n<!-- HAND EDIT -->\n")

        pre = _run_cli(repo, "verify")
        assert pre.returncode == 1
        combined = (pre.stdout or "") + (pre.stderr or "")
        assert "verify --fix" in combined, (
            "drift message no longer mentions 'verify --fix' -- if the message "
            "text changed, it must still name a command that genuinely works."
        )

        # Run exactly the command named in the message.
        fixed = _run_cli(repo, "verify", "--fix")
        assert fixed.returncode == 0, (
            "the command named in the drift message ('pinax verify --fix') "
            "did not actually clear the drift it was suggested for."
        )
