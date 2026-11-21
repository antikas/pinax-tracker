"""The README's owner-gate section gives the upgrade order before strict.

No run exercises an earlier Pinax under strict, so the section states only
the instruction and none of that client's behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"

UPGRADE_ORDER = (
    "Before you set `strict`, upgrade every clone, and every older `pinax` on "
    "PATH that a pre-commit hook could run, to 0.4.0 or later."
)
UNEXERCISED = (
    "which an earlier Pinax does not write",
    "an earlier Pinax reports drift",
    "Its own tracker commands are not refused under strict",
    "still append and commit",
    "every clone that already runs 0.4.0 or later also reports drift",
    "run `pinax verify --fix` on each of those clones",
    "The command prints the same warning when it sets strict.",
)


def _owner_gate_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Owner-gate classes")
    rest = text[start + len("## Owner-gate classes"):]
    end = rest.find("\n## ")
    section = rest if end == -1 else rest[:end]
    return re.sub(r"\s+", " ", section)


def test_the_section_gives_the_upgrade_order_before_strict():
    assert UPGRADE_ORDER in _owner_gate_section()


def test_the_section_states_no_unexercised_earlier_pinax_behaviour():
    section = _owner_gate_section()
    for phrase in UNEXERCISED:
        assert phrase not in section, phrase


def test_hook_refuses_every_commit_phrase_is_gone():
    section = _owner_gate_section()
    assert "its pre-commit hook refuses every commit" not in section
