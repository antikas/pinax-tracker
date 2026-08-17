from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.statusview import find_pinax_repo_root, status_view

pytestmark = pytest.mark.deep


_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env(**overrides: str) -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    env.update(overrides)
    return env


def _append(log_dir: str, seq: int, ts: str, etype: str, payload: dict) -> None:
    event = mint_event(
        seq=seq,
        ts=ts,
        actor="tester@host",
        etype=etype,
        payload=payload,
    )
    append_event(log_dir, event, actor="tester@host")


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    log_dir = root / ".ergon" / "log"
    log_dir.mkdir(parents=True)
    _append(str(log_dir), 0, "2026-07-01T00:00:00Z", "ergon.created", {"repo": "repo"})
    _append(str(log_dir), 1, "2026-07-01T00:00:01Z", "phase.opened", {"phase": "pnx"})
    _append(
        str(log_dir),
        2,
        "2026-07-01T00:00:02Z",
        "item.created",
        {"item_id": "pnx-build", "title": "Build thing", "prefix": "pnx"},
    )
    _append(
        str(log_dir),
        3,
        "2026-07-01T00:00:03Z",
        "item.claimed",
        {"item_id": "pnx-build"},
    )
    _append(
        str(log_dir),
        4,
        "2026-07-01T00:00:04Z",
        "item.status_changed",
        {"item_id": "pnx-build", "status": "building"},
    )
    _append(
        str(log_dir),
        5,
        "2026-07-01T00:00:05Z",
        "item.created",
        {"item_id": "pnx-done", "title": "Done thing", "prefix": "pnx"},
    )
    _append(
        str(log_dir),
        6,
        "2026-07-07T00:00:00Z",
        "item.completed",
        {"item_id": "pnx-done", "briefing": "done"},
    )
    _append(
        str(log_dir),
        7,
        "2026-07-01T00:00:07Z",
        "item.created",
        {"item_id": "pnx-old", "title": "Old done", "prefix": "pnx"},
    )
    _append(
        str(log_dir),
        8,
        "2026-06-01T00:00:00Z",
        "item.completed",
        {"item_id": "pnx-old", "briefing": "old"},
    )
    _append(
        str(log_dir),
        9,
        "2026-07-01T00:00:09Z",
        "item.created",
        {"item_id": "pnx-park", "title": "Parked thing", "prefix": "pnx"},
    )
    _append(
        str(log_dir),
        10,
        "2026-07-01T00:00:10Z",
        "item.parked",
        {"item_id": "pnx-park", "reason": "waiting"},
    )
    _append(
        str(log_dir),
        11,
        "2026-07-01T00:00:11Z",
        "item.created",
        {"item_id": "pnx-next", "title": "Next thing", "prefix": "pnx"},
    )
    return root


def test_status_view_repo_contract(repo):
    payload = status_view(
        repo_root=str(repo),
        scope="repo",
        now="2026-07-08T00:00:00Z",
    )

    assert payload["schema"] == "pinax.status.v1"
    assert payload["scope"] == "repo"
    view = payload["repo"]
    assert view["id"] == "repo"
    assert view["building"][0]["id"] == "pnx-build"
    assert view["building"][0]["owner"] == "tester@host"
    assert [item["id"] for item in view["shipped_recent"]] == ["pnx-done"]
    assert view["shipped_earlier_count"] == 1
    assert view["parked"] == [
        {
            "id": "pnx-park",
            "title": "Parked thing",
            "kind": "parked",
            "reason": "waiting",
        }
    ]
    assert view["next"] == {"id": "pnx-next", "title": "Next thing"}
    assert view["queue_depth"] == 1


def test_find_repo_root_and_auto_scope_from_subdir(repo):
    subdir = repo / "docs" / "nested"
    subdir.mkdir(parents=True)
    assert find_pinax_repo_root(str(subdir)) == str(repo)

    payload = status_view(
        repo_root=str(subdir),
        scope="auto",
        now="2026-07-08T00:00:00Z",
    )
    assert payload["scope"] == "repo"
    assert payload["repo"]["id"] == "repo"


def test_portfolio_scope_uses_pinax_roots(repo, tmp_path, monkeypatch):
    root = tmp_path / "roots"
    root.mkdir()
    target = root / "target"
    target.mkdir()
    os.rename(repo, target / "repo")
    monkeypatch.setenv("PINAX_ROOTS", str(root))

    payload = status_view(
        repo_root=str(tmp_path / "outside"),
        scope="portfolio",
        now="2026-07-08T00:00:00Z",
    )

    assert payload["schema"] == "pinax.status.v1"
    assert payload["scope"] == "portfolio"
    assert [repo_view["id"] for repo_view in payload["repos"]] == ["repo"]


def test_status_cli_json_and_one_arg_error(repo):
    result = subprocess.run(
        [sys.executable, "-m", "pinax", "status", "--json"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_build_env(),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema"] == "pinax.status.v1"
    assert result.stdout.isascii()

    bad = subprocess.run(
        [sys.executable, "-m", "pinax", "status", "pnx-build"],
        cwd=repo,
        capture_output=True,
        text=True,
        env=_build_env(),
    )
    assert bad.returncode == 2
    assert "pinax status [--json]" in bad.stderr
    assert "pinax status <id> <state>" in bad.stderr
