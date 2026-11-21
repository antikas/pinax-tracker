"""
tests/test_release_workflow.py: the release workflow's download and verify steps.

Runs the two shell steps of .github/workflows/release.yml, as their own text,
in a temporary layout shaped like the hosted publishing job, and checks that
the private workflow differs from the public one only inside those steps and
that the public export carries it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
EXPORTER = REPO_ROOT / "tools" / "export_public.py"
VERSION_STEP = "Check the tag against the package version"
DOWNLOAD_STEP = "Download the GitHub release assets"
VERIFY_STEP = "Verify the release assets"

# The public repository's workflow text before the verify step was tightened.
PUBLIC_TEXT = """\
name: Publish release assets to PyPI

on:
  workflow_dispatch:
    inputs:
      release_tag:
        description: GitHub release tag containing the reviewed package artifacts
        required: true
        type: string

concurrency:
  group: pypi-publish
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  publish:
    name: Publish to PyPI
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/project/pinax-tracker/
    permissions:
      contents: read
      id-token: write

    steps:
      - name: Check the tag against the package version
        env:
          GH_TOKEN: ${{ github.token }}
          RELEASE_TAG: ${{ inputs.release_tag }}
          REPOSITORY: ${{ github.repository }}
        shell: bash
        run: |
          set -euo pipefail
          true

      - name: Download the GitHub release assets
        env:
          GH_TOKEN: ${{ github.token }}
          RELEASE_TAG: ${{ inputs.release_tag }}
          REPOSITORY: ${{ github.repository }}
        shell: bash
        run: |
          set -euo pipefail
          case "$RELEASE_TAG" in
            v[0-9]*.[0-9]*.[0-9]*) ;;
            *)
              echo "release_tag must use the form vX.Y.Z" >&2
              exit 1
              ;;
          esac

          mkdir -p release-assets dist
          gh release download "$RELEASE_TAG" \\
            --repo "$REPOSITORY" \\
            --dir release-assets \\
            --pattern "pinax_tracker-*.whl" \\
            --pattern "pinax_tracker-*.tar.gz" \\
            --pattern "SHA256SUMS"

      - name: Verify the release assets
        env:
          RELEASE_TAG: ${{ inputs.release_tag }}
        shell: bash
        run: |
          set -euo pipefail
          cd release-assets

          test -f SHA256SUMS
          mapfile -t packages < <(
            find . -maxdepth 1 -type f \\
              \\( -name "pinax_tracker-*.whl" -o -name "pinax_tracker-*.tar.gz" \\) \\
              -printf "%f\\n" | sort
          )
          test "${#packages[@]}" -eq 2
          sha256sum --check --strict SHA256SUMS

          cp -- "${packages[@]}" ../dist/

      - name: Publish the verified distributions
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # release/v1
        with:
          packages-dir: dist
          verify-metadata: true
          skip-existing: false
          attestations: true
          print-hash: true
"""

WHEEL = "pinax_tracker-0.4.0-py3-none-any.whl"
SECOND_WHEEL = "pinax_tracker-0.4.0-py2-none-any.whl"
SDIST = "pinax_tracker-0.4.0.tar.gz"
SECOND_SDIST = "pinax_tracker-0.3.9.tar.gz"
OTHER_VERSION_WHEEL = "pinax_tracker-0.3.9-py3-none-any.whl"


# ---------------------------------------------------------------------------
# Plain text parsing of the workflow
# ---------------------------------------------------------------------------

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _run_block_span(lines: list[str], step_name: str) -> tuple[int, int]:
    """Return the line span [start, end) of the step's 'run: |' key and body."""
    header = f"- name: {step_name}"
    starts = [i for i, line in enumerate(lines) if line.strip() == header]
    assert len(starts) == 1, f"expected one step named {step_name!r}"
    step_indent = _indent(lines[starts[0]])
    for i in range(starts[0] + 1, len(lines)):
        line = lines[i]
        if line.strip() and _indent(line) <= step_indent:
            break
        if line.strip() == "run: |":
            key_indent = _indent(line)
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > key_indent):
                end += 1
            while end > i + 1 and not lines[end - 1].strip():
                end -= 1
            return i, end
    raise AssertionError(f"step {step_name!r} has no 'run: |' block")


