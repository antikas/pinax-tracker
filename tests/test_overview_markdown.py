"""Portfolio Markdown rendering tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep

from pinax.append import append_event
from pinax.event import mint_event
from pinax.commands.overview import run as overview_run, _repo_head_sha
from pinax.commands.registry_cmd import run_add
from pinax.projection import render_overview, render_overview_markdown


_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_env() -> dict:
    """
    Environment with THIS worktree's pinax package first on PYTHONPATH.

    Load-bearing for TestPostCommitHook: the editable pip install may point
    at a DIFFERENT pinax checkout (e.g. the non-worktree `pinax` repo this
    linked branch is a linked worktree of) -- `python -m pinax` run with
    cwd inside a bare temp dir and no PYTHONPATH override would silently
    exercise that other checkout's code, not this branch's changes. Every
    subprocess that (transitively, via a git hook) invokes `python -m
    pinax` in these tests must inherit this env. Same pattern as
    tests/test_merge_safety.py's `_build_env`.
    """
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


def _git(repo_root: str, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
        env=env if env is not None else _build_env(),
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


def _append(log_dir: str, seq: int, ts: str, actor: str, etype: str, payload: dict) -> dict:
    event = mint_event(seq=seq, ts=ts, actor=actor, etype=etype, payload=payload)
    append_event(log_dir, event, actor=actor)
    return event


def _seed_repo_with_items(repo_dir: str) -> None:
    log_dir = os.path.join(repo_dir, ".ergon", "log")
    os.makedirs(log_dir, exist_ok=True)
    actor = "operator@example.test"
    _append(log_dir, 0, "2026-07-04T00:00:00Z", actor, "ergon.created", {"repo": "seed"})
    _append(log_dir, 1, "2026-07-04T00:00:01Z", actor, "phase.opened", {"phase": "p1"})
    _append(log_dir, 2, "2026-07-04T00:00:02Z", actor, "item.created",
            {"item_id": "pnx-done1", "title": "Done item", "prefix": "p1"})
    _append(log_dir, 3, "2026-07-04T00:00:03Z", actor, "item.completed",
            {"item_id": "pnx-done1", "briefing": "shipped"})


# ---------------------------------------------------------------------------
# 1-2: render_overview_markdown — pure renderer tests
# ---------------------------------------------------------------------------

class TestRenderOverviewMarkdownPure:
    def _reports(self) -> list[dict]:
        return [
            {"id": "zzz", "initialised": False},
            {"id": "aaa", "initialised": True, "total_items": 1, "by_status": {"queued": 1},
             "next": {"id": "aaa-1", "title": "T"}, "parked": [], "blocked": []},
        ]

    def _stamp(self) -> dict:
        return {
            "generated_at": "2026-07-04T12:00:00Z",
            "shas": {"zzz": "deadbeef" * 5, "aaa": None},
        }

    def test_deterministic_same_input_same_output(self):
        reports, stamp = self._reports(), self._stamp()
        first = render_overview_markdown(reports, stamp)
        second = render_overview_markdown(reports, stamp)
        assert first == second

    def test_sorted_by_repo_id_in_body_and_shas(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert out.index("## aaa") < out.index("## zzz")
        assert out.index("- aaa:") < out.index("- zzz:")

    def test_stamp_footer_grammar(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert "_Generated: 2026-07-04T12:00:00Z_" in out
        assert "Source SHAs:" in out
        assert "---" in out

    def test_none_sha_renders_no_git_explicitly_never_dropped(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert "- aaa: (no git)" in out

    def test_real_sha_rendered_verbatim(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert f"- zzz: {'deadbeef' * 5}" in out

    def test_do_not_hand_edit_notice_present(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert "Do not hand-edit" in out
        assert "hooks/post-commit" in out

    def test_title_is_pinax_portfolio(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert out.startswith("# Pinax Portfolio\n")

    def test_needs_attention_section_present(self):
        out = render_overview_markdown(self._reports(), self._stamp())
        assert "## Needs attention (cross-repo)" in out

    def test_shared_body_content_with_render_overview(self):
        """SSOT: the per-repo body is the same text in both renderers
        (shared _render_repo_sections) -- the live view and the committed
        file cannot drift textually apart."""
        reports = self._reports()
        plain = render_overview(reports)
        md = render_overview_markdown(reports, self._stamp())
        assert "- status: ok · 1 items · queued=1" in plain
        assert "- status: ok · 1 items · queued=1" in md
        assert "- next: aaa-1  T" in plain
        assert "- next: aaa-1  T" in md
        assert "not initialised (no .ergon/log" in plain
        assert "not initialised (no .ergon/log" in md


# ---------------------------------------------------------------------------
# 3: CLI --markdown wiring
# ---------------------------------------------------------------------------

class TestOverviewMarkdownCLI:
    def setup_method(self) -> None:
        self.hub = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.hub, ".ergon", "log"), exist_ok=True)
        _append(os.path.join(self.hub, ".ergon", "log"), 0, "2026-07-04T00:00:00Z",
                "operator@example.test", "ergon.created", {"repo": "hub"})

        self.registered = tempfile.mkdtemp()
        _seed_repo_with_items(self.registered)

        run_add(self.hub, repo_id="registeredrepo", path=self.registered,
                actor="operator@example.test", as_json=True)

    def test_markdown_flag_writes_portfolio_file(self, capsys):
        capsys.readouterr()  # drain run_add's own output

        overview_run(self.hub, as_markdown=True, roots=[])
        out = capsys.readouterr().out
        assert "wrote PORTFOLIO.md" in out

        portfolio_path = os.path.join(self.hub, "PORTFOLIO.md")
        assert os.path.isfile(portfolio_path)
        with open(portfolio_path, encoding="utf-8") as fh:
            content = fh.read()
        assert "# Pinax Portfolio" in content
        assert "registeredrepo" in content
        assert "Source SHAs:" in content

    def test_markdown_never_writes_into_discovered_repo(self, capsys):
        capsys.readouterr()

        before = {}
        for root, _dirs, files in os.walk(self.registered):
            for f in files:
                p = os.path.join(root, f)
                before[p] = os.path.getmtime(p)

        overview_run(self.hub, as_markdown=True, roots=[])
        capsys.readouterr()

        after = {}
        for root, _dirs, files in os.walk(self.registered):
            for f in files:
                p = os.path.join(root, f)
                after[p] = os.path.getmtime(p)

        assert before == after, "overview --markdown must never write into a discovered repo's tree"
        assert not os.path.isfile(os.path.join(self.registered, "PORTFOLIO.md"))
        assert os.path.isfile(os.path.join(self.hub, "PORTFOLIO.md"))

    def test_json_mode_does_not_write_portfolio_file(self, capsys):
        capsys.readouterr()
        overview_run(self.hub, as_json=True, roots=[])
        capsys.readouterr()
        assert not os.path.isfile(os.path.join(self.hub, "PORTFOLIO.md"))

    def test_plain_mode_does_not_write_portfolio_file(self, capsys):
        capsys.readouterr()
        overview_run(self.hub, as_json=False, as_markdown=False, roots=[])
        capsys.readouterr()
        assert not os.path.isfile(os.path.join(self.hub, "PORTFOLIO.md"))

    def test_json_wins_when_both_flags_passed(self, capsys):
        capsys.readouterr()
        overview_run(self.hub, as_json=True, as_markdown=True, roots=[])
        out = capsys.readouterr().out
        payload = json.loads(out)  # fails if the markdown branch ran instead
        assert "repos" in payload
        assert not os.path.isfile(os.path.join(self.hub, "PORTFOLIO.md"))

    def test_markdown_regenerate_twice_body_identical(self, capsys):
        capsys.readouterr()

        overview_run(self.hub, as_markdown=True, roots=[])
        capsys.readouterr()
        with open(os.path.join(self.hub, "PORTFOLIO.md"), encoding="utf-8") as fh:
            first = fh.read()

        overview_run(self.hub, as_markdown=True, roots=[])
        capsys.readouterr()
        with open(os.path.join(self.hub, "PORTFOLIO.md"), encoding="utf-8") as fh:
            second = fh.read()

        # The stamp's generated-at line may legitimately differ across two
        # wall-clock reads; the body above the footer must be byte-identical
        # for an unchanged log state.
        first_body = first.split("---\n", 1)[0]
        second_body = second.split("---\n", 1)[0]
        assert first_body == second_body


# ---------------------------------------------------------------------------
# 4: _repo_head_sha
# ---------------------------------------------------------------------------

@requires_git
class TestRepoHeadSha:
    def test_real_git_repo_returns_full_sha(self):
        repo = tempfile.mkdtemp()
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.email", "test@pinax.test")
        _git(repo, "config", "user.name", "Pinax Test")
        with open(os.path.join(repo, "f.txt"), "w") as fh:
            fh.write("x\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "seed")

        sha = _repo_head_sha(repo)
        assert sha is not None
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_non_git_dir_returns_none(self):
        d = tempfile.mkdtemp()
        assert _repo_head_sha(d) is None


# ---------------------------------------------------------------------------
# 5: hooks/post-commit — real git repo, real commit, real hook file
# ---------------------------------------------------------------------------

_HOOK_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "post-commit",
)


@requires_git
class TestPostCommitHook:
    """Exercise the shipped hooks/post-commit script in a real Git repository.

    The test performs a real commit and installs the hook file into the
    repository hook directory; it does not simulate the hook in process.
    """

    def setup_method(self) -> None:
        self.repo = tempfile.mkdtemp()
        _git(self.repo, "init", "-b", "main")
        _git(self.repo, "config", "user.email", "test@pinax.test")
        _git(self.repo, "config", "user.name", "Pinax Test")

        r = subprocess.run(
            [sys.executable, "-m", "pinax", "init", "--actor", "operator@example.test"],
            cwd=self.repo, capture_output=True, text=True, env=_build_env(),
        )
        assert r.returncode == 0, r.stderr

        # Manual install (documented in the hook's own docstring -- this
        # hook is NOT auto-installed by 'pinax init', unlike pre-commit).
        hooks_dir = os.path.join(self.repo, ".git", "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        hook_dst = os.path.join(hooks_dir, "post-commit")
        shutil.copyfile(_HOOK_SRC, hook_dst)
        if os.name != "nt":
            st = os.stat(hook_dst)
            os.chmod(hook_dst, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "init: pinax ergon base")

    def _commit_count(self) -> int:
        out = _git(self.repo, "log", "--oneline").stdout
        return len([line for line in out.splitlines() if line.strip()])

    def test_hook_regenerates_and_commits_portfolio(self):
        before_count = self._commit_count()

        with open(os.path.join(self.repo, "note.txt"), "w") as fh:
            fh.write("hello\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "a real human commit")

        portfolio_path = os.path.join(self.repo, "PORTFOLIO.md")
        assert os.path.isfile(portfolio_path), "post-commit hook did not create PORTFOLIO.md"

        after_count = self._commit_count()
        # human commit (+1) + exactly one hook follow-up commit (+1).
        assert after_count == before_count + 2

        last_subject = _git(self.repo, "log", "-1", "--format=%s").stdout.strip()
        assert last_subject == "pinax: regenerate PORTFOLIO.md"

    def test_hook_recursion_guard_does_not_loop_forever(self):
        """A second human commit still produces exactly one more follow-up
        commit -- the guard resets per human commit rather than disabling
        the hook permanently, and the loop terminates each time."""
        with open(os.path.join(self.repo, "note1.txt"), "w") as fh:
            fh.write("one\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "human commit 1")
        count_after_first = self._commit_count()

        with open(os.path.join(self.repo, "note2.txt"), "w") as fh:
            fh.write("two\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-m", "human commit 2")
        count_after_second = self._commit_count()

        assert count_after_second == count_after_first + 2
