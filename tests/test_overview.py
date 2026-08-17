"""Portfolio overview discovery and rendering tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep

from pinax.append import append_event
from pinax.event import mint_event
from pinax.fold import fold
from pinax.commands.registry_cmd import run_add
from pinax.commands.overview import (
    run as overview_run,
    _discover_repos,
    _summarise_repo,
    _scan_roots_for_ergon,
    _dedupe_worktrees,
    _dedupe_physical_path,
    _physical_path,
    _is_bare_repo,
)
from pinax.projection import render_overview


def _git(repo_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo_root}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git not available on PATH")


def _make_dir_alias(target: str, link: str) -> bool:
    """
    Create an OS-level directory alias at `link` pointing at `target`: a
    Windows directory junction (`mklink /J`) or a POSIX directory symlink.
    Returns True on success, False if alias creation
    isn't possible in this environment (permissions vary by host) -- callers
    should skip the test rather than fail the whole run.
    """
    if sys.platform == "win32":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str, payload: dict) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _seed_repo_with_items(repo_dir: str) -> None:
    """Seed a repo with one done, one parked, one blocked, one queued item."""
    log_dir = os.path.join(repo_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    actor = "operator@example.test"
    _append(log_dir, 0, "2026-07-01T00:00:00Z", actor, "ergon.created", {"repo": "seed"})
    _append(log_dir, 1, "2026-07-01T00:00:01Z", actor, "phase.opened", {"phase": "p1"})
    _append(log_dir, 2, "2026-07-01T00:00:02Z", actor, "item.created",
            {"item_id": "pnx-done1", "title": "Done item", "prefix": "p1"})
    _append(log_dir, 3, "2026-07-01T00:00:03Z", actor, "item.completed",
            {"item_id": "pnx-done1", "briefing": "shipped"})
    _append(log_dir, 4, "2026-07-01T00:00:04Z", actor, "item.created",
            {"item_id": "pnx-park1", "title": "Parked item", "prefix": "p1"})
    _append(log_dir, 5, "2026-07-01T00:00:05Z", actor, "item.parked",
            {"item_id": "pnx-park1", "reason": "waiting on decision"})
    _append(log_dir, 6, "2026-07-01T00:00:06Z", actor, "item.created",
            {"item_id": "pnx-block1", "title": "Blocked item", "prefix": "p1"})
    _append(log_dir, 7, "2026-07-01T00:00:07Z", actor, "item.blocked",
            {"item_id": "pnx-block1", "gate": "decision"})
    _append(log_dir, 8, "2026-07-01T00:00:08Z", actor, "item.created",
            {"item_id": "pnx-queued1", "title": "Queued item", "prefix": "p1"})


class TestOverviewMultiRepo:
    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(self.hub, ".ergon", "log"), 0, "2026-07-01T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        self.registered = tempfile.mkdtemp()
        _seed_repo_with_items(self.registered)

        self.uninitialised = tempfile.mkdtemp()  # a real dir, but no .ergon at all

        run_add(self.hub, repo_id="registeredrepo", path=self.registered,
                actor="operator@example.test", as_json=True)
        run_add(self.hub, repo_id="uninitrepo", path=self.uninitialised,
                actor="operator@example.test", as_json=True)

    def test_summarise_initialised_repo_counts(self):
        summary = _summarise_repo("registeredrepo", self.registered)
        assert summary["initialised"] is True
        assert summary["total_items"] == 4
        assert summary["by_status"]["done"] == 1
        assert summary["by_status"]["parked"] == 1
        assert summary["by_status"]["blocked"] == 1
        assert summary["by_status"]["queued"] == 1
        assert len(summary["parked"]) == 1
        assert summary["parked"][0]["id"] == "pnx-park1"
        assert summary["parked"][0]["reason"] == "waiting on decision"
        assert len(summary["blocked"]) == 1
        assert summary["blocked"][0]["gate"] == "decision"

    def test_summarise_uninitialised_repo(self):
        summary = _summarise_repo("uninitrepo", self.uninitialised)
        assert summary["initialised"] is False
        assert "total_items" not in summary

    def test_overview_run_json_shape(self, capsys):
        overview_run(self.hub, as_json=True, roots=[])
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "repos" in payload
        by_id = {r["id"]: r for r in payload["repos"]}
        assert by_id["registeredrepo"]["initialised"] is True
        assert by_id["uninitrepo"]["initialised"] is False
        hub_id = os.path.basename(os.path.normpath(self.hub))
        assert hub_id in by_id

    def test_overview_human_readable_flags_needs_attention(self, capsys):
        overview_run(self.hub, as_json=False, roots=[])
        out = capsys.readouterr().out
        assert "not initialised" in out
        assert "Needs attention" in out
        assert "registeredrepo/pnx-park1" in out
        assert "registeredrepo/pnx-block1" in out

    def test_overview_never_writes_into_registered_repo(self, capsys):
        """PURE READ across repos: no file appears/mtimes change in the
        registered repo's tree as a result of running overview."""
        before = {}
        for root, _dirs, files in os.walk(self.registered):
            for f in files:
                p = os.path.join(root, f)
                before[p] = os.path.getmtime(p)

        overview_run(self.hub, as_json=True, roots=[])
        capsys.readouterr()

        after = {}
        for root, _dirs, files in os.walk(self.registered):
            for f in files:
                p = os.path.join(root, f)
                after[p] = os.path.getmtime(p)

        assert before == after, "pinax overview must never write into a registered repo's tree"

    def test_overview_never_touches_knowledge_plane(self, capsys):
        sources_toml = os.path.join("/workspace/knowledge", ".koine-memory", "sources.toml")
        if not os.path.isfile(sources_toml):
            pytest.skip("sources.toml not at expected path — skipping live KP check")
        mtime_before = os.path.getmtime(sources_toml)
        overview_run(self.hub, as_json=True, roots=[])
        capsys.readouterr()
        mtime_after = os.path.getmtime(sources_toml)
        assert mtime_before == mtime_after, (
            "pinax overview wrote to sources.toml — this is a knowledge-plane violation!"
        )

    def test_overview_deterministic_repeat_calls(self, capsys):
        overview_run(self.hub, as_json=False, roots=[])
        first = capsys.readouterr().out
        overview_run(self.hub, as_json=False, roots=[])
        second = capsys.readouterr().out
        assert first == second


