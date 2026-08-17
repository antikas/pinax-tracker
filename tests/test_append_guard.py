"""Append guard tests for persistent and isolated event logs."""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from pinax.append import (
    AppendLeakGuardError,
    _is_isolated_log_dir,
    _test_leak_reason,
    append_event,
)
from pinax.event import mint_event
from pinax.fold import fold_events, read_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _real_dir_under_here() -> str:
    """
    A scratch directory that is NOT under the OS temp tree — simulating a
    real, persistent repo log directory for guard purposes — created next to
    this test file and always cleaned up by the caller.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scratch_guard")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 1 & 2 & 3: append_event behaviour in a real (non-isolated) log dir
# ---------------------------------------------------------------------------

class TestGuardInRealLogDir:
    def setup_method(self) -> None:
        self.real_dir = _real_dir_under_here()

    def teardown_method(self) -> None:
        shutil.rmtree(self.real_dir, ignore_errors=True)

    def test_normal_event_still_appends(self) -> None:
        """A normal event from a real actor into a real log dir appends fine."""
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="operator@host",
            etype="ergon.created", payload={"repo": "pinax"}, prev="",
        )
        path = append_event(self.real_dir, event, actor="operator@host")

        assert os.path.exists(path)
        state = fold_events(read_events(self.real_dir))
        assert "ergon" in state

    def test_test_actor_rejected_in_real_log_dir(self) -> None:
        """An event whose actor contains 'test' is refused into a real log dir."""
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="test@host",
            etype="ergon.created", payload={"repo": "leak"}, prev="",
        )
        with pytest.raises(AppendLeakGuardError):
            append_event(self.real_dir, event, actor="test@host")

        # Nothing was written — the shard file was never created.
        shard_path = os.path.join(self.real_dir, "test-host.jsonl")
        assert not os.path.exists(shard_path)

    def test_foreign_ticket_id_rejected_in_real_log_dir(self) -> None:
        """
        A foreign-format item id is refused.
        """
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="reviewer@host",
            etype="item.completed", payload={"item_id": "T-001"}, prev="",
        )
        with pytest.raises(AppendLeakGuardError) as exc_info:
            append_event(self.real_dir, event, actor="reviewer@host")

        assert "T-001" in str(exc_info.value)
        shard_path = os.path.join(self.real_dir, "reviewer-host.jsonl")
        assert not os.path.exists(shard_path)

    def test_rejection_leaves_shard_unchanged_when_prior_events_exist(self) -> None:
        """A guard rejection never corrupts or truncates an existing shard."""
        good = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="operator@host",
            etype="ergon.created", payload={"repo": "pinax"}, prev="",
        )
        append_event(self.real_dir, good, actor="operator@host")
        shard_path = os.path.join(self.real_dir, "operator-host.jsonl")
        before = open(shard_path, "rb").read()

        leaked = mint_event(
            seq=1, ts="2026-07-08T00:00:01Z", actor="operator@host",
            etype="item.completed", payload={"item_id": "FOREIGN-001"}, prev=good["id"],
        )
        with pytest.raises(AppendLeakGuardError):
            append_event(self.real_dir, leaked, actor="operator@host")

        after = open(shard_path, "rb").read()
        assert before == after


# ---------------------------------------------------------------------------
# 4: the same leak-shaped events append fine in an isolated (tmp) log dir
# ---------------------------------------------------------------------------

class TestGuardExemptInIsolatedLogDir:
    def setup_method(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()

    def teardown_method(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_test_actor_appends_fine_in_isolated_dir(self) -> None:
        """
        Every hermetic test in this suite already does exactly this
        (actor='test@host' or similar, into a tempfile.mkdtemp() dir) — the
        guard must never fire there, or the whole existing suite breaks.
        """
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="test@host",
            etype="ergon.created", payload={"repo": "test"}, prev="",
        )
        path = append_event(self.tmp_dir, event, actor="test@host")
        assert os.path.exists(path)

    def test_foreign_ticket_id_appends_fine_in_isolated_dir(self) -> None:
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="reviewer@host",
            etype="item.completed", payload={"item_id": "T-001"}, prev="",
        )
        path = append_event(self.tmp_dir, event, actor="reviewer@host")
        assert os.path.exists(path)


# ---------------------------------------------------------------------------
# 5: unit coverage of the two guard helpers directly
# ---------------------------------------------------------------------------

class TestGuardHelpers:
    def test_is_isolated_log_dir_true_under_temp(self) -> None:
        assert _is_isolated_log_dir(tempfile.gettempdir()) is True
        assert _is_isolated_log_dir(tempfile.mkdtemp()) is True

    def test_is_isolated_log_dir_false_for_real_path(self) -> None:
        real_dir = _real_dir_under_here()
        try:
            assert _is_isolated_log_dir(real_dir) is False
        finally:
            shutil.rmtree(real_dir, ignore_errors=True)

    def test_leak_reason_none_for_clean_event(self) -> None:
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="operator@host",
            etype="item.created",
            payload={"item_id": "pnx-abc123", "title": "T", "prefix": "pnx", "status": "queued"},
            prev="",
        )
        assert _test_leak_reason(event, "operator@host") is None

    def test_leak_reason_set_for_test_actor(self) -> None:
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="Test-Runner@host",
            etype="ergon.created", payload={"repo": "x"}, prev="",
        )
        reason = _test_leak_reason(event, "Test-Runner@host")
        assert reason is not None
        assert "test" in reason.lower()

    def test_leak_reason_set_for_foreign_id_shape(self) -> None:
        event = mint_event(
            seq=0, ts="2026-07-08T00:00:00Z", actor="reviewer@host",
            etype="item.completed", payload={"item_id": "FOREIGN-001"}, prev="",
        )
        reason = _test_leak_reason(event, "reviewer@host")
        assert reason is not None
        assert "FOREIGN-001" in reason

    def test_leak_reason_none_for_lowercase_hash_shaped_id(self) -> None:
        """
        This repo's own id scheme (ADR-003: <lowercase-prefix>-<lowercase
        base32 hash>, e.g. '', 'ab-x1y2', ids like the tests'
        ''/'') must never trip the foreign-id-shape signal.
        """
        for item_id in ("pnx-abc123", "pnx-x1", "pnx-test", "ab-x1y2z3", "cc-abcd"):
            event = mint_event(
                seq=0, ts="2026-07-08T00:00:00Z", actor="operator@host",
                etype="item.completed", payload={"item_id": item_id}, prev="",
            )
            assert _test_leak_reason(event, "operator@host") is None, item_id
