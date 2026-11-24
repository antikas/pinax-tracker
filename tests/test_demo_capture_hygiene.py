from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "demo").is_dir():
    pytest.skip("the demo folder is absent, as it is in the exported tree", allow_module_level=True)
CHECKER = ROOT / "demo" / "verify_capture_hygiene.py"
OLD_FIXTURE = ROOT / "tests" / "fixtures" / "demo_capture_hygiene" / "old_public_surface.txt"
SHELL_HELPER = ROOT / "demo" / "capture_hygiene.sh"
FACTORY_CAPTURE = ROOT / "demo" / "factory_video" / "capture_factory.py"


def test_hygiene_checker_rejects_old_capture_fixture() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(OLD_FIXTURE)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "drive-letter path" in result.stdout
    assert "production item id" in result.stdout


def test_capture_entrypoints_use_the_demo_prefix_and_neutral_visible_paths() -> None:
    captures = [
        ROOT / "demo" / "factory_video" / "capture_factory.py",
        ROOT / "demo" / "walkthrough" / "capture_session.sh",
        ROOT / "demo" / "walkthrough" / "capture_advanced.sh",
        ROOT / "demo" / "walkthrough" / "capture_gates.sh",
    ]

    for capture in captures:
        text = capture.read_text(encoding="utf-8")
        assert 'DEMO_PREFIX = "dmo"' in text or 'DEMO_PREFIX="dmo"' in text
        assert "D:/t" not in text
        if capture.suffix == ".sh":
            assert 'source "$REPO_ROOT/demo/capture_hygiene.sh"' in text
        else:
            assert "for alias in path_aliases" in text

    assert 'created item ([a-z][a-z0-9]*-[a-z0-9]+)' in (
        ROOT / "demo" / "walkthrough" / "build_walkthrough.py"
    ).read_text(encoding="utf-8")


def test_neutralisation_handles_windows_and_msys_path_aliases() -> None:
    command = (
        f'source "{SHELL_HELPER}"; '
        'capture_visible "D:/t/demo/.git /d/t/demo/.git D:\\\\t\\\\demo\\\\.git" '
        '"D:\\\\t\\\\demo" "demo-capture/session"'
    )
    result = subprocess.run(["bash", "-c", command], text=True, capture_output=True, check=True)
    assert result.stdout.count("demo-capture/session") == 3
    assert "D:" not in result.stdout
    assert "/d/" not in result.stdout

    spec = spec_from_file_location("factory_capture", FACTORY_CAPTURE)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    aliases = module.path_aliases(r"D:\t\demo")
    assert {r"D:\t\demo", "D:/t/demo", "/d/t/demo"}.issubset(aliases)


@pytest.mark.parametrize("line", ["/home/someone/x", "/Users/someone/x", "To /tmp/x.git", "/d/t/x", "/mnt/d/x"])
def test_hygiene_checker_rejects_absolute_paths(tmp_path: Path, line: str) -> None:
    (tmp_path / "transcript.log").write_text(f"12:00:00 {line}\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(CHECKER), str(tmp_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 1, result.stdout


def test_hygiene_checker_accepts_neutral_evidence_and_rejects_an_empty_scan(tmp_path: Path) -> None:
    command = [sys.executable, str(CHECKER), str(tmp_path)]
    assert subprocess.run(command, capture_output=True, check=False).returncode == 1
    (tmp_path / "transcript.log").write_text(
        "12:00:00 created item dmo-ab12 in demo-capture/session/.ergon\n", encoding="utf-8"
    )
    assert subprocess.run(command, capture_output=True, check=False).returncode == 0


@pytest.mark.parametrize("name", ["session", "advanced", "gates"])
@pytest.mark.parametrize("push_rc", [0, 23])
def test_setup_push_streams_are_neutralised_and_keep_the_exit_status(
    tmp_path: Path, name: str, push_rc: int
) -> None:
    if not shutil.which("bash"):
        pytest.skip("bash is required by the capture scripts")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    git = stubs / "git"
    git.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = push ]; then echo "push out $PWD"; echo "To $PWD.remote.git" >&2; exit {push_rc}; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    pinax = stubs / "pinax"
    pinax.write_text('#!/bin/sh\n[ "$1" = init ]\n', encoding="utf-8")
    git.chmod(0o755)
    pinax.chmod(0o755)
    work = tmp_path / "work"
    env = os.environ | {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "TMP_CAPTURE_DIR": str(work),
        "PINAX_DEMO_EVIDENCE_ROOT": str(tmp_path / "evidence"),
    }
    script = ROOT / "demo" / "walkthrough" / f"capture_{name}.sh"
    result = subprocess.run(["bash", str(script)], text=True, capture_output=True, env=env, check=False)

    assert result.returncode == 2
    assert str(tmp_path) not in result.stdout + result.stderr
    transcript = (work / "transcript.log").read_text(encoding="utf-8")
    assert f"push out demo-capture/{name}" in transcript
    assert f"To demo-capture/{name}.remote.git" in transcript
    assert f"(exit {push_rc})" in transcript.split("$ git push", 1)[1].split("==", 1)[0]
    assert ("FAILED: git push" in result.stdout) is (push_rc != 0)
