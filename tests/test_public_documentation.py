"""The public pages agree with the code, with each other and with the export."""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import inspect
import re
import sys
import textwrap
from pathlib import Path, PurePosixPath

import pytest

from pinax import __main__ as cli
from pinax import append, fold, refusal, sync

ROOT = Path(__file__).resolve().parent.parent
EXPORTER = ROOT / "tools" / "export_public.py"

README = "README.md"
COMMANDS = "docs/commands.md"
SHARING = "docs/sharing-and-sync.md"
DATA_MODEL = "docs/data-model-and-integrity.md"
PORTFOLIO = "docs/portfolio-and-web.md"
PAGES = (README, COMMANDS, SHARING, DATA_MODEL, PORTFOLIO)

# Split so that this file does not itself hold a name it searches for.
ARCHIVED_NAMES = (
    "DESIGN" + ".md",
    "portfolio" + "-views",
    "docs/" + "decisions",
    "docs/" + "architecture",
    "docs/" + "archive",
)


def _text(page: str) -> str:
    return (ROOT / page).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Reading a page
# ---------------------------------------------------------------------------

def _split_fences(text: str) -> tuple[str, list[str]]:
    """The text outside fenced blocks, and each fenced block's body."""
    outside: list[str] = []
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.split("\n"):
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append("\n".join(current))
                current = None
        elif current is None:
            outside.append(line)
        else:
            current.append(line)
    assert current is None, "an unclosed fenced block"
    return "\n".join(outside), blocks