def _run_block(text: str, step_name: str) -> str:
    lines = text.splitlines()
    start, end = _run_block_span(lines, step_name)
    body = lines[start + 1:end]
    width = min(_indent(line) for line in body if line.strip())
    return "\n".join(line[width:] for line in body) + "\n"


def _without_run_blocks(text: str) -> str:
    lines = text.splitlines()
    for step in (VERIFY_STEP, DOWNLOAD_STEP, VERSION_STEP):
        start, end = _run_block_span(lines, step)
        del lines[start:end]
    return "\n".join(lines) + "\n"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Running a step's text with bash
# ---------------------------------------------------------------------------

def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not on PATH")
    if "\\windows\\system32\\" in os.path.normcase(bash).replace("/", "\\"):
        pytest.skip("bash resolves to the Windows System32 launcher, which starts WSL")
    if shutil.which("sha256sum") is None:
        pytest.skip("sha256sum is not on PATH")
    return bash


def _run_step(bash: str, workdir: Path, step: str, variables: dict[str, str]) -> subprocess.CompletedProcess:
    assignments = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in variables.items())
    script = workdir / "step.sh"
    script.write_bytes((assignments + _run_block(_workflow_text(), step)).encode("utf-8"))
    env = dict(os.environ)
    env.update(variables)
    return subprocess.run(
        [bash, "-e", script.name], cwd=workdir, env=env,
        capture_output=True, text=True, timeout=60,
    )


def _write_stub_gh(stub_dir: Path, fixtures: Path, record: Path) -> None:
    stub_dir.mkdir(parents=True, exist_ok=True)
    gh = stub_dir / "gh"
    gh.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(record.as_posix())}\n"
            "dir=.\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = --dir ]; then dir=\"$2\"; shift; fi\n"
            "  shift\n"
            "done\n"
            f"cp -- {shlex.quote(fixtures.as_posix())}/* \"$dir\"/ 2>/dev/null || true\n"
        ).encode("utf-8")
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_download(bash: str, tmp_path: Path, tag: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / WHEEL).write_bytes(b"wheel bytes")
    record = tmp_path / "gh-calls.txt"
    stub_dir = tmp_path / "stub-bin"
    _write_stub_gh(stub_dir, fixtures, record)
    job = tmp_path / "job"
    job.mkdir()
    path = stub_dir.as_posix() + os.pathsep + os.environ.get("PATH", "")
    result = _run_step(bash, job, DOWNLOAD_STEP, {
        "PATH": path,
        "GH_TOKEN": "test-token",
        "RELEASE_TAG": tag,
        "REPOSITORY": "example/pinax-tracker",
    })
    calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return result, calls


def _sums_line(name: str, content: bytes, binary: bool = False) -> str:
    return f"{hashlib.sha256(content).hexdigest()} {'*' if binary else ' '}{name}\n"


def _run_verify(bash: str, tmp_path: Path, files: dict[str, bytes], sums: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    job = tmp_path / "job"
    assets = job / "release-assets"
    assets.mkdir(parents=True)
    (job / "dist").mkdir()
    for name, content in files.items():
        (assets / name).write_bytes(content)
    (assets / "SHA256SUMS").write_bytes(sums.encode("utf-8"))
    result = _run_step(bash, job, VERIFY_STEP, {"RELEASE_TAG": "v0.4.0"})
    return result, sorted(p.name for p in (job / "dist").iterdir())


# ---------------------------------------------------------------------------
# Tag check
# ---------------------------------------------------------------------------

@pytest.mark.deep
@pytest.mark.parametrize("tag", ["v0.4.0", "v10.20.30"])
def test_well_formed_tag_reaches_the_download(tmp_path, tag):
    result, calls = _run_download(_bash(), tmp_path, tag)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    assert calls[0].startswith(f"release download {tag} ")
    assert (tmp_path / "job" / "release-assets" / WHEEL).is_file()
    assert (tmp_path / "job" / "dist").is_dir()


@pytest.mark.deep
@pytest.mark.parametrize("tag", ["v1.2.3/../x", "v1x.2.3", "v1.2.3-rc1", "1.2.3", "v1.2", ""])
def test_malformed_tag_is_refused_before_any_fetch(tmp_path, tag):
    result, calls = _run_download(_bash(), tmp_path, tag)
    assert result.returncode != 0
    assert "release_tag must use the form vX.Y.Z" in result.stderr
    assert calls == []


# ---------------------------------------------------------------------------
# Tag against package version
# ---------------------------------------------------------------------------

def test_release_tag_input_is_required_and_has_no_default():
    lines = _workflow_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "release_tag:")
    body = []
    for line in lines[start + 1:]:
        if line.strip() and _indent(line) <= _indent(lines[start]):
            break
        body.append(line.strip())
    assert "required: true" in body
    assert not any(line.startswith("default:") for line in body), "release_tag must not carry a default tag"


