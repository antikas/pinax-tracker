"""
A Pinax commit contains only the paths Pinax staged for it.

Real Git repositories throughout: the property is about what Git puts in a
commit, which no scripted runner can show.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from pinax.commands import doctor_cmd
from pinax.doctor import uncommitted_ergon_files
from pinax.fold import fold, state_to_json_safe

from tests.test_doctor import (
    _FAR_FUTURE,
    _HUGE_HOURS,
    _add_item,
    _append_uncommitted_item,
    _commit_all,
    _git,
    _init_repo,
    _log_dir,
    _pinax,
    _read_shards,
    requires_git,
)

pytestmark = requires_git

_OTHER = ("gone.txt", "staged_new.txt", "dirty.txt")


@pytest.fixture()
def repo(tmp_path):
    root = str(tmp_path)
    _init_repo(root)
    assert _pinax(root, "init").returncode == 0
    for name in ("gone.txt", "dirty.txt"):
        with open(os.path.join(root, name), "w") as fh:
            fh.write("seed\n")
    _commit_all(root, "init: pinax ergon base")
    return root


def _other_work(root: str) -> str:
    """Stage a deletion and a new file and leave one change unstaged."""
    _git(root, "rm", "-q", "gone.txt")
    with open(os.path.join(root, "staged_new.txt"), "w") as fh:
        fh.write("new\n")
    _git(root, "add", "staged_new.txt")
    with open(os.path.join(root, "dirty.txt"), "a") as fh:
        fh.write("edited\n")
    return _other_status(root)


def _other_status(root: str) -> str:
    return _git(root, "status", "--porcelain", "--", *_OTHER).stdout


def _changed_paths(root: str, rev: str = "HEAD") -> list[str]:
    out = _git(root, "show", "--name-only", "--format=", rev).stdout
    return [line for line in out.splitlines() if line]


def test_a_sync_commit_leaves_other_staged_work_staged(repo):
    item_id = _add_item(repo, "first")
    before = _other_work(repo)
    assert before != ""

    result = _pinax(repo, "park", item_id, "--reason", "later", "--actor", "t@h")
    assert result.returncode == 0, result.stdout + result.stderr

    subject = _git(repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == f"pinax: item.parked {item_id}"
    assert sorted(_changed_paths(repo)) == sorted(
        [".ergon/board.md", f".ergon/items/{item_id}.md", ".ergon/log/t-h.jsonl"]
    )
    assert _other_status(repo) == before


def test_a_new_items_projection_file_is_in_its_commit(repo):
    before = _other_work(repo)

    item_id = _add_item(repo, "second")

    paths = _changed_paths(repo)
    assert sorted(paths) == sorted(
        [".ergon/board.md", f".ergon/items/{item_id}.md", ".ergon/log/t-h.jsonl"]
    )
    assert _other_status(repo) == before
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert f".ergon/items/{item_id}.md" in tracked


def test_doctor_reconcile_commits_only_the_orphaned_files(repo):
    _append_uncommitted_item(repo, "orphaned trail")
    before = _other_work(repo)

    result = _pinax(
        repo, "doctor", "--reconcile", "--now", _FAR_FUTURE,
        "--stale-hours", _HUGE_HOURS, stdin="y\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "committed" in result.stdout
    paths = _changed_paths(repo)
    assert paths and all(path.startswith(".ergon/") for path in paths)
    assert _other_status(repo) == before


def test_an_unfinished_merge_is_reported_as_a_refused_commit(repo):
    _git(repo, "checkout", "-q", "-b", "side")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("side\n")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("main\n")
    _commit_all(repo, "main")
    merged = _git(repo, "merge", "side", check=False)
    assert merged.returncode != 0
    assert os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))

    result = _pinax(repo, "add", "--title", "mid merge", "--actor", "t@h", "--json")

    assert result.returncode == 7, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["status"] == "commit_refused"
    shard = _read_shards(repo)["t-h.jsonl"].decode("utf-8")
    assert "mid merge" in shard


def test_replaying_the_log_matches_the_projection_the_commit_carried(repo):
    item_id = _add_item(repo, "replayed")
    _other_work(repo)
    _pinax(repo, "park", item_id, "--reason", "later", "--actor", "t@h")

    committed = _git(repo, "show", f"HEAD:.ergon/items/{item_id}.md").stdout
    with open(os.path.join(repo, ".ergon", "items", f"{item_id}.md"), newline="") as fh:
        assert fh.read() == committed
    state = state_to_json_safe(fold(_log_dir(repo)))
    assert state["items"][item_id]["status"] == "parked"



def _diverged_remote(repo: str, tmp_path_factory) -> tuple[str, str]:
    """Publish an item, then let another clone publish its own change first."""
    hub = str(tmp_path_factory.mktemp("hub"))
    _git(hub, "init", "-q", "--bare", "-b", "main")
    _git(repo, "remote", "add", "origin", hub)
    _git(repo, "push", "-q", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    item_id = _add_item(repo, "contested")
    _git(repo, "push", "-q", "origin", "main")

    elsewhere = str(tmp_path_factory.mktemp("elsewhere"))
    _git(elsewhere, "clone", "-q", hub, ".")
    _git(elsewhere, "config", "user.email", "o@h")
    _git(elsewhere, "config", "user.name", "Other")
    assert _pinax(elsewhere, "add", "--title", "first", "--actor", "o@h").returncode == 0
    _git(elsewhere, "push", "-q", "origin", "main")
    return item_id, hub


def test_a_diverged_remote_leaves_other_staged_work_out_of_every_commit(
    repo, tmp_path_factory
):
    item_id, hub = _diverged_remote(repo, tmp_path_factory)

    before = _other_work(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    result = _pinax(repo, "park", item_id, "--reason", "later", "--actor", "t@h", "--json")

    assert result.returncode == 5, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["status"] == "push_rejected"
    assert _other_status(repo) == before
    commits = _git(repo, "rev-list", f"{head}..HEAD").stdout.split()
    assert len(commits) == 1
    assert all(p.startswith(".ergon/") for p in _changed_paths(repo, commits[0]))
    remote = _git(hub, "rev-list", "main").stdout.split()
    assert commits[0] not in remote


@pytest.mark.parametrize("set_aside", ["commit", "stash"])
def test_the_recovery_in_adr_006_publishes_a_kept_event(repo, tmp_path_factory, set_aside):
    item_id, hub = _diverged_remote(repo, tmp_path_factory)
    _other_work(repo)
    assert _pinax(repo, "park", item_id, "--reason", "later", "--actor", "t@h").returncode == 5

    if set_aside == "commit":
        _git(repo, "commit", "-q", "-m", "other work")
    else:
        _git(repo, "stash", "push", "-q", "--staged")
    pulled = _git(repo, "pull", "--no-rebase", check=False)
    assert pulled.returncode != 0
    assert "CONFLICT" in pulled.stdout and ".ergon/board.md" in pulled.stdout

    assert _pinax(repo, "verify", "--fix").returncode == 0
    _git(repo, "add", ".ergon")
    _git(repo, "commit", "--no-edit", "-q")
    _git(repo, "push", "-q")

    assert _git(repo, "rev-parse", "HEAD").stdout == _git(hub, "rev-parse", "main").stdout
    assert _pinax(repo, "verify").returncode == 0
    state = state_to_json_safe(fold(_log_dir(repo)))
    assert state["items"][item_id]["status"] == "parked"
    assert len(state["items"]) == 2


@pytest.mark.parametrize("set_aside", ["commit", "stash"])
def test_the_recovery_in_adr_006_claims_after_an_annulled_claim(repo, tmp_path_factory, set_aside):
    item_id, hub = _diverged_remote(repo, tmp_path_factory)
    _other_work(repo)
    refused = _pinax(repo, "claim", item_id, "--actor", "t@h", "--json")
    assert refused.returncode == 5, refused.stdout + refused.stderr

    if set_aside == "commit":
        _git(repo, "commit", "-q", "-m", "other work")
    else:
        _git(repo, "stash", "push", "-q", "--staged")

    claimed = _pinax(repo, "claim", item_id, "--actor", "t@h", "--json")

    assert claimed.returncode == 0, claimed.stdout + claimed.stderr
    if set_aside == "stash":
        assert _git(repo, "stash", "pop", "-q", check=False).returncode == 0
    assert _pinax(repo, "verify").returncode == 0
    state = state_to_json_safe(fold(_log_dir(repo)))
    assert state["items"][item_id]["owner"] == "t@h"


def test_annulling_the_only_items_creation_commits_the_projection_deletion(repo):
    item_id = _add_item(repo, "short lived")
    shard = _read_shards(repo)["t-h.jsonl"].decode("utf-8")
    created = next(
        event["id"]
        for event in map(json.loads, shard.splitlines())
        if event["type"] == "item.created"
    )
    before = _other_work(repo)

    result = _pinax(repo, "annul", created, "--reason", "test", "--actor", "t@h")

    assert result.returncode == 0, result.stdout + result.stderr
    assert f".ergon/items/{item_id}.md" in _changed_paths(repo)
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert f".ergon/items/{item_id}.md" not in tracked
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    assert _other_status(repo) == before


_HOOK_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "post-commit"
)


def _stage_rename_in_ergon(root: str) -> None:
    os.makedirs(os.path.join(root, ".ergon", "extra"), exist_ok=True)
    with open(os.path.join(root, ".ergon", "extra", "old.txt"), "w") as fh:
        fh.write("moved\n")
    _commit_all(root, "add extra")
    _git(root, "mv", ".ergon/extra/old.txt", ".ergon/extra/new.txt")


def _reconcile(root: str):
    return _pinax(
        root, "doctor", "--reconcile", "--now", _FAR_FUTURE,
        "--stale-hours", _HUGE_HOURS, stdin="y\n",
    )


def test_the_post_commit_hook_commits_only_the_portfolio(repo):
    with open(os.path.join(repo, "PORTFOLIO.md"), "w") as fh:
        fh.write("stale\n")
    _commit_all(repo, "seed portfolio")
    hook = os.path.join(repo, ".git", "hooks", "post-commit")
    shutil.copyfile(_HOOK_SRC, hook)
    os.chmod(hook, 0o755)
    for name in ("staged_new.txt", "trigger.txt"):
        with open(os.path.join(repo, name), "w") as fh:
            fh.write("work\n")
        _git(repo, "add", name)

    _git(repo, "commit", "-q", "-m", "human commit", "--", "trigger.txt")

    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "pinax: regenerate PORTFOLIO.md"
    assert _changed_paths(repo) == ["PORTFOLIO.md"]
    assert _git(repo, "diff", "--cached", "--name-only").stdout.split() == ["staged_new.txt"]


def _install_post_commit_hook(root: str) -> None:
    hook = os.path.join(root, ".git", "hooks", "post-commit")
    shutil.copyfile(_HOOK_SRC, hook)
    os.chmod(hook, 0o755)


def _seed_portfolio(root: str) -> None:
    with open(os.path.join(root, "PORTFOLIO.md"), "w") as fh:
        fh.write("stale\n")
    _commit_all(root, "seed portfolio")


def _commit_new_file(root: str, name: str) -> None:
    with open(os.path.join(root, name), "w") as fh:
        fh.write("work\n")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", name, "--", name)


def test_the_post_commit_hook_stages_nothing_during_a_cherry_pick(repo):
    _seed_portfolio(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_new_file(repo, "picked.txt")
    _git(repo, "checkout", "-q", "main")
    _install_post_commit_hook(repo)

    _git(repo, "cherry-pick", "side")

    assert _changed_paths(repo) == ["picked.txt"]
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_the_post_commit_hook_keeps_the_portfolio_out_of_rebased_commits(repo):
    _seed_portfolio(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    _commit_new_file(repo, "first.txt")
    _commit_new_file(repo, "second.txt")
    _git(repo, "checkout", "-q", "main")
    _commit_new_file(repo, "base.txt")
    _git(repo, "checkout", "-q", "side")
    _install_post_commit_hook(repo)

    _git(repo, "rebase", "main")

    assert _changed_paths(repo, "HEAD") == ["second.txt"]
    assert _changed_paths(repo, "HEAD~1") == ["first.txt"]
    assert _git(repo, "diff", "--cached", "--name-only").stdout == ""


def test_doctor_reconcile_commits_a_staged_rename_in_one_pass(repo):
    _stage_rename_in_ergon(repo)

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    out = _git(repo, "show", "--name-only", "--no-renames", "--format=", "HEAD").stdout
    assert sorted(out.split()) == [".ergon/extra/new.txt", ".ergon/extra/old.txt"]


def test_doctor_reconcile_counts_both_paths_of_a_rename(repo):
    _stage_rename_in_ergon(repo)

    result = _reconcile(repo)

    assert "doctor: committed 2 file(s)." in result.stdout
    assert "(2 file(s)," in _git(repo, "log", "-1", "--format=%s").stdout


def test_doctor_reconcile_commits_a_staged_deletion(repo):
    os.makedirs(os.path.join(repo, ".ergon", "extra"), exist_ok=True)
    with open(os.path.join(repo, ".ergon", "extra", "gone.txt"), "w") as fh:
        fh.write("removed\n")
    _commit_all(repo, "add extra")
    _git(repo, "rm", "-q", ".ergon/extra/gone.txt")

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git add failed" not in result.stderr
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    assert _changed_paths(repo) == [".ergon/extra/gone.txt"]


def test_doctor_reconcile_commits_a_rename_git_does_not_detect(repo):
    _git(repo, "config", "status.renames", "false")
    _stage_rename_in_ergon(repo)

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "git add failed" not in result.stderr
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    out = _git(repo, "show", "--name-only", "--no-renames", "--format=", "HEAD").stdout
    assert sorted(out.split()) == [".ergon/extra/new.txt", ".ergon/extra/old.txt"]


def test_doctor_reconcile_commits_exactly_the_ergon_files_beside_a_rename(repo):
    _stage_rename_in_ergon(repo)
    _append_uncommitted_item(repo, "orphaned trail")
    before = _other_work(repo)

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    out = _git(repo, "show", "--name-only", "--no-renames", "--format=", "HEAD").stdout
    paths = out.split()
    assert paths == sorted(paths)
    assert all(p.startswith(".ergon/") for p in paths)
    assert ".ergon/extra/old.txt" in paths and ".ergon/extra/new.txt" in paths
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    assert _other_status(repo) == before


def test_doctor_reconcile_commits_exactly_the_ergon_files_with_outside_work_staged(repo):
    _append_uncommitted_item(repo, "orphaned trail")
    before = _other_work(repo)
    expected = sorted(
        line[3:] for line in _git(repo, "status", "--porcelain", "-uall", "--", ".ergon").stdout.splitlines()
    )

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    shown = _git(repo, "show", "--name-only", "--no-renames", "--format=", "HEAD").stdout
    assert shown.splitlines() == expected
    assert _other_status(repo) == before


def test_doctor_reconcile_in_an_unfinished_merge_reports_and_loses_nothing(repo):
    _git(repo, "checkout", "-q", "-b", "side")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("side\n")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    with open(os.path.join(repo, "conflict.txt"), "w") as fh:
        fh.write("main\n")
    _commit_all(repo, "main")
    assert _git(repo, "merge", "side", check=False).returncode != 0
    _append_uncommitted_item(repo, "orphaned trail")
    shards = _read_shards(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "unfinished operation" in result.stderr
    assert "doctor: committed" not in result.stdout
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert os.path.exists(os.path.join(repo, ".git", "MERGE_HEAD"))
    assert _read_shards(repo) == shards
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout != ""


def test_doctor_reconcile_leaves_a_conflict_in_the_tracker_unresolved(repo):
    conflicted = os.path.join(repo, ".ergon", "extra", "conflict.txt")
    os.makedirs(os.path.dirname(conflicted), exist_ok=True)
    _git(repo, "checkout", "-q", "-b", "side")
    with open(conflicted, "w") as fh:
        fh.write("side\n")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", "main")
    os.makedirs(os.path.dirname(conflicted), exist_ok=True)
    with open(conflicted, "w") as fh:
        fh.write("main\n")
    _commit_all(repo, "main")
    assert _git(repo, "merge", "side", check=False).returncode != 0
    before = _git(repo, "status", "--porcelain", "--", ".ergon").stdout
    assert before.startswith("AA ")

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == before


def _add_then_delete_in_ergon(root: str) -> str:
    os.makedirs(os.path.join(root, ".ergon", "extra"), exist_ok=True)
    path = os.path.join(root, ".ergon", "extra", "brief.txt")
    with open(path, "w") as fh:
        fh.write("short lived\n")
    _git(root, "add", path)
    os.remove(path)
    return _git(root, "status", "--porcelain", "--", ".ergon").stdout


def _conflict_on_branches(root: str) -> None:
    """Two branches change one new file differently; main is checked out."""
    _git(root, "checkout", "-q", "-b", "side")
    with open(os.path.join(root, "conflict.txt"), "w") as fh:
        fh.write("side\n")
    _commit_all(root, "side")
    _git(root, "checkout", "-q", "main")
    with open(os.path.join(root, "conflict.txt"), "w") as fh:
        fh.write("main\n")
    _commit_all(root, "main")


def _start_cherry_pick(root: str):
    return _git(root, "cherry-pick", "side", check=False)


def _start_rebase(root: str):
    _git(root, "checkout", "-q", "side")
    return _git(root, "rebase", "main", check=False)


def _start_revert(root: str):
    # The file changes again, so reverting the commit that added it conflicts.
    with open(os.path.join(root, "conflict.txt"), "w") as fh:
        fh.write("later\n")
    _commit_all(root, "later")
    return _git(root, "revert", "--no-edit", "HEAD~1", check=False)


@pytest.mark.parametrize("start", [_start_cherry_pick, _start_rebase, _start_revert])
def test_doctor_reconcile_waits_for_every_unfinished_operation(repo, start):
    _conflict_on_branches(repo)
    assert start(repo).returncode != 0
    _append_uncommitted_item(repo, "orphaned trail")
    head = _git(repo, "rev-parse", "HEAD").stdout

    result = _reconcile(repo)

    assert "unfinished operation" in result.stderr
    assert "doctor: committed" not in result.stdout
    assert _git(repo, "rev-parse", "HEAD").stdout == head


def test_doctor_reconcile_waits_for_a_conflict_a_stash_pop_left(repo):
    conflicted = os.path.join(repo, ".ergon", "extra", "c.txt")
    os.makedirs(os.path.dirname(conflicted), exist_ok=True)
    with open(conflicted, "w") as fh:
        fh.write("base\n")
    _commit_all(repo, "base")
    with open(conflicted, "w") as fh:
        fh.write("stashed\n")
    _git(repo, "stash", "push", "-q")
    with open(conflicted, "w") as fh:
        fh.write("committed\n")
    _commit_all(repo, "changed")
    assert _git(repo, "stash", "pop", "-q", check=False).returncode != 0
    head = _git(repo, "rev-parse", "HEAD").stdout
    before = _git(repo, "status", "--porcelain", "--", ".ergon").stdout

    result = _reconcile(repo)

    assert "unfinished operation" in result.stderr
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == before


def test_doctor_reconcile_is_not_stopped_by_what_a_quit_rebase_left_behind(repo):
    _conflict_on_branches(repo)
    _git(repo, "checkout", "-q", "side")
    assert _git(repo, "rebase", "main", check=False).returncode != 0
    _git(repo, "rebase", "--quit")
    _git(repo, "reset", "-q", "--hard")
    assert os.path.exists(os.path.join(repo, ".git", "REBASE_HEAD"))
    _append_uncommitted_item(repo, "orphaned trail")

    result = _reconcile(repo)

    assert "unfinished operation" not in result.stderr
    assert "doctor: committed" in result.stdout


def test_doctor_reconcile_clears_an_intent_to_add_file_since_deleted(repo):
    os.makedirs(os.path.join(repo, ".ergon", "extra"), exist_ok=True)
    path = os.path.join(repo, ".ergon", "extra", "brief.txt")
    with open(path, "w") as fh:
        fh.write("short lived\n")
    _git(repo, "add", "-N", path)
    os.remove(path)
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout.startswith(" D ")

    result = _reconcile(repo)

    assert "doctor: cleared 1 file(s)" in result.stdout
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""


def test_doctor_reconcile_clears_a_file_added_then_deleted(repo):
    assert _add_then_delete_in_ergon(repo).startswith("AD ")
    head = _git(repo, "rev-parse", "HEAD").stdout

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "doctor: cleared 1 file(s)" in result.stdout
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""
    assert _git(repo, "rev-parse", "HEAD").stdout == head


def test_doctor_reconcile_clears_a_file_added_then_deleted_beside_an_orphan(repo):
    _add_then_delete_in_ergon(repo)
    _append_uncommitted_item(repo, "orphaned trail")

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "doctor: committed" in result.stdout
    assert ".ergon/extra/brief.txt" not in _changed_paths(repo)
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""


def test_doctor_reconcile_names_a_recreated_rename_source_once(repo):
    _stage_rename_in_ergon(repo)
    with open(os.path.join(repo, ".ergon", "extra", "old.txt"), "w") as fh:
        fh.write("recreated\n")

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "doctor: committed 2 file(s)." in result.stdout
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""


def test_a_rename_in_the_worktree_column_is_read_as_a_rename(repo):
    os.makedirs(os.path.join(repo, ".ergon", "extra"), exist_ok=True)
    old = os.path.join(repo, ".ergon", "extra", "old.txt")
    new = os.path.join(repo, ".ergon", "extra", "new.txt")
    with open(old, "w") as fh:
        fh.write("moved\n")
    _commit_all(repo, "add extra")
    os.rename(old, new)
    _git(repo, "add", "-N", new)
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout.startswith(" R ")

    assert uncommitted_ergon_files(repo) == [
        {"path": ".ergon/extra/new.txt", "state": "modified", "renamed_from": ".ergon/extra/old.txt"}
    ]

    result = _reconcile(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "status", "--porcelain", "--", ".ergon").stdout == ""


def test_doctor_reports_both_paths_of_a_rename(repo):
    _stage_rename_in_ergon(repo)

    result = _pinax(repo, "doctor", "--now", _FAR_FUTURE, "--stale-hours", _HUGE_HOURS)

    assert "across 2 uncommitted .ergon file(s)" in result.stdout


def test_a_commit_refusal_on_stdout_is_reported(monkeypatch, capsys):
    def fake_git(_root, args):
        refused = args[0] == "commit"
        return subprocess.CompletedProcess(
            args,
            1 if refused else 0,
            stdout=b"nothing added to commit\n" if refused else b"",
            stderr=b"",
        )

    monkeypatch.setattr(doctor_cmd, "_git", fake_git)
    monkeypatch.setattr(doctor_cmd, "_prompt", lambda _question: "y")
    report = {
        "uncommitted": {
            "available": True,
            "files": [{"path": ".ergon/log/a.jsonl", "state": "modified"}],
            "events": [],
        }
    }

    doctor_cmd._reconcile_uncommitted(".", report)

    assert "nothing added to commit" in capsys.readouterr().err


def test_a_refusing_pre_commit_hook_keeps_the_event_and_the_other_work(repo):
    hook = os.path.join(repo, ".git", "hooks", "pre-commit")
    with open(hook, "w") as fh:
        fh.write("#!/bin/sh\necho 'refused by test hook' >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    before = _other_work(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout

    result = _pinax(repo, "add", "--title", "refused one", "--actor", "t@h", "--json")

    assert result.returncode == 7, result.stdout + result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["status"] == "commit_refused"
    assert "refused one" in _read_shards(repo)["t-h.jsonl"].decode("utf-8")
    assert _git(repo, "rev-parse", "HEAD").stdout == head
    assert _other_status(repo) == before
