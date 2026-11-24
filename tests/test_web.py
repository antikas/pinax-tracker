"""End-to-end checks for the loopback-only Pinax web view."""

from __future__ import annotations

import html
import json
import os
import queue
import re
import threading
import signal
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from pinax.commands.web_cmd import _board_page, _history_page, _item_page, _local_authority
from pinax.fold import fold, read_events
from pinax.projection import render_board


SOURCE_ROOT = Path(__file__).resolve().parent.parent
ACTOR = "owner@desk"
pytestmark = pytest.mark.deep


def _env() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SOURCE_ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    return env


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo,
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _tracked_repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "tracked"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "owner@example.invalid")
    _git(repo, "config", "user.name", "Owner")

    initialized = _run(repo, "init", "--actor", ACTOR)
    assert initialized.returncode == 0, initialized.stderr
    _git(repo, "add", "--", ".ergon/.gitignore", ".ergon/.gitattributes")
    _git(repo, "commit", "--allow-empty", "-qm", "Track Pinax repository settings")

    created = _run(
        repo,
        "add",
        "--title",
        "Unsafe <b>title</b>",
        "--actor",
        ACTOR,
        "--offline",
        "--json",
    )
    assert created.returncode == 0, created.stderr
    item_id = json.loads(created.stdout)["item_id"]

    briefing = "A long briefing with authored <script>text</script>.\n\n" + ("Detail. " * 400)
    briefing_path = tmp_path / "briefing.md"
    briefing_path.write_text(briefing, encoding="utf-8")
    completed = _run(
        repo,
        "done",
        item_id,
        "--briefing",
        str(briefing_path),
        "--actor",
        ACTOR,
        "--offline",
    )
    assert completed.returncode == 0, completed.stderr
    other = _run(repo, "add", "--title", "A second item.created event", "--actor", ACTOR, "--offline")
    assert other.returncode == 0, other.stderr
    return repo, item_id, briefing