def _run_version_check(
    bash: str, tmp_path: Path, tag: str, pyproject_version: str, init_version: str,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    fixtures = tmp_path / "version-fixtures"
    fixtures.mkdir()
    (fixtures / "pyproject.toml").write_text(
        f'[project]\nname = "pinax-tracker"\nversion = "{pyproject_version}"\n', encoding="utf-8")
    (fixtures / "__init__.py").write_text(f'__version__ = "{init_version}"\n', encoding="utf-8")
    record = tmp_path / "gh-api-calls.txt"
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    gh = stub_dir / "gh"
    gh.write_bytes(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(record.as_posix())}\n"
            "case \"$*\" in\n"
            f"  *pyproject.toml*) cat {shlex.quote((fixtures / 'pyproject.toml').as_posix())} ;;\n"
            f"  *__init__.py*) cat {shlex.quote((fixtures / '__init__.py').as_posix())} ;;\n"
            "  *) exit 1 ;;\n"
            "esac\n"
        ).encode("utf-8")
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    job = tmp_path / "job"
    job.mkdir()
    path = stub_dir.as_posix() + os.pathsep + os.environ.get("PATH", "")
    result = _run_step(bash, job, VERSION_STEP, {
        "PATH": path,
        "GH_TOKEN": "test-token",
        "RELEASE_TAG": tag,
        "REPOSITORY": "example/pinax-tracker",
    })
    calls = record.read_text(encoding="utf-8").splitlines() if record.exists() else []
    return result, calls


@pytest.mark.deep
def test_tag_equal_to_the_package_version_passes(tmp_path):
    result, calls = _run_version_check(_bash(), tmp_path, "v0.4.0", "0.4.0", "0.4.0")
    assert result.returncode == 0, result.stderr
    assert any("pyproject.toml" in call and "ref=v0.4.0" in call for call in calls)


@pytest.mark.deep
@pytest.mark.parametrize("case, tag, pyproject_version, init_version", [
    ("tag ahead of pyproject", "v0.4.1", "0.4.0", "0.4.0"),
    ("tag behind pyproject", "v0.3.9", "0.4.0", "0.4.0"),
    ("pyproject and module disagree", "v0.4.0", "0.4.0", "0.3.0"),
    ("module differs from tag only", "v0.4.0", "0.4.0", "0.4.1"),
])
def test_tag_that_differs_from_the_package_version_fails(tmp_path, case, tag, pyproject_version, init_version):
    result, _ = _run_version_check(_bash(), tmp_path, tag, pyproject_version, init_version)
    assert result.returncode != 0, f"{case} was accepted"
    assert "does not match" in result.stderr


@pytest.mark.deep
def test_version_check_runs_before_the_download():
    names = [line.strip()[len("- name: "):] for line in _workflow_text().splitlines()
             if line.strip().startswith("- name: ")]
    assert names.index(VERSION_STEP) < names.index(DOWNLOAD_STEP)


# ---------------------------------------------------------------------------
# Verify step
# ---------------------------------------------------------------------------

@pytest.mark.deep
@pytest.mark.parametrize("binary", [False, True])
def test_one_wheel_and_one_sdist_listed_exactly_are_copied(tmp_path, binary):
    files = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    sums = "".join(_sums_line(name, content, binary) for name, content in files.items())
    result, dist = _run_verify(_bash(), tmp_path, files, sums)
    assert result.returncode == 0, result.stderr
    assert dist == sorted(files)