def _spans(prose: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", prose)


def _anchors(text: str) -> set[str]:
    """The anchors a Git host gives the page's headings."""
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for line in _split_fences(text)[0].split("\n"):
        heading = re.match(r"#{1,6} +(.*?) *$", line)
        if not heading:
            continue
        slug = re.sub(r"[^\w\- ]", "", heading.group(1).lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(f"{slug}-{count}" if count else slug)
    return anchors


def _links(text: str) -> list[str]:
    prose = re.sub(r"`[^`\n]*`", "", _split_fences(text)[0])
    return (
        re.findall(r"\]\(\s*([^)\s]*)[^)]*\)", prose)
        + re.findall(r"(?m)^ *\[[^\]]+\]:\s*(\S+)", prose)
        + re.findall(r"<(https?:[^>]+)>", prose)
        + re.findall(r"(?:href|src)=[\"']([^\"']*)", prose)
    )


def _section(page: str, heading: str) -> str:
    """The body under a heading, up to the next heading of its level or above."""
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    inside = fenced = False
    for line in _text(page).split("\n"):
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line == heading:
            inside = True
            continue
        elif inside and not fenced and re.match(r"#{1,%d} " % level, line):
            break
        if inside:
            body.append(line)
    assert inside, f"{page} has no heading {heading!r}"
    return "\n".join(body)


def _table(text: str, first_header: str) -> list[list[str]]:
    """The body rows of every table whose first header cell is first_header."""
    rows: list[list[str]] = []
    state = "out"
    for line in _split_fences(text)[0].split("\n"):
        if not line.startswith("|"):
            state = "out"
            continue
        cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        if state == "out":
            state = "rule" if cells[0] == first_header else "skip"
        elif state == "rule":
            state = "in"
        elif state == "in":
            rows.append(cells)
    return rows


# ---------------------------------------------------------------------------
# The public selection
# ---------------------------------------------------------------------------

def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_public_for_documents", EXPORTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _working_tree_names() -> list[str]:
    skipped = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".verdicts", "build", "dist"}
    names = []
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        parts = relative.parts
        if skipped.intersection(parts) or any(p.endswith(".egg-info") for p in parts):
            continue
        if path.is_file():
            names.append(relative.as_posix())
    return sorted(names)


def _selection() -> set[str]:
    """
    The files an export of the working tree selects, by the exporter's own
    rule. The exported tree carries no exporter and is the selection.
    """
    names = _working_tree_names()
    if not EXPORTER.is_file():
        return set(names)
    exporter = _load_exporter()
    exporter._git = lambda repo, *args: "\n".join(names) + "\n"
    return set(exporter._members(ROOT, "HEAD"))


def test_the_export_selects_the_five_pages_and_no_archived_page():
    if not EXPORTER.is_file():
        pytest.skip("tools/export_public.py is absent, as it is in the exported tree")
    selected = _selection()
    markdown = {n for n in selected if n.startswith("docs/") or "/" not in n and n.endswith(".md")}
    assert markdown == set(PAGES)
    for name in selected:
        assert not any(old in name for old in ARCHIVED_NAMES), name


def test_nothing_in_the_selection_names_an_archived_page():
    found = [
        f"{name}: {old}"
        for name in sorted(_selection())
        for old in ARCHIVED_NAMES
        if old in (ROOT / name).read_bytes().decode("utf-8", errors="replace")
    ]
    assert found == []


def test_nothing_in_the_selection_names_a_decision_record():
    identifier = re.compile(r"adr[-_ ]?[0-9]", re.IGNORECASE)
    names = sorted(_selection())
    assert "tests/test_public_documentation.py" in names
    found = [
        f"{name}:{number}: {line.strip()}"
        for name in names
        for number, line in enumerate(
            (ROOT / name).read_bytes().decode("utf-8", errors="replace").split("\n"), 1
        )
        if identifier.search(line)
    ]
    assert found == [], "\n" + "\n".join(found)


# ---------------------------------------------------------------------------
# Links and wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", PAGES)
def test_every_link_resolves_inside_the_public_selection(page):
    links = _links(_text(page))
    assert links
    selected = _selection()
    broken = []
    for link in links:
        path, _, anchor = link.partition("#")
        parts: list[str] = list(PurePosixPath(page).parent.parts) if path else []
        for part in PurePosixPath(path).parts if path else PurePosixPath(page).parts:
            if part == ".." and parts:
                parts.pop()
            else:
                parts.append(part)
        target = "/".join(parts)
        if target not in selected or (
            anchor and (not target.endswith(".md") or anchor not in _anchors(_text(target)))
        ):
            broken.append(link)
    assert broken == []


@pytest.mark.parametrize("page", PAGES)
def test_no_page_holds_a_long_dash_a_private_path_or_an_archived_name(page):
    text = _text(page)
    assert "—" not in text and "–" not in text
    private = [
        # The worked examples live in alex's home directory and nowhere else.
        r"/home/(?!alex\b)\w+", r"/Users/\w+", r"/root/", r"/mnt/", r"\b[A-Za-z]:[\\/]",
        r"~/(?!src\b|work\b|planning\b|nowhere\b)\S+", r"~/\.",
        r"\bdocs/(?:items|cycles|campaigns|release)\b", r"\btools/", r"\.claude\b", r"\.autobuild",
        r"\bCLAUDE\.md\b", r"\bAGENTS\.md\b",
    ]
    for pattern in private:
        assert not re.search(pattern, text), pattern
    names = set(ARCHIVED_NAMES)
    archive = ROOT / "docs" / "archive"
    if archive.is_dir():
        names |= {p.name for p in archive.rglob("*") if p.is_file() and p.name != "README.md"}
    for name in names:
        assert name not in text, name
    assert not re.search(r"\bADR-\d", text)


# ---------------------------------------------------------------------------
# The command reference and the parser
# ---------------------------------------------------------------------------

def _parsers(parser=None, path=()) -> dict[tuple, argparse.ArgumentParser]:
    """Every parser by its words: the program, each group and each command."""
    parser = parser or cli.build_parser()
    found = {path: parser}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for word, child in action.choices.items():
                found.update(_parsers(child, path + (word,)))
    return found


PARSERS = _parsers()
LEAVES = {p: v for p, v in PARSERS.items() if v.get_default("func") is not None}
GROUPS = {p[0] for p in PARSERS if len(p) == 2}


def _options(parser) -> set[str]:
    return {s for a in parser._actions for s in a.option_strings if s.startswith("--")}


def _arguments(parser) -> set[str]:
    """Each long option but help, and each positional, under its shown name."""
    shown = set()
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        longs = [s for s in action.option_strings if s.startswith("--")]
        shown.update(longs or [action.metavar or action.dest])
    return shown


def _usage(path: tuple) -> str:
    """The usage line argparse builds, without its wrapping or colours."""
    optionals, positionals = [], []
    for action in PARSERS[path]._actions:
        choices = "{" + ",".join(map(str, action.choices)) + "}" if action.choices else None
        if isinstance(action, argparse._SubParsersAction):
            positionals.append(f"{choices} ...")
        elif not action.option_strings:
            name = action.metavar or choices or action.dest
            positionals.append(f"[{name}]" if action.nargs == "?" else name)
        else:
            part = action.option_strings[0]
            if action.nargs != 0:
                part += " " + (action.metavar or choices or action.dest.upper())
            optionals.append(part if action.required else f"[{part}]")
    return " ".join(["usage:", "pinax", *path, *optionals, *positionals])


def _usage_blocks(text: str) -> list[str]:
    blocks = []
    for block in _split_fences(text)[1]:
        if block.startswith("usage: "):
            kept = [line for line in block.split("\n") if not re.match(r"pinax .*error:", line)]
            blocks.append(_flat(" ".join(kept)))
    return blocks


def _command_of(words: list[str]) -> tuple | None:
    """The longest command the leading words name, or None when they name none."""
    best = None
    for length in (1, 2):
        if tuple(words[:length]) in PARSERS and len(words) >= length:
            best = tuple(words[:length])
    return best


def _entries() -> dict[tuple, str]:
    """Each command, with the text of every entry whose heading names it."""
    entries: dict[tuple, str] = {}
    for heading in re.findall(r"(?m)^### `pinax .*$", _text(COMMANDS)):
        body = _section(COMMANDS, heading)
        for span in _spans(heading):
            command = _command_of(span.split()[1:])
            assert command is not None, f"{heading} names no command"
            members = [p for p in LEAVES if p[: len(command)] == command]
            assert members, heading
            for leaf in members:
                entries[leaf] = entries.get(leaf, "") + "\n" + body
    return entries


def test_every_command_and_subcommand_has_an_entry():
    assert set(_entries()) == set(LEAVES)
    headings = re.findall(r"(?m)^### (`pinax .*)$", _text(COMMANDS))
    anchors = set().union(*(_anchors(f"# {heading}") for heading in headings))
    index = _text(COMMANDS).split("## What every command shares", 1)[0]
    parts = index.split("The same commands in alphabetical order:")
    assert len(parts) == 2
    for part in parts:
        links = re.findall(r"\[([^\]]*)\]\(#(pinax-[^)]*)\)", part)
        assert {anchor for _, anchor in links} == anchors
        for label, _ in links:
            for span in _spans(label):
                assert _command_of(span.split()) is not None, span


@pytest.mark.parametrize("path", sorted(PARSERS), ids=lambda p: " ".join(p) or "pinax")
def test_the_usage_on_the_page_is_the_parsers_own(path):
    start = " ".join(["usage:", "pinax", *path])
    mine = []
    for block in _usage_blocks(_text(COMMANDS)):
        words = re.match(r"usage: pinax((?: [a-z][a-z-]*)*)", block).group(1).split()
        if _command_of(words) == (path or None):
            mine.append(block)
    assert mine, f"no usage block starts {start!r}"
    assert set(mine) == {_usage(path)}
    if path in LEAVES:
        assert _usage(path) in _usage_blocks(_entries()[path])


# Its entry gives the one option it has, --json, in the usage line only.
NO_OPTION_TABLE = {("policy", "check")}


@pytest.mark.parametrize("path", sorted(LEAVES), ids=" ".join)
def test_every_option_has_a_row_and_every_row_is_an_option(path):
    entry = _entries()[path]
    rows = _table(entry, "Option")
    named = set()
    for row in rows:
        for span in _spans(row[0]):
            named.add(span.split()[0])
    assert (not rows) == (path in NO_OPTION_TABLE)
    if rows:
        assert _arguments(LEAVES[path]) <= named
    sharing = [p for p, text in _entries().items() if text == entry]
    assert named <= set().union(*(_arguments(LEAVES[p]) for p in sharing))


# A Git option the page says no command uses, and two abbreviations of
# --json and --root that the page gives as examples of abbreviating.
NOT_PINAX_OPTIONS = {"--no-verify", "--js", "--ro"}


def test_the_page_names_no_command_or_option_the_parser_lacks():
    prose, blocks = _split_fences(_text(COMMANDS))
    spans = _spans(prose)
    lines = [line.strip() for block in blocks for line in block.split("\n")]
    top = {p[0] for p in PARSERS if p}
    everything = set().union(*(_options(p) for p in PARSERS.values()))
    wrong = []
    for text in spans + lines:
        if text.startswith("git "):
            continue
        for option in re.findall(r"(?<![\w-])--[a-z][a-z-]*", text):
            if option not in everything | NOT_PINAX_OPTIONS:
                wrong.append(text)
        called = re.match(r"(?:usage: )?pinax +([a-z].*)", text)
        # A bare span such as `dep add` or `doctor --json` names a command too.
        bare = re.match(r"((?:%s) (?:--|[a-z]).*)" % "|".join(top), text) if text in spans else None
        if not (called or bare):
            continue
        words = (called or bare).group(1).split()
        # A printed message starts with its command and a colon.
        message = words[0].endswith(":") or words[1:2] and words[1].endswith(":")
        words = [w.rstrip(":") for w in words]
        command = _command_of(words)
        if command is None:
            wrong.append(text)
            continue
        if command[0] in GROUPS and len(command) == 1:
            rest = words[1] if len(words) > 1 else ""
            if re.match(r"[a-z]", rest):
                wrong.append(text)
                continue
        if message:
            continue
        allowed = _options(PARSERS[command])
        for option in re.findall(r"(?<![\w-])--[a-z][a-z-]*", text):
            if option not in allowed and option in everything:
                wrong.append(text)
    assert wrong == []


def test_the_page_lists_the_write_commands():
    section = _section(COMMANDS, "### Which commands write events")
    sentence = re.search(r"The write commands are (.*?)\. ", section).group(1)
    assert sorted(_spans(sentence)) == sorted(cli.WRITE_COMMANDS)


# ---------------------------------------------------------------------------
# Exit codes and outcome names
# ---------------------------------------------------------------------------

EXIT_CODES = {n: v for n, v in vars(sync).items() if n.startswith("EXIT_")}
REFUSALS = {v for n, v in vars(refusal).items() if n.isupper() and isinstance(v, str)}

# What the meaning column must say for each constant, by the constant's name.
MEANINGS = {
    "EXIT_OK": r"event is committed, and pushed",
    "EXIT_UNKNOWN_TARGET": r"names an item.*neither the local log nor the remote holds",
    "EXIT_INVALID_ACTOR": r"writer is not `role@host`",
    "EXIT_CLAIM_SUPERSEDED": r"another writer's claim holds the item",
    "EXIT_REMOTE_REQUIRED": r"remote's published branch could not be read",
    "EXIT_PUSH_EXHAUSTED": r"push was rejected on all",
    "EXIT_CLOCK_BEHIND": r"timestamp is behind the newest live remote event",
    "EXIT_COMMIT_REFUSED": r"commit was refused",
    "EXIT_POLICY_REFUSED": r"policy refuses the event",
    "EXIT_LOG_DAMAGED": r"line that cannot be used as an event",
    "EXIT_TEST_RUN_REFUSED": r"`PINAX_TEST_RUN` is `1`",
}


def _exit_rows() -> dict[int, list[str]]:
    rows = _table(_section(COMMANDS, "### Common exit codes"), "Code")
    codes = [int(row[0]) for row in rows]
    assert len(codes) == len(set(codes))
    return {int(row[0]): row for row in rows}


def test_every_exit_code_is_in_the_table_with_its_meaning():
    rows = _exit_rows()
    assert set(MEANINGS) == set(EXIT_CODES)
    for name, code in EXIT_CODES.items():
        assert code in rows, f"{name} = {code} is not in the table"
        assert re.search(MEANINGS[name], rows[code][1]), name


def test_the_table_gives_each_outcome_its_code_and_no_other():
    rows = _exit_rows()
    assert set(rows) == set(EXIT_CODES.values()) | {cli._INTERRUPT_EXIT_CODE}
    for code, row in rows.items():
        named = set(_spans(row[2]))
        outcomes = {s for s, c in sync._EXIT_BY_STATUS.items() if c == code}
        assert outcomes <= named, code
        foreign = named - outcomes - REFUSALS - {"claim"}
        assert foreign == set(), code
        assert (named - {"claim"}) - REFUSALS <= set(sync._EXIT_BY_STATUS), code
    assert set(_spans(rows[cli._INTERRUPT_EXIT_CODE][2])) == {refusal.INTERRUPTED}


def test_every_name_sync_status_can_hold_is_in_the_table_or_the_refusal_list():
    table = {span for row in _exit_rows().values() for span in _spans(row[2])}
    assert REFUSALS | set(sync._EXIT_BY_STATUS) == table - {"claim"}
    section = _section(COMMANDS, "### Common exit codes")
    sentence = re.search(
        r"refusal names that can appear before the sequence starts are (.*?)\. ", section
    ).group(1)
    assert set(_spans(sentence)) == REFUSALS


# ---------------------------------------------------------------------------
# Commands that write events and never push
# ---------------------------------------------------------------------------

def _reaches(func, wanted: str, seen: set) -> bool:
    """True when func, or a function it calls in pinax, calls wanted."""
    if func in seen or not inspect.isfunction(func):
        return False
    seen.add(func)
    module = sys.modules[func.__module__]
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(func)))):
        if isinstance(node, ast.ImportFrom) and node.level and node.module:
            base = func.__module__.rsplit(".", node.level)[0]
            target = importlib.import_module(base + "." + node.module)
            for alias in node.names:
                if _reaches(getattr(target, alias.name, None), wanted, seen):
                    return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", None))
            if name == wanted:
                return True
            if isinstance(node.func, ast.Name) and _reaches(
                module.__dict__.get(node.func.id), wanted, seen
            ):
                return True
    return False


