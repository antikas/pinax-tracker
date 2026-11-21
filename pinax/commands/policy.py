"""
pinax policy claim-expiry --hours N [--actor ...] [--offline] [--json]
pinax policy process --mode flow|strict [--actor ...] [--offline] [--json]
pinax policy owner-gates --class TEXT [--class TEXT ...] [--actor ...] [--offline] [--json]
pinax policy decision-actors --decider ROLE@HOST [--decider ROLE@HOST ...] [--actor ...] [--offline] [--json]
pinax policy check [--json]

The setting commands each publish one repository-level event:

- claim-expiry: policy.claim_expiry_set, carrying the hours.
- process: policy.process_set, carrying flow or strict.
- owner-gates: policy.owner_gate_classes_set, carrying the whole list.
- decision-actors: policy.decision_actors_set, carrying the whole list of
  actors who may decide under strict. --actor stays the writer.

Each runs the publish sequence (pinax.sync) exactly as park and release
do: mint the event, then fetch (unless offline), fold, append, commit and
push. Refuses an actor handle that is not role@host before minting
anything; see pinax.sync for the exit codes and offline rules. Each event
names no item, so its commit subject is the event type alone.

A claim expiry applies to every claim from its own position in claim
order onward (ADR-007): the policy in force for a claim is the last such
event at or before that claim, and twenty-four hours when the log carries
none before it. Setting a policy today therefore never changes how a claim
already folded on any machine.

For the process and the class list, the last event of each in fold order
is the one in force, with flow and the built-in class list when the log
carries none (pinax.owner_gates owns the rule).

check is read-only: it folds the local log and lists the blocked items for
the owner that carry no valid owner-gate class, exiting 0 under flow and 1
under strict when it lists any.

Refusals, before anything is minted: a claim expiry that is not a positive
number of hours; a process that is neither flow nor strict; a class list
that is empty, holds an empty class or names one class twice; a decider
that is not role@host (exit 2). Nothing else
is checked here - the fold reads the event and decides.
"""

from __future__ import annotations

import json
import math
import os
import sys

from .. import refusal
from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored

STRICT_UPGRADE_WARNING = (
    "pinax: every clone, and every older pinax on PATH, must run 0.4.0 or later "
    "before this is used; an older client will report drift, and after "
    "upgrading, pinax verify --fix repairs it."
)

# pinax.sync imports pinax.projection, which imports pinax.commands.dep at
# its own top level; deferred to run() below rather than imported here, to
# stay clear of that chain the way pinax.commands.dep itself now must.