def _failing_cases() -> dict[str, tuple[dict[str, bytes], str]]:
    pair = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes"}
    pair_sums = "".join(_sums_line(n, c) for n, c in pair.items())
    two_wheels = {WHEEL: b"wheel bytes", SECOND_WHEEL: b"other wheel"}
    two_sdists = {WHEEL: b"wheel bytes", SDIST: b"sdist bytes", SECOND_SDIST: b"old sdist"}
    return {
        "two wheels and no sdist": (two_wheels, "".join(_sums_line(n, c) for n, c in two_wheels.items())),
        "one wheel and two sdists": (two_sdists, "".join(_sums_line(n, c) for n, c in two_sdists.items())),
        "sums list only the wheel": (pair, _sums_line(WHEEL, pair[WHEEL])),
        "sums list a third name": (
            {**pair, "README.txt": b"extra"},
            pair_sums + _sums_line("README.txt", b"extra"),
        ),
        # The extra name sorts after both package names in the C locale, so
        # only the count of listed names can refuse it.
        "sums list a third name that sorts last": (
            {**pair, "zz-extra.txt": b"extra"},
            pair_sums + _sums_line("zz-extra.txt", b"extra"),
        ),
        "wheel line has no digest": (pair, f"{WHEEL}\n" + _sums_line(SDIST, pair[SDIST])),
        "two sdists with the first sorted one listed": (
            two_sdists,
            _sums_line(WHEEL, two_sdists[WHEEL]) + _sums_line(SECOND_SDIST, two_sdists[SECOND_SDIST]),
        ),
        "two wheels with the first sorted one listed": (
            {SECOND_WHEEL: b"other wheel", **pair},
            _sums_line(SECOND_WHEEL, b"other wheel") + _sums_line(SDIST, pair[SDIST]),
        ),
        # Every check passes for a pair of another version; only the names
        # derived from the tag can refuse it.
        "pair of another version than the tag": (
            {OTHER_VERSION_WHEEL: b"old wheel", SECOND_SDIST: b"old sdist"},
            _sums_line(OTHER_VERSION_WHEEL, b"old wheel") + _sums_line(SECOND_SDIST, b"old sdist"),
        ),
        "wrong wheel digest":(pair, _sums_line(WHEEL, b"tampered") + _sums_line(SDIST, pair[SDIST])),
        "wrong sdist digest": (pair, _sums_line(WHEEL, pair[WHEEL]) + _sums_line(SDIST, b"tampered")),
    }


@pytest.mark.deep
@pytest.mark.parametrize("case", sorted(_failing_cases()))
def test_verify_refuses_and_copies_nothing(tmp_path, case):
    files, sums = _failing_cases()[case]
    result, dist = _run_verify(_bash(), tmp_path, files, sums)
    assert result.returncode != 0, f"{case} was accepted"
    assert dist == []


# ---------------------------------------------------------------------------
# Nothing outside the two shell steps differs from the public workflow
# ---------------------------------------------------------------------------

def test_workflow_matches_public_text_outside_the_two_steps():
    assert _without_run_blocks(_workflow_text()) == _without_run_blocks(PUBLIC_TEXT)


def test_run_block_extraction_takes_the_whole_block():
    block = _run_block(PUBLIC_TEXT, VERIFY_STEP)
    assert block.startswith("set -euo pipefail\ncd release-assets\n")
    assert block.endswith('cp -- "${packages[@]}" ../dist/\n')


# ---------------------------------------------------------------------------
# The public export carries the workflow
# ---------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_public_under_test", EXPORTER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.deep
def test_export_carries_the_release_workflow(tmp_path):
    if not EXPORTER.is_file():
        pytest.skip("tools/export_public.py is absent, as it is in the exported tree")
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    hooks = tmp_path / "hooks-empty"
    hooks.mkdir()

    source = tmp_path / "source"
    _git(tmp_path, "clone", "-q", "--no-hardlinks", str(REPO_ROOT), str(source))
    _git(source, "config", "core.hooksPath", str(hooks))
    for relative in (".github/workflows/release.yml", "tools/export_public.py"):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, destination)
        _git(source, "add", "--", relative)
    _git(source, "commit", "-q", "--allow-empty", "-m", "test: working tree workflow and exporter")

    target = tmp_path / "target"
    _git(tmp_path, "init", "-q", str(target))
    _git(target, "config", "core.hooksPath", str(hooks))
    public_workflow = target / ".github" / "workflows" / "release.yml"
    public_workflow.parent.mkdir(parents=True)
    public_workflow.write_bytes(PUBLIC_TEXT.encode("utf-8"))
    _git(target, "add", "--", ".github/workflows/release.yml")
    _git(target, "commit", "-q", "-m", "test: public workflow")

    _load_exporter().export(source, "HEAD", target)

    assert public_workflow.is_file(), "the export deleted the release workflow"
    assert public_workflow.read_bytes() == WORKFLOW.read_bytes()
