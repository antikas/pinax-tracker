"""
pinax init — create .ergon/, .ergon/log/, install .ergon/.gitattributes.

ADR-002: .gitattributes content is exactly:
    *.jsonl text eol=lf merge=union
    .ergon/** text eol=lf

Emits ergon.created (and phase.opened for 'init' phase) events so a fresh
repo folds to a valid, non-empty initialised state.

Idempotent: re-running init does not corrupt or duplicate events.  The check
is: if .ergon/log/ already contains a shard with an ergon.created event, skip
the init events (but still ensure the directory structure + .gitattributes
are present — those are always safe to recreate).
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import fold


_GITATTRIBUTES_CONTENT = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"

# consumer repo's own blanket `*.jsonl` gitignore rule shadows
# local-only log, projections conflict, completion events never cross
# branches.  A NESTED .gitignore co-located with .ergon/.gitattributes
# fixes this unconditionally, regardless of what the consumer's own
# top-level .gitignore says: git evaluates the closest (most specific)
# .gitignore first, and a negation there can re-include a file even when a
# broader parent pattern excludes it (as long as the parent DIRECTORY
# itself was never excluded — only individual files are, here).  This is
# more robust than writing into the consumer's own .gitignore (which this
# tool does not own and must not silently rewrite).
_GITIGNORE_CONTENT = (
    "# Pinax event log - never let a broader/parent .gitignore swallow this\n"
    "# A consumer repository may ignore all JSONL files with a blanket\n"
    "# *.jsonl rule silently untracked .ergon/log/, causing invisible\n"
    "# per-worktree logs, projection conflicts, and events that never\n"
    "# crossed branches. See 'pinax doctor' / 'pinax verify' class 4.\n"
    "!/log/*.jsonl\n"
)

# shutil.which('pinax') -> hook-run-time sys.executable's find_spec ->
# the ABSOLUTE interpreter path baked in at 'pinax init' time (below,
# substituted for __INSTALL_TIME_PYTHON__ by _install_pre_commit_hook).
# That third candidate is the interpreter that ran 'pinax init' — it had
# 'pinax' importable by construction — and resolves the case where the
# hook-run-time interpreter (shebang / git-bash PATH) is a different,
# pinax-less python from the one pinax was actually installed/dev-linked
# into (e.g. a project venv never exposed on PATH).
_PRE_COMMIT_HOOK_CONTENT = """\
#!/usr/bin/env python3
# Pinax drift lint pre-commit hook — auto-installed by 'pinax init'.
# Source: hooks/pre-commit in the repo.  Do not edit by hand.
import importlib.util, os, shutil, subprocess, sys

# Baked in at 'pinax init' time: the interpreter that ran init (which by
# definition had pinax importable then). Fallback candidate 3, after
# shutil.which('pinax') and hook-run-time sys.executable's own find_spec
_INSTALL_TIME_PYTHON = __INSTALL_TIME_PYTHON__

