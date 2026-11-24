"""The README gives the upgrade order before any clone writes, and every place names one release."""

from __future__ import annotations

import re
from pathlib import Path

from pinax.commands.policy import STRICT_UPGRADE_WARNING

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
COMMANDS = ROOT / "docs" / "commands.md"
SHARING = ROOT / "docs" / "sharing-and-sync.md"

UPGRADE_ORDER = (
    "Before any clone writes, make sure every clone, and every hook's `PATH`, "
    "runs 0.5.0 or later."
)
OR_LATER = r"(\d+\.\d+\.\d+) or later"
BEFORE = r"release before (\d+\.\d+\.\d+)"


def test_the_readme_gives_the_upgrade_order():
    assert UPGRADE_ORDER in README.read_text(encoding="utf-8")


def test_every_statement_of_the_upgrade_rule_names_one_release():
    sharing = SHARING.read_text(encoding="utf-8")
    # The README, the message, the page's own sentence, its copy of the message,
    # and its two sentences about an older release.
    stated = {
        "README.md": re.findall(OR_LATER, README.read_text(encoding="utf-8")),
        "STRICT_UPGRADE_WARNING": re.findall(OR_LATER, STRICT_UPGRADE_WARNING),
        "docs/sharing-and-sync.md, or later": re.findall(OR_LATER, sharing),
        "docs/sharing-and-sync.md, release before": re.findall(BEFORE, sharing),
    }
    assert {place: len(found) for place, found in stated.items()} == {
        "README.md": 1,
        "STRICT_UPGRADE_WARNING": 1,
        "docs/sharing-and-sync.md, or later": 2,
        "docs/sharing-and-sync.md, release before": 2,
    }
    assert len({release for found in stated.values() for release in found}) == 1, stated


def test_the_pages_quote_the_message_as_printed():
    for page in (COMMANDS, SHARING):
        assert STRICT_UPGRADE_WARNING in page.read_text(encoding="utf-8").splitlines(), page.name
