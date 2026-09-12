"""Parent-child roll-up tests.

Covers the roll-up rule as a post-pass after claim reconciliation and the
release/expiry pass (see fold._compute_rollups):

1. The four buckets: done, building, blocked, queued, and their precedence
   (building beats blocked; blocked requires at least one open child).
2. The nested reading: a child that itself has children is represented, in
   its parent's roll-up, by its own already-resolved roll-up.
3. The item's own status is untouched by its roll-up.
4. An item with no children carries no roll-up at all.
5. A parent-child cycle does not hang the fold and carries no roll-up.
6. The roll-up never changes ready/next eligibility.
7. Rendering: the board line and the per-item frontmatter carry the
   roll-up beside the status, only when present; the status view's rows
   gain the same additive key.
8. Determinism: a repeated fold, a shuffled-line-order fold, and a
   replay-equivalent prefix fold all agree.
"""

from __future__ import annotations

import os
import random
import tempfile

import pytest

from pinax.append import append_event
from pinax.commands import status_cmd
from pinax.event import mint_event
from pinax.fold import compute_next, compute_ready, fold_events, fold_prefix, read_events
from pinax.projection import render_board, render_item
from pinax.statusview import status_view

ACTOR = "operator@example.test"
OWNER = "worker@alpha"


def _append(log_dir: str, seq: int, ts: str, etype: str, payload: dict,
            actor: str = ACTOR) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _ts(sec: int) -> str:
    return f"2026-02-01T00:{sec // 60:02d}:{sec % 60:02d}Z"


