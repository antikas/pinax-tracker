"""Tests for `pinax overview --remote`.

The remote portfolio fold: `overview --remote` folds over repo REMOTES, not
local clones.  The manifest is the hub log's url-bearing registry entries
(`pinax registry add --id <id> --url <url>` — same registry event, one new
optional payload field, SSOT); each remote's PUSHED tip is fetched to a
per-run temp scratch (git transport; GitHub contents-API fallback) and folded
through the ONE existing fold.

Covers:
1.  End-to-end over TWO real git remotes (real bare repos, real pushes, real
    `git ls-remote`/`git clone` subprocesses — the test_merge_safety.py
    "prove on the Test path" discipline, incl. its
    PYTHONPATH-augmented env helper): both remotes summarised, correct
    counts, tip shas match the remotes' published tips, the hub itself NOT
    in the remote view.
2.  THE FRESHNESS CONTRACT, explicitly: committed-but-unpushed work is
    INVISIBLE to --remote (by design — git's publish contract); after the
    push it appears.
3.  Determinism: same remote state → byte-identical output, plain and
    --json, across repeated runs.
4.  --remote --markdown is rejected (exit 1, nothing written) — PORTFOLIO.md
    stays the committed LOCAL-fold projection.
5.  An unreachable remote is reported explicitly ("error" entry + rendered
    "unreachable" + Needs-attention), never silently dropped; exit stays 0.
6.  A pushed repo WITHOUT .ergon/log renders "not initialised"; an empty
    remote (nothing pushed) renders "(nothing pushed)".
7.  A dangling remote HEAD (bare repo whose HEAD points at an unpushed
    branch) falls back deterministically to the published branch.
8.  url-only registry entries are remote-manifest entries: the LOCAL
    overview skips them (no crash, not listed).
9.  The GitHub contents-API fallback, against a STUBBED http_get (the real
    API is never hit): URL parsing, happy path (identical fold + sha to the
    git transport shape), 404 → not initialised, 403 → clear rate-limit
    error, fallback wiring (github-URL-only, non-github re-raises the git
    error).
"""

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
from pinax.commands.overview import run as overview_run, _remote_manifest
from pinax.remote import (
    RemoteFetchError,
    _pick_remote_branch,
    fetch_remote_events,
    fetch_remote_github_api,
    parse_github_url,
)


_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env(**overrides: str) -> dict:
    """PYTHONPATH-augmented env for pinax/git subprocesses (the
    test_merge_safety.py helper pattern)."""
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    env.update(overrides)
    return env


