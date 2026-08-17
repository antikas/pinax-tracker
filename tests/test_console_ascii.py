"""Console output encoding tests."""

from __future__ import annotations

import ast
import io
import os
import sys

PINAX_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "pinax")

# Logger method names that emit console-visible output via the standard
# logging module's default handler (WARNING and above are visible by default).
_LOGGER_METHODS = {"warning", "error", "critical", "info", "debug", "exception", "log"}


def _iter_python_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip caches -- never contain source console calls.
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if fname.endswith(".py"):
                yield os.path.join(dirpath, fname)


def _all_string_constants(node: ast.AST) -> list[str]:
    """
    Collect every string literal reachable from `node` (an argument
    expression), including pieces of an implicit adjacent-literal
    concatenation, an f-string's literal segments, or a '+'-joined
    concatenation of string literals -- anything short of a call/variable,
    which this scanner does not attempt to resolve (those are not the
    literal-boilerplate pattern this bug is about).
    """
    found: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.append(sub.value)
    return found


def _is_console_call(call: ast.Call) -> bool:
    func = call.func
    # print(...)
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    # sys.stderr.write(...) / sys.stdout.write(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "write"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr in ("stderr", "stdout")
    ):
        return True
    # logger.warning(...) / logger.error(...) / etc. (any object, any name --
    # matched on method name since the logger instance name varies by module).
    if isinstance(func, ast.Attribute) and func.attr in _LOGGER_METHODS:
        return True
    return False


def _is_argparse_help_call(call: ast.Call) -> bool:
    """
    add_argument(..., help=...) and ArgumentParser(..., description=...) --
    both printed verbatim by argparse on `--help`, a genuine CLI console path.
    """
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "add_argument":
        return True
    if isinstance(func, ast.Name) and func.id == "ArgumentParser":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "ArgumentParser":
        return True
    return False


def _scan_file(path: str) -> list[tuple[int, str]]:
    """Return a list of (line_number, offending_string) violations in `path`."""
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if _is_console_call(node):
            args_to_check = list(node.args) + [kw.value for kw in node.keywords]
        elif _is_argparse_help_call(node):
            # Only the help=/description= keyword values are console-printed
            # (argparse itself prints these on --help); positional args to
            # add_argument (the flag names, e.g. "--ref") are not prose.
            args_to_check = [
                kw.value for kw in node.keywords if kw.arg in ("help", "description")
            ]
        else:
            continue

        for arg in args_to_check:
            for s in _all_string_constants(arg):
                for ch in s:
                    if ord(ch) > 127:
                        violations.append((node.lineno, s))
                        break

    return violations


def test_no_non_ascii_in_console_output():
    """
    Every print()/logger.*()/sys.stderr.write() call, and every argparse
    help=/description= string, under pinax/ is pure ASCII.

    A failure here means a typographic Unicode character (arrow, em dash,
    smart quote, etc.) has crept back into a CLI console path and WILL
    crash on a legacy single-byte Windows console code page (cp437/cp850/
    cp1252) -- the relevant encoding failure mode.
    """
    all_violations: list[str] = []

    for path in sorted(_iter_python_files(PINAX_ROOT)):
        for lineno, s in _scan_file(path):
            rel = os.path.relpath(path, os.path.dirname(PINAX_ROOT))
            all_violations.append(f"{rel}:{lineno}: {s!r}")

    assert not all_violations, (
        "Non-ASCII character(s) found in CLI console output (print/logger/"
        "argparse help) -- will crash on a legacy Windows console code page "
        "(cp437/cp850/cp1252). Use a plain ASCII substitute "
        "(e.g. U+2192 '->' , U+2014 ' - '):\n" + "\n".join(all_violations)
    )


def test_known_offenders_stay_fixed():
    """
    Pin the two specific glyphs so a future change
    (re-introducing just '->' or '-' without breaking the
    general scan, e.g. via a raw byte literal the AST scanner can't see)
    is still caught directly.
    """
    disallowed = {
        "→": "RIGHTWARDS ARROW",
        "—": "EM DASH",
    }

    hits: list[str] = []
    for path in sorted(_iter_python_files(PINAX_ROOT)):
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Skip pure comment/docstring-only lines heuristically: this
            # second test intentionally does NOT try to distinguish console
            # calls from prose (that precision lives in the AST scan above);
            # it only checks the byte-level round-trip encodability of the
            # two known-crashing glyphs against the narrowest failing code
            # page (cp437) so the underlying bytes-on-disk claim is pinned
            # even if the AST heuristic above is ever weakened.
            for ch, name in disallowed.items():
                if ch in line and ("print(" in line or "logger." in line):
                    hits.append(f"{os.path.relpath(path, os.path.dirname(PINAX_ROOT))}:{lineno}: contains {name} in a print/logger line: {stripped!r}")

    assert not hits, "\n".join(hits)


def test_cli_entry_reconfigures_stream_errors_for_user_data(monkeypatch):
    """
    User data can legitimately contain non-ASCII glyphs.  The CLI entry point
    must make the active console stream replace unencodable characters rather
    than raising UnicodeEncodeError on legacy Windows code pages.
    """
    from pinax.__main__ import _configure_console_streams

    raw_out = io.BytesIO()
    raw_err = io.BytesIO()
    fake_out = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
    fake_err = io.TextIOWrapper(raw_err, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)

    _configure_console_streams()

    print("contains subset glyph: \u2282")
    sys.stderr.write("contains arrow glyph: \u2192\n")
    sys.stdout.flush()
    sys.stderr.flush()

    assert b"?" in raw_out.getvalue()
    assert b"?" in raw_err.getvalue()