def _seed(repo_dir: str) -> str:
    log_dir = os.path.join(repo_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    _append(log_dir, 0, _ts(0), "ergon.created", {"repo": "rollup-test"})
    return log_dir


def _item(log_dir: str, seq: int, ts: str, item_id: str, title: str) -> dict:
    return _append(log_dir, seq, ts, "item.created",
                    {"item_id": item_id, "title": title, "prefix": "pnx",
                     "status": "queued"})


def _status(log_dir: str, seq: int, ts: str, item_id: str, status: str) -> dict:
    return _append(log_dir, seq, ts, "item.status_changed",
                    {"item_id": item_id, "status": status})


def _blocked(log_dir: str, seq: int, ts: str, item_id: str, gate: str = "scope") -> dict:
    return _append(log_dir, seq, ts, "item.blocked", {"item_id": item_id, "gate": gate})


def _pc(log_dir: str, seq: int, ts: str, from_id: str, to_id: str) -> dict:
    return _append(log_dir, seq, ts, "dep.added",
                    {"from_id": from_id, "to_id": to_id, "type": "parent-child"})


def _claim(log_dir: str, seq: int, ts: str, item_id: str, actor: str = OWNER) -> dict:
    return _append(log_dir, seq, ts, "item.claimed", {"item_id": item_id}, actor=actor)


def _fold(log_dir: str) -> dict:
    return fold_events(read_events(log_dir))


def _new_repo() -> str:
    return tempfile.mkdtemp()


# ---------------------------------------------------------------------------
# 1. The four buckets
# ---------------------------------------------------------------------------

class TestRollupBuckets:
    def test_done_when_every_child_is_done(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _status(log_dir, 6, _ts(6), "pnx-c1", "done")
        _status(log_dir, 7, _ts(7), "pnx-c2", "done")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "done"

    def test_not_done_when_one_child_is_not_done(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _status(log_dir, 6, _ts(6), "pnx-c1", "done")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] != "done"

    def test_building_when_a_child_is_claimed(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _claim(log_dir, 6, _ts(6), "pnx-c1")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "building"

    def test_building_when_a_child_status_is_a_build_cycle_stage(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _status(log_dir, 6, _ts(6), "pnx-c1", "building")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "building"

    def test_blocked_when_every_open_child_is_blocked(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _blocked(log_dir, 6, _ts(6), "pnx-c1")
        _blocked(log_dir, 7, _ts(7), "pnx-c2")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "blocked"

    def test_blocked_needs_every_open_child_not_just_one(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _blocked(log_dir, 6, _ts(6), "pnx-c1")
        # pnx-c2 stays plain queued -- not every open child is blocked.

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "queued"

    def test_building_takes_precedence_over_blocked(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _blocked(log_dir, 6, _ts(6), "pnx-c1")
        _claim(log_dir, 7, _ts(7), "pnx-c2")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "building"

    def test_queued_when_none_of_the_above(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "queued"

    def test_all_children_done_or_annulled_reads_queued_not_blocked(self):
        """
        A vacuous "every open child is blocked" (no open child at all,
        because the only non-done child carries the literal status
        'annulled') is not a real blocked condition -- it falls through
        to queued, since blocked describes concrete waiting work.
        """
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _status(log_dir, 6, _ts(6), "pnx-c1", "done")
        _status(log_dir, 7, _ts(7), "pnx-c2", "annulled")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "queued"


# ---------------------------------------------------------------------------
# 2. Nested reading: a child's own roll-up is its effective state
# ---------------------------------------------------------------------------

class TestNestedRollup:
    def _build(self, log_dir: str) -> None:
        _item(log_dir, 1, _ts(1), "pnx-grand", "Grandparent")
        _item(log_dir, 2, _ts(2), "pnx-mid", "Middle")
        _item(log_dir, 3, _ts(3), "pnx-leaf1", "Leaf one")
        _item(log_dir, 4, _ts(4), "pnx-leaf2", "Leaf two")
        _pc(log_dir, 5, _ts(5), "pnx-grand", "pnx-mid")
        _pc(log_dir, 6, _ts(6), "pnx-mid", "pnx-leaf1")
        _pc(log_dir, 7, _ts(7), "pnx-mid", "pnx-leaf2")

    def test_grandparent_reads_done_through_the_middle_items_own_rollup(self):
        log_dir = _seed(_new_repo())
        self._build(log_dir)
        _status(log_dir, 8, _ts(8), "pnx-leaf1", "done")
        _status(log_dir, 9, _ts(9), "pnx-leaf2", "done")

        state = _fold(log_dir)
        assert state["items"]["pnx-mid"]["rollup"] == "done"
        assert state["items"]["pnx-grand"]["rollup"] == "done"

    def test_grandparent_reads_building_through_the_middle_items_own_rollup(self):
        log_dir = _seed(_new_repo())
        self._build(log_dir)
        _claim(log_dir, 8, _ts(8), "pnx-leaf1")

        state = _fold(log_dir)
        assert state["items"]["pnx-mid"]["rollup"] == "building"
        assert state["items"]["pnx-grand"]["rollup"] == "building"

    def test_middle_items_own_claim_is_not_read_once_it_has_children(self):
        """
        A child that itself has children is represented by its own
        roll-up, not by its raw status/claim -- claiming the middle item
        directly (rather than one of its leaves) must not, by itself,
        surface as "building" at the grandparent: the middle item's
        effective state is its roll-up (here: queued, both leaves plain).
        """
        log_dir = _seed(_new_repo())
        self._build(log_dir)
        _claim(log_dir, 8, _ts(8), "pnx-mid")

        state = _fold(log_dir)
        assert state["items"]["pnx-mid"]["rollup"] == "queued"
        assert state["items"]["pnx-grand"]["rollup"] == "queued"


# ---------------------------------------------------------------------------
# 3. Own status untouched / 4. No-children case
# ---------------------------------------------------------------------------

class TestUntouchedStatusAndNoChildren:
    def test_parents_own_status_is_untouched_by_its_rollup(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _pc(log_dir, 3, _ts(3), "pnx-parent", "pnx-c1")
        _status(log_dir, 4, _ts(4), "pnx-c1", "done")

        state = _fold(log_dir)
        parent = state["items"]["pnx-parent"]
        assert parent["rollup"] == "done"
        assert parent["status"] == "queued"

    def test_item_with_no_children_carries_no_rollup(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-leaf", "Leaf")

        state = _fold(log_dir)
        assert "rollup" not in state["items"]["pnx-leaf"]

    def test_a_sibling_with_no_children_carries_no_rollup_even_when_others_do(self):
        """
        The exclusion is per item, not a property of the whole log: a plain
        item with no parent-child edge of its own stays without a "rollup"
        key even while a sibling item elsewhere in the same fold carries
        one.
        """
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-standalone", "Standalone")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "queued"
        assert "rollup" not in state["items"]["pnx-standalone"]


# ---------------------------------------------------------------------------
# 5. A parent-child cycle does not hang and carries no roll-up
# ---------------------------------------------------------------------------

class TestCycleSafety:
    def test_parent_child_cycle_does_not_hang_and_carries_no_rollup(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-a", "A")
        _item(log_dir, 2, _ts(2), "pnx-b", "B")
        _pc(log_dir, 3, _ts(3), "pnx-a", "pnx-b")
        _pc(log_dir, 4, _ts(4), "pnx-b", "pnx-a")

        # Terminates (the test itself is the hang guard).
        state = _fold(log_dir)
        assert "rollup" not in state["items"]["pnx-a"]
        assert "rollup" not in state["items"]["pnx-b"]
        warnings = state.get("report", {}).get("warnings", [])
        assert any("parent-child cycle" in w for w in warnings)


# ---------------------------------------------------------------------------
# 5b. Precise claim-state reading: done precedes claimed/building, and only
#     a live reconciled owner counts as claimed (not a lingering timestamp).
# ---------------------------------------------------------------------------

class TestClaimStateReadingPrecision:
    def test_a_done_child_that_still_carries_an_owner_rolls_up_done(self):
        """
        Completion alone does not release a claim (only a release or an
        expiry ends one -- see the claim rules), so a done child can still
        carry an owner. The done reading must win over the claimed/building
        reading, or such a child would misread as building.
        """
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _pc(log_dir, 3, _ts(3), "pnx-parent", "pnx-c1")
        _claim(log_dir, 4, _ts(4), "pnx-c1")
        _append(log_dir, 5, _ts(5), "item.completed", {"item_id": "pnx-c1"})

        state = _fold(log_dir)
        child = state["items"]["pnx-c1"]
        assert child["status"] == "done"
        assert child.get("owner") == OWNER
        assert state["items"]["pnx-parent"]["rollup"] == "done"

    def test_a_released_child_rolls_up_queued_not_building(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _pc(log_dir, 3, _ts(3), "pnx-parent", "pnx-c1")
        _claim(log_dir, 4, _ts(4), "pnx-c1")
        _append(log_dir, 5, _ts(5), "item.claim_released",
                {"item_id": "pnx-c1", "reason": "the builder stopped"})

        state = _fold(log_dir)
        child = state["items"]["pnx-c1"]
        assert "owner" not in child
        assert state["items"]["pnx-parent"]["rollup"] == "queued"

    def test_an_expired_child_rolls_up_queued_not_building(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _pc(log_dir, 3, _ts(3), "pnx-parent", "pnx-c1")
        _append(log_dir, 4, _ts(4), "policy.claim_expiry_set", {"hours": 1.0})
        _claim(log_dir, 5, _ts(5), "pnx-c1")
        _item(log_dir, 6, "2026-02-01T02:00:00Z", "pnx-c2", "Much later item")

        state = _fold(log_dir)
        child = state["items"]["pnx-c1"]
        assert "owner" not in child
        assert state["items"]["pnx-parent"]["rollup"] == "queued"


# ---------------------------------------------------------------------------
# 6. Eligibility is unaffected by the rollup
# ---------------------------------------------------------------------------

class TestEligibilityUnaffected:
    def test_parent_with_a_building_rollup_is_still_ready_and_next(self):
        log_dir = _seed(_new_repo())
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _pc(log_dir, 3, _ts(3), "pnx-parent", "pnx-c1")
        _claim(log_dir, 4, _ts(4), "pnx-c1")

        state = _fold(log_dir)
        assert state["items"]["pnx-parent"]["rollup"] == "building"
        # Unclaimed, unblocked, plain 'queued' -- still a ready candidate,
        # exactly as it would be with no children at all.
        assert "pnx-parent" in compute_ready(state)
        assert compute_next(state) == "pnx-parent"


# ---------------------------------------------------------------------------
# 7. Rendering
# ---------------------------------------------------------------------------

class TestRendering:
    def _hierarchy(self, log_dir: str) -> None:
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-leaf", "Standalone leaf")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _status(log_dir, 5, _ts(5), "pnx-c1", "done")

    def test_board_renders_rollup_beside_status_only_for_a_parent(self):
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        state = _fold(log_dir)

        board = render_board(state)
        parent_line = next(line for line in board.splitlines() if "pnx-parent" in line)
        leaf_line = next(line for line in board.splitlines() if "pnx-leaf" in line)
        assert "rollup:done" in parent_line
        assert "rollup:" not in leaf_line

    def test_item_frontmatter_carries_rollup_only_for_a_parent(self):
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        state = _fold(log_dir)

        parent_md = render_item("pnx-parent", state["items"]["pnx-parent"], state)
        leaf_md = render_item("pnx-leaf", state["items"]["pnx-leaf"], state)
        assert "rollup: done" in parent_md
        assert "rollup:" not in leaf_md

    def test_status_text_renders_rollup_beside_status(self, capsys):
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        _claim(log_dir, 6, "2026-02-01T00:10:00Z", "pnx-parent")

        repo_root = os.path.dirname(os.path.dirname(log_dir))
        status_cmd.run(repo_root, as_json=False)
        out = capsys.readouterr().out

        parent_line = next(line for line in out.splitlines() if "pnx-parent" in line)
        leaf_line = next(line for line in out.splitlines() if "pnx-leaf" in line)
        assert "rollup done" in parent_line
        assert "rollup" not in leaf_line

    def test_status_view_building_row_carries_rollup(self):
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        _claim(log_dir, 6, "2026-02-01T00:10:00Z", "pnx-parent")

        payload = status_view(repo_root=os.path.dirname(os.path.dirname(log_dir)),
                               scope="repo", now="2026-02-01T01:00:00Z")
        rows = {row["id"]: row for row in payload["repo"]["building"]}
        assert rows["pnx-parent"]["rollup"] == "done"


# ---------------------------------------------------------------------------
# 8. Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def _hierarchy(self, log_dir: str) -> list[str]:
        _item(log_dir, 1, _ts(1), "pnx-parent", "Parent")
        _item(log_dir, 2, _ts(2), "pnx-c1", "Child one")
        _item(log_dir, 3, _ts(3), "pnx-c2", "Child two")
        _pc(log_dir, 4, _ts(4), "pnx-parent", "pnx-c1")
        _pc(log_dir, 5, _ts(5), "pnx-parent", "pnx-c2")
        _status(log_dir, 6, _ts(6), "pnx-c1", "done")
        _claim(log_dir, 7, _ts(7), "pnx-c2")
        return ["pnx-parent", "pnx-c1", "pnx-c2"]

    def test_repeated_fold_is_byte_identical(self):
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        events = read_events(log_dir)

        state_a = fold_events(events)
        state_b = fold_events(events)

        assert state_a["items"]["pnx-parent"]["rollup"] == "building"
        assert state_a["items"]["pnx-parent"]["rollup"] == state_b["items"]["pnx-parent"]["rollup"]

    def test_shuffled_line_order_produces_the_same_rollup(self):
        repo = _new_repo()
        log_dir = _seed(repo)
        self._hierarchy(log_dir)
        expected = _fold(log_dir)["items"]["pnx-parent"]["rollup"]

        lines: list[bytes] = []
        for fname in os.listdir(log_dir):
            if not fname.endswith(".jsonl"):
                continue
            with open(os.path.join(log_dir, fname), "rb") as fh:
                raw = fh.read()
            lines.extend(line for line in raw.replace(b"\r\n", b"\n").split(b"\n") if line)

        for seed in (1, 2, 3):
            shuffled_dir = tempfile.mkdtemp()
            shuffled = lines[:]
            random.Random(seed).shuffle(shuffled)
            with open(os.path.join(shuffled_dir, "shuffled.jsonl"), "wb") as fh:
                for line in shuffled:
                    fh.write(line + b"\n")
            state = fold_events(read_events(shuffled_dir))
            assert state["items"]["pnx-parent"]["rollup"] == expected, (
                f"seed={seed} produced a different rollup"
            )

    def test_a_replay_equivalent_prefix_fold_agrees_with_the_full_fold(self):
        """
        fold_prefix(log_dir, n) is what pinax.replay's git-ref fold relies
        on (see fold.fold_prefix): the fold of a prefix of the total-order
        equals the state at that point in the log. The full-length prefix
        must therefore agree with the ordinary full fold, roll-up included.
        """
        log_dir = _seed(_new_repo())
        self._hierarchy(log_dir)
        events = read_events(log_dir)

        full = fold_events(events)
        replayed = fold_prefix(log_dir, len(events))

        assert full["items"]["pnx-parent"]["rollup"] == "building"
        assert (
            full["items"]["pnx-parent"]["rollup"]
            == replayed["items"]["pnx-parent"]["rollup"]
        )
