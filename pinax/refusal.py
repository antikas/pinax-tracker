"""
Named refusals: a command that stops before the publish sequence runs.

A refusal prints its message on standard error and ends the process with
its exit code, exactly as a plain sys.exit would. It also carries a fixed
name, so the CLI can report under --json which refusal ended the command
without reading the message text (see pinax.__main__). An uncaught Refusal
is an ordinary SystemExit: the exit code is the same either way.
"""

from __future__ import annotations

import os
import sys
from typing import NoReturn

# The refusal names a write command's --json result carries as its
# sync_status when it ends before the publish sequence. Fixed strings: a
# caller matches on them.
INVALID_ARGUMENT = "invalid_argument"
NOT_A_TRACKER = "not_a_tracker"
ROOT_MISMATCH = "root_mismatch"
PREFIX_REFUSED = "prefix_refused"
INVALID_DEPENDENCY = "invalid_dependency"
INTERNAL_ERROR = "internal_error"
# The log holds a line that cannot be used as an event. The publish sequence
# has a status of the same name; a command that reads the log before the
# sequence ends under this one.
LOG_DAMAGED = "log_damaged"
# PINAX_TEST_RUN is 1 and the log is not under the temporary directory.
TEST_RUN_REFUSED = "test_run_refused"
# An interrupt (KeyboardInterrupt) ended the command; not raised as a
# Refusal, since the process still ends by the interrupt itself.
INTERRUPTED = "interrupted"
# The publish sequence's own name for an event naming an item that does not
# exist; a command that checks its target itself refuses under the same name.
UNKNOWN_TARGET = "unknown_target"


class Refusal(SystemExit):
    """A SystemExit that also names why the command stopped."""

    def __init__(self, code: int, name: str) -> None:
        super().__init__(code)
        self.name = name


def refuse(message: str, name: str, code: int = 1) -> NoReturn:
    """Print message on standard error and end with code, named name."""
    print(message, file=sys.stderr)
    raise Refusal(code, name)


def require_tracker(log_dir: str) -> None:
    """Refuse, as not_a_tracker with exit 1, when log_dir does not exist."""
    if not os.path.isdir(log_dir):
        refuse("pinax: .ergon/log/ not found - run 'pinax init' first.", NOT_A_TRACKER)
