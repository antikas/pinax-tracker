"""Registry command and fold tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold
from pinax.commands.registry_cmd import run_add, run_rm, run_list

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


# ---------------------------------------------------------------------------
# Fold-level: last-write-wins ordering
# ---------------------------------------------------------------------------

class TestRegistryFold:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        _make_repo(self.tmp)
        self.log_dir = os.path.join(self.tmp, ".ergon", "log")

    def test_single_add_present(self):
        _append(self.log_dir, 0, "2026-07-02T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "sample-project", "path": "/workspace/sample-project"})
        state = fold(self.log_dir)
        assert "sample-project" in state["registry"]
        assert state["registry"]["sample-project"]["path"] == "/workspace/sample-project"

    def test_add_then_remove_absent(self):
        _append(self.log_dir, 0, "2026-07-02T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "sample-project", "path": "/workspace/sample-project"})
        _append(self.log_dir, 1, "2026-07-02T00:00:01Z", "a@h", "registry.repo_removed",
                {"repo_id": "sample-project"})
        state = fold(self.log_dir)
        assert "sample-project" not in state.get("registry", {})

    def test_add_remove_readd_present(self):
        _append(self.log_dir, 0, "2026-07-02T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "sample-project", "path": "/workspace/sample-project"})
        _append(self.log_dir, 1, "2026-07-02T00:00:01Z", "a@h", "registry.repo_removed",
                {"repo_id": "sample-project"})
        _append(self.log_dir, 2, "2026-07-02T00:00:02Z", "a@h", "registry.repo_added",
                {"repo_id": "sample-project", "path": "/workspace/sample-project-v2"})
        state = fold(self.log_dir)
        assert state["registry"]["sample-project"]["path"] == "/workspace/sample-project-v2"

    def test_remove_then_add_present_regardless_of_line_order(self):
        """Order-independence: rm (seq=0) then add (seq=1) -> add wins, and
        shuffling the two lines on disk must not change the result."""
        ev_rm = mint_event(seq=0, ts="2026-07-02T00:00:00Z", actor="a@h",
                            etype="registry.repo_removed", payload={"repo_id": "sample-project"})
        ev_add = mint_event(seq=1, ts="2026-07-02T00:00:01Z", actor="a@h",
                             etype="registry.repo_added",
                             payload={"repo_id": "sample-project", "path": "/workspace/sample-project"})
        # Write in reverse order on disk — read_events() sorts by (seq, ts, actor, id).
        shard = os.path.join(self.log_dir, "a-h.jsonl")
        from pinax.event import serialise
        with open(shard, "w", newline="\n", encoding="utf-8") as fh:
            fh.write(serialise(ev_add) + "\n")
            fh.write(serialise(ev_rm) + "\n")
        state = fold(self.log_dir)
        assert "sample-project" in state["registry"]

    def test_no_registry_events_no_registry_key(self):
        """A log with no registry events produces no 'registry' key at all
        (preserves golden-state compatibility, mirrors the deps/edges absence rule)."""
        _append(self.log_dir, 0, "2026-07-02T00:00:00Z", "a@h", "ergon.created", {"repo": "x"})
        state = fold(self.log_dir)
        assert "registry" not in state

    def test_missing_repo_id_or_path_is_ignored_not_fatal(self):
        _append(self.log_dir, 0, "2026-07-02T00:00:00Z", "a@h", "registry.repo_added", {"path": "D:/x"})
        _append(self.log_dir, 1, "2026-07-02T00:00:01Z", "a@h", "registry.repo_removed", {})
        # Should not raise; both events are ignored (forward-compatible tolerance).
        state = fold(self.log_dir)
        assert state.get("registry", {}) == {}


# ---------------------------------------------------------------------------
# CLI: run_add / run_rm / run_list (Python API, in-process)
# ---------------------------------------------------------------------------

class TestRegistryCommandsAPI:
    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        _make_repo(self.hub)
        self.other = tempfile.mkdtemp()  # a real directory to register

    def test_run_add_appends_and_folds(self, capsys):
        run_add(self.hub, repo_id="otherrepo", path=self.other, actor="operator@example.test", as_json=True)
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["repo_id"] == "otherrepo"
        assert result["operation"] == "add"

        log_dir = os.path.join(self.hub, ".ergon", "log")
        state = fold(log_dir)
        assert "otherrepo" in state["registry"]
        # Path is stored forward-slash-normalised and absolute.
        assert "\\" not in state["registry"]["otherrepo"]["path"]

    def test_run_add_rejects_invalid_repo_id(self):
        with pytest.raises(SystemExit):
            run_add(self.hub, repo_id="Not Valid!", path=self.other, as_json=True)

    def test_run_add_rejects_nonexistent_path(self):
        with pytest.raises(SystemExit):
            run_add(self.hub, repo_id="ghost", path=os.path.join(self.other, "does-not-exist"), as_json=True)

    def test_run_add_requires_init(self):
        bare = tempfile.mkdtemp()  # no .ergon at all
        with pytest.raises(SystemExit):
            run_add(bare, repo_id="otherrepo", path=self.other, as_json=True)

    def test_run_rm_removes(self, capsys):
        run_add(self.hub, repo_id="otherrepo", path=self.other, actor="operator@example.test", as_json=True)
        capsys.readouterr()  # drain
        run_rm(self.hub, repo_id="otherrepo", actor="operator@example.test", as_json=True)
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["operation"] == "remove"

        log_dir = os.path.join(self.hub, ".ergon", "log")
        state = fold(log_dir)
        assert "otherrepo" not in state.get("registry", {})

    def test_run_list_is_read_only(self, capsys):
        run_add(self.hub, repo_id="otherrepo", path=self.other, actor="operator@example.test", as_json=True)
        capsys.readouterr()
        log_dir = os.path.join(self.hub, ".ergon", "log")
        events_before = fold(log_dir)

        run_list(self.hub, as_json=True)
        out = capsys.readouterr().out
        result = json.loads(out)
        assert "otherrepo" in result["registry"]

        events_after = fold(log_dir)
        assert events_before == events_after  # nothing appended by list

    def test_run_add_idempotent_upsert(self, capsys):
        """Re-adding the same id with a different path upserts (last-write-wins),
        it does not error and does not create two registry entries."""
        run_add(self.hub, repo_id="otherrepo", path=self.other, actor="operator@example.test", as_json=True)
        capsys.readouterr()
        other2 = tempfile.mkdtemp()
        run_add(self.hub, repo_id="otherrepo", path=other2, actor="operator@example.test", as_json=True)
        capsys.readouterr()
        log_dir = os.path.join(self.hub, ".ergon", "log")
        state = fold(log_dir)
        assert len(state["registry"]) == 1
        assert state["registry"]["otherrepo"]["path"] == os.path.abspath(other2).replace("\\", "/")


# ---------------------------------------------------------------------------
# CLI: subprocess-level hard-rejection tests (python -m pinax registry ...)
# ---------------------------------------------------------------------------

class TestRegistryCLISubprocess:
    def test_cli_add_rejects_bad_id(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        other = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "add", "--id", "Bad Id", "--path", other],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert result.returncode != 0

    def test_cli_add_list_rm_roundtrip(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        other = tempfile.mkdtemp()
        r1 = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "add", "--id", "otherrepo", "--path", other, "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert r1.returncode == 0, r1.stderr

        r2 = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "list", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert r2.returncode == 0, r2.stderr
        listed = json.loads(r2.stdout)
        assert "otherrepo" in listed["registry"]

        r3 = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "rm", "--id", "otherrepo", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        assert r3.returncode == 0, r3.stderr

        r4 = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "list", "--json"],
            capture_output=True, cwd=hub, env=_build_env(),
        )
        listed_after = json.loads(r4.stdout)
        assert "otherrepo" not in listed_after["registry"]


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

class TestRegistryUrlEntries:
    """`registry add --url` — the manifest `pinax overview --remote` folds.
    Same registry.repo_added event, one new OPTIONAL payload field (SSOT)."""

    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        _make_repo(self.hub)
        self.log_dir = os.path.join(self.hub, ".ergon", "log")
        self.other = tempfile.mkdtemp()

    def test_fold_carries_url(self):
        _append(self.log_dir, 0, "2026-07-05T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "remoterepo", "url": "https://github.com/o/r.git"})
        state = fold(self.log_dir)
        assert state["registry"]["remoterepo"]["url"] == "https://github.com/o/r.git"
        assert "path" not in state["registry"]["remoterepo"]

    def test_fold_carries_both_path_and_url(self):
        _append(self.log_dir, 0, "2026-07-05T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "bothrepo", "path": "/workspace/both",
                 "url": "https://github.com/o/both.git"})
        state = fold(self.log_dir)
        entry = state["registry"]["bothrepo"]
        assert entry["path"] == "/workspace/both"
        assert entry["url"] == "https://github.com/o/both.git"

    def test_fold_ignores_event_with_neither_path_nor_url(self):
        _append(self.log_dir, 0, "2026-07-05T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "nakedrepo"})
        state = fold(self.log_dir)
        assert "nakedrepo" not in state.get("registry", {})

    def test_run_add_url_only(self, capsys):
        run_add(self.hub, repo_id="remoterepo",
                url="https://github.com/o/r.git",
                actor="operator@example.test", as_json=True)
        result = json.loads(capsys.readouterr().out)
        assert result["url"] == "https://github.com/o/r.git"
        assert result["path"] is None
        state = fold(self.log_dir)
        assert state["registry"]["remoterepo"]["url"] == "https://github.com/o/r.git"

    def test_run_add_rejects_neither_path_nor_url(self):
        with pytest.raises(SystemExit):
            run_add(self.hub, repo_id="nothing", actor="operator@example.test", as_json=True)

    def test_run_add_path_still_validated_when_url_present(self):
        """--url does not relax --path's directory validation."""
        with pytest.raises(SystemExit):
            run_add(self.hub, repo_id="badpath",
                    path=os.path.join(self.other, "does-not-exist"),
                    url="https://github.com/o/r.git", as_json=True)

    def test_run_list_shows_url(self, capsys):
        run_add(self.hub, repo_id="remoterepo",
                url="https://github.com/o/r.git",
                actor="operator@example.test", as_json=True)
        capsys.readouterr()
        run_list(self.hub, as_json=False)
        out = capsys.readouterr().out
        assert "url=https://github.com/o/r.git" in out

    def test_lww_readd_with_url_replaces_path_entry(self):
        """Last-write-wins replaces the WHOLE entry (same discipline as
        before): a later url-only re-add supersedes an earlier path entry."""
        _append(self.log_dir, 0, "2026-07-05T00:00:00Z", "a@h", "registry.repo_added",
                {"repo_id": "repo1", "path": "/workspace/repo1"})
        _append(self.log_dir, 1, "2026-07-05T00:00:01Z", "a@h", "registry.repo_added",
                {"repo_id": "repo1", "url": "https://github.com/o/repo1.git"})
        state = fold(self.log_dir)
        entry = state["registry"]["repo1"]
        assert entry["url"] == "https://github.com/o/repo1.git"
        assert "path" not in entry

    def test_cli_subprocess_add_url_only(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        r = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "add",
             "--id", "remoterepo", "--url", "https://github.com/o/r.git", "--json"],
            capture_output=True, cwd=hub, env=_build_env(), text=True,
        )
        assert r.returncode == 0, r.stderr
        r2 = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "list", "--json"],
            capture_output=True, cwd=hub, env=_build_env(), text=True,
        )
        listed = json.loads(r2.stdout)
        assert listed["registry"]["remoterepo"]["url"] == "https://github.com/o/r.git"

    def test_cli_subprocess_add_neither_rejected(self):
        hub = tempfile.mkdtemp()
        subprocess.run([sys.executable, "-m", "pinax", "init"], check=True,
                       capture_output=True, cwd=hub, env=_build_env())
        r = subprocess.run(
            [sys.executable, "-m", "pinax", "registry", "add", "--id", "nothing"],
            capture_output=True, cwd=hub, env=_build_env(), text=True,
        )
        assert r.returncode != 0
        assert "--path" in r.stderr and "--url" in r.stderr