def _unpublished_writers() -> set[tuple]:
    """Each command whose handler appends an event without the publish sequence."""
    assert append.append_event.__name__ == "append_event"
    assert sync.run_sequence.__name__ == "run_sequence"
    return {
        path for path, parser in LEAVES.items()
        if _reaches(parser.get_default("func"), "append_event", set())
        and not _reaches(parser.get_default("func"), "run_sequence", set())
    }


def _form(span: str) -> str:
    return re.sub(r"[<>]", "", span.removeprefix("pinax "))


def _listed_never_pushing() -> dict[str, set[str]]:
    listed = {}
    limits = _table(_section(README, "## Limits"), "Command")
    listed[README] = {_form(s) for row in limits for s in _spans(row[0])}
    # The table also holds commands that write files and no event.
    table = _table(_section(COMMANDS, "### Commands that never push"), "Command")
    listed[COMMANDS] = {
        _form(_spans(row[0])[0]) for row in table
        if any(re.fullmatch(r"(?:ergon|phase|item|registry)\.[a-z_]+", s) for s in _spans(row[1]))
    }
    section = _section(SHARING, "### Events from commands that never push")
    assert "These commands also skip" in section
    prose = section.split("These commands also skip", 1)[0]
    listed[SHARING] = {_form(s) for s in _spans(prose) if s.startswith("pinax ")}
    return listed