def _git(cwd: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, env=_build_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result.stdout.strip()


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(not _git_available(), reason="git not available on PATH")


def _pinax(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=cwd, capture_output=True, text=True, env=_build_env(),
    )


def _pinax_ok(cwd: str, *args: str) -> subprocess.CompletedProcess:
    r = _pinax(cwd, *args)
    if r.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {cwd}:\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    return r


def _fwd(path: str) -> str:
    """Forward-slash a path for use as a git remote URL string."""
    return path.replace("\\", "/")


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str, payload: dict) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _make_project_repo(parent: str, name: str, titles: list[str]) -> str:
    """A real git repo with an initialised .ergon and `titles` items, committed."""
    repo = os.path.join(parent, name)
    os.makedirs(repo)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@pinax.test")
    _git(repo, "config", "user.name", "Pinax Test")
    _pinax_ok(repo, "init", "--actor", "operator@example.test")
    for title in titles:
        _pinax_ok(repo, "add", "--title", title, "--actor", "operator@example.test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _make_bare_remote(parent: str, name: str, from_repo: str | None = None,
                      branch: str = "main") -> str:
    """A real bare remote; optionally push `from_repo`'s branch into it.

    NOTE: `git init --bare` leaves HEAD at the init default (usually
    refs/heads/master) — pushing `main` leaves HEAD dangling, which is
    exactly the misconfigured-remote case the deterministic branch fallback
    exists for. Tests that require a configured remote set HEAD after the
    push; test_dangling_head_falls_back leaves it dangling on purpose.
    """
    bare = os.path.join(parent, name)
    _git(parent, "init", "--bare", bare)
    if from_repo is not None:
        _git(from_repo, "push", _fwd(bare), branch)
        _git(bare, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    return bare


def _make_hub(parent: str) -> str:
    """A hub repo with an initialised .ergon (the manifest home)."""
    hub = os.path.join(parent, "hub")
    os.makedirs(hub)
    _git(hub, "init", "-b", "main")
    _git(hub, "config", "user.email", "test@pinax.test")
    _git(hub, "config", "user.name", "Pinax Test")
    _pinax_ok(hub, "init", "--actor", "operator@example.test")
    return hub


# ---------------------------------------------------------------------------
# End-to-end: real git remotes
# ---------------------------------------------------------------------------

@requires_git
class TestRemoteFoldRealGit:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.proj_a = _make_project_repo(self.tmp, "proja", ["Item A1", "Item A2"])
        self.proj_b = _make_project_repo(self.tmp, "projb", ["Item B1"])
        self.bare_a = _make_bare_remote(self.tmp, "remote-a.git", self.proj_a)
        self.bare_b = _make_bare_remote(self.tmp, "remote-b.git", self.proj_b)
        self.hub = _make_hub(self.tmp)
        _pinax_ok(self.hub, "registry", "add", "--id", "repoa",
                  "--url", _fwd(self.bare_a), "--actor", "operator@example.test")
        _pinax_ok(self.hub, "registry", "add", "--id", "repob",
                  "--url", _fwd(self.bare_b), "--actor", "operator@example.test")

    def _remote_json(self) -> dict:
        r = _pinax_ok(self.hub, "overview", "--remote", "--json")
        return json.loads(r.stdout)

    def test_two_remotes_folded_with_correct_counts_and_shas(self):
        payload = self._remote_json()
        by_id = {rep["id"]: rep for rep in payload["repos"]}
        assert set(by_id) == {"repoa", "repob"}, (
            "The remote view folds exactly the url-registered manifest — "
            f"got {sorted(by_id)}"
        )
        assert by_id["repoa"]["initialised"] is True
        assert by_id["repoa"]["total_items"] == 2
        assert by_id["repob"]["total_items"] == 1
        # The rendered sha is the remote's PUSHED tip (the per-repo stamp).
        assert by_id["repoa"]["sha"] == _git(self.bare_a, "rev-parse", "refs/heads/main")
        assert by_id["repob"]["sha"] == _git(self.bare_b, "rev-parse", "refs/heads/main")
        assert by_id["repoa"]["url"] == _fwd(self.bare_a)

    def test_hub_itself_not_in_remote_view(self):
        payload = self._remote_json()
        ids = [rep["id"] for rep in payload["repos"]]
        assert "hub" not in ids, (
            "Remote mode folds ONLY the pushed manifest — the hub's local "
            "log participates solely as the manifest source"
        )

    def test_unpushed_work_is_invisible_then_visible_after_push(self):
        """THE FRESHNESS CONTRACT: only what is PUSHED to the remote exists
        in this view — committed-but-unpushed work is invisible BY DESIGN
        (git's own publish contract), and appears exactly on push."""
        _pinax_ok(self.proj_a, "add", "--title", "Unpushed item",
                  "--actor", "operator@example.test")
        _git(self.proj_a, "add", "-A")
        _git(self.proj_a, "commit", "-m", "committed but NOT pushed")

        before = self._remote_json()
        by_id = {rep["id"]: rep for rep in before["repos"]}
        assert by_id["repoa"]["total_items"] == 2, (
            "Committed-but-unpushed work leaked into the remote fold — the "
            "freshness contract is 'pushed state only'"
        )

        _git(self.proj_a, "push", _fwd(self.bare_a), "main")
        after = self._remote_json()
        by_id = {rep["id"]: rep for rep in after["repos"]}
        assert by_id["repoa"]["total_items"] == 3, (
            "Pushed work must appear in the remote fold"
        )
        assert by_id["repoa"]["sha"] == _git(self.bare_a, "rev-parse", "refs/heads/main")

    def test_remote_render_deterministic_repeat_runs(self):
        """Same remote state → byte-identical render, plain and --json."""
        plain_1 = _pinax_ok(self.hub, "overview", "--remote").stdout
        plain_2 = _pinax_ok(self.hub, "overview", "--remote").stdout
        assert plain_1 == plain_2
        assert plain_1 != ""
        json_1 = _pinax_ok(self.hub, "overview", "--remote", "--json").stdout
        json_2 = _pinax_ok(self.hub, "overview", "--remote", "--json").stdout
        assert json_1 == json_2

    def test_plain_render_carries_url_and_pushed_sha(self):
        out = _pinax_ok(self.hub, "overview", "--remote").stdout
        tip_a = _git(self.bare_a, "rev-parse", "refs/heads/main")
        assert f"- remote: {_fwd(self.bare_a)} @ {tip_a}" in out, (
            "Each remote section must state exactly which pushed tip its "
            "numbers describe (the per-repo stamp discipline)"
        )

    def test_remote_markdown_rejected_and_writes_nothing(self):
        r = _pinax(self.hub, "overview", "--remote", "--markdown")
        assert r.returncode == 1
        assert "--markdown" in r.stderr
        assert not os.path.exists(os.path.join(self.hub, "PORTFOLIO.md")), (
            "A rejected --remote --markdown must not write PORTFOLIO.md"
        )

    def test_unreachable_remote_reported_not_dropped(self):
        ghost = _fwd(os.path.join(self.tmp, "does-not-exist.git"))
        _pinax_ok(self.hub, "registry", "add", "--id", "ghostrepo",
                  "--url", ghost, "--actor", "operator@example.test")
        r = _pinax_ok(self.hub, "overview", "--remote", "--json")
        payload = json.loads(r.stdout)
        by_id = {rep["id"]: rep for rep in payload["repos"]}
        assert "ghostrepo" in by_id, "an unreachable remote must still be reported"
        assert "error" in by_id["ghostrepo"]
        assert by_id["ghostrepo"]["url"] == ghost

        plain = _pinax_ok(self.hub, "overview", "--remote").stdout
        assert "unreachable" in plain
        assert f"- ghostrepo · unreachable remote: {ghost}" in plain, (
            "the unreachable remote must surface under Needs attention"
        )

    def test_remote_mode_never_writes_into_hub_tree(self):
        before = {}
        for root, _dirs, files in os.walk(self.hub):
            for f in files:
                p = os.path.join(root, f)
                before[p] = os.path.getmtime(p)
        _pinax_ok(self.hub, "overview", "--remote", "--json")
        after = {}
        for root, _dirs, files in os.walk(self.hub):
            for f in files:
                p = os.path.join(root, f)
                after[p] = os.path.getmtime(p)
        assert before == after, "overview --remote is a pure read of the hub tree"


@requires_git
class TestRemoteEdgeStates:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.hub = _make_hub(self.tmp)

    def test_pushed_repo_without_ergon_is_not_initialised(self):
        plain = os.path.join(self.tmp, "plainrepo")
        os.makedirs(plain)
        _git(plain, "init", "-b", "main")
        _git(plain, "config", "user.email", "test@pinax.test")
        _git(plain, "config", "user.name", "Pinax Test")
        with open(os.path.join(plain, "README.md"), "w") as fh:
            fh.write("no ergon here\n")
        _git(plain, "add", "-A")
        _git(plain, "commit", "-m", "seed")
        bare = _make_bare_remote(self.tmp, "plain.git", plain)

        _pinax_ok(self.hub, "registry", "add", "--id", "plainrepo",
                  "--url", _fwd(bare), "--actor", "operator@example.test")
        r = _pinax_ok(self.hub, "overview", "--remote", "--json")
        by_id = {rep["id"]: rep for rep in json.loads(r.stdout)["repos"]}
        assert by_id["plainrepo"]["initialised"] is False
        assert by_id["plainrepo"]["sha"] == _git(bare, "rev-parse", "refs/heads/main")

    def test_empty_remote_renders_nothing_pushed(self):
        bare = _make_bare_remote(self.tmp, "empty.git")  # no push at all
        _pinax_ok(self.hub, "registry", "add", "--id", "emptyrepo",
                  "--url", _fwd(bare), "--actor", "operator@example.test")
        r = _pinax_ok(self.hub, "overview", "--remote", "--json")
        by_id = {rep["id"]: rep for rep in json.loads(r.stdout)["repos"]}
        assert by_id["emptyrepo"]["initialised"] is False
        assert by_id["emptyrepo"]["sha"] is None
        plain = _pinax_ok(self.hub, "overview", "--remote").stdout
        assert "(nothing pushed)" in plain

    def test_dangling_remote_head_falls_back_to_published_branch(self):
        """A bare remote whose HEAD points at an unpushed branch (the raw
        `git init --bare` + push-main shape) must still fold: the documented
        deterministic fallback picks the published branch."""
        proj = _make_project_repo(self.tmp, "danglingproj", ["Dangling item"])
        bare = os.path.join(self.tmp, "dangling.git")
        _git(self.tmp, "init", "--bare", bare)
        _git(proj, "push", _fwd(bare), "main")
        # HEAD deliberately left at the init default (dangling if != main).

        _pinax_ok(self.hub, "registry", "add", "--id", "danglingrepo",
                  "--url", _fwd(bare), "--actor", "operator@example.test")
        r = _pinax_ok(self.hub, "overview", "--remote", "--json")
        by_id = {rep["id"]: rep for rep in json.loads(r.stdout)["repos"]}
        assert by_id["danglingrepo"]["initialised"] is True
        assert by_id["danglingrepo"]["total_items"] == 1
        assert by_id["danglingrepo"]["sha"] == _git(bare, "rev-parse", "refs/heads/main")


# ---------------------------------------------------------------------------
# The manifest boundary: url-only entries vs the LOCAL overview
# ---------------------------------------------------------------------------

class TestUrlOnlyEntriesAndLocalOverview:
    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        log_dir = os.path.join(self.hub, ".ergon", "log")
        os.makedirs(log_dir, exist_ok=True)
        _append(log_dir, 0, "2026-07-05T00:00:00Z", "operator@example.test",
                "ergon.created", {"repo": "hub"})
        _append(log_dir, 1, "2026-07-05T00:00:01Z", "operator@example.test",
                "registry.repo_added",
                {"repo_id": "remoteonly", "url": "https://github.com/x/y.git"})

    def test_local_overview_skips_url_only_entry(self, capsys):
        overview_run(self.hub, as_json=True, roots=[])
        payload = json.loads(capsys.readouterr().out)
        ids = [rep["id"] for rep in payload["repos"]]
        assert "remoteonly" not in ids, (
            "a url-only registry entry is a remote-manifest entry — the "
            "local overview has nothing local to fold for it"
        )

    def test_remote_manifest_lists_url_entries_sorted(self):
        registry = {
            "zzz": {"url": "u-z"},
            "aaa": {"url": "u-a"},
            "localonly": {"path": "/some/where"},
        }
        assert _remote_manifest(registry) == [("aaa", "u-a"), ("zzz", "u-z")]

    def test_remote_mode_with_empty_manifest_renders_empty_view(self, capsys):
        hub2 = tempfile.mkdtemp()
        log_dir = os.path.join(hub2, ".ergon", "log")
        os.makedirs(log_dir, exist_ok=True)
        _append(log_dir, 0, "2026-07-05T00:00:00Z", "operator@example.test",
                "ergon.created", {"repo": "hub2"})
        overview_run(hub2, as_json=True, remote=True)
        captured = capsys.readouterr()
        assert json.loads(captured.out) == {"repos": []}
        assert "no remotes registered" in captured.err


# ---------------------------------------------------------------------------
# GitHub contents-API fallback (stubbed http_get — the real API is never hit)
# ---------------------------------------------------------------------------

def _shard_bytes(events: list[dict]) -> bytes:
    return b"".join(
        (json.dumps(e, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
        for e in events
    )


def _fake_github(sha: str, shards: dict[str, list[dict]],
                 default_branch: str = "main"):
    """Build an http_get stub serving a fake GitHub repo `o/r` at tip `sha`
    whose .ergon/log holds `shards` ({filename: [events]})."""
    base = "https://api.github.com/repos/o/r"
    listing = [
        {"name": name,
         "download_url": f"https://raw.fake/{name}"}
        for name in sorted(shards)
    ]

    def http_get(url: str) -> tuple[int, bytes]:
        if url == base:
            return 200, json.dumps({"default_branch": default_branch}).encode()
        if url == f"{base}/commits/{default_branch}":
            return 200, json.dumps({"sha": sha}).encode()
        if url == f"{base}/contents/.ergon/log?ref={sha}":
            return 200, json.dumps(listing).encode()
        for name in shards:
            if url == f"https://raw.fake/{name}":
                return 200, _shard_bytes(shards[name])
        return 404, b"{}"

    return http_get


class TestGithubUrlParsing:
    def test_https_forms(self):
        assert parse_github_url("https://github.com/owner/repo") == ("owner", "repo")
        assert parse_github_url("https://github.com/owner/repo.git") == ("owner", "repo")
        assert parse_github_url("https://github.com/owner/repo/") == ("owner", "repo")

    def test_ssh_forms(self):
        assert parse_github_url("git@github.com:owner/repo.git") == ("owner", "repo")
        assert parse_github_url("ssh://git@github.com/owner/repo") == ("owner", "repo")

    def test_non_github_is_none(self):
        assert parse_github_url("https://gitlab.com/owner/repo") is None
        assert parse_github_url("/workspace/some/local/path.git") is None
        assert parse_github_url("git@bitbucket.org:o/r.git") is None


class TestGithubApiFallback:
    def _events(self) -> list[dict]:
        e1 = mint_event(seq=0, ts="2026-07-05T00:00:00Z", actor="operator@example.test",
                        etype="ergon.created", payload={"repo": "api"})
        e2 = mint_event(seq=1, ts="2026-07-05T00:00:01Z", actor="operator@example.test",
                        etype="item.created",
                        payload={"item_id": "pnx-api1", "title": "Api item",
                                 "prefix": "pnx"})
        return [e1, e2]

    def test_happy_path_folds_events_and_carries_sha(self):
        sha = "a" * 40
        http_get = _fake_github(sha, {"operator-test.jsonl": self._events()})
        fetched = fetch_remote_github_api("https://github.com/o/r.git", http_get=http_get)
        assert fetched["sha"] == sha
        assert fetched["has_log"] is True
        item_ids = [e["payload"].get("item_id") for e in fetched["events"]
                    if e["type"] == "item.created"]
        assert item_ids == ["pnx-api1"]

    def test_empty_log_dir_listing_is_not_initialised(self):
        sha = "b" * 40
        http_get = _fake_github(sha, {})  # listing serves an EMPTY directory
        fetched = fetch_remote_github_api("https://github.com/o/r", http_get=http_get)
        assert fetched["sha"] == sha
        assert fetched["has_log"] is False
        assert fetched["events"] == []

    def test_404_listing_means_not_initialised(self):
        base = "https://api.github.com/repos/o/r"
        sha = "c" * 40

        def http_get(url: str) -> tuple[int, bytes]:
            if url == base:
                return 200, json.dumps({"default_branch": "main"}).encode()
            if url == f"{base}/commits/main":
                return 200, json.dumps({"sha": sha}).encode()
            return 404, b"{}"

        fetched = fetch_remote_github_api("https://github.com/o/r", http_get=http_get)
        assert fetched == {"sha": sha, "has_log": False, "events": []}

    def test_rate_limit_is_a_clear_error(self):
        def http_get(url: str) -> tuple[int, bytes]:
            return 403, b"{}"

        with pytest.raises(RemoteFetchError, match="rate limit"):
            fetch_remote_github_api("https://github.com/o/r", http_get=http_get)

    def test_non_github_url_raises(self):
        with pytest.raises(RemoteFetchError, match="not a GitHub URL"):
            fetch_remote_github_api("https://gitlab.com/o/r", http_get=lambda u: (200, b"{}"))


class TestFallbackWiring:
    def _failing_git(self, url: str, scratch_dir: str) -> dict:
        raise RemoteFetchError("simulated git transport failure")

    def test_github_url_falls_back_to_api(self):
        sha = "d" * 40
        http_get = _fake_github(sha, {})
        fetched = fetch_remote_events(
            "https://github.com/o/r.git",
            scratch_dir=tempfile.mkdtemp(),
            http_get=http_get,
            git_fetch=self._failing_git,
        )
        assert fetched["sha"] == sha

    def test_non_github_url_reraises_git_error(self):
        with pytest.raises(RemoteFetchError, match="simulated git transport failure"):
            fetch_remote_events(
                "https://gitlab.com/o/r.git",
                scratch_dir=tempfile.mkdtemp(),
                http_get=lambda u: (200, b"{}"),
                git_fetch=self._failing_git,
            )

    def test_both_transports_failing_reports_both(self):
        def http_get(url: str) -> tuple[int, bytes]:
            return 403, b"{}"

        with pytest.raises(RemoteFetchError, match="git transport failed.*fallback failed"):
            fetch_remote_events(
                "https://github.com/o/r.git",
                scratch_dir=tempfile.mkdtemp(),
                http_get=http_get,
                git_fetch=self._failing_git,
            )


class TestPickRemoteBranch:
    def test_symref_wins_when_published(self):
        heads = {"refs/heads/dev": "s1", "refs/heads/main": "s2"}
        assert _pick_remote_branch("refs/heads/dev", heads) == "refs/heads/dev"

    def test_dangling_symref_falls_back_main_master_alpha(self):
        assert _pick_remote_branch(
            "refs/heads/gone", {"refs/heads/main": "s", "refs/heads/zzz": "s"}
        ) == "refs/heads/main"
        assert _pick_remote_branch(
            "refs/heads/gone", {"refs/heads/master": "s", "refs/heads/zzz": "s"}
        ) == "refs/heads/master"
        assert _pick_remote_branch(
            "refs/heads/gone", {"refs/heads/bbb": "s", "refs/heads/aaa": "s"}
        ) == "refs/heads/aaa"

    def test_empty_remote_is_none(self):
        assert _pick_remote_branch("refs/heads/main", {}) is None