class TestRenderOverviewPure:
    """render_overview is a pure function — test it directly without any I/O."""

    def test_sorted_by_repo_id_regardless_of_input_order(self):
        reports = [
            {"id": "zzz", "initialised": False},
            {"id": "aaa", "initialised": False},
        ]
        out = render_overview(reports)
        assert out.index("## aaa") < out.index("## zzz")

    def test_deterministic_same_input_same_output(self):
        reports = [
            {"id": "p", "initialised": True, "total_items": 1, "by_status": {"queued": 1},
             "next": {"id": "p-1", "title": "T"}, "parked": [], "blocked": []},
        ]
        assert render_overview(reports) == render_overview(reports)

    def test_no_needs_attention_section_says_none(self):
        reports = [
            {"id": "p", "initialised": True, "total_items": 1, "by_status": {"queued": 1},
             "next": None, "parked": [], "blocked": []},
        ]
        out = render_overview(reports)
        assert "## Needs attention (cross-repo)" in out
        assert "(none)" in out


# ---------------------------------------------------------------------------
# registry-as-override backward compat, scan determinism.
# ---------------------------------------------------------------------------

def _init_ergon_repo(path: str, seed: bool = True) -> None:
    """Minimal .ergon/log/ directory, optionally seeded with one item."""
    log_dir = os.path.join(path, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    if seed:
        _append(log_dir, 0, "2026-07-03T00:00:00Z", "operator@example.test",
                "ergon.created", {"repo": os.path.basename(path)})


class TestRootScanDiscovery:
    """A configured root discovers fixture repositories with `.ergon` logs."""

    def setup_method(self) -> None:
        self.roots_parent = tempfile.mkdtemp()
        self.root = os.path.join(self.roots_parent, "src")
        os.makedirs(self.root)

    def test_scan_finds_ergon_dir_under_root(self):
        repo = os.path.join(self.root, "somerepo")
        os.makedirs(repo)
        _init_ergon_repo(repo)

        found = _scan_roots_for_ergon([self.root], max_depth=3)
        assert os.path.normcase(os.path.abspath(repo)) in {
            os.path.normcase(os.path.abspath(p)) for p in found
        }

    def test_scan_finds_nested_repo_within_default_depth(self):
        # Covers a nested workspace path (one extra level).
        nested = os.path.join(self.root, "sample-project", "sample-project")
        os.makedirs(nested)
        _init_ergon_repo(nested)

        found = _scan_roots_for_ergon([self.root], max_depth=3)
        assert os.path.normcase(os.path.abspath(nested)) in {
            os.path.normcase(os.path.abspath(p)) for p in found
        }

    def test_scan_does_not_recurse_into_a_found_repo(self):
        """A repo's own subtree (e.g. docs/cycles/) must not itself be
        scanned for a second, nested .ergon/ hit."""
        repo = os.path.join(self.root, "somerepo")
        os.makedirs(os.path.join(repo, "docs", "cycles"))
        _init_ergon_repo(repo)
        # A stray .ergon-looking dir under the repo's own tree must be ignored.
        os.makedirs(os.path.join(repo, "docs", "cycles", ".ergon", "log"))

        found = _scan_roots_for_ergon([self.root], max_depth=3)
        assert len(found) == 1

    def test_run_discovers_repo_via_scan_alone_no_registry(self, capsys):
        """End-to-end: pinax overview, roots pointed at a fixture, ZERO
        registry entries -- the repo still appears."""
        hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(hub, ".ergon", "log"), 0, "2026-07-03T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        repo = os.path.join(self.root, "scannedrepo")
        os.makedirs(repo)
        _seed_repo_with_items(repo)

        overview_run(hub, as_json=True, roots=[self.root])
        out = capsys.readouterr().out
        payload = json.loads(out)
        by_id = {r["id"]: r for r in payload["repos"]}
        assert "scannedrepo" in by_id
        assert by_id["scannedrepo"]["initialised"] is True
        assert by_id["scannedrepo"]["total_items"] == 4

    def test_scan_is_bounded_depth(self):
        too_deep = os.path.join(self.root, "a", "b", "c", "d", "toodeep")
        os.makedirs(too_deep)
        _init_ergon_repo(too_deep)

        found = _scan_roots_for_ergon([self.root], max_depth=1)
        assert found == []

    def test_scan_deterministic_repeat_calls(self):
        for name in ["zzz_repo", "aaa_repo", "mmm_repo"]:
            repo = os.path.join(self.root, name)
            os.makedirs(repo)
            _init_ergon_repo(repo)

        first = _scan_roots_for_ergon([self.root], max_depth=3)
        second = _scan_roots_for_ergon([self.root], max_depth=3)
        assert first == second

    def test_missing_root_is_skipped_not_fatal(self):
        ghost_root = os.path.join(self.roots_parent, "does-not-exist")
        found = _scan_roots_for_ergon([ghost_root], max_depth=3)
        assert found == []


@requires_git
class TestGitWorktreeDedup:
    """Linked worktrees for one repository produce one portfolio entry."""

    def setup_method(self) -> None:
        self.parent = tempfile.mkdtemp()
        self.roots_dir = os.path.join(self.parent, "src")
        os.makedirs(self.roots_dir)

        self.main_repo = os.path.join(self.roots_dir, "mainrepo")
        os.makedirs(self.main_repo)
        _git(self.main_repo, "init", "-b", "main")
        _git(self.main_repo, "config", "user.email", "test@pinax.test")
        _git(self.main_repo, "config", "user.name", "Pinax Test")
        _init_ergon_repo(self.main_repo)
        with open(os.path.join(self.main_repo, "README.md"), "w") as fh:
            fh.write("seed\n")
        _git(self.main_repo, "add", "-A")
        _git(self.main_repo, "commit", "-m", "seed")

    # the same scanned root, as can occur with linked worktrees
        self.worktree_path = os.path.join(self.roots_dir, "mainrepo-wt")
        _git(self.main_repo, "worktree", "add", "-b", "feature-branch",
             self.worktree_path)

    def test_two_worktrees_fold_to_one_entry(self):
        found = _scan_roots_for_ergon([self.roots_dir], max_depth=3)
        assert len(found) == 2  # both are found by the raw scan...

        deduped = _dedupe_worktrees(found)
        assert len(deduped) == 1  # ...but fold to one after worktree dedup

    def test_primary_worktree_wins_over_linked(self):
        found = _scan_roots_for_ergon([self.roots_dir], max_depth=3)
        deduped = _dedupe_worktrees(found)
        winner = os.path.normcase(os.path.abspath(deduped[0]))
        assert winner == os.path.normcase(os.path.abspath(self.main_repo))

    def test_run_overview_counts_worktree_pair_once(self, capsys):
        hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(hub, ".ergon", "log"), 0, "2026-07-03T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        overview_run(hub, as_json=True, roots=[self.roots_dir])
        out = capsys.readouterr().out
        payload = json.loads(out)
        ids = [r["id"] for r in payload["repos"]]
        # Only "mainrepo" appears once -- never "mainrepo" AND "mainrepo-wt".
        assert ids.count("mainrepo") == 1
        assert "mainrepo-wt" not in ids


@requires_git
class TestBareRepoSkipped:
    """Bare repositories are excluded from discovery."""

    def setup_method(self) -> None:
        self.parent = tempfile.mkdtemp()
        self.roots_dir = os.path.join(self.parent, "src")
        os.makedirs(self.roots_dir)

        # A bare repo with a stray .ergon/ physically present under it (a
        # this could only happen by accident, e.g. a stray script).  Must be
        # excluded regardless.
        self.bare_repo = os.path.join(self.roots_dir, "bare.git")
        _git(self.parent, "init", "--bare", self.bare_repo)
        os.makedirs(os.path.join(self.bare_repo, ".ergon", "log"))

        self.normal_repo = os.path.join(self.roots_dir, "normalrepo")
        os.makedirs(self.normal_repo)
        _init_ergon_repo(self.normal_repo)

    def test_is_bare_repo_detects_bare(self):
        assert _is_bare_repo(self.bare_repo) is True
        assert _is_bare_repo(self.normal_repo) is False

    def test_dedupe_worktrees_drops_bare_repo(self):
        found = _scan_roots_for_ergon([self.roots_dir], max_depth=3)
        assert any(
            os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(self.bare_repo))
            for p in found
        )  # the raw scan finds it (it has a .ergon/ dir)...

        deduped = _dedupe_worktrees(found)
        deduped_norm = {os.path.normcase(os.path.abspath(p)) for p in deduped}
        assert os.path.normcase(os.path.abspath(self.bare_repo)) not in deduped_norm
        assert os.path.normcase(os.path.abspath(self.normal_repo)) in deduped_norm

    def test_run_overview_excludes_bare_repo(self, capsys):
        hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(hub, ".ergon", "log"), 0, "2026-07-03T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        overview_run(hub, as_json=True, roots=[self.roots_dir])
        out = capsys.readouterr().out
        payload = json.loads(out)
        ids = [r["id"] for r in payload["repos"]]
        assert "bare.git" not in ids
        assert "normalrepo" in ids