def test_the_lists_of_commands_that_never_push_agree_with_each_other_and_the_code():
    listed = _listed_never_pushing()
    expected = _unpublished_writers()
    assert expected and not {" ".join(p) for p in expected} & set(cli.WRITE_COMMANDS)
    for page, forms in listed.items():
        assert {_command_of(form.split()) for form in forms} == expected, page
        assert forms == listed[COMMANDS], page
        for form in forms:
            command = _command_of(form.split())
            extra = form.split()[len(command):]
            assert set(extra) <= _arguments(PARSERS[command]), f"{page}: {form}"


NUMBERS = {
    word: number for number, word in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve".split()
    )
}


def _number(text: str) -> float | None:
    if text.lower() in NUMBERS:
        return float(NUMBERS[text.lower()])
    return float(text) if re.fullmatch(r"\d+(?:\.\d+)?", text) else None


def test_the_stated_number_of_commands_that_never_push_is_the_tables():
    rows = _table(_section(COMMANDS, "### Commands that never push"), "Command")
    stated = re.findall(
        r"\b(\w+) commands (?:change files in your repository and never push|under \[Commands that never push\])",
        _text(COMMANDS),
    )
    assert stated
    assert {_number(word) for word in stated} == {float(len(rows))}


# ---------------------------------------------------------------------------
# Stated numbers
# ---------------------------------------------------------------------------

