"""Board rendering tests."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold
from pinax.projection import render_board
from pinax.commands.board_cmd import run as board_run


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str, payload: dict) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _seed_repo(repo_dir: str) -> None:
    log_dir = os.path.join(repo_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    actor = "operator@example.test"
    _append(log_dir, 0, "2026-07-01T00:00:00Z", actor, "ergon.created", {"repo": "seed"})
    _append(log_dir, 1, "2026-07-01T00:00:01Z", actor, "phase.opened", {"phase": "p1"})
    _append(log_dir, 2, "2026-07-01T00:00:02Z", actor, "item.created",
            {"item_id": "pnx-a1", "title": "Item A", "prefix": "p1"})


class TestBoardCommand:
    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        _seed_repo(self.repo)

    def test_board_matches_render_board_of_fresh_fold(self, capsys):
        board_run(self.repo, as_json=False)
        out = capsys.readouterr().out

        log_dir = os.path.join(self.repo, ".ergon", "log")
        expected = render_board(fold(log_dir))
        assert out == expected

    def test_board_json_shape(self, capsys):
        board_run(self.repo, as_json=True)
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "state" in payload
        assert "repo" in payload
        assert "pnx-a1" in payload["state"]["items"]

    def test_board_missing_ergon_exits_1(self):
        bare = tempfile.mkdtemp()
        with pytest.raises(SystemExit):
            board_run(bare, as_json=False)

    def test_board_never_writes_to_repo(self, capsys):
        before = {}
        for root, _dirs, files in os.walk(self.repo):
            for f in files:
                p = os.path.join(root, f)
                before[p] = os.path.getmtime(p)

        board_run(self.repo, as_json=False)
        capsys.readouterr()
        board_run(self.repo, as_json=True)
        capsys.readouterr()

        after = {}
        for root, _dirs, files in os.walk(self.repo):
            for f in files:
                p = os.path.join(root, f)
                after[p] = os.path.getmtime(p)

        assert before == after, "pinax board must never write to the repo (pure read)"
        # No board.md was created by the read-only command (init/regenerate never ran).
        assert not os.path.isfile(os.path.join(self.repo, ".ergon", "board.md"))
