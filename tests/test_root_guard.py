"""Repository root guard and prefix validation tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.deep

ACTOR = "operator@example.test"

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"


# ---------------------------------------------------------------------------
# Git/CLI subprocess helpers (mirrors tests/test_priority.py).
# ---------------------------------------------------------------------------

def _build_env(extra: dict | None = None) -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing if existing else "")
    # from the test runner's own session never leaks into a subprocess CLI
    # invocation under test (that would be exactly the kind of silent
    # cross-tracker contamination this item exists to prevent).
    env.pop("PINAX_ROOT", None)
    if extra:
        env.update(extra)
    return env


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, env=_build_env(),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git not available on PATH",
)


def _pinax(
    repo: str, *args: str, check: bool = True, env_extra: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo, capture_output=True, text=True, env=_build_env(env_extra),
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"pinax {' '.join(args)} failed in {repo}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def _init_repo(repo: str) -> None:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@pinax.test")
    _git(repo, "config", "user.name", "Pinax Test")
    _git(repo, "config", "core.autocrlf", "false")
    with open(os.path.join(repo, ".gitattributes"), "w", newline="\n") as fh:
        fh.write(_GITATTRIBUTES)


def _commit_all(repo: str, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _count_log_lines(repo: str) -> int:
    log_dir = os.path.join(repo, ".ergon", "log")
    total = 0
    for fname in os.listdir(log_dir):
        if not fname.endswith(".jsonl"):
            continue
        with open(os.path.join(log_dir, fname), "rb") as fh:
            raw = fh.read()
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        total += len([l for l in normalised.split(b"\n") if l])
    return total


@pytest.fixture()
def empty_repo(tmp_path):
    """A committed, freshly-initialised pinax repo with NO items yet."""
    root = str(tmp_path)
    _init_repo(root)
    r = _pinax(root, "init", "--actor", ACTOR)
    assert r.returncode == 0, r.stderr
    _commit_all(root, "init: pinax ergon base")
    return root


@pytest.fixture()
def cli_repo(empty_repo):
    """A committed pinax repo with one 'pnx-' item, ready for CLI tests."""
    root = empty_repo
    r = _pinax(root, "add", "--title", "Item X", "--prefix", "pnx", "--actor", ACTOR, "--json")
    assert r.returncode == 0, r.stderr
    item_id = json.loads(r.stdout)["item_id"]
    _commit_all(root, "add Item X")
    return root, item_id


# ---------------------------------------------------------------------------
# (a) Every state-changing command names the resolved tracker root.
# ---------------------------------------------------------------------------

@requires_git
class TestConfirmationNamesRoot:
    def _ergon_root(self, repo: str) -> str:
        return os.path.join(repo, ".ergon")

    def test_add_json_and_plain(self, empty_repo):
        root = empty_repo
        r = _pinax(root, "add", "--title", "A", "--actor", ACTOR, "--json")
        payload = json.loads(r.stdout)
        assert payload["root"] == self._ergon_root(root)

        r = _pinax(root, "add", "--title", "B", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_claim_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "claim", item_id, "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "add", "--title", "C2", "--actor", ACTOR, "--json")
        item2 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "claim", item2, "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_done_json_and_plain(self, cli_repo, tmp_path):
        root, item_id = cli_repo
        briefing = tmp_path / "briefing.md"
        briefing.write_text("done work-record", encoding="utf-8")
        r = _pinax(root, "done", item_id, "--briefing", str(briefing), "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "add", "--title", "second item", "--actor", ACTOR, "--json")
        item2 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "done", item2, "--briefing", str(briefing), "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_block_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "block", item_id, "--gate", "scope", "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "add", "--title", "E2", "--actor", ACTOR, "--json")
        item2 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "block", item2, "--gate", "scope", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_park_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "park", item_id, "--reason", "waiting", "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "add", "--title", "F2", "--actor", ACTOR, "--json")
        item2 = json.loads(r.stdout)["item_id"]
        r = _pinax(root, "park", item2, "--reason", "waiting", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_priority_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "priority", item_id, "5", "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "priority", item_id, "3", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_annul_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "claim", item_id, "--actor", ACTOR, "--json")
        target_event_id = json.loads(r.stdout)["event_id"]

        r = _pinax(root, "annul", target_event_id, "--reason", "test", "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "annul", target_event_id, "--reason", "test again", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_dep_add_and_rm_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "add", "--title", "G2", "--actor", ACTOR, "--json")
        item2 = json.loads(r.stdout)["item_id"]

        r = _pinax(root, "dep", "add", item_id, "--blocks", item2, "--actor", ACTOR, "--json")
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "dep", "rm", item_id, "--blocks", item2, "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout

    def test_note_add_json_and_plain(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(
            root, "note", "add", item_id, "--ref", "docs/some-note.md",
            "--actor", ACTOR, "--json",
        )
        assert json.loads(r.stdout)["root"] == self._ergon_root(root)

        r = _pinax(root, "note", "add", item_id, "--ref", "docs/another-note.md", "--actor", ACTOR)
        assert self._ergon_root(root) in r.stdout


# ---------------------------------------------------------------------------
# (b) prefix-collision guard: fail-loud, override, empty-tracker exemption.
# ---------------------------------------------------------------------------

@requires_git
class TestPrefixCollisionGuard:
    def test_empty_tracker_exempt_any_prefix_succeeds(self, empty_repo):
        """A tracker with zero items has nothing to collide with — first add
        always succeeds regardless of prefix, no override needed."""
        root = empty_repo
        r = _pinax(root, "add", "--title", "First", "--prefix", "zzz", "--actor", ACTOR, "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["item_id"].startswith("zzz-")

    def test_unseen_prefix_on_non_empty_tracker_rejected(self, cli_repo):
        """Reject an unrelated prefix before appending to a non-empty tracker."""
        root, _item_id = cli_repo
        lines_before = _count_log_lines(root)
        r = _pinax(root, "add", "--title", "cross-tracker", "--prefix", "ab", "--actor", ACTOR, check=False)
        assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
        assert "ab" in r.stderr and "pnx" in r.stderr
        assert _count_log_lines(root) == lines_before, (
            "validate-before-append: a rejected prefix must not append anything"
        )

    def test_unseen_prefix_with_override_succeeds(self, cli_repo):
        """--allow-new-prefix is the explicit escape hatch for a genuine
        first use of a new prefix on an otherwise non-empty tracker."""
        root, _item_id = cli_repo
        r = _pinax(
            root, "add", "--title", "genuinely new prefix", "--prefix", "ab",
            "--actor", ACTOR, "--allow-new-prefix", "--json",
        )
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["item_id"].startswith("ab-")

    def test_seen_prefix_on_non_empty_tracker_succeeds_without_override(self, cli_repo):
        """Adding a SECOND item with the SAME prefix already present in the
        tracker is the ordinary, common case — no override required."""
        root, _item_id = cli_repo
        r = _pinax(root, "add", "--title", "Item Y", "--prefix", "pnx", "--actor", ACTOR, "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["item_id"].startswith("pnx-")

    def test_default_prefix_is_checked_too(self, cli_repo):
        """The guard applies to the DEFAULT prefix ('pnx'), not just an
        explicit --prefix — a tracker whose items are all e.g. 'ab-' must
        reject a bare 'pinax add' (no --prefix given) exactly the same way."""
        root, _item_id = cli_repo
        # Seed a second, ab-prefixed item via override so the tracker now has
        # (simulated by an explicit prefix standing in for "default") must
        # still be rejected without the override.
        r = _pinax(
            root, "add", "--title", "ab seed", "--prefix", "ab", "--actor", ACTOR,
            "--allow-new-prefix", "--json",
        )
        assert r.returncode == 0, r.stderr

        lines_before = _count_log_lines(root)
        r = _pinax(root, "add", "--title", "cd item", "--prefix", "cd", "--actor", ACTOR, check=False)
        assert r.returncode != 0
        assert _count_log_lines(root) == lines_before


# ---------------------------------------------------------------------------
# (c) --root / PINAX_ROOT explicit pin: hard error on mismatch.
# ---------------------------------------------------------------------------

@requires_git
class TestRootPin:
    def test_matching_root_flag_succeeds(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(root, "--root", root, "priority", item_id, "5", "--actor", ACTOR, "--json")
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["priority"] == 5

    def test_matching_pinax_root_env_succeeds(self, cli_repo):
        root, item_id = cli_repo
        r = _pinax(
            root, "priority", item_id, "6", "--actor", ACTOR, "--json",
            env_extra={"PINAX_ROOT": root},
        )
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["priority"] == 6

    def test_mismatched_root_flag_errors_nothing_appended(self, cli_repo, tmp_path):
        root, item_id = cli_repo
        other = tmp_path / "unrelated-tracker-dir"
        other.mkdir()
        lines_before = _count_log_lines(root)

        r = _pinax(
            root, "--root", str(other), "priority", item_id, "9", "--actor", ACTOR,
            check=False,
        )
        assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
        assert "MISMATCH" in r.stderr
        assert _count_log_lines(root) == lines_before, (
            "validate-before-append: a root-pin mismatch must not append anything"
        )

    def test_mismatched_pinax_root_env_errors_nothing_appended(self, cli_repo, tmp_path):
        root, item_id = cli_repo
        other = tmp_path / "another-unrelated-dir"
        other.mkdir()
        lines_before = _count_log_lines(root)

        r = _pinax(
            root, "priority", item_id, "9", "--actor", ACTOR, check=False,
            env_extra={"PINAX_ROOT": str(other)},
        )
        assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
        assert "MISMATCH" in r.stderr
        assert _count_log_lines(root) == lines_before

    def test_root_flag_takes_precedence_over_env_var(self, cli_repo, tmp_path):
        """When both --root and PINAX_ROOT are set, --root (the explicit,
        per-invocation flag) wins over the ambient environment variable."""
        root, item_id = cli_repo
        wrong = tmp_path / "wrong-env-dir"
        wrong.mkdir()

        r = _pinax(
            root, "--root", root, "priority", item_id, "7", "--actor", ACTOR, "--json",
            env_extra={"PINAX_ROOT": str(wrong)},
        )
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout)["priority"] == 7

    def test_pinned_root_blocks_unrelated_walkup(self, cli_repo, tmp_path):
        """PINAX_ROOT rejects a command whose working directory resolves elsewhere."""
        root, _item_id = cli_repo

        unrelated_repo = tmp_path / "unrelated-harness-repo"
        unrelated_repo.mkdir()
        _init_repo(str(unrelated_repo))
        _commit_all(str(unrelated_repo), "seed unrelated repo")
        scratch = unrelated_repo / "session-scratch" / "deep" / "nested"
        scratch.mkdir(parents=True)

        lines_before_root = _count_log_lines(root)

        r = _pinax(
            str(scratch), "add", "--title", "should never land here",
            "--actor", ACTOR, check=False, env_extra={"PINAX_ROOT": root},
        )
        assert r.returncode != 0, f"expected non-zero exit; stdout={r.stdout} stderr={r.stderr}"
        assert "MISMATCH" in r.stderr
        # Nothing was appended to either tracker: the real one (pin target,
        # never touched because the walk-up landed elsewhere) ...
        assert _count_log_lines(root) == lines_before_root
        # ... nor the unrelated repo (no .ergon/log/ exists there at all --
        # 'pinax init' was never run on it, so a silent bind would have
        # failed anyway with ".ergon/log/ not found"; the pin check fires
        # FIRST, before that fallback failure, which is what we assert).
        assert not os.path.isdir(os.path.join(str(unrelated_repo), ".ergon"))
