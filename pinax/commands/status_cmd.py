"""
pinax status [--json] [--repo <path>|--portfolio]
pinax status <id> <state> [--actor ...] [--json]

Zero positional args render the live status view.  Two positional args keep
the historical setter form: append item.status_changed and regenerate the
projection.  Exactly one positional arg is an explicit usage error.
"""

from __future__ import annotations

import datetime
import json
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import read_events
from ..statusview import status_view

_VALID_STATES = frozenset({
    "queued",
    "ready",
    "building",
    "blind-verify",
    "adjudicate",
    "done",
    "blocked",
    "parked",
})


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _set_status(
    repo_root: str,
    item_id: str,
    new_status: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """
    Historical setter form: append item.status_changed in repo_root.

    This output path is intentionally kept byte-compatible with the previous
    command for successful calls.
    """
    import os
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    if new_status not in _VALID_STATES:
        print(
            f"pinax: unknown status '{new_status}'. "
            f"Valid: {', '.join(sorted(_VALID_STATES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0

    _actor = actor or _default_actor()
    ts = _utc_now_iso()

    actor_events = [e for e in events if e.get("actor") == _actor]
    prev = actor_events[-1]["id"] if actor_events else ""

    payload = {"item_id": item_id, "status": new_status}
    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="item.status_changed",
        payload=payload,
        prev=prev,
    )
    append_event(log_dir, event, actor=_actor)

    from ..projection import regenerate
    regenerate(repo_root)

    from ..doctor import warn_if_log_ignored
    warn_if_log_ignored(repo_root)

    result = {
        "item_id": item_id,
        "status": new_status,
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
        "type": "item.status_changed",
    }

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        print(f"pinax: item {item_id} status -> {new_status} (by {_actor})")
        print(f"       event_id={event['id'][:12]}... seq={next_seq}")


def _print_repo(repo: dict, *, indent: str = "") -> None:
    branch = f" @ {repo.get('branch')}" if repo.get("branch") else ""
    print(f"{indent}repo: {repo.get('id', 'repo')}{branch}")

    if not repo.get("initialised", True):
        print(f"{indent}  not initialised: .ergon/log not found")
        return

    building = repo.get("building", [])
    print(f"{indent}  building ({len(building)}):")
    if building:
        for item in building:
            owner = item.get("owner") or "unowned"
            since = item.get("since") or "?"
            print(
                f"{indent}    {item['id']}  {item.get('title', '')}  "
                f"[{item.get('stage', '')}; {owner}; since {since}]"
            )
    else:
        print(f"{indent}    (none)")

    shipped = repo.get("shipped_recent", [])
    earlier = repo.get("shipped_earlier_count", 0)
    print(f"{indent}  shipped ({len(shipped)}):")
    if shipped:
        for item in shipped:
            done_at = item.get("done_at") or "?"
            print(f"{indent}    {item['id']}  {item.get('title', '')}  done {done_at}")
    else:
        print(f"{indent}    (none)")
    if earlier:
        print(f"{indent}    (+{earlier} earlier)")

    parked = repo.get("parked", [])
    print(f"{indent}  parked / blocked ({len(parked)}):")
    if parked:
        for item in parked:
            reason = item.get("reason") or "(no reason)"
            print(
                f"{indent}    {item['id']}  {item.get('title', '')}  "
                f"{item.get('kind', '')}: {reason}"
            )
    else:
        print(f"{indent}    (none)")

    print(f"{indent}  next:")
    next_item = repo.get("next")
    if next_item:
        print(f"{indent}    {next_item['id']}  {next_item.get('title', '')}")
    else:
        print(f"{indent}    (none - ready queue empty)")
    print(f"{indent}  ready queue: {repo.get('queue_depth', 0)} item(s)")

    notices = repo.get("notices", 0)
    if notices:
        print(
            f"{indent}  notices: {notices} claim-reconciliation notice(s) - "
            "run 'pinax doctor' for detail"
        )
    for warning in repo.get("warnings", []):
        print(f"{indent}  warning: {warning}")


def _print_status(payload: dict) -> None:
    print("pinax status")
    print()
    if payload.get("scope") == "repo":
        _print_repo(payload["repo"])
        return

    repos = payload.get("repos", [])
    if not repos:
        for warning in payload.get("warnings", []):
            print(f"  {warning}")
        return

    for index, repo in enumerate(repos):
        if index:
            print()
        _print_repo(repo)


def run(
    repo_root: str,
    item_id: str | None = None,
    new_status: str | None = None,
    actor: str | None = None,
    as_json: bool = False,
    scope: str = "auto",
    since_days: int | None = 7,
    all_branches: bool = False,
) -> None:
    """Execute either the status view or the historical setter form."""
    if (item_id is None) != (new_status is None):
        print(
            "pinax status: use either 'pinax status [--json]' "
            "or 'pinax status <id> <state>'.",
            file=sys.stderr,
        )
        sys.exit(2)

    if item_id is not None and new_status is not None:
        if scope == "portfolio":
            print("pinax status: setter form cannot use --portfolio.", file=sys.stderr)
            sys.exit(2)
        _set_status(
            repo_root=repo_root,
            item_id=item_id,
            new_status=new_status,
            actor=actor,
            as_json=as_json,
        )
        return

    try:
        payload = status_view(
            repo_root=repo_root,
            scope=scope,
            since_days=since_days,
            all_branches=all_branches,
        )
    except ValueError as exc:
        print(f"pinax status: {exc}", file=sys.stderr)
        sys.exit(1)

    if as_json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    else:
        _print_status(payload)
