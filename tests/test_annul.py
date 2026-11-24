"""`event.annulled` tombstone tests for the fold and CLI.

Covers:
1. Happy-path: `pinax annul` appends event.annulled and regenerates the projection.
2. Tamper-evidence suppression is SPECIFIC: annulling one tampered id suppresses only that
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
        assert os.path.exists(board_path), "pinax annul must regenerate board.md"
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
# 2. Tamper-evidence suppression is SPECIFIC — never a blanket suppression
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
        # An annulment names an event that exists; init's own first event is one.
        target = read_events(os.path.join(hub, ".ergon", "log"))[0]["id"]
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "annul", target, "--reason", "x", "--json"],
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


# ---------------------------------------------------------------------------
# 9. Annulling an annulment restores its target
# ---------------------------------------------------------------------------

def _git(root: str, *args: str) -> subprocess.CompletedProcess:
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, env=_build_env())
    assert done.returncode == 0, done.stderr
    return done


def _configure(root: str) -> None:
    _git(root, "config", "user.email", "clone@pinax.example")
    _git(root, "config", "user.name", "Pinax Clone")
    _git(root, "config", "core.autocrlf", "false")


def _init_repo(root: str) -> None:
    _git(root, "init", "-b", "main")
    _configure(root)


def _commit_all(root: str, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _pinax(root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=root, capture_output=True, text=True, env=_build_env(),
    )


def _json(root: str, *args: str) -> dict:
    done = _pinax(root, *args, "--json")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.fixture()
def clones(clone_wired_to_hub, clone_of_hub):
    one = clone_wired_to_hub(
        init_repo=_init_repo, git=_git, pinax=_pinax, commit_all=_commit_all,
        actor="one@laptop",
    )
    two = clone_of_hub(
        git=_git, name="two", config={"core.autocrlf": "false"}, configure=_configure
    )
    return one, two


def _assert_queued(root: str, item_id: str) -> None:
    assert item_id in _pinax(root, "ready").stdout
    board = _pinax(root, "board").stdout
    assert item_id in board
    with open(os.path.join(root, ".ergon", "board.md"), encoding="utf-8") as fh:
        assert f"- {item_id} · Alpha · queued" in fh.read()
    assert os.path.isfile(os.path.join(root, ".ergon", "items", f"{item_id}.md"))
    verified = _pinax(root, "verify")
    assert verified.returncode == 0, verified.stderr


class TestAnnulAnAnnulment:
    def test_a_restored_item_is_back_in_both_clones(self, clones):
        one, two = clones
        added = _json(one, "add", "--title", "Alpha", "--prefix", "pnx")
        item_id = added["item_id"]
        first = _json(one, "annul", added["event_id"], "--reason", "mistake")
        page = os.path.join(one, ".ergon", "items", f"{item_id}.md")
        assert item_id not in _pinax(one, "ready").stdout
        assert not os.path.exists(page)

        second = _pinax(one, "annul", first["event_id"], "--reason", "undo")
        assert second.returncode == 0, second.stderr
        lines = second.stdout.splitlines()
        assert lines[0].startswith(f"pinax: event {first['event_id']} annulled (reason='undo')")
        assert lines[-1] == f"pinax: event {added['event_id']} is in effect again"

        _assert_queued(one, item_id)
        _git(two, "pull", "--no-rebase")
        _assert_queued(two, item_id)

    def test_a_chain_names_what_each_step_restores(self, clones):
        one, _ = clones
        added = _json(one, "add", "--title", "Alpha", "--prefix", "pnx")
        a0 = _json(one, "annul", added["event_id"], "--reason", "a0")
        b0 = _json(one, "annul", added["event_id"], "--reason", "b0")

        a1 = _pinax(one, "annul", a0["event_id"], "--reason", "a1")
        assert "in effect again" not in a1.stdout
        log_dir = os.path.join(one, ".ergon", "log")
        b1 = _pinax(one, "annul", b0["event_id"], "--reason", "b1")
        assert f"event {added['event_id']} is in effect again" in b1.stdout
        b1_id = max(read_events(log_dir), key=lambda e: e["seq"])["id"]
        b2 = _pinax(one, "annul", b1_id, "--reason", "b2")
        assert f"event {b0['event_id']} is in effect again" in b2.stdout
        assert added["item_id"] not in fold(log_dir).get("items", {})

    def test_the_restored_target_may_be_known_only_from_the_remote(self, clones):
        one, two = clones
        added = _json(one, "add", "--title", "Alpha", "--prefix", "pnx")
        first = _json(one, "annul", added["event_id"], "--reason", "mistake")
        _git(two, "switch", "-c", "side")
        second = _pinax(two, "annul", first["event_id"], "--reason", "undo")
        assert second.returncode == 0, second.stderr
        assert f"event {added['event_id']} is in effect again" in second.stdout

    def test_a_restored_item_does_not_displace_its_replacement(self, clones):
        one, _ = clones
        added = _json(one, "add", "--title", "Alpha", "--prefix", "pnx")
        first = _json(one, "annul", added["event_id"], "--reason", "mistake")
        again = _json(one, "add", "--title", "Alpha", "--prefix", "pnx")
        assert again["item_id"] != added["item_id"]
        _json(one, "annul", first["event_id"], "--reason", "undo")
        _assert_queued(one, added["item_id"])
        _assert_queued(one, again["item_id"])

    def test_ordinary_rules_decide_between_a_restored_event_and_a_later_one(self):
        def priority(seq: int, value: int) -> dict:
            return mint_event(seq, f"2026-07-04T00:00:{seq:02d}Z", "a@h", "item.priority_set",
                              {"item_id": "pnx-x", "priority": value})

        def claim(seq: int, actor: str) -> dict:
            return mint_event(seq, f"2026-07-04T00:00:{seq:02d}Z", actor, "item.claimed",
                              {"item_id": "pnx-x"})

        def annul(seq: int, target: dict) -> dict:
            return mint_event(seq, f"2026-07-04T00:00:{seq:02d}Z", "a@h", "event.annulled",
                              {"target_id": target["id"], "reason": "r"})

        created = mint_event(0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                             {"item_id": "pnx-x", "title": "X"})
        low, high = priority(1, 1), priority(4, 4)
        early, late = claim(2, "early@h"), claim(5, "late@h")
        plain = [created, low, early, high, late]
        gone = [annul(6, low), annul(7, early)]
        undone = [annul(8, gone[0]), annul(9, gone[1])]

        assert fold_events(plain + gone)["items"]["pnx-x"]["owner"] == "late@h"
        assert fold_events(plain + gone + undone)["items"] == fold_events(plain)["items"]

    def test_the_clock_rule_reads_a_restored_event(self):
        from pinax.sync import _newest_live_remote_event

        old = mint_event(0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                         {"item_id": "pnx-a", "title": "A"})
        future = mint_event(1, "2030-01-01T00:00:00Z", "a@h", "item.created",
                            {"item_id": "pnx-b", "title": "B"})
        first = mint_event(2, "2026-07-04T00:00:02Z", "a@h", "event.annulled",
                           {"target_id": future["id"], "reason": "r"})
        second = mint_event(3, "2026-07-04T00:00:03Z", "a@h", "event.annulled",
                            {"target_id": first["id"], "reason": "r"})
        assert _newest_live_remote_event([old, future, first])[0] == first
        assert _newest_live_remote_event([old, future, first, second])[0] == future


class TestRejectedClaimAnnulmentIsFinal:
    def test_annulling_a_rejected_claims_own_annulment_is_refused(self, tmp_path, capsys):
        from pinax.commands.claim import run as claim_run
        from test_sync_sequence import FakeGit, _fail

        repo = _make_repo(str(tmp_path))
        log_dir = os.path.join(repo, ".ergon", "log")
        _append(log_dir, 0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                {"item_id": "pnx-x1", "title": "X"})
        with pytest.raises(SystemExit) as exited:
            claim_run(repo, "pnx-x1", actor="builder@alpha", as_json=True,
                      runner=FakeGit(pushes=[_fail("rejected")] * 3, remote_events=[]))
        assert exited.value.code == 5
        capsys.readouterr()
        events = read_events(log_dir)
        own = [e for e in events if e["type"] == "event.annulled"]
        assert len(own) == 1

        with pytest.raises(SystemExit) as exited:
            annul_run(repo, own[0]["id"], "undo", actor="other@beta",
                      runner=FakeGit(remote_events=[]))
        assert exited.value.code == 1
        captured = capsys.readouterr()
        assert "never published" in captured.err
        report = json.loads(captured.out)
        assert report["status"] == "final_annulment"
        assert report["appended"] is False
        assert read_events(log_dir) == events

    @pytest.mark.parametrize("claim_actor, reason", [
        ("builder@alpha", "a manual reason"),
        ("rival@beta", "the event could not be published, so it never took effect"),
    ])
    def test_every_other_annulment_of_a_claim_can_be_annulled(
        self, tmp_path, capsys, claim_actor, reason
    ):
        repo = _make_repo(str(tmp_path))
        log_dir = os.path.join(repo, ".ergon", "log")
        _append(log_dir, 0, "2026-07-04T00:00:00Z", "a@h", "item.created",
                {"item_id": "pnx-x1", "title": "X"})
        claim = _append(log_dir, 1, "2026-07-04T00:00:01Z", claim_actor, "item.claimed",
                        {"item_id": "pnx-x1"})
        manual = _append(log_dir, 2, "2026-07-04T00:00:02Z", "builder@alpha", "event.annulled",
                         {"target_id": claim["id"], "reason": reason})
        assert fold(log_dir)["items"]["pnx-x1"].get("owner") != claim_actor
        annul_run(repo, manual["id"], "undo", actor="other@beta")
        assert f"event {claim['id']} is in effect again" in capsys.readouterr().out


def test_help_says_what_annulling_an_annulment_does():
    done = subprocess.run(
        [sys.executable, "-m", "pinax", "--help"],
        capture_output=True, text=True, env=_build_env(COLUMNS="80"),
    )
    entries = " ".join(done.stdout.split()).split("positional arguments:", 1)[1]
    assert "Cancel one earlier event" in entries
    assert "annulling an annulment brings back what it cancelled" in entries
    assert "the annulment a rejected claim wrote cannot be annulled" in entries
