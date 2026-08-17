"""Manage optional repository registry entries for portfolio discovery.

Registry entries record a local path, a remote URL, or both in the event log.
They supplement root scanning and provide stable identifiers for repositories
outside configured roots. Registry updates validate identifiers and regenerate
the local projection through the standard append path.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

from ..append import append_event
from ..event import mint_event
from ..fold import fold, read_events

_REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _utc_now_iso() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_actor() -> str:
    import socket
    return f"operator@{socket.gethostname()}"


def _validate_repo_id(repo_id: str) -> None:
    if not repo_id or not _REPO_ID_RE.match(repo_id):
        print(
            f"pinax registry: invalid repo id '{repo_id}'. "
            "Must match ^[a-z0-9][a-z0-9_-]*$ (lowercase slug, no path separators).",
            file=sys.stderr,
        )
        sys.exit(1)


def _next_seq_and_prev(log_dir: str, actor: str) -> tuple[int, str, list[dict]]:
    events = read_events(log_dir)
    next_seq = (max(e["seq"] for e in events) + 1) if events else 0
    actor_events = [e for e in events if e.get("actor") == actor]
    prev = actor_events[-1]["id"] if actor_events else ""
    return next_seq, prev, events


def run_add(
    repo_root: str,
    repo_id: str,
    path: str | None = None,
    actor: str | None = None,
    as_json: bool = False,
    url: str | None = None,
) -> None:
    """
    Execute pinax registry add in repo_root (the hub repo).

    At least one of `path` (local override for `pinax overview` discovery) or
    `url` (remote-manifest entry for `pinax overview --remote`) is
    required; an entry may carry both.  Nothing is appended on rejection
    (validate-before-append invariant).
    """
    _validate_repo_id(repo_id)

    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    url = url.strip() if url else None
    if not path and not url:
        print(
            "pinax registry: must supply --path <dir> and/or --url <git remote url>.",
            file=sys.stderr,
        )
        sys.exit(1)

    normalised_path: str | None = None
    if path:
        if not os.path.isdir(path):
            print(
                f"pinax registry: path '{path}' does not exist or is not a directory.",
                file=sys.stderr,
            )
            sys.exit(1)
        normalised_path = os.path.abspath(path).replace("\\", "/")

    payload: dict = {"repo_id": repo_id}
    if normalised_path:
        payload["path"] = normalised_path
    if url:
        payload["url"] = url

    _actor = actor or _default_actor()
    ts = _utc_now_iso()
    next_seq, prev, _events = _next_seq_and_prev(log_dir, _actor)

    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="registry.repo_added",
        payload=payload,
        prev=prev,
    )
    append_event(log_dir, event, actor=_actor)

    result = {
        "repo_id": repo_id,
        "path": normalised_path,
        "url": url,
        "operation": "add",
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
    }
    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        target_bits = [b for b in (normalised_path, f"url={url}" if url else None) if b]
        print(f"pinax: registry repo added: {repo_id} -> {' '.join(target_bits)}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq} actor={_actor}")


def run_rm(
    repo_root: str,
    repo_id: str,
    actor: str | None = None,
    as_json: bool = False,
) -> None:
    """Execute pinax registry rm in repo_root (the hub repo)."""
    _validate_repo_id(repo_id)

    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    _actor = actor or _default_actor()
    ts = _utc_now_iso()
    next_seq, prev, _events = _next_seq_and_prev(log_dir, _actor)

    event = mint_event(
        seq=next_seq,
        ts=ts,
        actor=_actor,
        etype="registry.repo_removed",
        payload={"repo_id": repo_id},
        prev=prev,
    )
    append_event(log_dir, event, actor=_actor)

    result = {
        "repo_id": repo_id,
        "operation": "remove",
        "event_id": event["id"],
        "seq": next_seq,
        "actor": _actor,
        "ts": ts,
    }
    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        print(f"pinax: registry repo removed: {repo_id}")
        print(f"       event_id={event['id'][:12]}... seq={next_seq} actor={_actor}")


def run_list(
    repo_root: str,
    as_json: bool = False,
) -> None:
    """
    Execute pinax registry list in repo_root — read-only fold, no writes.

    Lists the registered repos (excluding the hub itself, which `pinax
    overview` always includes implicitly regardless of registration).
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")
    if not os.path.isdir(log_dir):
        print("pinax: .ergon/log/ not found - run 'pinax init' first.", file=sys.stderr)
        sys.exit(1)

    state = fold(log_dir)
    registry: dict = state.get("registry", {})

    if as_json:
        print(json.dumps({"registry": registry}, sort_keys=True, ensure_ascii=True))
        return

    print("pinax registry")
    print()
    if not registry:
        print("  (no repos registered - 'pinax overview' still includes this hub repo)")
        return
    for repo_id in sorted(registry.keys()):
        entry = registry[repo_id]
        target_bits = []
        if entry.get("path"):
            target_bits.append(entry["path"])
        if entry.get("url"):
            target_bits.append(f"url={entry['url']}")
        print(f"  {repo_id:<20}  {' '.join(target_bits)}")