class TestRegistryOverrideBackwardCompat:
    """Registry entries supplement scanning without duplicating repositories."""

    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(self.hub, ".ergon", "log"), 0, "2026-07-03T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        self.roots_parent = tempfile.mkdtemp()
        self.root = os.path.join(self.roots_parent, "src")
        os.makedirs(self.root)

    def test_registry_only_repo_outside_scanned_roots_still_appears(self, capsys):
        """The exact 'existing seeded registry entries from 2026-07-03 must
        not break' scenario: a repo registered the OLD way, living OUTSIDE
        any scanned root, must still show up (registry as override)."""
        outside_repo = tempfile.mkdtemp()  # NOT under self.root
        _seed_repo_with_items(outside_repo)
        run_add(self.hub, repo_id="legacyregistered", path=outside_repo,
                actor="operator@example.test", as_json=True)
        capsys.readouterr()  # drain run_add's own JSON output

        overview_run(self.hub, as_json=True, roots=[self.root])
        out = capsys.readouterr().out
        payload = json.loads(out)
        by_id = {r["id"]: r for r in payload["repos"]}
        assert "legacyregistered" in by_id
        assert by_id["legacyregistered"]["initialised"] is True

    def test_registry_entry_and_scan_hit_same_path_no_double_count(self, capsys):
        """A repo BOTH registered explicitly AND discoverable by scan (same
        absolute path) appears exactly once -- registry id wins."""
        repo = os.path.join(self.root, "overlaprepo")
        os.makedirs(repo)
        _seed_repo_with_items(repo)
        run_add(self.hub, repo_id="myoverlap", path=repo,
                actor="operator@example.test", as_json=True)
        capsys.readouterr()  # drain run_add's own JSON output

        overview_run(self.hub, as_json=True, roots=[self.root])
        out = capsys.readouterr().out
        payload = json.loads(out)
        ids = [r["id"] for r in payload["repos"]]
        # Exactly one entry for this path: the registry's chosen id.
        assert ids.count("myoverlap") == 1
        assert "overlaprepo" not in ids

    def test_discover_repos_dedupes_registry_path_against_scan_hit(self):
        """Unit-level: _discover_repos itself performs the path dedup
        between stage-2 (registry) and stage-3 (scan), not just end-to-end."""
        repo = os.path.join(self.root, "overlaprepo2")
        os.makedirs(repo)
        _init_ergon_repo(repo)
        registry = {"pinnedid": {"path": repo, "added_by": "x", "added_at": "y"}}

        repos = _discover_repos(self.hub, registry, roots=[self.root], max_depth=3)
        ids = [r[0] for r in repos]
        assert ids.count("pinnedid") == 1
        assert "overlaprepo2" not in ids