def run_claim_expiry(
    repo_root: str,
    hours: float,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax policy claim-expiry in repo_root.

    Hands a repository-level policy.claim_expiry_set event with the given
    hours to the publish sequence.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    refusal.require_tracker(log_dir)

    if not math.isfinite(hours) or hours <= 0:
        refusal.refuse(
            f"pinax: a claim expiry must be a positive number of hours, got "
            f"{hours!r}.",
            refusal.INVALID_ARGUMENT,
        )

    from .. import sync

    _actor = actor or default_actor()
    _hours = float(hours)

    outcome = sync.run_sequence(
        repo_root,
        event_type="policy.claim_expiry_set",
        payload={"hours": _hours},
        actor=_actor,
        ts=utc_now_iso(),
        # Repository-level: this policy is about the tracker, not an item.
        item_id="",
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = {
        "hours": _hours,
        "event_id": event["id"],
        "seq": event["seq"],
        "actor": _actor,
        "ts": event["ts"],
        "type": "policy.claim_expiry_set",
        "root": ergon_dir,
    }

    result.update(outcome.publication())
    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        # visible the moment it happens.
        print(
            f"pinax: claim expiry set to {_hours}h by {_actor} in {ergon_dir}"
        )
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")


def _require_log(ergon_dir: str) -> None:
    refusal.require_tracker(os.path.join(ergon_dir, "log"))


def _publish_policy(
    repo_root: str,
    event_type: str,
    payload: dict,
    actor: str | None,
    as_json: bool,
    offline: bool,
    runner,
    summary: str,
) -> None:
    """Publish one repository-level policy event and report it."""
    from .. import sync

    ergon_dir = os.path.join(repo_root, ".ergon")
    _actor = actor or default_actor()
    outcome = sync.run_sequence(
        repo_root,
        event_type=event_type,
        payload=payload,
        actor=_actor,
        ts=utc_now_iso(),
        item_id="",
        offline=offline,
        runner=runner,
    )
    sync.conclude(outcome)

    event = outcome.event
    warn_if_log_ignored(repo_root)

    result = dict(
        payload,
        event_id=event["id"],
        seq=event["seq"],
        actor=_actor,
        ts=event["ts"],
        type=event_type,
        root=ergon_dir,
    )
    result.update(outcome.publication())
    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    else:
        print(f"pinax: {summary} by {_actor} in {ergon_dir}")
        print(f"       event_id={event['id'][:12]}... seq={event['seq']}")


def run_process(
    repo_root: str,
    mode: str,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax policy process in repo_root.

    Hands a repository-level policy.process_set event to the publish
    sequence. Refuses, before anything is minted, a mode that is neither
    flow nor strict. After setting strict it prints STRICT_UPGRADE_WARNING
    (to stderr under --json, so the JSON line stays the only output).

    runner is injectable for tests only; the CLI passes none.
    """
    from .. import owner_gates

    ergon_dir = os.path.join(repo_root, ".ergon")
    _require_log(ergon_dir)
    if mode not in owner_gates.PROCESSES:
        refusal.refuse(
            f"pinax: the process must be one of {', '.join(owner_gates.PROCESSES)}, "
            f"got {mode!r}.",
            refusal.INVALID_ARGUMENT,
        )
    _publish_policy(
        repo_root, "policy.process_set", {"process": mode},
        actor, as_json, offline, runner, f"process set to {mode}",
    )
    if mode == owner_gates.STRICT:
        # Strict puts the class on the board and in item files, which a
        # client older than 0.3.0 does not render, so the warning repeats
        # the upgrade order here.
        print(STRICT_UPGRADE_WARNING, file=sys.stderr if as_json else sys.stdout)


def run_owner_gates(
    repo_root: str,
    classes: list[str],
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax policy owner-gates in repo_root.

    Hands a repository-level policy.owner_gate_classes_set event, carrying
    the whole list, to the publish sequence. Refuses, before anything is
    minted, an empty list, an empty class, or one class given twice.

    runner is injectable for tests only; the CLI passes none.
    """
    from .. import owner_gates

    ergon_dir = os.path.join(repo_root, ".ergon")
    _require_log(ergon_dir)
    cleaned = [" ".join(c.split()) for c in (classes or [])]
    if not cleaned or any(not c for c in cleaned):
        refusal.refuse(
            "pinax: give at least one owner-gate class, none of them empty.",
            refusal.INVALID_ARGUMENT,
        )
    seen = set()
    for entry in cleaned:
        key = owner_gates.normalise(entry)
        if key in seen:
            refusal.refuse(
                f"pinax: the owner-gate class '{entry}' is given twice.",
                refusal.INVALID_ARGUMENT,
            )
        seen.add(key)
    _publish_policy(
        repo_root, "policy.owner_gate_classes_set", {"classes": cleaned},
        actor, as_json, offline, runner,
        f"owner-gate classes set ({'; '.join(cleaned)})",
    )


def run_decision_actors(
    repo_root: str,
    deciders: list[str],
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax policy decision-actors in repo_root.

    Hands a repository-level policy.decision_actors_set event, carrying the
    whole list, to the publish sequence. Each decider is checked as an
    actor handle and stored with its host in lower case; a decider that is
    not role@host is refused with exit 2 before anything is minted. A
    decider given twice is kept once.

    runner is injectable for tests only; the CLI passes none.
    """
    from .. import sync
    from ..append import normalise_actor

    ergon_dir = os.path.join(repo_root, ".ergon")
    _require_log(ergon_dir)
    if not deciders:
        refusal.refuse(
            "pinax: give at least one --decider.",
            refusal.INVALID_ARGUMENT,
            sync.EXIT_INVALID_ACTOR,
        )
    listed: list[str] = []
    for entry in deciders:
        invalid = sync.invalid_actor_message(entry)
        if invalid is not None:
            refusal.refuse(
                f"pinax: the decider {entry!r} is not a valid handle, so nothing "
                "was appended; every decider must be written as role@host.",
                refusal.INVALID_ARGUMENT,
                sync.EXIT_INVALID_ACTOR,
            )
        normalised = normalise_actor(entry)
        if normalised not in listed:
            listed.append(normalised)
    _publish_policy(
        repo_root, "policy.decision_actors_set", {"actors": listed},
        actor, as_json, offline, runner,
        f"decision actors set ({'; '.join(listed)})",
    )


def run_check(repo_root: str, as_json: bool = False) -> None:
    """
    Execute pinax policy check in repo_root.

    Read-only: folds the local log and lists every blocked item for the
    owner that carries no valid owner-gate class. Exits 0 under flow
    whatever it finds; under strict, exits 1 when it lists any item.
    """
    from .. import owner_gates
    from ..fold import fold

    ergon_dir = os.path.join(repo_root, ".ergon")
    _require_log(ergon_dir)
    state = fold(os.path.join(ergon_dir, "log"))
    process = owner_gates.process_mode(state)
    listed = list(owner_gates.classes(state))
    found = owner_gates.unclassed_blocked(state)
    failing = process == owner_gates.STRICT and bool(found)

    if as_json:
        print(json.dumps(
            {"process": process, "classes": listed, "unclassed": found, "ok": not failing},
            sort_keys=True, ensure_ascii=True,
        ))
    else:
        if not found:
            print(f"pinax: process {process}; every blocked item for the owner names a valid owner-gate class.")
        else:
            print(f"pinax: process {process}; {len(found)} blocked item(s) for the owner name no valid owner-gate class:")
            for entry in found:
                given = entry["owner_gate"]
                why = f"'{given}' is not on the list" if given else "no class"
                print(f"  - {entry['id']} - {entry['title']} - gate={entry['gate']} - {why}")
            print(f"  Owner-gate classes: {'; '.join(listed)}.")
    if failing:
        sys.exit(1)
