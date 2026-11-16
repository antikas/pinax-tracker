"""
pinax.owner_gates - the one owner of the owner-gate class rule.

A project chooses its process with a repository-level policy event:

- flow (the default): a block for the owner needs no class. A class given
  with `pinax block --owner-gate` is recorded as written and never checked.
- strict: a block for the owner must name an owner-gate class from the
  project's list. A block with no class, or with a class not on the list,
  is refused before anything is appended.

A block is for the owner when its gate is one of OWNER_GATES: `decision`
(the owner must decide) and `destructive` (the owner must approve an
irreversible act). `scope` parks a finding in the backlog and `proposal`
holds a proposed item for review; neither waits on an owner gate, so
neither needs a class.

The list of classes is held in the project policy too. A log that sets no
list runs on DEFAULT_CLASSES.

Everything here is pure: it reads fold state and returns values or
messages. The commands and the publish sequence ask it; none of them keeps
its own copy of the rule.
"""

from __future__ import annotations

from typing import Iterable

FLOW = "flow"
STRICT = "strict"
PROCESSES = (FLOW, STRICT)
DEFAULT_PROCESS = FLOW

# The gates that put a blocked item in front of the owner.
OWNER_GATES = frozenset({"decision", "destructive"})

# The owner-gate classes a project runs on until a policy event sets others.
DEFAULT_CLASSES = (
    "production deployment",
    "publication",
    "real child or school data",
    "irreversible or destructive act",
    "material change of scope, cost or architecture",
    "login-bound act or owner-held input",
    "start signal for a material plan",
)


def normalise(value: str) -> str:
    """Case and whitespace folded, so a class matches however it is typed."""
    return " ".join(value.split()).casefold()


def process_mode(state: dict) -> str:
    """The process the folded log runs on: the last policy event's, else flow."""
    policy = state.get("process_policy")
    if isinstance(policy, dict) and policy.get("process") in PROCESSES:
        return policy["process"]
    return DEFAULT_PROCESS


def classes(state: dict) -> tuple[str, ...]:
    """The owner-gate classes in force: the last policy event's, else the defaults."""
    policy = state.get("owner_gate_policy")
    if isinstance(policy, dict):
        listed = policy.get("classes")
        if isinstance(listed, list) and listed:
            return tuple(listed)
    return DEFAULT_CLASSES


def match(value: str | None, listed: Iterable[str]) -> str | None:
    """The listed class value names, in its listed spelling, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    wanted = normalise(value)
    for entry in listed:
        if normalise(entry) == wanted:
            return entry
    return None


def _list_text(listed: Iterable[str]) -> str:
    return "; ".join(listed)


def block_refusal(state: dict, gate: str, owner_gate: str | None) -> str | None:
    """
    None when the block may be appended; otherwise the one-sentence refusal.

    Only strict refuses anything. In strict, a block for the owner needs a
    listed class, and any class given on another gate must be listed too.
    """
    if process_mode(state) != STRICT:
        return None
    listed = classes(state)
    given = owner_gate if isinstance(owner_gate, str) and owner_gate.strip() else None
    if given is None and gate not in OWNER_GATES:
        return None
    if given is not None and match(given, listed) is not None:
        return None
    if given is None:
        problem = f"a block with gate {gate} must name an owner-gate class"
    else:
        problem = f"'{given.strip()}' is not an owner-gate class"
    return (
        f"pinax: this project runs the strict process and {problem} with "
        f"--owner-gate, one of: {_list_text(listed)}; nothing was appended."
    )


def recorded_class(state: dict, owner_gate: str | None) -> str | None:
    """
    The class a block records: the listed spelling when it matches the list,
    else the value as given (flow accepts any), else None when none is given.
    """
    if not isinstance(owner_gate, str) or not owner_gate.strip():
        return None
    return match(owner_gate, classes(state)) or " ".join(owner_gate.split())


def unclassed_blocked(state: dict) -> list[dict]:
    """
    Every blocked item for the owner with no valid class, sorted by id.

    Each entry is {"id", "title", "gate", "owner_gate"}; owner_gate is the
    recorded value, or "" when the item carries none.
    """
    listed = classes(state)
    found = []
    items = state.get("items", {})
    for item_id in sorted(items):
        item = items[item_id]
        if item.get("status") != "blocked" or item.get("gate") not in OWNER_GATES:
            continue
        recorded = item.get("owner_gate", "")
        if match(recorded, listed) is not None:
            continue
        found.append(
            {
                "id": item_id,
                "title": item.get("title", ""),
                "gate": item.get("gate", ""),
                "owner_gate": recorded or "",
            }
        )
    return found