class TestOverviewDeterministicWithScan:
    """Identical roots and filesystem state produce identical portfolio output."""

    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(self.hub, ".ergon", "log"), 0, "2026-07-03T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        self.roots_parent = tempfile.mkdtemp()
        self.root = os.path.join(self.roots_parent, "src")
        os.makedirs(self.root)
        for name in ["repoc", "repoa", "repob"]:
            repo = os.path.join(self.root, name)
            os.makedirs(repo)
            _seed_repo_with_items(repo)

    def test_regenerate_twice_byte_identical(self, capsys):
        overview_run(self.hub, as_json=False, roots=[self.root])
        first = capsys.readouterr().out
        overview_run(self.hub, as_json=False, roots=[self.root])
        second = capsys.readouterr().out
        assert first == second
        assert first != ""

    def test_json_regenerate_twice_byte_identical(self, capsys):
        overview_run(self.hub, as_json=True, roots=[self.root])
        first = capsys.readouterr().out
        overview_run(self.hub, as_json=True, roots=[self.root])
        second = capsys.readouterr().out
        assert first == second


# ---------------------------------------------------------------------------
# scan of both would otherwise double-count every repo underneath.
# ---------------------------------------------------------------------------

class TestPhysicalPathDedup:
    """A REAL directory junction (Windows) / symlink (POSIX) aliasing the
    same on-disk repo must collapse to ONE entry -- same "prove on the
    Test path" discipline as TestGitWorktreeDedup."""

    def setup_method(self) -> None:
        self.parent = tempfile.mkdtemp()
        self.roots_dir = os.path.join(self.parent, "roots")
        os.makedirs(self.roots_dir)

        # The REAL, physical location of the repo.
        self.real_root = os.path.join(self.parent, "real_root")
        os.makedirs(self.real_root)
        self.real_repo = os.path.join(self.real_root, "somerepo")
        os.makedirs(self.real_repo)
        _init_ergon_repo(self.real_repo)

        # An alias root pointing at real_root -- so the SAME on-disk repo is
        # reachable at two different path strings:
        #   <real_root>/somerepo    (direct)
        #   <alias_root>/somerepo   (through the junction/symlink)
        self.alias_root = os.path.join(self.roots_dir, "alias_root")
        if not _make_dir_alias(self.real_root, self.alias_root):
            pytest.skip("could not create a directory junction/symlink in this environment")
        self.via_alias = os.path.join(self.alias_root, "somerepo")

    def test_physical_path_resolves_alias_to_real_target(self):
        assert _physical_path(self.real_repo) == _physical_path(self.via_alias)

    def test_dedupe_physical_path_collapses_alias_pair(self):
        deduped = _dedupe_physical_path([self.real_repo, self.via_alias])
        assert len(deduped) == 1

    def test_deterministic_survivor_alphabetically_first_scanned_path(self):
        expected = sorted(
            [self.real_repo, self.via_alias],
            key=lambda p: os.path.normcase(os.path.abspath(p)),
        )[0]

        forward = _dedupe_physical_path([self.real_repo, self.via_alias])
        reverse = _dedupe_physical_path([self.via_alias, self.real_repo])
        assert forward == reverse
        assert os.path.normcase(os.path.abspath(forward[0])) == os.path.normcase(
            os.path.abspath(expected)
        )

    def test_scan_of_real_and_alias_root_discovers_repo_once(self):
        """Scan a temporary root through two paths that resolve to one location."""
        found = _scan_roots_for_ergon([self.real_root, self.alias_root], max_depth=3)
        assert len(found) == 2  # the raw scan finds it under both paths...

        deduped = _dedupe_physical_path(_dedupe_worktrees(found))
        assert len(deduped) == 1  # ...but physical-path dedup collapses it to one

    def test_run_overview_counts_alias_pair_once(self, capsys):
        hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(hub, ".ergon", "log"), 0, "2026-07-06T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        overview_run(hub, as_json=True, roots=[self.real_root, self.alias_root])
        out = capsys.readouterr().out
        payload = json.loads(out)
        ids = [r["id"] for r in payload["repos"]]
        assert ids.count("somerepo") == 1

    def test_discover_repos_dedupes_registry_alias_against_scan_hit(self):
        """A repo registered via its ALIAS path, also discoverable by
        scanning the REAL root, must not double count -- registry-vs-scan
        through a physical alias, not just a literal string match."""
        hub = tempfile.mkdtemp()
        registry = {"aliasedid": {"path": self.via_alias, "added_by": "x", "added_at": "y"}}

        repos = _discover_repos(hub, registry, roots=[self.real_root], max_depth=3)
        ids = [r[0] for r in repos]
        assert ids.count("aliasedid") == 1
        assert "somerepo" not in ids

    def test_discover_repos_dedupes_hub_alias_against_scan_hit(self):
        """The hub repo itself, reached via its alias path, still only
        counts once -- must not disturb the hub-always-first guarantee."""
        repos = _discover_repos(self.real_repo, {}, roots=[self.alias_root], max_depth=3)
        ids = [r[0] for r in repos]
        assert ids.count("somerepo") == 1
        # Hub is still first.
        assert repos[0][0] == "somerepo"


