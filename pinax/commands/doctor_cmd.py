"""Console interface for read-only tracker diagnosis and guided reconciliation.

Default mode reports uncommitted event shards, stale claims, legacy-board
contradictions, and ignored event-log paths. `--reconcile` offers normal Git
commits for uncommitted shards and append-only completion or park actions for
stale claims. JSON output is deterministic for identical repository state and
reference time.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

from ..append import append_event
from ..doctor import DEFAULT_STALE_HOURS, diagnose, parse_ts
from ..event import mint_event
from ..fold import read_events

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _utc_now() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _prompt(text: str) -> str | None:
    """input() wrapper: None on EOF/interrupt (caller takes the safe default)."""
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        print("")  # keep the console on a fresh line after an aborted prompt
        return None


def _print_report(repo_root: str, report: dict) -> None:
    """Human-readable, ASCII-only rendering of the diagnosis report."""
    print(f"pinax doctor: {repo_root}")
    print(f"  reference time: {report['now']}  stale threshold: "
          f"{report['stale_hours']}h")

    health = report.get("install_health", {})
    console = health.get("console", {})
    exe = health.get("executable") or "(not found)"
    editable = health.get("editable_install_target") or "(not detected)"
    print("  [0] install health:")
    print(f"        executable on PATH: {exe}")
    print(f"        hook resolution mode: {health.get('hook_resolution_mode', 'unknown')}")
    print(
        "        console encoding: "
        f"stdout={console.get('stdout_encoding')} errors={console.get('stdout_errors')}; "
        f"stderr={console.get('stderr_encoding')} errors={console.get('stderr_errors')}; "
        f"replace guard={'yes' if console.get('replace_guard') else 'no'}"
    )
    print(f"        editable install target: {editable}")

    unc = report["uncommitted"]
    if not unc["available"]:
        print("  [1] uncommitted shard events: UNAVAILABLE (git not usable here)")
    elif not unc["files"]:
        print("  [1] uncommitted shard events: none")
    else:
        n_files = len(unc["files"])
        n_events = len(unc["events"])
        print(f"  [1] uncommitted shard events: {n_events} event(s) across "
              f"{n_files} uncommitted .ergon file(s)")
        for entry in unc["files"]:
            print(f"        {entry['path']} ({entry['state']})")
        for ev in unc["events"]:
            item = f" item={ev['item_id']}" if ev.get("item_id") else ""
            print(f"        event {ev['id'][:12]}... {ev['type']}{item} "
                  f"ts={ev['ts']} actor={ev['actor']}")

    claims = report["stale_claims"]
    if not claims:
        print("  [2] stale claims: none")
    else:
        print(f"  [2] stale claims (claim without done, older than "
              f"{report['stale_hours']}h): {len(claims)}")
        for c in claims:
            age = ("unparseable claimed_at" if c["age_hours"] is None
                   else f"{c['age_hours']}h old")
            print(f"        {c['item_id']} claimed by {c['owner']} at "
                  f"{c['claimed_at']} ({age}) status={c['status']}")

    legacy = report["legacy"]
    if not legacy["checked"]:
        print("  [3] legacy-board cross-check: skipped (no legacy board found; "
              "use --legacy-board PATH)")
    elif not legacy["contradictions"]:
        print(f"  [3] legacy-board cross-check ({legacy['path']}): "
              "no contradictions")
    else:
        print(f"  [3] legacy-board cross-check ({legacy['path']}): "
              f"{len(legacy['contradictions'])} contradiction(s)")
        for f in legacy["contradictions"]:
            print(f"        {f['item_id']}: legacy status '{f['legacy_status']}' "
                  f"vs pinax '{f['pinax_status']}' ({f['file']})")

    tracking = report.get("log_tracking", {})
    if not tracking.get("available"):
        print("  [4] log tracking (gitignore swallow check): UNAVAILABLE (git not usable here)")
    elif tracking.get("ignored"):
        print(
            f"  [4] log tracking (gitignore swallow check): FAIL - "
            f"{tracking.get('probe_path')} would be git-ignored right now"
        )
        print(
            "        a .gitignore rule is shadowing the event log shard "
            "directory -- new shard files will be silently local-only, "
            "never committed, invisible across worktrees/branches "
            "(the event log is ignored by Git)."
        )
        print(
            "        fix: run 'pinax init' again to (re)install the "
            ".ergon/.gitignore negation, or fix your repo's .gitignore."
        )
    else:
        n_disk = tracking.get("shards_on_disk", 0)
        n_tracked = tracking.get("shards_tracked", 0)
        print(
            f"  [4] log tracking (gitignore swallow check): OK "
            f"({n_tracked}/{n_disk} on-disk shard file(s) git-tracked)"
        )

    if report["findings"]:
        print(f"pinax doctor: {report['findings']} finding(s).")
    else:
        print("pinax doctor: no findings - tracker trails are clean.")


def _git(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_root, capture_output=True)


def _reconcile_uncommitted(repo_root: str, report: dict) -> None:
    """Guided fix, class 1: one ordinary git commit of the orphaned paths."""
    unc = report["uncommitted"]
    if not unc["available"] or not unc["files"]:
        return
    paths = [entry["path"] for entry in unc["files"]]
    n_events = len(unc["events"])
    answer = _prompt(
        f"doctor: commit {len(paths)} orphaned .ergon file(s) "
        f"({n_events} event(s)) as-is? [y/N] "
    )
    if answer is None or answer.strip().lower() not in ("y", "yes"):
        print("doctor: leaving uncommitted files in place.")
        return
    add = _git(repo_root, ["add", "--", *paths])
    if add.returncode != 0:
        print("doctor: git add failed: "
              + add.stderr.decode("utf-8", errors="replace").strip(),
              file=sys.stderr)
        return
    message = (f"pinax doctor: commit orphaned .ergon tracker trail "
               f"({len(paths)} file(s), {n_events} event(s))")
    commit = _git(repo_root, ["commit", "-m", message])
    if commit.returncode != 0:
        print("doctor: git commit failed: "
              + commit.stderr.decode("utf-8", errors="replace").strip(),
              file=sys.stderr)
        return
    print(f"doctor: committed {len(paths)} file(s).")


def _reconcile_stale_claims(
    repo_root: str,
    log_dir: str,
    report: dict,
    actor: str,
    now_iso: str,
) -> int:
    """
    Guided fix, class 2: prompt done/park/skip per stale claim; append
    resolutions through the normal event path.  Returns the number of
    events appended.
    """
    claims = report["stale_claims"]
    if not claims:
        return 0

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0
    actor_events = [e for e in events if e.get("actor") == actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    appended = 0
    for c in claims:
        item_id = c["item_id"]
        age = ("age unknown" if c["age_hours"] is None
               else f"{c['age_hours']}h old")
        answer = _prompt(
            f"doctor: {item_id} claimed by {c['owner']} ({age}, "
            f"status={c['status']}) -- [d]one / [p]ark / [s]kip: "
        )
        if answer is None:
            print("doctor: input ended - skipping remaining stale claims.")
            break
        choice = answer.strip().lower()
        if choice in ("d", "done"):
            briefing = _prompt(f"doctor: one-line briefing for {item_id}: ")
            if briefing is None or not briefing.strip():
                print(f"doctor: no briefing given - skipping {item_id}.")
                continue
            etype = "item.completed"
            payload = {
                "item_id": item_id,
                "briefing": briefing.strip(),
                "source": "pinax-doctor",
                "stale_owner": c["owner"],
            }
        elif choice in ("p", "park"):
            reason = _prompt(f"doctor: park reason for {item_id}: ")
            if reason is None or not reason.strip():
                print(f"doctor: no reason given - skipping {item_id}.")
                continue
            etype = "item.parked"
            payload = {
                "item_id": item_id,
                "reason": reason.strip(),
                "source": "pinax-doctor",
                "stale_owner": c["owner"],
            }
        else:
            print(f"doctor: skipped {item_id}.")
            continue

        event = mint_event(
            seq=next_seq,
            ts=now_iso,
            actor=actor,
            etype=etype,
            payload=payload,
            prev=prev,
        )
        append_event(log_dir, event, actor=actor)
        prev = event["id"]
        next_seq += 1
        appended += 1
        verb = "done" if etype == "item.completed" else "parked"
        print(f"doctor: {item_id} marked {verb} "
              f"(event_id={event['id'][:12]}... seq={event['seq']}).")

    if appended:
        from ..projection import regenerate
        regenerate(repo_root)
        print(f"doctor: appended {appended} event(s) via the normal event "
              "path; projection regenerated. Commit .ergon/ with your "
              "normal git flow.")
    return appended


def run(
    repo_root: str,
    stale_hours: float | None = None,
    legacy_board: str | None = None,
    now_iso: str | None = None,
    reconcile: bool = False,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """Execute pinax doctor in repo_root."""
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if reconcile and as_json:
        print("pinax doctor: --reconcile is prompt-driven and not combinable "
              "with --json.", file=sys.stderr)
        sys.exit(1)

    if now_iso is not None:
        now = parse_ts(now_iso)
        if now is None:
            print(f"pinax doctor: --now must be UTC ISO-8601 "
                  f"(YYYY-MM-DDTHH:MM:SSZ), got: {now_iso}", file=sys.stderr)
            sys.exit(1)
    else:
        now = _utc_now()

    threshold = stale_hours if stale_hours is not None else DEFAULT_STALE_HOURS

    report = diagnose(
        repo_root=repo_root,
        log_dir=log_dir,
        now=now,
        stale_hours=threshold,
        legacy_board=legacy_board,
    )

    if as_json:
        print(json.dumps(report, sort_keys=True, ensure_ascii=True))
        sys.exit(1 if report["findings"] else 0)

    _print_report(repo_root, report)

    if not reconcile:
        sys.exit(1 if report["findings"] else 0)

    # --- guided action -------------------------------------------------------
    _actor = actor or _default_actor()
    _reconcile_uncommitted(repo_root, report)
    _reconcile_stale_claims(
        repo_root, log_dir, report, _actor, now.strftime(_TS_FMT)
    )
    if report["legacy"]["contradictions"]:
        print("doctor: legacy-board contradictions are report-only - legacy "
              "files are frozen archives; correct the PINAX side with normal "
              "commands (done/park/status) if the legacy record is right.")
    sys.exit(0)
