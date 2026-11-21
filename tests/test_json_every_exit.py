"""
A write command given --json prints one JSON result on every exit.

The write commands are the subcommands whose handler runs the publish
sequence. Under --json each of them writes exactly one JSON object line to
standard output, whatever ends it: its own result on exit 0, the
sequence's report when the sequence fails, and otherwise a refusal object
with "published" false, a "sync_status" naming the refusal, the refusal's
text as "error", the exit code and the subcommand. The exit code is the
one the command had before. Without --json nothing changes.

Every command runs as a separate process with this source tree first on
PYTHONPATH, in temporary git repositories.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent

OWNER = "owner@desk"
WORKER = "builder@desk"
BAD_ACTOR = "nobody"
NOT_FOUND = "pinax: .ergon/log/ not found - run 'pinax init' first."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: str, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SOURCE_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env.pop("PINAX_ROOT", None)
    env.pop("PINAX_OFFLINE", None)
    return env


def _pinax(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pinax", *args],
        cwd=cwd, env=_env(), capture_output=True, text=True,
    )


def _ok(repo: str, *args: str) -> dict:
    done = _pinax(repo, *args, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def _git_repo(path: str) -> str:
    os.makedirs(path)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "builder@example.invalid")
    _git(path, "config", "user.name", "Builder")
    _git(path, "config", "core.autocrlf", "false")
    return path


def _only_json(done: subprocess.CompletedProcess) -> dict:
    """Standard output is exactly one line, and it is a JSON object."""
    lines = done.stdout.splitlines()
    assert len(lines) == 1, f"stdout={done.stdout!r} stderr={done.stderr!r}"
    result = json.loads(lines[0])
    assert isinstance(result, dict)
    return result


def _assert_refusal(
    done: subprocess.CompletedProcess, command: str, status: str, code: int
) -> dict:
    result = _only_json(done)
    assert done.returncode == code, done.stdout + done.stderr
    assert result["published"] is False
    assert result["sync_status"] == status
    assert isinstance(result["error"], str) and result["error"].strip()
    assert result["exit_code"] == code
    assert result["command"] == command
    return result


@pytest.fixture(scope="module")
def tracker(tmp_path_factory) -> dict:
    """
    A published tracker with three items, an edge and a live claim; nothing
    run against it here appends, so every test shares it.
    """
    root = tmp_path_factory.mktemp("json-every-exit")
    repo = _git_repo(str(root / "repo"))
    bare = str(root / "origin.git")
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", bare],
        check=True, capture_output=True, text=True,
    )
    done = _pinax(repo, "init", "--actor", OWNER)
    assert done.returncode == 0, done.stderr
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", bare)
    _git(repo, "push", "-q", "origin", "main")
    first = _ok(repo, "add", "--title", "First", "--actor", OWNER)
    second = _ok(repo, "add", "--title", "Second", "--actor", OWNER)["item_id"]
    third = _ok(repo, "add", "--title", "Third", "--actor", OWNER)["item_id"]
    _ok(repo, "dep", "add", third, "--blocks", second, "--actor", OWNER)
    _ok(repo, "claim", third, "--actor", WORKER)
    briefing = root / "briefing.md"
    briefing.write_text("Finished.\n", encoding="utf-8")
    outside = _git_repo(str(root / "outside"))
    return {
        "repo": repo,
        "outside": outside,
        "first": first["item_id"],
        "first_event": first["event_id"],
        "second": second,
        "third": third,
        "briefing": str(briefing),
        "root": str(root),
    }


def _valid(name: str, t: dict) -> list[str]:
    """Arguments the command accepts, with no --actor and no --json."""
    first, second, third = t["first"], t["second"], t["third"]
    return {
        "add": ["add", "--title", "Another", "--offline"],
        "claim": ["claim", first],
        "done": ["done", first, "--briefing", t["briefing"], "--offline"],
        "block": ["block", first, "--gate", "scope", "--offline"],
        "park": ["park", first, "--reason", "later", "--offline"],
        "decide": [
            "decide", first, "--option", "A", "--label", "Go on",
            "--effect", "release", "--reason", "chosen", "--offline",
        ],
        "release": ["release", third, "--reason", "handing over", "--offline"],
        "policy claim-expiry": ["policy", "claim-expiry", "--hours", "12", "--offline"],
        "policy process": ["policy", "process", "--mode", "flow", "--offline"],
        "policy owner-gates": ["policy", "owner-gates", "--class", "money", "--offline"],
        "policy decision-actors": [
            "policy", "decision-actors", "--decider", OWNER, "--offline",
        ],
        "priority": ["priority", first, "2", "--offline"],
        "annul": ["annul", t["first_event"], "--reason", "junk", "--offline"],
        "dep add": ["dep", "add", first, "--blocks", second, "--offline"],
        "dep rm": ["dep", "rm", third, "--blocks", second, "--offline"],
        "note add": ["note", "add", first, "--ref", "docs/two.md", "--offline"],
    }[name]


# Each command with one required argument left out.
MISSING = {
    "add": ["add", "--offline"],
    "claim": ["claim"],
    "done": ["done", "pnx-0000", "--offline"],
    "block": ["block", "pnx-0000", "--offline"],
    "park": ["park", "pnx-0000", "--offline"],
    "decide": [
        "decide", "pnx-0000", "--label", "Go on", "--effect", "release",
        "--reason", "chosen", "--offline",
    ],
    "release": ["release", "pnx-0000", "--offline"],
    "policy claim-expiry": ["policy", "claim-expiry", "--offline"],
    "policy process": ["policy", "process", "--offline"],
    "policy owner-gates": ["policy", "owner-gates", "--offline"],
    "policy decision-actors": ["policy", "decision-actors", "--offline"],
    "priority": ["priority", "pnx-0000", "--offline"],
    "annul": ["annul", "abc", "--offline"],
    "dep add": ["dep", "add", "--blocks", "pnx-0001", "--offline"],
    "dep rm": ["dep", "rm", "--blocks", "pnx-0001", "--offline"],
    "note add": ["note", "add", "pnx-0000", "--offline"],
}

WRITE_COMMANDS = sorted(MISSING)


# ---------------------------------------------------------------------------
# The scope is every subcommand whose handler reaches the publish sequence
# ---------------------------------------------------------------------------

def _leaf_parsers(parser, path=()):
    import argparse

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _leaf_parsers(child, path + (name,))
            return
    yield " ".join(path), parser


def _reaches_sequence(func, seen: set) -> bool:
    """True when func, or a function it calls in pinax, calls run_sequence."""
    if func in seen or not inspect.isfunction(func):
        return False
    seen.add(func)
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    module = sys.modules[func.__module__]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            base = func.__module__.rsplit(".", node.level)[0]
            target = importlib.import_module(base + "." + node.module)
            for alias in node.names:
                if _reaches_sequence(getattr(target, alias.name, None), seen):
                    return True
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute) and callee.attr == "run_sequence":
                return True
            if isinstance(callee, ast.Name):
                if _reaches_sequence(module.__dict__.get(callee.id), seen):
                    return True
    return False


def test_the_write_command_scope_is_every_handler_that_reaches_the_sequence():
    from pinax import __main__ as cli

    found = {
        path for path, leaf in _leaf_parsers(cli.build_parser())
        if _reaches_sequence(leaf.get_default("func"), set())
    }
    assert found == set(WRITE_COMMANDS)
    assert set(cli.WRITE_COMMANDS) == found


# ---------------------------------------------------------------------------
# Refusals before the sequence, for every write command
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", WRITE_COMMANDS)
def test_outside_a_tracker_is_named_not_a_tracker(tracker, name):
    done = _pinax(tracker["outside"], *_valid(name, tracker), "--actor", OWNER, "--json")
    result = _assert_refusal(done, name, "not_a_tracker", 1)
    assert result["error"] == NOT_FOUND
    assert NOT_FOUND in done.stderr


@pytest.mark.parametrize("name", WRITE_COMMANDS)
def test_a_missing_required_argument_is_named_invalid_argument(tracker, name):
    done = _pinax(tracker["repo"], *MISSING[name], "--json")
    result = _assert_refusal(done, name, "invalid_argument", 2)
    assert "usage:" in result["error"]
    assert "usage:" in done.stderr


@pytest.mark.parametrize("name", WRITE_COMMANDS)
def test_a_bad_actor_passes_the_sequence_report_through(tracker, name):
    done = _pinax(tracker["repo"], *_valid(name, tracker), "--actor", BAD_ACTOR, "--json")
    result = _only_json(done)
    assert done.returncode == 2, done.stdout + done.stderr
    assert result["published"] is False
    assert result["sync_status"] == "invalid_actor"
    assert result["message"].strip()


@pytest.mark.parametrize("name", WRITE_COMMANDS)
def test_a_root_mismatch_is_named_root_mismatch(tracker, name):
    done = _pinax(
        tracker["repo"], "--root", tracker["outside"],
        *_valid(name, tracker), "--actor", OWNER, "--json",
    )
    result = _assert_refusal(done, name, "root_mismatch", 1)
    assert "ROOT MISMATCH" in result["error"]


# ---------------------------------------------------------------------------
# Command-specific refusals
# ---------------------------------------------------------------------------

def _decide(t: dict, option="A", label="Go on", effect="release", reason="chosen"):
    return [
        "decide", t["first"], "--option", option, "--label", label,
        "--effect", effect, "--reason", reason, "--actor", OWNER, "--offline",
    ]


@pytest.mark.parametrize("field", ["option", "label", "reason"])
def test_a_blank_decide_value_is_named_invalid_argument(tracker, field):
    done = _pinax(tracker["repo"], *_decide(tracker, **{field: " "}), "--json")
    result = _assert_refusal(done, "decide", "invalid_argument", 2)
    assert "nothing was appended" in result["error"]


def test_a_bad_decide_effect_is_named_invalid_argument(tracker):
    done = _pinax(tracker["repo"], *_decide(tracker, effect="later"), "--json")
    result = _assert_refusal(done, "decide", "invalid_argument", 2)
    assert "invalid choice" in result["error"]


def test_a_blank_release_reason_is_named_invalid_argument(tracker):
    done = _pinax(
        tracker["repo"], "release", tracker["third"], "--reason", " ",
        "--actor", OWNER, "--offline", "--json",
    )
    result = _assert_refusal(done, "release", "invalid_argument", 1)
    assert "non-empty" in result["error"]


def test_releasing_an_unknown_item_is_named_unknown_target(tracker):
    done = _pinax(
        tracker["repo"], "release", "pnx-0000", "--reason", "gone",
        "--actor", OWNER, "--offline", "--json",
    )
    _assert_refusal(done, "release", "unknown_target", 1)


def test_an_unseen_prefix_is_named_prefix_refused(tracker):
    done = _pinax(
        tracker["repo"], "add", "--title", "Elsewhere", "--prefix", "zzz",
        "--actor", OWNER, "--offline", "--json",
    )
    result = _assert_refusal(done, "add", "prefix_refused", 1)
    assert "--allow-new-prefix" in result["error"]


@pytest.mark.parametrize("op", ["add", "rm"])
@pytest.mark.parametrize("shape", [
    ["--to", "pnx-0001"],
    ["--to", "pnx-0001", "--type", "blocks", "--blocks", "pnx-0001"],
    [],
])
def test_invalid_dep_arguments_are_named_invalid_dependency(tracker, op, shape):
    done = _pinax(
        tracker["repo"], "dep", op, tracker["first"], *shape,
        "--actor", OWNER, "--offline", "--json",
    )
    _assert_refusal(done, f"dep {op}", "invalid_dependency", 1)


def test_a_self_dependency_is_named_invalid_dependency(tracker):
    done = _pinax(
        tracker["repo"], "dep", "add", tracker["first"], "--blocks", tracker["first"],
        "--actor", OWNER, "--offline", "--json",
    )
    result = _assert_refusal(done, "dep add", "invalid_dependency", 1)
    assert "self-dep" in result["error"]


def test_an_invalid_decider_is_named_invalid_argument(tracker):
    done = _pinax(
        tracker["repo"], "policy", "decision-actors", "--decider", "nobody",
        "--actor", OWNER, "--offline", "--json",
    )
    _assert_refusal(done, "policy decision-actors", "invalid_argument", 2)


def test_an_unreadable_briefing_is_named_internal_error(tracker, tmp_path):
    briefing = tmp_path / "briefing.md"
    briefing.write_bytes(b"\xff\xfe not utf-8 \xc3\x28\n")
    done = _pinax(
        tracker["repo"], "done", tracker["first"], "--briefing", str(briefing),
        "--actor", OWNER, "--offline", "--json",
    )
    result = _assert_refusal(done, "done", "internal_error", 1)
    assert "UnicodeDecodeError" in result["error"]
    assert "Traceback" in done.stderr


# ---------------------------------------------------------------------------
# A failed sequence's report passes through unchanged
# ---------------------------------------------------------------------------

def test_a_claim_with_no_remote_prints_the_sequence_report_unchanged(tmp_path):
    repo = _git_repo(str(tmp_path / "lonely"))
    assert _pinax(repo, "init", "--actor", OWNER).returncode == 0
    item = _ok(repo, "add", "--title", "Alone", "--actor", OWNER, "--offline")["item_id"]
    plain = _pinax(repo, "claim", item, "--actor", WORKER)
    assert plain.returncode == 4, plain.stdout + plain.stderr
    report = json.loads(plain.stdout.strip().splitlines()[-1])
    done = _pinax(repo, "claim", item, "--actor", WORKER, "--json")
    assert done.returncode == 4
    assert _only_json(done) == report
    assert report["published"] is False
    assert "error" not in report


# ---------------------------------------------------------------------------
# Deciding the mode from the argument list
# ---------------------------------------------------------------------------

def test_a_literal_json_value_does_not_switch_the_mode(tracker):
    done = _pinax(tracker["repo"], *_decide(tracker, label=" ", reason="--json"))
    assert done.returncode == 2
    assert done.stdout == ""
    done = _pinax(
        tracker["repo"], "add", "--title=--json", "--prefix", "zzz",
        "--actor", OWNER, "--offline",
    )
    assert done.returncode == 1
    assert done.stdout == ""


def test_a_real_json_flag_among_decide_values_switches_the_mode(tracker):
    args = _decide(tracker, label=" ")
    args.insert(args.index("--label"), "--json")
    _assert_refusal(_pinax(tracker["repo"], *args), "decide", "invalid_argument", 2)


def test_an_abbreviated_json_flag_switches_the_mode(tracker):
    done = _pinax(tracker["outside"], *_valid("park", tracker), "--js")
    _assert_refusal(done, "park", "not_a_tracker", 1)


def _park(t: dict, reason: str = "later") -> list[str]:
    return ["park", t["first"], "--reason", reason, "--actor", OWNER, "--offline"]


@pytest.mark.parametrize("value", ["-h", "--help"])
def test_a_help_flag_given_as_a_value_is_an_argument_error(tracker, value):
    done = _pinax(tracker["repo"], *_park(tracker, reason=value), "--json")
    _assert_refusal(done, "park", "invalid_argument", 2)


@pytest.mark.parametrize("flag", ["--ro", "--roo", "--root"])
def test_an_abbreviated_root_pin_is_resolved(tracker, flag):
    done = _pinax(tracker["repo"], flag, tracker["outside"], *_park(tracker), "--json")
    _assert_refusal(done, "park", "root_mismatch", 1)


@pytest.mark.parametrize("flag", ["--help", "--he", "-h"])
def test_help_under_json_prints_the_help_unchanged(tracker, flag):
    plain = _pinax(tracker["repo"], "park", "--help")
    assert plain.returncode == 0 and plain.stdout.startswith("usage: pinax park")
    done = _pinax(tracker["repo"], *_park(tracker), "--json", flag)
    assert (done.returncode, done.stdout) == (0, plain.stdout)


def test_the_version_under_json_prints_the_version_unchanged(tracker):
    plain = _pinax(tracker["repo"], "--version")
    done = _pinax(tracker["repo"], "--version", *_park(tracker), "--json")
    assert (done.returncode, done.stdout) == (0, plain.stdout)


@pytest.mark.parametrize("args", [
    ["park", "{first}", "--reason", "r", "--actor", OWNER, "--offline", "--json=1"],
    ["--json", "park", "{first}", "--reason", "r", "--actor", OWNER, "--offline"],
    ["--js", "park", "{first}", "--reason", "r", "--actor", OWNER, "--offline"],
    ["dep", "--json", "add", "{first}", "--blocks", "{second}", "--actor", OWNER, "--offline"],
], ids=["json-equals", "json-before-command", "abbreviated-before-command", "json-before-op"])
def test_a_misplaced_json_flag_is_a_json_argument_error(tracker, args):
    argv = [a.format(**tracker) for a in args]
    command = "dep add" if argv[0] == "dep" else "park"
    done = _pinax(tracker["repo"], *argv)
    _assert_refusal(done, command, "invalid_argument", 2)


# One case per refusal name, and an uncaught exception.
EXIT_CASES = {
    "not_a_tracker": ("outside", ["park", "pnx-0000", "--reason", "r", "--actor", OWNER]),
    "invalid_argument": ("repo", ["park", "pnx-0000", "--offline"]),
    "root_mismatch": ("repo", ["--root", "{outside}", "park", "{first}", "--reason", "r"]),
    "prefix_refused": ("repo", ["add", "--title", "x", "--prefix", "zzz", "--offline"]),
    "invalid_dependency": ("repo", ["dep", "add", "{first}", "--to", "{second}", "--offline"]),
    "unknown_target": ("repo", ["release", "pnx-0000", "--reason", "r", "--offline"]),
    "invalid_actor": ("repo", ["park", "{first}", "--reason", "r", "--actor", BAD_ACTOR]),
}


@pytest.mark.parametrize("status", sorted(EXIT_CASES))
def test_the_exit_code_is_the_same_with_or_without_json(tracker, status):
    where, args = EXIT_CASES[status]
    argv = [a.format(**tracker) for a in args]
    plain = _pinax(tracker[where], *argv)
    done = _pinax(tracker[where], *argv, "--json")
    assert plain.returncode != 0
    assert done.returncode == plain.returncode
    assert _only_json(done)["sync_status"] == status


def test_every_early_refusal_is_raised_with_its_name():
    """
    No write command module ends through a bare sys.exit, and every
    refusal names a constant from pinax.refusal, so the CLI never has to
    read a message to name a refusal.
    """
    from pinax import __main__ as cli
    from pinax import refusal

    names = {
        value for key, value in vars(refusal).items()
        if key.isupper() and isinstance(value, str)
    }
    modules = {"pinax.__main__"} | {
        "pinax.commands." + word for word in (
            "add", "annul", "block", "claim", "decide", "dep", "done",
            "note", "park", "policy", "priority", "release",
        )
    }
    for name in sorted(modules):
        module = importlib.import_module(name)
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            # main and _run_with_json_result re-raise argparse's or the
            # command's own exit code; run_check is not a write command.
            if node.name in ("run_check", "_exit_code", "_run_with_json_result", "main"):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                if call.func.attr == "exit":
                    raise AssertionError(f"{name}.{node.name} calls sys.exit")
                if call.func.attr == "refuse":
                    label = call.args[1]
                    assert isinstance(label, ast.Attribute), f"{name}.{node.name}"
                    assert getattr(refusal, label.attr) in names
    source = inspect.getsource(cli._run_with_json_result)
    assert source.count("kept.kept()") == 1
    assert '"error": error' in source


# ---------------------------------------------------------------------------
# The mode follows what argparse parses, for the Python running it
# ---------------------------------------------------------------------------

DOUBLE_DASH = [
    ("outside", ["--", "add", "--title", "T", "--actor", OWNER, "--json"],
     "add", "not_a_tracker", 1),
    ("repo", ["--", "add", "--json"], "add", "invalid_argument", 2),
    ("repo", ["--", "done", "{first}", "--briefing", "/nonexistent/briefing.md",
              "--actor", OWNER, "--offline", "--json"], "done", "invalid_argument", 1),
    ("repo", ["--", "add", "--title", "T", "--prefix", "zzz", "--actor", OWNER,
              "--offline", "--json"], "add", "prefix_refused", 1),
    ("repo", ["--root", "{outside}", "--", "add", "--title", "T", "--actor", OWNER,
              "--json"], "add", "root_mismatch", 1),
    ("repo", ["dep", "--", "add", "{first}", "--to", "{second}", "--actor", OWNER,
              "--offline", "--json"], "dep add", "invalid_dependency", 1),
]


@pytest.mark.parametrize(
    "where, args, command, status, code", DOUBLE_DASH,
    ids=["outside", "missing-title", "no-briefing", "prefix", "root-pin", "dep"],
)
def test_a_double_dash_is_read_as_argparse_reads_it(tracker, where, args, command, status, code):
    argv = [a.format(**tracker) for a in args]
    plain = _pinax(tracker[where], *[a for a in argv if a != "--json"])
    done = _pinax(tracker[where], *argv)
    assert plain.returncode == code
    _assert_refusal(done, command, status, code)


def _no_stdout() -> None:
    os.close(1)


@pytest.mark.skipif(os.name != "posix", reason="closes the child's stdout before exec")
@pytest.mark.parametrize("actor, code", [(OWNER, 0), (BAD_ACTOR, 2)])
def test_with_no_stdout_the_exit_code_is_kept(tmp_path, actor, code):
    repo = _git_repo(str(tmp_path / "repo"))
    assert _pinax(repo, "init", "--actor", OWNER).returncode == 0
    done = subprocess.run(
        [sys.executable, "-m", "pinax", "add", "--title", "T", "--actor", actor,
         "--offline", "--json"],
        cwd=repo, env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, preexec_fn=_no_stdout,
    )
    assert done.returncode == code, done.stderr
    assert "Traceback" not in done.stderr


@pytest.mark.parametrize("json_flag", [[], ["--json"]], ids=["plain", "json"])
def test_an_abbreviated_root_pin_does_not_bind_decide_values(tracker, json_flag):
    """
    '--ro PATH decide ... --reason --offline' is an argument error, as it
    was before --json detection existed: the decide value binding
    recognises only '--root' itself before the subcommand.
    """
    before = _git(tracker["repo"], "rev-parse", "HEAD")
    done = _pinax(
        tracker["repo"], "--ro", tracker["repo"], "decide", tracker["first"],
        "--option", "a", "--label", "A", "--effect", "release", "--reason",
        "--offline", "--actor", OWNER, *json_flag,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "--reason: expected one argument" in done.stderr
    assert _git(tracker["repo"], "rev-parse", "HEAD") == before
    assert _git(tracker["repo"], "status", "--porcelain", "--", ".ergon") == ""
    if json_flag:
        _assert_refusal(done, "decide", "invalid_argument", 2)
    else:
        assert done.stdout == ""


@pytest.mark.skipif(os.name != "posix", reason="a POSIX hook and SIGINT")
def test_an_interrupt_prints_one_json_result_and_ends_as_before(tmp_path):
    import signal
    import time

    repo = _git_repo(str(tmp_path / "repo"))
    assert _pinax(repo, "init", "--actor", OWNER).returncode == 0
    marker = tmp_path / "hook-started"
    hook = Path(repo) / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\nsleep 30\n", encoding="utf-8")
    hook.chmod(0o755)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pinax", "add", "--title", "T", "--actor", OWNER,
         "--offline", "--json"],
        cwd=repo, env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    deadline = time.monotonic() + 60
    while not marker.exists():
        assert proc.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    proc.send_signal(signal.SIGINT)
    stdout, stderr = proc.communicate(timeout=60)
    assert proc.returncode == -signal.SIGINT, stderr
    assert "KeyboardInterrupt" in stderr
    lines = stdout.splitlines()
    assert len(lines) == 1, stdout
    result = json.loads(lines[0])
    assert result["published"] is False
    assert result["sync_status"] == "interrupted"
    assert result["command"] == "add"


# ---------------------------------------------------------------------------
# Without --json nothing changes
# ---------------------------------------------------------------------------

def test_a_refusal_without_json_is_unchanged(tracker):
    done = _pinax(tracker["outside"], *_valid("park", tracker), "--actor", OWNER)
    assert (done.returncode, done.stdout, done.stderr) == (1, "", NOT_FOUND + "\n")
    done = _pinax(tracker["repo"], *MISSING["park"])
    assert done.returncode == 2
    assert done.stdout == ""
    assert done.stderr.startswith("usage: pinax park")
    done = _pinax(
        tracker["repo"], "dep", "add", tracker["first"], "--to", "pnx-0001",
        "--actor", OWNER, "--offline",
    )
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.startswith("pinax dep add: --to requires --type <t>.")


# ---------------------------------------------------------------------------
# The documents say what is true
# ---------------------------------------------------------------------------

README = SOURCE_ROOT / "README.md"
DESIGN = SOURCE_ROOT / "DESIGN.md"

REFUSAL_SENTENCES = (
    "A write command refused before the sequence runs prints a refusal result.",
    "Its `published` is false, and its `sync_status` names the refusal: "
    "`invalid_argument`, `not_a_tracker`, `root_mismatch`, `prefix_refused`, "
    "`invalid_dependency`, `unknown_target` or `internal_error`.",
    "Its `error` holds the refusal's message, and `exit_code` and `command` "
    "name the exit code and the subcommand.",
    "When the sequence fails, the result is the sequence's own report.",
)


def _publishing_section(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    section = text.split("\n## Publishing a state change\n", 1)[1].split("\n## ", 1)[0]
    return " ".join(section.split())


@pytest.mark.parametrize("path", [README, DESIGN], ids=["readme", "design"])
def test_the_publishing_section_states_one_json_result_on_every_exit(path):
    section = _publishing_section(path)
    assert "Every write command's JSON result carries" not in section
    assert "a write command prints exactly one JSON object on standard output" in section
    for sentence in REFUSAL_SENTENCES:
        assert sentence in section, sentence
    assert section.isascii()
    assert "–" not in section and "—" not in section


def test_the_readme_names_the_scope_as_the_commands_that_run_the_sequence():
    section = _publishing_section(README)
    assert "A write command is one of the commands that run the publish sequence." in section
    assert (
        "Every command above that appends an event (add, claim, done, block, park, "
        "decide, release, the four policy setters, priority, dep, note, annul) runs "
        "the same publish sequence." in section
    )


PUBLISHED_SENTENCE = (
    "`published` is true only when this run's push to the remote default "
    "branch was accepted, or when the remote default branch already held "
    "the event when this run fetched it."
)


@pytest.mark.parametrize("path", [README, DESIGN], ids=["readme", "design"])
def test_published_is_stated_as_the_code_decides_it(path):
    section = _publishing_section(path)
    assert PUBLISHED_SENTENCE in section
    assert "true only when the event is on the remote default branch" not in section
    assert "`interrupted`" in section


HELP_SENTENCE = (
    "A help or version request is the one exception: it prints its text "
    "unchanged and exits 0."
)


@pytest.mark.parametrize("path", [README, DESIGN], ids=["readme", "design"])
def test_the_publishing_section_names_help_as_the_one_exception(path):
    section = _publishing_section(path)
    assert HELP_SENTENCE in section
    assert "The exit code is the same with or without `--json`" in section


def test_the_design_states_how_refusals_are_named_and_what_is_covered():
    # Pinned by test_every_early_refusal_is_raised_with_its_name and by the
    # argument-error, refusal and internal-error rows above.
    section = _publishing_section(DESIGN)
    assert "A refusal is named where it is raised, never read from its message." in section
    assert (
        "The CLI entry point parses with the output captured and takes the "
        "mode from what argparse parsed, for the Python running it, so an "
        "argument error, an early refusal and an uncaught exception are all "
        "covered." in section
    )


def test_the_design_names_the_scope_as_the_commands_that_run_the_sequence():
    section = _publishing_section(DESIGN)
    assert (
        "A write command is one of the commands that run the publish sequence: add, "
        "claim, done, block, park, decide, release, the four policy setters, "
        "priority, annul, dep add, dep rm and note add." in section
    )
