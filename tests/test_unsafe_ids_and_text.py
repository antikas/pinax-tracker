"""
No item id makes Pinax write outside .ergon/items/, and no text ends a
command in a traceback.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from pinax import __main__ as cli
from pinax.append import UnwritableEventError, append_event
from pinax.event import MAX_SEQ, mint_event
from pinax.fold import fold, fold_events, read_events
from tests.test_malformed_log_lines import (
    ACTOR, SHARD, _git, _pinax, _plant, _tracker, _tree,
)

LONE = "\udcff"


@pytest.fixture()
def tracker(tmp_path) -> Path:
    return _tracker(tmp_path / "repo")


def _planted(item_id: str = "pnx-zzzz", title: str = "Planted") -> dict:
    return mint_event(
        seq=50, ts="2026-11-23T10:00:00Z", actor="other@desk", etype="item.created",
        payload={"item_id": item_id, "title": title, "prefix": "pnx", "status": "queued"},
    )


def _run(repo: Path, monkeypatch, capsys, *args: str) -> tuple[int, str, str]:
    """Run the CLI in this process, which can be handed text no locale encodes."""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("PINAX_ROOT", raising=False)
    capsys.readouterr()
    code = 0
    try:
        cli.main(list(args))
    except SystemExit as exc:
        code = exc.code or 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    return code, captured.out, captured.err


@pytest.mark.parametrize("prefix", ["a/b", "../x", "a:b", "a b", "", "_a", "-a", "é", "a\n", LONE])
def test_add_refuses_an_unsafe_prefix(tracker, monkeypatch, capsys, prefix):
    before = _tree(tracker)

    code, out, err = _run(
        tracker, monkeypatch, capsys,
        "add", "--title", "T", "--prefix=" + prefix, "--actor", ACTOR, "--offline",
        "--allow-new-prefix", "--json",
    )

    assert code == 1
    lines = out.splitlines()
    assert len(lines) == 1
    result = json.loads(lines[0])
    assert result["sync_status"] == "invalid_argument"
    assert result["published"] is False
    assert "prefix" in err
    assert _tree(tracker) == before


@pytest.mark.parametrize("prefix", ["a", "A9", "9-a_b"])
def test_add_accepts_a_safe_prefix(tracker, prefix):
    done = _pinax(
        tracker, "add", "--title", "T", "--prefix", prefix, "--allow-new-prefix",
        "--actor", ACTOR, "--offline", "--json",
    )
    item_id = json.loads(done.stdout)["item_id"]
    assert (tracker / ".ergon" / "items" / f"{item_id}.md").is_file()


def test_an_id_that_climbs_out_never_overwrites_a_file(tracker):
    readme = tracker / "README.md"
    readme.write_bytes(b"known bytes\n")
    _plant(tracker, json.dumps(_planted("../../README")))

    board = _pinax(tracker, "board")
    assert board.returncode == 0
    assert "README" not in board.stdout
    assert "cannot be a file name" in board.stderr
    add = _pinax(tracker, "add", "--title", "T", "--actor", ACTOR, "--offline")
    assert add.returncode == 0, add.stderr
    assert readme.read_bytes() == b"known bytes\n"


@pytest.mark.parametrize("item_id", [
    "", ".", "..", "a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b",
    "a\x00b", "a\nb", "a\x7fb", "a\x85b", "a ", "a.",
])
def test_the_fold_ignores_an_id_that_cannot_be_a_file_name(item_id, caplog):
    with caplog.at_level(logging.WARNING, logger="pinax.fold"):
        state = fold_events([_planted(item_id)])
    assert state.get("items", {}) == {}
    assert "cannot be a file name" in caplog.text


@pytest.mark.parametrize("item_id", ["pnx-a1b2", "a b-c", ".a", "é-1", "a..b", "-a"])
def test_the_fold_keeps_every_other_id(item_id):
    assert list(fold_events([_planted(item_id)])["items"]) == [item_id]


def _item(tracker: Path) -> str:
    return next(iter(fold(str(tracker / ".ergon" / "log"))["items"]))


WRITES = {
    "add": ("add", "--title", LONE),
    "add pair": ("add", "--title", "\ud83d\ude00"),
    "done": ("done", "ITEM", "--briefing", "b.md", "--actor", f"b{LONE}@desk"),
    "block": ("block", "ITEM", "--gate", "scope", "--actor", f"b{LONE}@desk"),
    "park": ("park", "ITEM", "--reason", LONE),
    "decide": ("decide", "ITEM", "--option", "a", "--label", LONE, "--effect", "park",
               "--reason", "r"),
    "policy": ("policy", "note-refs", "--prefix", LONE),
    "note": ("note", "add", "ITEM", "--ref", "https://example.invalid/", "--caption", LONE),
    "priority": ("priority", "ITEM", "top", "--actor", f"b{LONE}@desk"),
    "dep": ("dep", "add", "ITEM", "--blocks", "ITEM2", "--actor", f"b{LONE}@desk"),
    "annul": ("annul", "EVENT", "--reason", LONE),
}


@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
@pytest.mark.parametrize("name", sorted(WRITES))
def test_a_write_given_a_lone_surrogate_appends_nothing(
    tracker, monkeypatch, capsys, name, as_json
):
    (tracker / "b.md").write_text("done", encoding="utf-8")
    item = _item(tracker)
    _pinax(tracker, "add", "--title", "Second", "--actor", ACTOR, "--offline")
    log = str(tracker / ".ergon" / "log")
    names = {
        "ITEM": item,
        "ITEM2": next(i for i in fold(log)["items"] if i != item),
        "EVENT": read_events(log)[0]["id"],
    }
    args = [names.get(word, word) for word in WRITES[name]] + ["--offline"]
    if "--actor" not in args:
        args += ["--actor", ACTOR]
    if as_json:
        args.append("--json")
    before, head = _tree(tracker), _git(tracker, "rev-parse", "HEAD")

    code, out, err = _run(tracker, monkeypatch, capsys, *args)

    assert code == 1
    assert "UTF-8 cannot encode" in err
    assert _tree(tracker) == before
    assert _git(tracker, "rev-parse", "HEAD") == head
    if as_json:
        assert len(out.splitlines()) == 1
        result = json.loads(out)
        assert result["sync_status"] == "invalid_argument"
        assert result["exit_code"] == 1
    else:
        assert out == ""


def test_a_command_that_never_pushes_refuses_it_too(tracker, monkeypatch, capsys):
    before = _tree(tracker)
    code, out, _err = _run(
        tracker, monkeypatch, capsys,
        "registry", "add", "--id", "r", "--url", "https://example.invalid/" + LONE,
        "--actor", ACTOR, "--json",
    )
    assert code == 1
    assert json.loads(out)["sync_status"] == "invalid_argument"
    assert _tree(tracker) == before


@pytest.mark.skipif(os.name == "nt", reason="arguments are not bytes on Windows")
def test_a_title_typed_in_another_encoding_is_refused(tracker):
    done = _pinax(
        tracker, "add", "--title", os.fsdecode(b"caf\xe9"), "--actor", ACTOR, "--offline",
        "--json", PYTHONUTF8="1",
    )
    assert done.returncode == 1
    assert json.loads(done.stdout)["sync_status"] == "invalid_argument"


def test_a_lone_surrogate_already_in_a_log_is_written_as_the_replacement_character(tracker):
    _plant(tracker, json.dumps(_planted(title="half \ud800 half")))

    assert _pinax(tracker, "board").returncode == 0
    add = _pinax(tracker, "add", "--title", "T", "--actor", ACTOR, "--offline")
    assert add.returncode == 0, add.stderr
    board = (tracker / ".ergon" / "board.md").read_text(encoding="utf-8")
    assert "half � half" in board
    page = (tracker / ".ergon" / "items" / "pnx-zzzz.md").read_text(encoding="utf-8")
    assert "half � half" in page
    assert _pinax(tracker, "verify").returncode == 0


def test_a_lone_surrogate_in_an_item_id_still_gives_one_page(tracker):
    _plant(tracker, json.dumps(_planted("pnx-\ud800")))
    _plant(tracker, json.dumps(_planted("pnx-\ud801", title="Other")))

    add = _pinax(tracker, "add", "--title", "T", "--actor", ACTOR, "--offline")
    assert add.returncode == 0, add.stderr
    assert (tracker / ".ergon" / "items" / "pnx-�.md").is_file()
    assert _pinax(tracker, "verify").returncode == 0


def test_no_command_writes_a_seq_past_the_bound(tracker, monkeypatch, capsys):
    last = mint_event(
        seq=MAX_SEQ, ts="2026-11-23T10:00:00Z", actor="other@desk", etype="item.created",
        payload={"item_id": "pnx-last", "title": "Last"},
    )
    _plant(tracker, json.dumps(last))
    assert _pinax(tracker, "ready").returncode == 0
    before = (tracker / SHARD).read_bytes()

    code, out, err = _run(
        tracker, monkeypatch, capsys,
        "add", "--title", "T", "--actor", ACTOR, "--offline", "--json",
    )

    assert code == 1
    assert json.loads(out)["sync_status"] == "invalid_argument"
    assert "largest sequence number" in err
    assert (tracker / SHARD).read_bytes() == before


def test_the_check_sits_in_the_append(tmp_path):
    event = _planted(title=LONE)
    with pytest.raises(UnwritableEventError):
        append_event(str(tmp_path), event)
    assert os.listdir(tmp_path) == []
