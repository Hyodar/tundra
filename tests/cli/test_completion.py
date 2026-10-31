"""Shell completion generated from the parser: ``python -m tundravm.completion SHELL``."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.cli.test_grammar import REMOVED, TWELVE
from tundravm.cli import build_parser
from tundravm.completion import SHELLS, Shell, main, render_completion

BASH = shutil.which("bash")


@pytest.mark.parametrize("shell", SHELLS)
def test_every_command_and_no_removed_one(shell: Shell) -> None:
    script = render_completion(build_parser(), shell)
    assert "python -m tundravm.completion" in script.splitlines()[1 if shell == "zsh" else 0]
    for name in TWELVE:
        assert re.search(rf"\b{name}\b", script), name
    for choices in ("simulated tdx", "mkosi systemd", "service prover", "deliver unlock"):
        assert choices in script, choices


def test_the_bash_command_list_is_the_twelve() -> None:
    script = render_completion(build_parser(), "bash")
    [first] = re.findall(r'compgen -W "([^"]*)" -- "\$cur"\)\)\n        return 0', script)
    commands = [word for word in first.split() if not word.startswith("-")]
    assert tuple(commands) == TWELVE
    cases = re.findall(r"^        ([a-z]+)\)$", script, re.MULTILINE)
    assert tuple(cases) == TWELVE
    assert not set(cases) & set(REMOVED)


def test_main_prints_the_script(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bash"]) == 0
    assert capsys.readouterr().out == render_completion(build_parser(), "bash")
    assert main(["tcsh"]) == 2
    assert "usage: python -m tundravm.completion" in capsys.readouterr().err


def test_zsh_script_is_an_autoloadable_compdef() -> None:
    script = render_completion(build_parser(), "zsh")
    assert script.startswith("#compdef tundravm\n")
    assert "'--mode[Security mode (required to start)]:mode:(simulated tdx)'" in script
    assert "'--for[One capability]:area:(build run tdx azure gcp)'" in script


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_bash_script_parses() -> None:
    script = render_completion(build_parser(), "bash")
    result = subprocess.run([str(BASH), "-n"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _bash_complete(words: Sequence[str], cwd: Path) -> list[str]:
    script = render_completion(build_parser(), "bash")
    quoted = " ".join(f"'{word}'" for word in words)
    driver = (
        f"{script}\nCOMP_WORDS=({quoted})\nCOMP_CWORD={len(words) - 1}\n"
        '_tundravm\nprintf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    result = subprocess.run(
        [str(BASH), "--norc", "-c", driver], text=True, capture_output=True, cwd=cwd, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_bash_completes_commands_choices_and_files(tmp_path: Path) -> None:
    (tmp_path / "node.py").write_text("", encoding="utf-8")
    assert _bash_complete(["tundravm", "bu"], tmp_path) == ["build"]
    assert _bash_complete(["tundravm", "run", "--mode", ""], tmp_path) == ["simulated", "tdx"]
    assert _bash_complete(["tundravm", "convert", "x", "--from", "m"], tmp_path) == ["mkosi"]
    assert _bash_complete(["tundravm", "secrets", "d"], tmp_path) == ["deliver"]
    assert _bash_complete(["tundravm", "deploy", "g"], tmp_path) == ["gcp"]
    assert _bash_complete(["tundravm", "check", "no"], tmp_path) == ["node.py"]
    assert _bash_complete(["tundravm", "build", "--repro"], tmp_path) == ["--reproduce"]
