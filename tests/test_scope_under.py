"""Tests for `--under` ancestor scoping and live-claim eligibility.

Covers:
1. fold.descendants(): transitive parent-child walk, excludes the ancestor
   itself, cycle-safe, raises ValueError on an unknown ancestor.
2. compute_ready/compute_next with `under`: candidates are restricted to
   the scope; a blocks edge crossing the scope boundary still gates
   readiness (the full graph is still evaluated, only the final candidate
   set is restricted); an unknown ancestor raises ValueError.
3. A live claim (a reconciled item.claimed, ADR-003) makes an item
   ineligible for ready/next everywhere, independent of `under` and
   independent of the item's status field -- a claim and a status change
   are two different events.
4. fold.claim_age_hours(): parses, rounds to one decimal, and returns None
   on an unparseable timestamp; never reads the wall clock itself.
5. A parent-child cycle reachable from an ancestor does not hang
   descendants(); a blocks cycle is still excluded from the ready set and
   still warns the same way when `under` is given.
6. CLI-level (in-process, fake tracker under the pytest temp tree, no git
   or subprocess): ready/next/status --under filtering, the JSON scope
   key (the ancestor id, or null when unscoped), the unknown-ancestor
   refusal (message on stderr, non-zero exit, nothing appended), and the
   status building list's owner + claim age for a claimed item.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile

import pytest

from pinax.append import append_event
from pinax.commands import next_cmd, ready, status_cmd
from pinax.event import mint_event
from pinax.fold import (
    claim_age_hours,
    compute_next,
    compute_ready,
    descendants,
    fold_events,
    read_events,
)
from pinax.statusview import status_view

ACTOR = "operator@example.test"


def _append(log_dir: str, seq: int, ts: str, etype: str, payload: dict,
            actor: str = ACTOR) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _fold(log_dir: str) -> dict:
    return fold_events(read_events(log_dir))


def _seed_repo(repo_dir: str) -> str:
    log_dir = os.path.join(repo_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _count_lines(log_dir: str) -> int:
    total = 0
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, fname), "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        total += len([line for line in normalised.split(b"\n") if line])
    return total


def _item(log_dir: str, seq: int, ts: str, item_id: str, title: str) -> dict:
    return _append(log_dir, seq, ts, "item.created",
                    {"item_id": item_id, "title": title, "prefix": "pnx",
                     "status": "queued"})


def _pc(log_dir: str, seq: int, ts: str, from_id: str, to_id: str) -> dict:
    return _append(log_dir, seq, ts, "dep.added",
                    {"from_id": from_id, "to_id": to_id, "type": "parent-child"})


def _blocks(log_dir: str, seq: int, ts: str, from_id: str, to_id: str) -> dict:
    return _append(log_dir, seq, ts, "dep.added",
                    {"from_id": from_id, "to_id": to_id, "type": "blocks"})


def _claim(log_dir: str, seq: int, ts: str, item_id: str, actor: str = ACTOR) -> dict:
    return _append(log_dir, seq, ts, "item.claimed", {"item_id": item_id}, actor=actor)


def _ts(sec: int) -> str:
    return f"2026-01-01T00:00:{sec:02d}Z"


# ---------------------------------------------------------------------------
# Fixture: engagement / use-case / segment hierarchy with a cross-scope block
#
#   engagement
#     usecase-a
#       seg-a1
#       seg-a2   <- blocked by seg-b1 (OUTSIDE usecase-a's descendants)
#     usecase-b
#       seg-b1
# ---------------------------------------------------------------------------

ENGAGEMENT = "pnx-engagement"
USECASE_A = "pnx-usecase-a"
USECASE_B = "pnx-usecase-b"
SEG_A1 = "pnx-seg-a1"
SEG_A2 = "pnx-seg-a2"
SEG_B1 = "pnx-seg-b1"


def _build_hierarchy(log_dir: str) -> None:
    _append(log_dir, 0, _ts(0), "ergon.created", {"repo": "scope-test"})
    _item(log_dir, 1, _ts(1), ENGAGEMENT, "Engagement")
    _item(log_dir, 2, _ts(2), USECASE_A, "Use case A")
    _item(log_dir, 3, _ts(3), USECASE_B, "Use case B")
    _item(log_dir, 4, _ts(4), SEG_A1, "Segment A1")
    _item(log_dir, 5, _ts(5), SEG_A2, "Segment A2")
    _item(log_dir, 6, _ts(6), SEG_B1, "Segment B1")
    _pc(log_dir, 7, _ts(7), ENGAGEMENT, USECASE_A)
    _pc(log_dir, 8, _ts(8), ENGAGEMENT, USECASE_B)
    _pc(log_dir, 9, _ts(9), USECASE_A, SEG_A1)
    _pc(log_dir, 10, _ts(10), USECASE_A, SEG_A2)
    _pc(log_dir, 11, _ts(11), USECASE_B, SEG_B1)
    # Cross-scope block: seg-b1 (under usecase-b) blocks seg-a2 (under
    # usecase-a) -- a legitimate cross-subtree dependency between two
    # different ancestors' descendants.
    _blocks(log_dir, 12, _ts(12), SEG_B1, SEG_A2)


# ---------------------------------------------------------------------------
# 1. fold.descendants()
# ---------------------------------------------------------------------------

class TestDescendants:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.log_dir = _seed_repo(self.repo)
        _build_hierarchy(self.log_dir)
        self.state = _fold(self.log_dir)

    def test_transitive_descendants_of_engagement(self):
        found = descendants(self.state, ENGAGEMENT)
        assert found == {USECASE_A, USECASE_B, SEG_A1, SEG_A2, SEG_B1}

    def test_descendants_of_a_leaf_is_empty(self):
        assert descendants(self.state, SEG_A1) == set()

    def test_ancestor_excludes_itself(self):
        assert USECASE_A not in descendants(self.state, USECASE_A)

    def test_scoped_to_one_use_case(self):
        assert descendants(self.state, USECASE_A) == {SEG_A1, SEG_A2}

    def test_unknown_ancestor_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown ancestor"):
            descendants(self.state, "pnx-does-not-exist")

    def test_parent_child_cycle_does_not_hang(self):
        cyc_dir = _seed_repo(tempfile.mkdtemp())
        _append(cyc_dir, 0, _ts(0), "ergon.created", {"repo": "cyc"})
        _item(cyc_dir, 1, _ts(1), "pnx-root", "Root")
        _item(cyc_dir, 2, _ts(2), "pnx-a", "A")
        _item(cyc_dir, 3, _ts(3), "pnx-b", "B")
        _pc(cyc_dir, 4, _ts(4), "pnx-root", "pnx-a")
        _pc(cyc_dir, 5, _ts(5), "pnx-root", "pnx-b")
        # The cycle: a -> b -> a.  Neither leg touches "pnx-root".
        _pc(cyc_dir, 6, _ts(6), "pnx-a", "pnx-b")
        _pc(cyc_dir, 7, _ts(7), "pnx-b", "pnx-a")
        state = _fold(cyc_dir)
        # Terminates (the test itself is the hang guard) and excludes the
        # ancestor even though the cycle loops back to reach it indirectly.
        assert descendants(state, "pnx-root") == {"pnx-a", "pnx-b"}
        assert "pnx-root" not in descendants(state, "pnx-root")


# ---------------------------------------------------------------------------
# 2. compute_ready / compute_next with `under`
# ---------------------------------------------------------------------------

class TestScopedReadyNext:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.log_dir = _seed_repo(self.repo)
        _build_hierarchy(self.log_dir)

    def test_ready_scoped_to_usecase_a_excludes_blocked_cross_scope_item(self):
        state = _fold(self.log_dir)
        # seg-a2 is blocked by seg-b1 (outside the scope, not done): the
        # full blocks graph still gates readiness even though seg-b1 is
        # never itself a candidate under this scope.
        assert compute_ready(state, under=USECASE_A) == [SEG_A1]

    def test_ready_scope_never_includes_items_outside_it(self):
        state = _fold(self.log_dir)
        ready_ids = compute_ready(state, under=USECASE_A)
        assert USECASE_B not in ready_ids
        assert SEG_B1 not in ready_ids
        assert ENGAGEMENT not in ready_ids

    def test_cross_scope_blocker_done_unblocks_the_scoped_item(self):
        seq = 13
        _append(self.log_dir, seq, _ts(13), "item.status_changed",
                 {"item_id": SEG_B1, "status": "done"})
        state = _fold(self.log_dir)
        assert compute_ready(state, under=USECASE_A) == [SEG_A1, SEG_A2]

    def test_unscoped_ready_is_unrestricted(self):
        state = _fold(self.log_dir)
        # seg-a2 alone is blocked (by seg-b1, not yet done); every other
        # item, including seg-b1 itself, is a plain queued item with no
        # blockers.
        assert set(compute_ready(state)) == {
            ENGAGEMENT, USECASE_A, USECASE_B, SEG_A1, SEG_B1,
        }

    def test_next_scoped_to_usecase_a(self):
        state = _fold(self.log_dir)
        assert compute_next(state, under=USECASE_A) == SEG_A1

    def test_next_unknown_ancestor_raises(self):
        state = _fold(self.log_dir)
        with pytest.raises(ValueError):
            compute_next(state, under="pnx-nope")

    def test_ready_unknown_ancestor_raises(self):
        state = _fold(self.log_dir)
        with pytest.raises(ValueError):
            compute_ready(state, under="pnx-nope")


# ---------------------------------------------------------------------------
# 3. Live-claim eligibility (global rule -- the ground-truth-11 regression)
# ---------------------------------------------------------------------------

class TestLiveClaimEligibility:
    def _two_item_log(self) -> str:
        log_dir = _seed_repo(tempfile.mkdtemp())
        _append(log_dir, 0, _ts(0), "ergon.created", {"repo": "claim-test"})
        _item(log_dir, 1, _ts(1), "pnx-first", "First")
        _item(log_dir, 2, _ts(2), "pnx-second", "Second")
        return log_dir

    def test_claimed_item_leaves_the_ready_set(self):
        log_dir = self._two_item_log()
        _claim(log_dir, 3, _ts(3), "pnx-first")
        state = _fold(log_dir)
        assert state["items"]["pnx-first"]["owner"] == ACTOR
        # The claim does not touch the status field.
        assert state["items"]["pnx-first"]["status"] == "queued"
        assert "pnx-first" not in compute_ready(state)
        assert "pnx-second" in compute_ready(state)

    def test_claim_changes_next_selection(self):
        """
        A claim must change eligibility: without the owner check, 'pnx-first'
        (created earlier) would still win compute_next's age tie-break
        despite being claimed.  It must not.
        """
        log_dir = self._two_item_log()
        state_before = _fold(log_dir)
        assert compute_next(state_before) == "pnx-first"

        _claim(log_dir, 3, _ts(3), "pnx-first")
        state_after = _fold(log_dir)
        assert compute_next(state_after) == "pnx-second"

    def test_claim_ineligibility_holds_under_a_scope_too(self):
        log_dir = self._two_item_log()
        _item(log_dir, 3, _ts(3), "pnx-root", "Root")
        _pc(log_dir, 4, _ts(4), "pnx-root", "pnx-first")
        _pc(log_dir, 5, _ts(5), "pnx-root", "pnx-second")
        _claim(log_dir, 6, _ts(6), "pnx-first")
        state = _fold(log_dir)
        assert compute_ready(state, under="pnx-root") == ["pnx-second"]

    def test_unclaimed_item_is_unaffected(self):
        log_dir = self._two_item_log()
        state = _fold(log_dir)
        assert set(compute_ready(state)) == {"pnx-first", "pnx-second"}


# ---------------------------------------------------------------------------
# 4. fold.claim_age_hours()
# ---------------------------------------------------------------------------

class TestClaimAgeHours:
    def test_computes_rounded_hours(self):
        now = datetime.datetime(2026, 1, 2, 12, 30, 0)
        age = claim_age_hours("2026-01-01T00:00:00Z", now)
        assert age == 36.5

    def test_zero_age(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)
        assert claim_age_hours("2026-01-01T00:00:00Z", now) == 0.0

    def test_unparseable_timestamp_returns_none(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)
        assert claim_age_hours("not-a-timestamp", now) is None

    def test_empty_timestamp_returns_none(self):
        now = datetime.datetime(2026, 1, 1, 0, 0, 0)
        assert claim_age_hours("", now) is None


# ---------------------------------------------------------------------------
# 5. Cycle detection is unchanged
# ---------------------------------------------------------------------------

class TestCyclesUnchangedUnderScope:
    def test_blocks_cycle_still_excluded_and_still_warns_when_scoped(self):
        log_dir = _seed_repo(tempfile.mkdtemp())
        _append(log_dir, 0, _ts(0), "ergon.created", {"repo": "cyc-blocks"})
        _item(log_dir, 1, _ts(1), "pnx-root", "Root")
        _item(log_dir, 2, _ts(2), "pnx-cyc-a", "CycA")
        _item(log_dir, 3, _ts(3), "pnx-cyc-b", "CycB")
        _item(log_dir, 4, _ts(4), "pnx-free", "Free")
        _pc(log_dir, 5, _ts(5), "pnx-root", "pnx-cyc-a")
        _pc(log_dir, 6, _ts(6), "pnx-root", "pnx-cyc-b")
        _pc(log_dir, 7, _ts(7), "pnx-root", "pnx-free")
        _blocks(log_dir, 8, _ts(8), "pnx-cyc-a", "pnx-cyc-b")
        _blocks(log_dir, 9, _ts(9), "pnx-cyc-b", "pnx-cyc-a")
        state = _fold(log_dir)

        ready_ids = compute_ready(state, under="pnx-root")
        assert ready_ids == ["pnx-free"]
        warnings = state.get("report", {}).get("warnings", [])
        assert any("dep cycle" in w for w in warnings)

    def test_parent_child_cycle_warning_present_regardless_of_under(self):
        log_dir = _seed_repo(tempfile.mkdtemp())
        _append(log_dir, 0, _ts(0), "ergon.created", {"repo": "cyc-pc"})
        _item(log_dir, 1, _ts(1), "pnx-root", "Root")
        _item(log_dir, 2, _ts(2), "pnx-a", "A")
        _item(log_dir, 3, _ts(3), "pnx-b", "B")
        _pc(log_dir, 4, _ts(4), "pnx-root", "pnx-a")
        _pc(log_dir, 5, _ts(5), "pnx-a", "pnx-b")
        _pc(log_dir, 6, _ts(6), "pnx-b", "pnx-a")
        state = _fold(log_dir)
        warnings = state.get("report", {}).get("warnings", [])
        assert any("parent-child cycle" in w for w in warnings)
        # The parent-child cycle does not gate readiness (blocks-only).
        assert set(compute_ready(state, under="pnx-root")) == {"pnx-a", "pnx-b"}


# ---------------------------------------------------------------------------
# 6. CLI-level: ready / next / status --under (in-process, fake tracker)
# ---------------------------------------------------------------------------

class TestReadyCommandUnder:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.log_dir = _seed_repo(self.repo)
        _build_hierarchy(self.log_dir)

    def test_scoped_json_is_the_envelope(self, capsys):
        ready.run(self.repo, as_json=True, under=USECASE_A)
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"ready": [SEG_A1], "under": USECASE_A}

    def test_unscoped_json_keeps_the_released_bare_array_shape(self, capsys):
        """
        The released shape of `ready --json` without `--under` is a bare
        JSON array of item ids -- unchanged, byte for byte, since a JSON
        array cannot carry a key. Only `--under` (or `--all-branches`,
        already an object beforehand) switches to an object.
        """
        state = _fold(self.log_dir)
        from pinax.fold import compute_ready

        expected = json.dumps(compute_ready(state), ensure_ascii=True)
        ready.run(self.repo, as_json=True)
        out = capsys.readouterr().out
        assert out.strip() == expected
        payload = json.loads(out)
        assert isinstance(payload, list)
        assert SEG_A1 in payload

    def test_unknown_ancestor_is_refused(self, capsys):
        lines_before = _count_lines(self.log_dir)
        with pytest.raises(SystemExit) as excinfo:
            ready.run(self.repo, as_json=True, under="pnx-nope")
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "unknown ancestor" in err
        assert _count_lines(self.log_dir) == lines_before


class TestNextCommandUnder:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.log_dir = _seed_repo(self.repo)
        _build_hierarchy(self.log_dir)

    def test_scoped_json_returns_the_scoped_winner(self, capsys):
        next_cmd.run(self.repo, as_json=True, under=USECASE_A)
        payload = json.loads(capsys.readouterr().out)
        assert payload["item_id"] == SEG_A1
        assert payload["under"] == USECASE_A

    def test_unscoped_json_carries_null_under(self, capsys):
        next_cmd.run(self.repo, as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["under"] is None

    def test_unknown_ancestor_is_refused(self, capsys):
        lines_before = _count_lines(self.log_dir)
        with pytest.raises(SystemExit) as excinfo:
            next_cmd.run(self.repo, as_json=True, under="pnx-nope")
        assert excinfo.value.code == 1
        assert "unknown ancestor" in capsys.readouterr().err
        assert _count_lines(self.log_dir) == lines_before


class TestStatusCommandUnder:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        self.log_dir = _seed_repo(self.repo)
        _build_hierarchy(self.log_dir)
        _claim(self.log_dir, 13, "2026-01-01T00:00:00Z", SEG_A1)

    def test_scoped_json_carries_under_and_restricts_lists(self, capsys):
        status_cmd.run(self.repo, as_json=True, under=USECASE_A)
        payload = json.loads(capsys.readouterr().out)
        assert payload["under"] == USECASE_A
        repo_view = payload["repo"]
        building_ids = {b["id"] for b in repo_view["building"]}
        assert building_ids == {SEG_A1}
        assert USECASE_B not in building_ids
        assert SEG_B1 not in building_ids

    def test_claimed_item_appears_in_building_with_owner(self, capsys):
        status_cmd.run(self.repo, as_json=True, under=USECASE_A)
        payload = json.loads(capsys.readouterr().out)
        entry = payload["repo"]["building"][0]
        assert entry["id"] == SEG_A1
        assert entry["owner"] == ACTOR
        assert entry["stage"] == "queued"
        assert entry["age_hours"] is not None

    def test_claimed_row_since_is_labelled_by_the_claim_not_creation(self, capsys):
        """
        SEG_A1 was created at 2026-01-01T00:00:04Z and claimed at
        2026-01-01T00:00:00Z. Its status field never changed, so the old
        status_changed_at/created_at fallback would show the creation
        time beside a claim age computed from a different moment. The row
        must read one way: 'since' is the claimed_at itself.
        """
        status_cmd.run(self.repo, as_json=True, under=USECASE_A)
        payload = json.loads(capsys.readouterr().out)
        entry = payload["repo"]["building"][0]
        assert entry["since"] == "2026-01-01T00:00:00Z"

    def test_claim_age_hours_is_computed_from_a_pinned_now(self):
        payload = status_view(
            repo_root=self.repo, scope="repo", under=USECASE_A,
            now="2026-01-02T00:00:00Z",
        )
        entry = payload["repo"]["building"][0]
        assert entry["age_hours"] == 24.0

    def test_unscoped_json_carries_null_under(self, capsys):
        status_cmd.run(self.repo, as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["under"] is None

    def test_unknown_ancestor_is_refused(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            status_cmd.run(self.repo, as_json=True, under="pnx-nope")
        assert excinfo.value.code == 1
        assert "unknown ancestor" in capsys.readouterr().err

    def test_under_with_portfolio_is_refused(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            status_cmd.run(self.repo, as_json=True, scope="portfolio", under=USECASE_A)
        assert excinfo.value.code == 1
        assert "--under" in capsys.readouterr().err

    def test_under_with_setter_form_is_refused(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            status_cmd.run(self.repo, item_id=SEG_A2, new_status="queued", under=USECASE_A)
        assert excinfo.value.code == 2
        assert "--under" in capsys.readouterr().err