def _start_web(repo: Path) -> tuple[subprocess.Popen[str], str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "pinax", "web", "--port", "0"],
        cwd=repo,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    addresses = queue.Queue()
    reader = threading.Thread(target=lambda: addresses.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        address = addresses.get(timeout=5).strip()
    except queue.Empty:
        process.terminate()
        process.wait(timeout=5)
        pytest.fail("the board did not announce its listening address")
    if not address and process.poll() is not None:
        error = process.stderr.read() if process.stderr is not None else ""
        if "Operation not permitted" in error:
            pytest.skip("the isolated runner does not permit loopback listeners")
        raise AssertionError(error)
    assert address.startswith("http://127.0.0.1:"), address
    return process, address


def _get(address: str, path: str = "/") -> tuple[int, str]:
    with urlopen(address + path, timeout=5) as response:
        return response.status, response.read().decode("utf-8")


def _stop(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        process.terminate()
        process.wait(timeout=5)
    else:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=5)
        assert process.returncode == 0


def _snapshot(repo: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(repo): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in repo.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_web_cli_serves_live_board_items_and_history_without_writes(tmp_path: Path) -> None:
    repo, item_id, briefing = _tracked_repo(tmp_path)
    before = _snapshot(repo)
    process, address = _start_web(repo)
    try:
        status, board = _get(address)
        state = fold(str(repo / ".ergon" / "log"))
        assert status == 200
        projected = render_board(state)
        assert "## Done" in projected and "<h2>Done</h2>" in board
        assert state["items"][item_id]["title"] in projected
        assert board.count(html.escape(state["items"][item_id]["title"])) == 1
        assert f'href="/items/{item_id}"' in board
        assert 'href="/history"' in board
        assert "Unsafe &lt;b&gt;title&lt;/b&gt;" in board
        assert "@media (max-width:" in board

        status, item = _get(address, f"/items/{item_id}")
        assert status == 200
        assert briefing[:80].replace("<", "&lt;").replace(">", "&gt;") in item
        assert "&lt;script&gt;text&lt;/script&gt;" in item

        status, history = _get(address, "/history")
        assert status == 200
        expected = read_events(str(repo / ".ergon" / "log"))
        assert [event["type"] for event in expected].count("item.created") >= 2
        assert re.findall(r'<li id="event-([^"]+)">', history) == [event["id"] for event in expected]

        missing = Request(address + "/items/unknown-item")
        try:
            urlopen(missing, timeout=5)
        except HTTPError as error:
            assert error.code == 404
            assert "was not found" in error.read().decode("utf-8")
        else:
            raise AssertionError("unknown item must return 404")

        for method in ("POST", "OPTIONS"):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(address + "/", method=method), timeout=5)
            assert error.value.code == 405
            assert error.value.headers["Allow"] == "GET, HEAD"

        assert _snapshot(repo) == before

        added = _run(
            repo,
            "add",
            "--title",
            "Visible after refresh",
            "--actor",
            ACTOR,
            "--offline",
        )
        assert added.returncode == 0, added.stderr
        after_write = _snapshot(repo)
        _, refreshed = _get(address)
        assert "Visible after refresh" in refreshed
        assert _snapshot(repo) == after_write
    finally:
        _stop(process)

    clean = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert clean.stdout == ""


def test_web_reports_empty_board_and_missing_tracker(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "owner@example.invalid")
    _git(repo, "config", "user.name", "Owner")
    initialized = _run(repo, "init", "--actor", ACTOR)
    assert initialized.returncode == 0, initialized.stderr

    process, address = _start_web(repo)
    try:
        _, board = _get(address)
        assert "No items yet." in board
    finally:
        _stop(process)

    missing_repo = tmp_path / "missing"
    missing_repo.mkdir()
    _git(missing_repo, "init", "-q", "-b", "main")
    missing = _run(missing_repo, "web", "--port", "0")
    assert missing.returncode == 1
    assert ".ergon/log/ not found" in missing.stderr


def test_web_pages_escape_tracker_text_and_reuse_fold_rendering(tmp_path: Path) -> None:
    repo, item_id, briefing = _tracked_repo(tmp_path)
    log_dir = repo / ".ergon" / "log"
    state = fold(str(log_dir))

    board = _board_page(state)
    item = _item_page(item_id, state["items"][item_id], state)
    history = _history_page(read_events(str(log_dir)))

    title = state["items"][item_id]["title"]
    prefix = f"- {item_id} · {title} · "
    detail = next(line.removeprefix(prefix) for line in render_board(state).splitlines() if line.startswith(prefix))
    assert html.escape(detail) in board
    assert board.count(html.escape(title)) == 1
    assert f'href="/items/{item_id}"' in board
    assert "Unsafe &lt;b&gt;title&lt;/b&gt;" in board
    assert "No items yet." in _board_page({"items": {}})
    assert briefing[:80].replace("<", "&lt;").replace(">", "&gt;") in item
    assert "&lt;script&gt;text&lt;/script&gt;" in item
    assert re.findall(r'<li id="event-([^"]+)">', history) == [
        event["id"] for event in read_events(str(log_dir))
    ]


def test_web_is_discoverable_and_refuses_a_repo_without_a_tracker(tmp_path: Path) -> None:
    help_result = _run(tmp_path, "web", "--help")
    assert help_result.returncode == 0
    assert "loopback" in help_result.stdout.lower()
    assert "--port PORT" in help_result.stdout

    invalid_port = _run(tmp_path, "web", "--port", "65536")
    assert invalid_port.returncode == 2
    assert "integer from 0 to 65535" in invalid_port.stderr

    missing_repo = tmp_path / "missing"
    missing_repo.mkdir()
    _git(missing_repo, "init", "-q", "-b", "main")
    missing = _run(missing_repo, "web", "--port", "0")
    assert missing.returncode == 1
    assert ".ergon/log/ not found" in missing.stderr


def test_the_selected_repository_only_accepts_a_local_authority() -> None:
    assert _local_authority(["127.0.0.1:8899"], 8899)
    assert _local_authority(["localhost:8899"], 8899)
    assert _local_authority(["LOCALHOST"], 80)
    assert not _local_authority(["example.invalid:8899"], 8899)
    assert not _local_authority(["localhost:9999"], 8899)
    assert not _local_authority(["localhost:8899", "localhost:8899"], 8899)
    assert not _local_authority([], 8899)