class TestPhysicalPathDedupDoesNotOvermerge:
    """Requirement: two genuinely distinct repos are never merged just
    because their paths look similar."""

    def setup_method(self) -> None:
        self.parent = tempfile.mkdtemp()
        self.root = os.path.join(self.parent, "src")
        os.makedirs(self.root)

    def test_similar_looking_distinct_paths_stay_distinct(self):
        repo_a = os.path.join(self.root, "pinax")
        repo_b = os.path.join(self.root, "pinax-2")
        os.makedirs(repo_a)
        os.makedirs(repo_b)
        _init_ergon_repo(repo_a)
        _init_ergon_repo(repo_b)

        found = _scan_roots_for_ergon([self.root], max_depth=3)
        deduped = _dedupe_physical_path(found)
        assert len(deduped) == 2

    def test_physical_path_differs_for_distinct_directories(self):
        repo_a = os.path.join(self.root, "pinax")
        repo_b = os.path.join(self.root, "pinax-copy")
        os.makedirs(repo_a)
        os.makedirs(repo_b)
        assert _physical_path(repo_a) != _physical_path(repo_b)


class TestPhysicalPathDedupDeterminism:
    """Same input set -> same single surviving entry, regardless of scan
    order; idempotent on repeat application."""

    def setup_method(self) -> None:
        self.parent = tempfile.mkdtemp()

    def test_dedupe_physical_path_idempotent(self):
        d = os.path.join(self.parent, "repo")
        os.makedirs(d)
        once = _dedupe_physical_path([d, d, d])
        assert once == [d]
        twice = _dedupe_physical_path(once)
        assert twice == once

    def test_dedupe_physical_path_order_independent(self):
        import itertools

        paths = []
        for name in ("aaa", "bbb", "ccc"):
            p = os.path.join(self.parent, name)
            os.makedirs(p)
            paths.append(p)

        results = set()
        for perm in itertools.permutations(paths):
            deduped = _dedupe_physical_path(list(perm))
            results.add(tuple(os.path.normcase(os.path.abspath(p)) for p in deduped))
        assert len(results) == 1
