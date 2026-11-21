"""
pinax decide <id> --option <key> --label <text> --effect release|park
             --reason <text> [--actor ...] [--offline] [--json]

Records a decision on an item as one item.decided event holding the option,
its label, the effect and the reason; the actor and the time are the
event's own. A decision is never a note: an item's newest note is read as
its brief elsewhere, and a decision must not replace it.

- release: only item.decided is appended. The item's status, gate,
  owner-gate class and claim do not change, and the command says so.
- park: item.decided and then an ordinary item.parked with the same reason
  are appended in one commit, so the fold parks the item exactly as
  pinax park does.

Runs the publish sequence (pinax.sync) once for the command. Refusals,
before anything is minted: a blank option, label or reason (exit 2), an
unknown item (exit 1), an actor that is not role@host (exit 2), and under
the strict process an actor not on the decision-actor list or no list at
all (exit 8, see pinax.owner_gates).

A repeat is harmless and finishes what an earlier run left undone. When
the latest decision on the item in the union of the working-tree log and
the remote default branch's committed shards has the same option, label,
effect, reason and actor, and for park the item is parked in that same
fold, nothing is appended. When the remote default branch already records
it the command exits 0 with sync status already_published. Otherwise the
publish sequence commits the existing events when they sit uncommitted
(after a refused commit), and pushes when HEAD holds commits the remote
lacks (after a rejected push), ending as a fresh decision would. The JSON
result's published key says whether the decision is on the remote.
"""

from __future__ import annotations

import json
import os
import sys

from .. import refusal
from ..doctor import default_actor, utc_now_iso, warn_if_log_ignored
from ..event import DECISION_EFFECTS, ITEM_DECIDED, decision_payload_problem

EXIT_BLANK_VALUE = 2


def _status_text(status: str, gate: str) -> str:
    return f"{status} (gate {gate})" if gate else status


def _matches(decision: object, wanted: dict, actor: str) -> bool:
    from ..append import normalise_actor

    if not isinstance(decision, dict):
        return False
    for key in ("option", "label", "effect", "reason"):
        if decision.get(key) != wanted[key]:
            return False
    recorded = decision.get("actor")
    return isinstance(recorded, str) and normalise_actor(recorded) == normalise_actor(actor)


def run(
    repo_root: str,
    item_id: str,
    option: str,
    label: str,
    effect: str,
    reason: str,
    actor: str | None = None,
    as_json: bool = False,
    offline: bool = False,
    runner=None,
) -> None:
    """
    Execute pinax decide in repo_root.

    runner is injectable for tests only; the CLI passes none.
    """
    ergon_dir = os.path.join(repo_root, ".ergon")
    log_dir = os.path.join(ergon_dir, "log")

    refusal.require_tracker(log_dir)

    payload = {
        "item_id": item_id,
        "option": option,
        "label": label,
        "effect": effect,
        "reason": reason,
    }
    problem = decision_payload_problem(payload)
    if problem is not None:
        if effect not in DECISION_EFFECTS:
            problem = f"the effect must be one of {', '.join(DECISION_EFFECTS)}, got {effect!r}"
        refusal.refuse(
            f"pinax: {problem}, so nothing was appended.",
            refusal.INVALID_ARGUMENT,
            EXIT_BLANK_VALUE,
        )

    from .. import owner_gates, sync
    from ..fold import fold

    _actor = actor or default_actor()
    follow_on = ()
    if effect == "park":
        follow_on = (("item.parked", {"item_id": item_id, "reason": reason}),)

    def already_recorded(folded: dict) -> bool:
        item = folded.get("items", {}).get(item_id)
        if not isinstance(item, dict) or not _matches(item.get("decision"), payload, _actor):
            return False
        return effect != "park" or item.get("status") == "parked"

    outcome = sync.run_sequence(
        repo_root,
        event_type=ITEM_DECIDED,
        payload=payload,
        actor=_actor,
        ts=utc_now_iso(),
        item_id=item_id,
        offline=offline,
        policy_check=lambda state: owner_gates.decide_refusal(state, _actor),
        follow_on=follow_on,
        already_recorded=already_recorded,
        runner=runner,
    )
    sync.conclude(outcome)

    appended = bool(outcome.events)
    if appended:
        warn_if_log_ignored(repo_root)
        state = fold(log_dir)
    else:
        state = outcome.state
    item = state.get("items", {}).get(item_id, {})
    status = item.get("status", "")
    gate = item.get("gate", "") if status == "blocked" else ""

    result = {
        "item_id": item_id,
        "option": option,
        "label": label,
        "effect": effect,
        "reason": reason,
        "actor": _actor,
        "appended": appended,
        "status": status,
        "gate": gate,
        "root": ergon_dir,
        "type": ITEM_DECIDED,
    }
    if appended:
        decided = outcome.events[0]
        result["decided_event_id"] = decided["id"]
        result["seq"] = decided["seq"]
        result["ts"] = decided["ts"]
        if effect == "park":
            result["parked_event_id"] = outcome.events[1]["id"]
    result.update(outcome.publication())

    if as_json:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return
    if not appended:
        print(
            f"pinax: decision already recorded on {item_id}; nothing appended; "
            f"status: {_status_text(status, gate)}"
        )
        return
    if effect == "park":
        print(f"pinax: decision recorded on {item_id}; item parked (reason={reason!r}) by {_actor}")
        print(
            f"       decided_event_id={result['decided_event_id'][:12]}... "
            f"parked_event_id={result['parked_event_id'][:12]}..."
        )
    else:
        print(
            f"pinax: decision recorded on {item_id}; status unchanged: "
            f"{_status_text(status, gate)}"
        )
        print(f"       decided_event_id={result['decided_event_id'][:12]}... seq={result['seq']}")