STATED = {
    "claim expiry": (
        fold.DEFAULT_CLAIM_EXPIRY_HOURS,
        (r"\b(\w+) hours\b", r"claim expiry in force, else (\w+)", r"stale threshold: (\d+)h"),
        (README, COMMANDS, SHARING, DATA_MODEL),
    ),
    "push attempts": (
        sync._MAX_PUSH_ATTEMPTS,
        (
            r"\b(?:up to|all|after) (\w+) attempts\b",
            r"number of attempts is at most (\w+)",
            r"\b(\w+) rejected pushes\b",
        ),
        (README, COMMANDS, SHARING),
    ),
    "clock tolerance": (
        sync._DEFAULT_CLOCK_TOLERANCE_S,
        (
            r"\bmore than (\w+) seconds\b",
            r"\bchanges the (\w+) seconds\b",
            r"\bup to (\w+) seconds earlier\b",
            r"tolerance(?:,)? (?:which )?is (\w+) seconds",
            r"newest remote event; (\w+) when unset",
        ),
        (README, COMMANDS, SHARING, DATA_MODEL),
    ),
}


@pytest.mark.parametrize("rule", sorted(STATED))
def test_each_stated_number_is_the_codes(rule):
    constant, patterns, pages = STATED[rule]
    for page in PAGES:
        found = [word for p in patterns for word in re.findall(p, _flat(_text(page)))]
        # "expiry hours" and the like state no number.
        numbers = [_number(word) for word in found if _number(word) is not None]
        assert numbers or page not in pages, page
        assert set(numbers) <= {float(constant)}, page


# ---------------------------------------------------------------------------
# The references a note accepts
# ---------------------------------------------------------------------------

def test_the_page_quotes_the_note_reference_refusal():
    from pinax.commands.note import ref_refusal

    message = ref_refusal({}, "../x")
    assert "exist" not in message
    assert message.replace("../x", "<ref>") in _entries()[("note", "add")]


@pytest.mark.parametrize("page", PAGES)
def test_no_page_says_a_referenced_file_must_exist(page):
    found = [
        sentence for sentence in re.split(r"(?<=[.;:])\s", _flat(_text(page)))
        if re.search(r"\breference|--ref\b|\bREF\b|note add", sentence)
        and re.search(r"file (?:that )?exists|existing file|file in th(?:e|is) repository", sentence)
    ]
    assert found == []
    assert "regular file that exists" not in _flat(_text(page))
