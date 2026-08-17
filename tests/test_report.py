"""Report command tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

from pinax.fold import fold
from pinax.commands.report_cmd import run as report_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_dir: str) -> str:
    """Create a minimal .ergon/log/ directory, return repo root."""
    log_dir = os.path.join(tmp_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    # Install .gitattributes
    ga_path = os.path.join(tmp_dir, ".ergon", ".gitattributes")
    with open(ga_path, "w", newline="\n") as fh:
        fh.write("*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n")
    return tmp_dir


def _write_events(log_dir: str, events: list[dict]) -> None:
    """Write events to a test shard, LF-terminated."""
    shard_path = os.path.join(log_dir, "operator-example.test.jsonl")
    with open(shard_path, "w", newline="\n", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True, ensure_ascii=True) + "\n")


def _make_event(seq: int, ts: str, actor: str, etype: str, payload: dict, prev: str = "") -> dict:
    """Build an event with a real blake2b hash id (matches pinax.event.mint_event)."""
    import hashlib
    import base64
    canonical = json.dumps(
        {"seq": seq, "ts": ts, "actor": actor, "type": etype, "payload": payload},
        sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.blake2b(canonical, digest_size=32).digest()
    eid = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
    return {
        "id": eid,
        "seq": seq,
        "ts": ts,
        "actor": actor,
        "type": etype,
        "payload": payload,
        "prev": prev,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReportBasic:
    """Basic shape and correctness of pinax report over a known log."""

    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        _make_repo(self.tmp)
        self.log_dir = os.path.join(self.tmp, ".ergon", "log")

        # Seed a realistic log: one ergon.created, one phase, four items.
        ts_base = "2026-06-30T10:00:00Z"
        events = []

        # ergon.created + phase.opened
        e0 = _make_event(0, ts_base, "operator@example.test", "ergon.created", {"repo": "test"})
        e1 = _make_event(1, ts_base, "operator@example.test", "phase.opened", {"phase": "pnx"}, prev=e0["id"])
        events += [e0, e1]

        # item A — will be done (shipped)
        eA = _make_event(2, ts_base, "operator@example.test", "item.created",
                         {"item_id": "pnx-aaa", "title": "Item A", "prefix": "pnx", "status": "queued"},
                         prev=e1["id"])
        eAdone = _make_event(3, "2026-06-30T11:00:00Z", "operator@example.test", "item.completed",
                             {"item_id": "pnx-aaa", "briefing": "done A"}, prev=eA["id"])
        events += [eA, eAdone]

        # item B — parked
        eB = _make_event(4, ts_base, "operator@example.test", "item.created",
                         {"item_id": "pnx-bbb", "title": "Item B", "prefix": "pnx", "status": "queued"},
                         prev=eAdone["id"])
        eBpark = _make_event(5, "2026-06-30T10:05:00Z", "operator@example.test", "item.parked",
                             {"item_id": "pnx-bbb", "reason": "needs owner decision"},
                             prev=eB["id"])
        events += [eB, eBpark]

        # item C — blocked (gate)
        eC = _make_event(6, ts_base, "operator@example.test", "item.created",
                         {"item_id": "pnx-ccc", "title": "Item C", "prefix": "pnx", "status": "queued"},
                         prev=eBpark["id"])
        eCblock = _make_event(7, "2026-06-30T10:06:00Z", "operator@example.test", "item.blocked",
                              {"item_id": "pnx-ccc", "gate": "decision"},
                              prev=eC["id"])
        events += [eC, eCblock]

        # item D — queued (will be next, no blockers on A which is done)
        eD = _make_event(8, ts_base, "operator@example.test", "item.created",
                         {"item_id": "pnx-ddd", "title": "Item D", "prefix": "pnx", "status": "queued"},
                         prev=eCblock["id"])
        events.append(eD)

        _write_events(self.log_dir, events)

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_json_shape(self) -> None:
        """--json output has the correct keys and section types."""
        out_lines = []
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        output = buf.getvalue()

        report = json.loads(output)
        assert "shipped" in report
        assert "parked" in report
        assert "failed" in report
        assert "next" in report
        assert isinstance(report["shipped"], list)
        assert isinstance(report["parked"], list)
        assert isinstance(report["failed"], list)

    def test_report_shipped_count(self) -> None:
        """shipped section contains the done item."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        report = json.loads(buf.getvalue())
        assert len(report["shipped"]) == 1
        assert report["shipped"][0]["id"] == "pnx-aaa"
        assert report["shipped"][0]["title"] == "Item A"

    def test_report_parked_count(self) -> None:
        """parked section contains the parked item with its reason."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        report = json.loads(buf.getvalue())
        assert len(report["parked"]) == 1
        assert report["parked"][0]["id"] == "pnx-bbb"
        assert "owner" in report["parked"][0]["reason"]

    def test_report_failed_count(self) -> None:
        """failed section contains the blocked item with gate."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        report = json.loads(buf.getvalue())
        assert len(report["failed"]) == 1
        assert report["failed"][0]["id"] == "pnx-ccc"
        assert report["failed"][0]["gate"] == "decision"

    def test_report_next(self) -> None:
        """next is the single queued item with no blockers."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        report = json.loads(buf.getvalue())
        assert report["next"] == "pnx-ddd"

    def test_report_deterministic(self) -> None:
        """Running report twice on the same log produces identical output."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as b1:
            report_run(repo_root=self.tmp, as_json=True)
        with contextlib.redirect_stdout(io.StringIO()) as b2:
            report_run(repo_root=self.tmp, as_json=True)
        assert b1.getvalue() == b2.getvalue()

    def test_report_readonly(self) -> None:
        """report does not write any new files to the filesystem."""
        log_dir = os.path.join(self.tmp, ".ergon", "log")
        before = {
            f: os.path.getmtime(os.path.join(log_dir, f))
            for f in os.listdir(log_dir)
        }
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            report_run(repo_root=self.tmp, as_json=True)
        after = {
            f: os.path.getmtime(os.path.join(log_dir, f))
            for f in os.listdir(log_dir)
        }
        assert before == after

    def test_report_human_readable_runs(self) -> None:
        """Human-readable (non-JSON) output does not raise."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=False)
        output = buf.getvalue()
        assert "pinax report" in output
        assert "shipped" in output
        assert "parked" in output
        assert "failed" in output
        assert "next" in output


class TestReportEmpty:
    """Report over an empty log (only ergon.created + phase.opened)."""

    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        _make_repo(self.tmp)
        log_dir = os.path.join(self.tmp, ".ergon", "log")
        ts = "2026-06-30T10:00:00Z"
        e0 = _make_event(0, ts, "operator@example.test", "ergon.created", {"repo": "test"})
        e1 = _make_event(1, ts, "operator@example.test", "phase.opened", {"phase": "pnx"}, prev=e0["id"])
        _write_events(log_dir, [e0, e1])

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_report_json(self) -> None:
        """Empty log produces empty sections and next=None."""
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            report_run(repo_root=self.tmp, as_json=True)
        report = json.loads(buf.getvalue())
        assert report["shipped"] == []
        assert report["parked"] == []
        assert report["failed"] == []
        assert report["next"] is None


class TestReportNoErgon:
    """Report fails cleanly when .ergon/ does not exist."""

    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_ergon_exits(self) -> None:
        """report exits with code 1 when .ergon/log/ is missing."""
        with pytest.raises(SystemExit) as exc:
            report_run(repo_root=self.tmp, as_json=False)
        assert exc.value.code == 1