def _pinax_verify_command():
    exe = shutil.which('pinax')
    if exe:
        return [exe, 'verify']
    if importlib.util.find_spec('pinax') is not None:
        return [sys.executable, '-m', 'pinax', 'verify']
    if _INSTALL_TIME_PYTHON and os.path.isfile(_INSTALL_TIME_PYTHON):
        check = subprocess.run(
            [_INSTALL_TIME_PYTHON, '-c', 'import pinax'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if check.returncode == 0:
            return [_INSTALL_TIME_PYTHON, '-m', 'pinax', 'verify']
    return None

def _find_repo_root():
    root = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    return root.stdout.strip() if root.returncode == 0 else os.getcwd()

repo_root = _find_repo_root()
if os.path.isdir(os.path.join(repo_root, '.ergon', 'log')):
    command = _pinax_verify_command()
    if command is None:
        print('pinax: WARNING - pre-commit hook could not find pinax; skipping verify.', file=sys.stderr)
        sys.exit(0)
    sys.exit(subprocess.run(command, cwd=repo_root).returncode)
"""


def _install_pre_commit_hook(repo_root: str) -> None:
    """
    Install the Pinax pre-commit hook into .git/hooks/pre-commit.

    Idempotent: overwrites an existing hook only if it was installed by Pinax
    (identified by the presence of 'Pinax drift lint' in the file) or if the
    file does not exist.  If a non-Pinax hook is present, skip and warn.
    """
    git_path = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
    )
    if git_path.returncode == 0:
        git_hooks_dir = git_path.stdout.strip()
        if not os.path.isabs(git_hooks_dir):
            git_hooks_dir = os.path.join(repo_root, git_hooks_dir)
    else:
        # Keep the lightweight filesystem fixture path used by embedders that
        # create a hook directory before initialising a full Git repository.
        git_hooks_dir = os.path.join(repo_root, ".git", "hooks")
    if not os.path.isdir(git_hooks_dir):
        # Not a git repo or hooks dir doesn't exist — skip silently.
        return

    hook_path = os.path.join(git_hooks_dir, "pre-commit")
    if os.path.isfile(hook_path):
        with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
        if "Pinax drift lint" not in existing:
            print(
                "pinax init: .git/hooks/pre-commit already exists and is not a Pinax hook. "
                "Skipping hook installation. Run 'pinax verify' manually before commits.",
                file=sys.stderr,
            )
            return

    # Bake the absolute path of the interpreter running THIS 'pinax init'
    # importable right now — that's how init itself is running — so it is
    # a valid third fallback candidate for a hook-run-time interpreter
    # (shebang / git-bash PATH resolution) that does not.
    rendered = _PRE_COMMIT_HOOK_CONTENT.replace(
        "__INSTALL_TIME_PYTHON__", repr(sys.executable)
    )
    with open(hook_path, "w", newline="\n", encoding="utf-8") as fh:
        fh.write(rendered)

    # Make executable on POSIX.
    if os.name != "nt":
        import stat
        current = os.stat(hook_path).st_mode
        os.chmod(hook_path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _utc_now_iso() -> str:
    """UTC ISO-8601 timestamp, fixed precision (seconds), no microseconds."""
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    """Fallback actor string when none is provided."""
    import socket
    hostname = socket.gethostname()
    return f"operator@{hostname}"


def run(repo_root: str, actor: str | None = None) -> None:
    """
    Execute pinax init in repo_root.

    Creates .ergon/ structure, installs .gitattributes, emits ergon.created
    (idempotent — skipped if already present in the fold state).
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    gitattributes_path = os.path.join(ergon_dir, ".gitattributes")
    gitignore_path = os.path.join(ergon_dir, ".gitignore")

    # Ensure directories exist.
    os.makedirs(log_dir, exist_ok=True)

    # Always write (or overwrite) .gitattributes — idempotent, exact content.
    with open(gitattributes_path, "w", newline="\n") as fh:
        fh.write(_GITATTRIBUTES_CONTENT)

    # idempotent, exact content, and deliberately unconditional: a repo
    # already broken by a pre-existing consumer .gitignore is fixed by
    # simply re-running 'pinax init', not just a fresh init.
    with open(gitignore_path, "w", newline="\n") as fh:
        fh.write(_GITIGNORE_CONTENT)

    # Check idempotency: if ergon.created already in the fold, skip events.
    state = fold(log_dir)
    if "ergon" in state:
        _install_pre_commit_hook(repo_root)
        print("pinax: .ergon already initialised (idempotent - skipped event emit).")
        return

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    # First event: seq=0, prev='' (sentinel for first in this shard).
    ergon_event = mint_event(
        seq=0,
        ts=ts,
        actor=_actor,
        etype="ergon.created",
        payload={"repo": os.path.basename(repo_root)},
        prev="",
    )
    append_event(log_dir, ergon_event, actor=_actor)

    # Second event: seq=1, prev=ergon_event['id'].
    phase_event = mint_event(
        seq=1,
        ts=ts,
        actor=_actor,
        etype="phase.opened",
        payload={"phase": "init"},
        prev=ergon_event["id"],
    )
    append_event(log_dir, phase_event, actor=_actor)

    # Regenerate the projection atomically after emitting events (ADR-002).
    from ..projection import regenerate
    regenerate(repo_root)

    # Install the pre-commit hook into .git/hooks/ if a .git directory exists.
    _install_pre_commit_hook(repo_root)

    print(f"pinax: initialised .ergon/ in {repo_root}")
    print(f"pinax: emitted ergon.created + phase.opened (actor={_actor})")
