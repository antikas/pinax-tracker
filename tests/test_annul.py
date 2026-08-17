"""`event.annulled` tombstone tests for the fold and CLI.

Covers:
1. Happy-path: `pinax annul` appends event.annulled and regenerates the projection.
2. ADR-001 suppression is SPECIFIC: annulling one tampered id suppresses only that
   id's tamper-evidence WARNING; a different, not-yet-annulled tampered/dangling
   event in the same fixture still warns exactly as before.
3. Handler-skip: the annulled event's own type handler (payload effects) is never
   applied, while event.annulled's own handler still runs and records
   state["annulled"].
4. Idempotency: annulling the same target twice is a no-op in effect.
5. Order-independence: the annulling event before vs after its target in total
   order produces identical downstream fold state.
6. Annulling a target id that never appears in the log is a harmless no-op.
7. `--json` output shape.
8. CLI argument wiring (missing --reason, missing positional id, requires init).

Everything runs against fixtures and temporary repositories.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event, serialise
from pinax.fold import fold, fold_events, read_events
from pinax.commands.annul import run as annul_run

pytestmark = pytest.mark.deep


_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env(**overrides: str) -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    env.update(overrides)
    return env


def _make_repo(tmp_dir: str) -> str:
    """Minimal .ergon/log/ directory — no git needed for the fold-level tests."""
    log_dir = os.path.join(tmp_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    return tmp_dir


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str, payload: dict, prev: str = "") -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload, prev=prev)
    append_event(log_dir, event, actor=actor)
    return event


def _write_shard(events: list, tmpdir: str, name: str = "test.jsonl") -> str:
    """Write a list of already-minted event dicts as JSONL to one shard file."""
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as fh:
        for e in events:
            fh.write(serialise(e).encode("utf-8") + b"\n")
    return path


# ---------------------------------------------------------------------------
# 1. Happy path: CLI append + projection regeneration
# ---------------------------------------------------------------------------

class TestAnnulHappyPath:
    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        _make_repo(self.hub)
        self.log_dir = os.path.join(self.hub, ".ergon", "log")

    def test_run_appends_event_annulled(self, capsys):
        target = _append(self.log_dir, 0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                          {"item_id": "pnx-x1", "title": "junk"})
        annul_run(self.hub, target_id=target["id"], reason="tampered/junk", actor="operator@example.test", as_json=False)
        capsys.readouterr()

        state = fold(self.log_dir)
        assert target["id"] in state.get("annulled", {})
        assert state["annulled"][target["id"]]["reason"] == "tampered/junk"
        assert state["annulled"][target["id"]]["actor"] == "operator@example.test"

    def test_run_regenerates_projection(self, capsys):
        target = _append(self.log_dir, 0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                          {"item_id": "pnx-x1", "title": "junk"})
        board_path = os.path.join(self.hub, ".ergon", "board.md")
        before = None
        if os.path.exists(board_path):
            with open(board_path, "r", encoding="utf-8") as fh:
                before = fh.read()
        annul_run(self.hub, target_id=target["id"], reason="tampered", actor="operator@example.test", as_json=False)
        capsys.readouterr()
        assert os.path.exists(board_path), "pinax annul must regenerate board.md (ADR-002)"
        with open(board_path, "r", encoding="utf-8") as fh:
            after = fh.read()
        # Projection was (re)written — not asserting content shape, only that
        # regenerate() ran without raising and produced a file.
        assert after is not None
        assert after != before or before is None

    def test_requires_init(self):
        bare = tempfile.mkdtemp()  # no .ergon at all
        with pytest.raises(SystemExit):
            annul_run(bare, target_id="whatever", reason="x", actor="operator@example.test", as_json=True)


# ---------------------------------------------------------------------------
# 2. ADR-001 suppression is SPECIFIC — never a blanket suppression
# ---------------------------------------------------------------------------

class TestAnnulSuppressesOnlySpecificId:
    def test_annulled_id_warning_gone_other_tamper_still_warns(self, caplog):
        actor = "a@h"

        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "itemA", "title": "x"})
        e1 = mint_event(seq=1, ts="2026-07-04T00:00:01Z", actor=actor,
                         etype="item.status_changed", payload={"item_id": "itemA", "status": "queued"},
                         prev=e0["id"])
        # Tamper e1's payload after minting but keep the stale id — id-integrity mismatch.
        tampered_e1 = dict(e1)
        tampered_e1["payload"] = dict(e1["payload"])
        tampered_e1["payload"]["status"] = "TAMPERED"
        # id intentionally NOT recomputed.

        e2 = mint_event(seq=2, ts="2026-07-04T00:00:02Z", actor=actor,
                         etype="item.status_changed", payload={"item_id": "itemA", "status": "blocked"},
                         prev=tampered_e1["id"])
        # e3 has a dangling prev — a SEPARATE, not-annulled tamper-evidence violation.
        e3 = mint_event(seq=3, ts="2026-07-04T00:00:03Z", actor=actor,
                         etype="item.status_changed", payload={"item_id": "itemA", "status": "done"},
                         prev="GARBAGE_DANGLING_PREV")

        e_annul = mint_event(seq=4, ts="2026-07-04T00:00:04Z", actor="reviewer@example.test",
                              etype="event.annulled",
                              payload={"target_id": tampered_e1["id"], "reason": "junk tamper"})

        tmpdir = tempfile.mkdtemp()
        _write_shard([e0, tampered_e1, e2, e3, e_annul], tmpdir)

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            events = read_events(tmpdir)

        tampered_id = tampered_e1["id"]
        id_warnings_for_tampered = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
            and "Integrity violation" in r.message
            and tampered_id in r.message
        ]
        assert id_warnings_for_tampered == [], (
            f"Expected the annulled id's own integrity WARNING to be suppressed; got: "
            + "\n".join(r.message for r in id_warnings_for_tampered)
        )

        # The other, un-annulled tamper (e3's dangling prev) must still warn.
        chain_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "Broken prev-chain" in r.message
        ]
        assert chain_warnings, (
            f"Expected e3's broken-prev-chain WARNING to still fire (never a blanket "
            f"suppression); got records: {[r.message for r in caplog.records]}"
        )
        assert any(str(e3.get("seq")) in r.message for r in chain_warnings)

        # Sanity: the fold still applies both events (detection, not rejection).
        state = fold_events(events)
        assert state["items"]["itemA"]["status"] == "done"


# ---------------------------------------------------------------------------
# 3. Handler-skip: payload effects suppressed, event.annulled's own handler runs
# ---------------------------------------------------------------------------

class TestAnnulHandlerSkip:
    def test_annulled_item_completed_effects_skipped_and_no_unknown_item_warning(self, caplog):
        actor = "a@h"
        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "pnx-real1", "title": "real"})
        # Bogus item.completed for an item id that was NEVER created — normally this
        # would log "item.completed for unknown item ghost-99 - ignored."
        bogus_completed = mint_event(seq=1, ts="2026-07-04T00:00:01Z", actor=actor,
                                      etype="item.completed",
                                      payload={"item_id": "ghost-99", "briefing": "n/a"})
        e_annul = mint_event(seq=2, ts="2026-07-04T00:00:02Z", actor="reviewer@example.test",
                              etype="event.annulled",
                              payload={"target_id": bogus_completed["id"], "reason": "foreign/junk event"})

        tmpdir = tempfile.mkdtemp()
        _write_shard([e0, bogus_completed, e_annul], tmpdir)

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            state = fold_events(read_events(tmpdir))

        unknown_item_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "unknown item ghost-99" in r.message
        ]
        assert unknown_item_warnings == [], (
            "Annulled event's own handler must never run — the 'unknown item' "
            "warning it would otherwise emit must not fire."
        )
        assert "ghost-99" not in state.get("items", {}), (
            "Annulled item.completed's payload effects must never apply."
        )

        assert bogus_completed["id"] in state.get("annulled", {})
        record = state["annulled"][bogus_completed["id"]]
        assert record["reason"] == "foreign/junk event"
        assert record["event_id"] == e_annul["id"]

        # Control: a genuinely unrelated item stays untouched.
        assert state["items"]["pnx-real1"]["status"] == "queued"

    def test_unannulled_bogus_completed_still_warns_and_is_ignored(self, caplog):
        """Control case: without an annul event, the same bogus item.completed
        DOES warn and DOES get ignored (proves the suppression above is real,
        not a pre-existing no-op)."""
        actor = "a@h"
        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "pnx-real1", "title": "real"})
        bogus_completed = mint_event(seq=1, ts="2026-07-04T00:00:01Z", actor=actor,
                                      etype="item.completed",
                                      payload={"item_id": "ghost-99", "briefing": "n/a"})

        tmpdir = tempfile.mkdtemp()
        _write_shard([e0, bogus_completed], tmpdir)

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            state = fold_events(read_events(tmpdir))

        unknown_item_warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "unknown item ghost-99" in r.message
        ]
        assert unknown_item_warnings, "Expected the un-annulled bogus event to warn."
        assert "ghost-99" not in state.get("items", {})


# ---------------------------------------------------------------------------
# 4. Idempotency — annulling the same target twice is a no-op in effect
# ---------------------------------------------------------------------------

class TestAnnulIdempotent:
    def _fold_with_n_annuls(self, n_annuls: int) -> dict:
        actor = "a@h"
        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "pnx-real1", "title": "real"})
        bogus_completed = mint_event(seq=1, ts="2026-07-04T00:00:01Z", actor=actor,
                                      etype="item.completed",
                                      payload={"item_id": "ghost-99", "briefing": "n/a"})
        events = [e0, bogus_completed]
        for i in range(n_annuls):
            events.append(mint_event(
                seq=2 + i, ts=f"2026-07-04T00:00:{2 + i:02d}Z", actor=f"reviewer{i}@h",
                etype="event.annulled",
                payload={"target_id": bogus_completed["id"], "reason": f"junk-{i}"},
            ))
        tmpdir = tempfile.mkdtemp()
        _write_shard(events, tmpdir)
        return fold_events(read_events(tmpdir))

    def test_single_vs_double_annul_same_observable_effect(self):
        state_one = self._fold_with_n_annuls(1)
        state_two = self._fold_with_n_annuls(2)

        # Both cases: the target's payload effects are suppressed identically.
        assert "ghost-99" not in state_one.get("items", {})
        assert "ghost-99" not in state_two.get("items", {})
        assert state_one["items"] == state_two["items"]

        # Both cases: the target is recorded as annulled (still annulled either way).
        target_id = None
        # Recover the target id via the shared bogus event content (deterministic
        # across both folds since seq/ts/actor/payload of the target are identical).
        for annulled_id in state_one.get("annulled", {}):
            target_id = annulled_id
        assert target_id is not None
        assert target_id in state_two.get("annulled", {})


# ---------------------------------------------------------------------------
# 5. Order-independence — annul before vs after its target in total order
# ---------------------------------------------------------------------------

class TestAnnulOrderIndependence:
    def _fold_with_annul_seq(self, annul_seq: int, target_seq: int) -> dict:
        actor = "a@h"
        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "pnx-real1", "title": "real"})
        bogus_completed = mint_event(seq=target_seq, ts="2026-07-04T00:00:01Z", actor=actor,
                                      etype="item.completed",
                                      payload={"item_id": "ghost-99", "briefing": "n/a"})
        e_annul = mint_event(seq=annul_seq, ts="2026-07-04T00:00:02Z", actor="reviewer@example.test",
                              etype="event.annulled",
                              payload={"target_id": bogus_completed["id"], "reason": "junk"})
        tmpdir = tempfile.mkdtemp()
        _write_shard([e0, bogus_completed, e_annul], tmpdir)
        return fold_events(read_events(tmpdir))

    def test_annul_before_target_same_result_as_annul_after(self):
        # Variant A: annul's seq (1) precedes target's seq (2) in total order.
        state_before = self._fold_with_annul_seq(annul_seq=1, target_seq=2)
        # Variant B: annul's seq (3) follows target's seq (2) in total order.
        state_after = self._fold_with_annul_seq(annul_seq=3, target_seq=2)

        # The visible, downstream effect must be identical regardless of position.
        assert state_before["items"] == state_after["items"]
        assert "ghost-99" not in state_before["items"]
        assert "ghost-99" not in state_after["items"]


# ---------------------------------------------------------------------------
# 6. Annulling an id that never appears in the log — harmless no-op
# ---------------------------------------------------------------------------

class TestAnnulNeverSeenTarget:
    def test_phantom_target_no_crash_no_effect(self, caplog):
        actor = "a@h"
        e0 = mint_event(seq=0, ts="2026-07-04T00:00:00Z", actor=actor,
                         etype="item.created", payload={"item_id": "pnx-real1", "title": "real"})
        e_annul = mint_event(seq=1, ts="2026-07-04T00:00:01Z", actor="reviewer@example.test",
                              etype="event.annulled",
                              payload={"target_id": "an-id-that-never-appears-anywhere", "reason": "phantom"})
        tmpdir = tempfile.mkdtemp()
        _write_shard([e0, e_annul], tmpdir)

        with caplog.at_level(logging.WARNING, logger="pinax.fold"):
            state = fold_events(read_events(tmpdir))

        assert state["items"]["pnx-real1"]["status"] == "queued"
        assert "an-id-that-never-appears-anywhere" in state.get("annulled", {})
        assert state["annulled"]["an-id-that-never-appears-anywhere"]["reason"] == "phantom"


# ---------------------------------------------------------------------------
# 7. --json output shape
# ---------------------------------------------------------------------------

class TestAnnulJsonOutput:
    def test_json_shape(self, capsys):
        hub = tempfile.mkdtemp()
        _make_repo(hub)
        log_dir = os.path.join(hub, ".ergon", "log")
        target = _append(log_dir, 0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                          {"item_id": "pnx-x1", "title": "junk"})

        annul_run(hub, target_id=target["id"], reason="tampered", actor="operator@example.test", as_json=True)
        out = capsys.readouterr().out
        result = json.loads(out)

        assert result["target_id"] == target["id"]
        assert result["reason"] == "tampered"
        assert result["type"] == "event.annulled"
        assert result["actor"] == "operator@example.test"
        assert "event_id" in result and result["event_id"]
        assert "seq" in result
        assert "ts" in result


# ---------------------------------------------------------------------------
# 8. CLI argument wiring (python -m pinax annul ...)
# ---------------------------------------------------------------------------

class TestAnnulCLIWiring:
    def test_cli_missing_reason_rejected(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", "some-event-id"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert result.returncode != 0

    def test_cli_missing_event_id_rejected(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", "--reason", "x"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert result.returncode != 0

    def test_cli_happy_path_roundtrip(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        r_add = subprocess.run(
            [sys.executable, "-m", "pinax", "add", "--title", "junk item", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert r_add.returncode == 0, r_add.stderr
        added = json.loads(r_add.stdout)
        target_event_id = added["event_id"]

        r_annul = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", target_event_id, "--reason", "test junk", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert r_annul.returncode == 0, r_annul.stderr
        result = json.loads(r_annul.stdout)
        assert result["target_id"] == target_event_id
        assert result["type"] == "event.annulled"

    def test_cli_actor_default_derivation(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", "phantom-id", "--reason", "x", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert result.returncode == 0, result.stderr
        parsed = json.loads(result.stdout)
        assert parsed["actor"].startswith("operator@")

    def test_cli_help_documents_annul(self):
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", "--help"],
            capture_output=True, env=_build_env(),
        )
        assert result.returncode == 0
        assert b"reason" in result.stdout.lower() or b"reason" in result.stderr.lower()
