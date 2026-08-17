"""
Tests for pinax doctor.

Covers all three diagnosis classes plus guided `--reconcile`:
  (1) uncommitted shard events in the working tree (orphaned trails from an
      ended session) are detected; a fully committed repo is clean.
  (2) stale claims (claim-without-done older than the threshold) are
      detected under a pinned --now; done/parked/blocked items and claims
      younger than the threshold are never flagged.
  (3) legacy-board frontmatter contradicting pinax facts on migrated items
      is flagged (done-ness disagreement only — vocabulary drift such as
      'todo' vs 'queued' is not a contradiction).
  (4) --reconcile: commits orphaned shards via an ordinary git commit
      WITHOUT altering shard bytes (append-only preserved), and prompts
      done/park/skip per stale claim, appending resolutions via the normal
      event path.
  (5) --json is deterministic (byte-identical for identical repo state and
      pinned --now) and refuses to combine with --reconcile.

Harness: real-git fixture, same shape as tests/test_all_branches.py /
tests/test_visibility.py — subprocess `git` + `python -m pinax`, plus
in-process pinax.fold.fold for fine-grained state assertions.

Determinism: every age-sensitive assertion pins --now explicitly; no test
depends on wall-clock outcomes (a claim made "now" is stale against the
pinned far-future --now, and non-stale against a huge --stale-hours).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from pinax.fold import fold

pytestmark = pytest.mark.deep

_PINAX_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITATTRIBUTES = "*.jsonl text eol=lf merge=union\n.ergon/** text eol=lf\n"

_FAR_FUTURE = "2036-01-01T00:00:00Z"     # any real claim ts is >> 24h older
_HUGE_HOURS = "9000000"                  # ~1000 years — nothing is that stale


def _build_env() -> dict:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = _PINAX_SRC + (os.pathsep + existing_pp if existing_pp else "")
    return env


_GIT_EXE_NAMES = ("git.exe", "git", "git.cmd", "git.bat")


def _env_without_git() -> dict | None:
    """
    A subprocess env whose PATH has every directory hosting a git
    executable stripped out — git becomes unresolvable for a child
    'python -m pinax ...' process, while everything else on PATH (needed
    for Python itself to run) is left intact.

    Returns None if a PATH lacking git could not be hermetically
    constructed (e.g. git is reachable some other way on this machine) —
    the caller should skip rather than risk a false pass/fail.
    """
    env = _build_env()
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    kept = [
        p for p in parts
        if not any(os.path.isfile(os.path.join(p, name)) for name in _GIT_EXE_NAMES)
    ]
    env["PATH"] = os.pathsep.join(kept)
    if shutil.which("git", path=env["PATH"]) is not None:
        return None
    return env


def _git(repo_root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    # Pass env so the Pinax pre-commit drift-lint hook (installed by
    # same harness discipline as tests/test_merge_safety.py.
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True,
        env=_build_env(),
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


requires_git = pytest.mark.skipif(
    not _git_available(), reason="git not available on PATH",
)


def _pinax(repo_root: str, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=repo_root, capture_output=True, text=True, env=_build_env(),
        input=stdin,
    )


def _init_repo(repo_root: str) -> None:
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "test")
    with open(os.path.join(repo_root, ".gitattributes"), "w", newline="\n") as f:
        f.write(_GITATTRIBUTES)


def _commit_all(repo_root: str, message: str) -> None:
    _git(repo_root, "add", "-A")
    _git(repo_root, "commit", "-m", message)


def _add_item(repo_root: str, title: str, actor: str = "t@h") -> str:
    result = _pinax(repo_root, "add", "--title", title, "--actor", actor, "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["item_id"]


def _log_dir(repo_root: str) -> str:
    return os.path.join(repo_root, ".ergon", "log")


def _read_shards(repo_root: str) -> dict[str, bytes]:
    log_dir = _log_dir(repo_root)
    out = {}
    for name in sorted(os.listdir(log_dir)):
        if name.endswith(".jsonl"):
            with open(os.path.join(log_dir, name), "rb") as fh:
                out[name] = fh.read()
    return out


def _log_tracking(repo_root: str) -> dict:
    """Return log-tracking data from the production CLI."""
    result = _pinax(repo_root, "doctor", "--json", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS)
    return json.loads(result.stdout)["log_tracking"]


def _break_log_tracking(repo_root: str) -> None:
    """
    Simulate a blanket root '*.jsonl' gitignore
    rule with the nested '.ergon/.gitignore' negation missing (the state a
    pre-fix / broken repo would be in), then commit it so the working tree
    is clean and any doctor/verify failure is attributable to the swallow
    alone, not to uncommitted or drifted files.
    """
    with open(os.path.join(repo_root, ".gitignore"), "w", newline="\n") as fh:
        fh.write("*.jsonl\n")
    nested = os.path.join(repo_root, ".ergon", ".gitignore")
    if os.path.isfile(nested):
        os.remove(nested)
    _git(repo_root, "add", "-A")
    # --no-verify: the repo's own Pinax pre-commit hook runs 'pinax verify',
    # which (correctly) refuses a commit that leaves the log swallowed. This
    # helper exists to construct that broken state as a fait accompli (as if
    # it arrived via a pre-guard install or an external clone) so doctor and
    # verify can be exercised against it — bypassing the hook here is the
    # test fixture deliberately creating the failure condition, not evidence
    # against the hook.
    _git(repo_root, "commit", "--no-verify",
         "-m", "break: blanket *.jsonl gitignore, drop nested negation")


@pytest.fixture()
def repo(tmp_path):
    """main with .ergon initialised and committed; clean working tree."""
    root = str(tmp_path)
    _init_repo(root)
    result = _pinax(root, "init")
    assert result.returncode == 0, result.stderr
    _commit_all(root, "init: pinax ergon base")
    return root


# ---------------------------------------------------------------------------
# (1) uncommitted shard events
# ---------------------------------------------------------------------------

@requires_git
def test_clean_repo_no_findings(repo):
    _add_item(repo, "committed item")
    _commit_all(repo, "item committed")

    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["findings"] == 0
    assert report["uncommitted"]["available"] is True
    assert report["uncommitted"]["files"] == []
    assert report["uncommitted"]["events"] == []
    assert report["stale_claims"] == []

    human = _pinax(repo, "doctor", "--now", _FAR_FUTURE,
                   "--stale-hours", _HUGE_HOURS)
    assert human.returncode == 0
    assert "no findings" in human.stdout


@requires_git
def test_uncommitted_shard_events_detected(repo):
    item_id = _add_item(repo, "orphaned item")  # NOT committed

    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["findings"] >= 1

    files = report["uncommitted"]["files"]
    assert any(f["path"].startswith(".ergon/log/") for f in files)

    events = report["uncommitted"]["events"]
    assert len(events) == 1
    assert events[0]["type"] == "item.created"
    assert events[0]["item_id"] == item_id
    assert events[0]["shard"].startswith(".ergon/log/")

    human = _pinax(repo, "doctor", "--now", _FAR_FUTURE,
                   "--stale-hours", _HUGE_HOURS)
    assert human.returncode == 1
    assert "uncommitted" in human.stdout
    assert "item.created" in human.stdout


# ---------------------------------------------------------------------------
# (2) stale claims (claim-without-done)
# ---------------------------------------------------------------------------

@requires_git
def test_stale_claim_detected_and_threshold_respected(repo):
    item_id = _add_item(repo, "claimed then abandoned")
    r = _pinax(repo, "claim", item_id, "--actor", "worker@example.test")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "claimed")

    # Against a far-future now, the claim is years old — stale at 24h default.
    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    claims = report["stale_claims"]
    assert len(claims) == 1
    assert claims[0]["item_id"] == item_id
    assert claims[0]["owner"] == "worker@example.test"
    assert claims[0]["age_hours"] > 24

    # Same repo, huge threshold — not stale, no findings.
    result2 = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                     "--stale-hours", _HUGE_HOURS)
    assert result2.returncode == 0
    assert json.loads(result2.stdout)["stale_claims"] == []


@requires_git
def test_stale_claim_excludes_settled_items(repo):
    done_id = _add_item(repo, "claimed then done")
    parked_id = _add_item(repo, "claimed then parked")
    r = _pinax(repo, "claim", done_id, "--actor", "a@h")
    assert r.returncode == 0, r.stderr
    r = _pinax(repo, "claim", parked_id, "--actor", "a@h")
    assert r.returncode == 0, r.stderr

    briefing = os.path.join(repo, "briefing.txt")
    with open(briefing, "w", newline="\n") as fh:
        fh.write("done properly\n")
    r = _pinax(repo, "done", done_id, "--briefing", briefing, "--actor", "a@h")
    assert r.returncode == 0, r.stderr
    r = _pinax(repo, "park", parked_id, "--reason", "later", "--actor", "a@h")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "settled items")
    os.remove(briefing)
    _commit_all(repo, "drop briefing file")

    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["stale_claims"] == []
    assert report["findings"] == 0


# ---------------------------------------------------------------------------
# (3) legacy-board frontmatter cross-check
# ---------------------------------------------------------------------------

def _write_legacy(board_dir: str, name: str, legacy_id: str, status: str) -> None:
    os.makedirs(board_dir, exist_ok=True)
    with open(os.path.join(board_dir, name), "w", newline="\n") as fh:
        fh.write(f"---\nid: {legacy_id}\ntitle: legacy entry\n"
                 f"status: {status}\n---\n\n## Briefing\n")


@requires_git
def test_legacy_contradiction_flagged_only_on_doneness(repo):
    queued_id = _add_item(repo, "migrated, still open in pinax")
    done_id = _add_item(repo, "migrated, done in pinax")
    briefing = os.path.join(repo, "b.txt")
    with open(briefing, "w", newline="\n") as fh:
        fh.write("closed\n")
    r = _pinax(repo, "done", done_id, "--briefing", briefing, "--actor", "a@h")
    assert r.returncode == 0, r.stderr
    os.remove(briefing)

    legacy_dir = os.path.join(repo, "legacy-board")
    # Contradiction: legacy says done, pinax says queued (case-insensitive id).
    _write_legacy(legacy_dir, "one.md", queued_id.upper(), "done")
    # Agreement on done-ness: no contradiction.
    _write_legacy(legacy_dir, "two.md", done_id, "done")
    # Vocabulary drift only ('todo' vs 'queued'): both not-done — no flag.
    _write_legacy(legacy_dir, "three.md", queued_id, "todo")
    # Unknown id: not a migrated item — ignored.
    _write_legacy(legacy_dir, "four.md", "zzz-unknown", "done")
    _commit_all(repo, "state with legacy board")

    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS,
                    "--legacy-board", legacy_dir)
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["legacy"]["checked"] is True
    contradictions = report["legacy"]["contradictions"]
    assert len(contradictions) == 1
    assert contradictions[0]["item_id"] == queued_id
    assert contradictions[0]["legacy_status"] == "done"
    assert contradictions[0]["pinax_status"] == "queued"
    assert contradictions[0]["file"].endswith("one.md")


@requires_git
def test_legacy_skipped_when_absent(repo):
    result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["legacy"]["checked"] is False
    assert report["legacy"]["contradictions"] == []


# ---------------------------------------------------------------------------
# (4) --reconcile guided action
# ---------------------------------------------------------------------------

@requires_git
def test_reconcile_commits_orphaned_shards_without_touching_bytes(repo):
    _add_item(repo, "orphaned trail")  # uncommitted
    shards_before = _read_shards(repo)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _pinax(repo, "doctor", "--reconcile", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS, stdin="y\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "committed" in result.stdout

    # Working tree clean under .ergon — the orphaned trail is now committed.
    status = _git(repo, "status", "--porcelain", "--", ".ergon").stdout.strip()
    assert status == ""
    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after != head_before

    # Append-only preserved: the commit changed NOTHING in the shard bytes.
    assert _read_shards(repo) == shards_before


@requires_git
def test_reconcile_declined_leaves_everything_in_place(repo):
    _add_item(repo, "orphaned trail")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = _pinax(repo, "doctor", "--reconcile", "--now", _FAR_FUTURE,
                    "--stale-hours", _HUGE_HOURS, stdin="n\n")
    assert result.returncode == 0, result.stdout + result.stderr

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    status = _git(repo, "status", "--porcelain", "--", ".ergon").stdout.strip()
    assert status != ""  # still uncommitted


@requires_git
def test_reconcile_prompts_done_park_skip_on_stale_claims(repo):
    id_a = _add_item(repo, "stale a")
    id_b = _add_item(repo, "stale b")
    id_c = _add_item(repo, "stale c")
    for iid in (id_a, id_b, id_c):
        r = _pinax(repo, "claim", iid, "--actor", "worker@example.test")
        assert r.returncode == 0, r.stderr
    _commit_all(repo, "three stale claims")  # class 1 empty -> no commit prompt

    # Prompts arrive sorted by item id: map answers accordingly.
    answers = {
        sorted([id_a, id_b, id_c])[0]: "d\nfinished offline, evidence in repo\n",
        sorted([id_a, id_b, id_c])[1]: "p\nsession died mid-build\n",
        sorted([id_a, id_b, id_c])[2]: "s\n",
    }
    stdin = "".join(answers[iid] for iid in sorted([id_a, id_b, id_c]))

    result = _pinax(repo, "doctor", "--reconcile", "--now", _FAR_FUTURE,
                    "--actor", "operator@example.test", stdin=stdin)
    assert result.returncode == 0, result.stdout + result.stderr

    state = fold(_log_dir(repo))
    items = state["items"]
    first, second, third = sorted([id_a, id_b, id_c])
    assert items[first]["status"] == "done"
    assert items[first]["briefing"] == "finished offline, evidence in repo"
    assert items[second]["status"] == "parked"
    assert items[second]["park_reason"] == "session died mid-build"
    # Skipped item untouched — still claimed, not done/parked.
    assert items[third]["status"] not in ("done", "parked")
    assert items[third]["owner"] == "worker@example.test"

    # Resolutions went through the normal event path with provenance, authored
    # by the operator, and are integrity-valid (ids verify on re-fold).
    from pinax.fold import read_events
    events = read_events(_log_dir(repo))
    resolved = [e for e in events
                if e.get("payload", {}).get("source") == "pinax-doctor"]
    assert len(resolved) == 2
    assert {e["type"] for e in resolved} == {"item.completed", "item.parked"}
    for e in resolved:
        assert e["actor"] == "operator@example.test"
        assert e["payload"]["stale_owner"] == "worker@example.test"

    # A second doctor pass sees no stale claims for the two resolved items.
    again = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    report = json.loads(again.stdout)
    stale_ids = {c["item_id"] for c in report["stale_claims"]}
    assert first not in stale_ids
    assert second not in stale_ids
    assert third in stale_ids


@requires_git
def test_reconcile_eof_takes_safe_default(repo):
    item_id = _add_item(repo, "stale, unattended")
    r = _pinax(repo, "claim", item_id, "--actor", "worker@example.test")
    assert r.returncode == 0, r.stderr
    _commit_all(repo, "stale claim")

    # No stdin at all: every prompt EOFs -> safe defaults, exit 0, no change.
    result = _pinax(repo, "doctor", "--reconcile", "--now", _FAR_FUTURE,
                    stdin="")
    assert result.returncode == 0, result.stdout + result.stderr
    state = fold(_log_dir(repo))
    assert state["items"][item_id]["status"] not in ("done", "parked")


# ---------------------------------------------------------------------------
# (5) JSON determinism + flag validation
# ---------------------------------------------------------------------------

@requires_git
def test_json_deterministic_and_ascii(repo):
    item_id = _add_item(repo, "titulo apendice éé")  # non-ASCII title
    r = _pinax(repo, "claim", item_id, "--actor", "a@h")
    assert r.returncode == 0, r.stderr

    first = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    second = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE)
    assert first.stdout == second.stdout
    assert first.stdout.isascii()


@requires_git
def test_reconcile_rejects_json(repo):
    result = _pinax(repo, "doctor", "--reconcile", "--json")
    assert result.returncode == 1
    assert "not combinable" in result.stderr


@requires_git
def test_bad_now_rejected(repo):
    result = _pinax(repo, "doctor", "--now", "yesterday-ish")
    assert result.returncode == 1
    assert "--now" in result.stderr


# ---------------------------------------------------------------------------
# gitignore-swallow guard, which shipped verified by manual CLI runs only.
#
# (a) 'pinax init' writes the nested .ergon/.gitignore negation on a fresh
#     init, AND heals a broken/pre-existing repo on re-init when a blanket
#     root '*.jsonl' gitignore would otherwise swallow the log directory.
# (b) 'pinax doctor' (class-4) and 'pinax verify' FAIL LOUDLY (exit 1) when
#     the log is git-ignored, and fail SAFE (no crash, no false claim of
#     cleanliness — 'unavailable' surfaces instead) when git itself is
#     unresolvable on PATH.
# (c) state-changing commands (add/claim/done/park/...) warn once on stderr,
#     non-blocking, when the log is ignored — they never abort the state
#     change; that hard-fail is doctor/verify's job alone.
# ---------------------------------------------------------------------------

# --- (a) init: nested .gitignore negation ----------------------------------

@requires_git
def test_init_writes_nested_gitignore_negation_on_fresh_init(tmp_path):
    root = str(tmp_path)
    _init_repo(root)

    result = _pinax(root, "init")
    assert result.returncode == 0, result.stderr

    gitignore_path = os.path.join(root, ".ergon", ".gitignore")
    assert os.path.isfile(gitignore_path)
    with open(gitignore_path, "r", encoding="utf-8") as fh:
        content = fh.read()
    assert "!/log/*.jsonl" in content

    gitattributes_path = os.path.join(root, ".ergon", ".gitattributes")
    with open(gitattributes_path, "r", encoding="utf-8") as fh:
        gat_content = fh.read()
    assert "merge=union" in gat_content

    # A brand new repo (no prior blanket gitignore trap) is never swallowed.
    tracking = _log_tracking(root)
    assert tracking["available"] is True
    assert tracking["ignored"] is False


@requires_git
def test_init_heals_preexisting_blanket_jsonl_gitignore_on_reinit(tmp_path):
    root = str(tmp_path)
    _init_repo(root)

    with open(os.path.join(root, ".gitignore"), "w", newline="\n") as fh:
        fh.write("*.jsonl\n")
    _git(root, "add", ".gitattributes", ".gitignore")
    _git(root, "commit", "-m", "repo with a pre-existing blanket *.jsonl gitignore")

    result = _pinax(root, "init")
    assert result.returncode == 0, result.stderr

    nested_gitignore = os.path.join(root, ".ergon", ".gitignore")
    assert os.path.isfile(nested_gitignore)

    # but the healing negation is missing — reproduce the swallow.
    os.remove(nested_gitignore)
    broken = _log_tracking(root)
    assert broken["available"] is True
    assert broken["ignored"] is True, (
        "removing the nested negation against a blanket root *.jsonl rule "
        "should reproduce the ignored-log condition"
    )

    # Re-running init heals it unconditionally — no flag, no special mode.
    heal = _pinax(root, "init")
    assert heal.returncode == 0, heal.stderr
    assert os.path.isfile(nested_gitignore)

    healed = _log_tracking(root)
    assert healed["available"] is True
    assert healed["ignored"] is False, "re-init must heal the swallow"


# --- (b) doctor / verify fail loud on swallow, fail safe without git -------

@requires_git
def test_doctor_and_verify_fail_loudly_when_log_ignored(repo):
    _add_item(repo, "an item")
    _commit_all(repo, "item committed")

    # Sanity: clean, tracked repo passes both before we break it.
    pre_doctor = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                        "--stale-hours", _HUGE_HOURS)
    assert pre_doctor.returncode == 0, pre_doctor.stdout + pre_doctor.stderr
    pre_verify = _pinax(repo, "verify")
    assert pre_verify.returncode == 0, pre_verify.stdout + pre_verify.stderr

    _break_log_tracking(repo)

    doctor_result = _pinax(repo, "doctor", "--json", "--now", _FAR_FUTURE,
                           "--stale-hours", _HUGE_HOURS)
    assert doctor_result.returncode == 1, doctor_result.stdout + doctor_result.stderr
    report = json.loads(doctor_result.stdout)
    assert report["log_tracking"]["available"] is True
    assert report["log_tracking"]["ignored"] is True
    assert report["findings"] >= 1

    doctor_human = _pinax(repo, "doctor", "--now", _FAR_FUTURE,
                          "--stale-hours", _HUGE_HOURS)
    assert doctor_human.returncode == 1
    assert "[4]" in doctor_human.stdout
    assert "FAIL" in doctor_human.stdout

    verify_result = _pinax(repo, "verify")
    assert verify_result.returncode == 1, verify_result.stdout + verify_result.stderr
    assert "SWALLOWED BY GITIGNORE" in verify_result.stderr.upper()


@requires_git
def test_doctor_and_verify_fail_safe_when_git_unavailable(repo):
    env = _env_without_git()
    if env is None:
        pytest.skip(
            "could not hermetically construct a PATH lacking git on this "
            "machine — skipping the git-unavailable fail-safe check"
        )

    _add_item(repo, "an item")  # created with git still available

    doctor_result = subprocess.run(
        [sys.executable, "-m", "pinax", "doctor", "--json", "--now", _FAR_FUTURE,
         "--stale-hours", _HUGE_HOURS],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert "Traceback" not in doctor_result.stderr, (
        "doctor must not crash when git is unavailable:\n" + doctor_result.stderr
    )
    report = json.loads(doctor_result.stdout)
    assert report["log_tracking"]["available"] is False
    assert report["log_tracking"]["ignored"] is False
    assert report["uncommitted"]["available"] is False
    # No git means neither class can positively confirm cleanliness — the
    # guard must surface that as "unavailable", never silently claim OK.
    assert doctor_result.returncode == 0  # no other findings; not a crash-driven 1

    doctor_human = subprocess.run(
        [sys.executable, "-m", "pinax", "doctor", "--now", _FAR_FUTURE,
         "--stale-hours", _HUGE_HOURS],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert "UNAVAILABLE" in doctor_human.stdout

    verify_result = subprocess.run(
        [sys.executable, "-m", "pinax", "verify"],
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert "Traceback" not in verify_result.stderr, (
        "verify must not crash when git is unavailable:\n" + verify_result.stderr
    )
    assert verify_result.returncode == 0
    assert "OK" in verify_result.stdout


# --- (c) warn-once, non-blocking, in state-changing commands ---------------

@requires_git
def test_state_changing_commands_silent_when_log_tracked(repo):
    result = _pinax(repo, "add", "--title", "normal", "--actor", "a@h", "--json")
    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr


@requires_git
def test_state_changing_commands_warn_once_but_never_abort_when_log_ignored(repo):
    _break_log_tracking(repo)

    add_result = _pinax(repo, "add", "--title", "still works", "--actor", "a@h", "--json")
    assert add_result.returncode == 0, add_result.stderr
    assert add_result.stderr.count("pinax: WARNING") == 1
    assert "gitignore" in add_result.stderr.lower()
    item_id = json.loads(add_result.stdout)["item_id"]

    claim_result = _pinax(repo, "claim", item_id, "--actor", "a@h")
    assert claim_result.returncode == 0, claim_result.stderr
    assert claim_result.stderr.count("pinax: WARNING") == 1

    briefing = os.path.join(repo, "briefing.txt")
    with open(briefing, "w", newline="\n") as fh:
        fh.write("done despite the swallow\n")
    done_result = _pinax(repo, "done", item_id, "--briefing", briefing, "--actor", "a@h")
    assert done_result.returncode == 0, done_result.stderr
    assert done_result.stderr.count("pinax: WARNING") == 1
    os.remove(briefing)

    state = fold(_log_dir(repo))
    assert state["items"][item_id]["status"] == "done", (
        "the warn-once guard must never block the state-changing command it decorates"
    )

    park_id = _add_item(repo, "will be parked despite the swallow")
    park_result = _pinax(repo, "park", park_id, "--reason", "later", "--actor", "a@h")
    assert park_result.returncode == 0, park_result.stderr
    assert park_result.stderr.count("pinax: WARNING") == 1

    state2 = fold(_log_dir(repo))
    assert state2["items"][park_id]["status"] == "parked"
