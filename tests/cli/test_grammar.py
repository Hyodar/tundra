"""The command surface: twelve commands, nothing else, and how a usage error reads."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tundravm import __version__
from tundravm.cli import COMMANDS, EXIT_BROKEN_PIPE, EXIT_OK, EXIT_SDK_ERROR, build_parser
from tundravm.completion import verbs_of

TWELVE = (
    "new",
    "check",
    "lock",
    "build",
    "show",
    "run",
    "deploy",
    "attest",
    "secrets",
    "verify",
    "convert",
    "doctor",
)
REMOVED = (
    "init",
    "inspect",
    "lint",
    "compile",
    "diff",
    "fetch",
    "bake",
    "measure",
    "sbom",
    "evidence",
    "changes",
    "release",
    "witness",
    "explore",
    "preview",
    "watch",
    "status",
    "clean",
    "config",
    "ci",
    "completion",
    "units",
    "import",
)


def test_the_parser_has_exactly_the_twelve_commands() -> None:
    assert COMMANDS == TWELVE
    assert tuple(verb.name for verb in verbs_of(build_parser())) == TWELVE


def test_no_arguments_prints_help_and_the_quickstart(cli: Cli) -> None:
    result = cli()
    assert result.code == EXIT_OK
    for name in TWELVE:
        assert f"    {name} " in result.out
    assert "quickstart:" in result.out and "tundravm new demo" in result.out


def test_version(capsys: pytest.CaptureFixture[str], cli: Cli) -> None:
    with pytest.raises(SystemExit):
        cli("--version")
    assert capsys.readouterr().out.strip() == f"tundravm {__version__}"


def test_a_typo_suggests_the_closest_command(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("biuld")
    assert exit_.value.code == EXIT_SDK_ERROR
    assert "did you mean 'build'?" in capsys.readouterr().err


def test_an_unknown_flag_suggests_the_closest_one(
    cli: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("build", "--reproducible")
    assert exit_.value.code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "did you mean --reproduce?" in err


def test_a_removed_flag_is_refused(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("build", "--variant", "default")
    assert exit_.value.code == EXIT_SDK_ERROR
    assert "unrecognized arguments: --variant" in capsys.readouterr().err


def test_run_without_a_mode_is_a_validation_error(project: Path, cli: Cli) -> None:
    result = cli("run")
    assert result.code == EXIT_SDK_ERROR
    assert result.err.startswith("error [E_VALIDATION]: run needs --mode simulated or --mode tdx")


def test_deploy_without_a_build_is_refused(project: Path, cli: Cli) -> None:
    result = cli("deploy", "gcp")
    assert result.code == EXIT_SDK_ERROR
    assert "deploy gcp needs --build BUILD" in result.err


def test_an_unknown_deploy_target_is_a_usage_error(
    cli: Cli, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("deploy", "aws", "--build", "x")
    assert exit_.value.code == EXIT_SDK_ERROR
    assert "invalid choice: 'aws'" in capsys.readouterr().err


def test_a_reader_that_closes_the_pipe_ends_the_command_quietly() -> None:
    """``tundravm show --json | head``: no BrokenPipeError traceback, 128 + SIGPIPE."""
    command = [sys.executable, "-m", "tundravm", "check", "--explain", "source-unpinned"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None and process.stderr is not None
    process.stdout.close()
    err = process.stderr.read()
    assert (process.wait(timeout=60), err) == (EXIT_BROKEN_PIPE, b"")


def test_json_errors_are_one_error_line_on_stderr(project: Path, cli: Cli) -> None:
    result = cli("show", "nonexistent-build", "--json")
    assert result.code == EXIT_SDK_ERROR
    [line] = result.err.splitlines()
    payload = json.loads(line)
    assert payload["schema"] == "tundravm.error/1"
    assert payload["code"] == "E_STATE" and payload["hint"]


def test_a_missing_image_names_where_it_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Cli
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TUNDRAVM_IMAGE", raising=False)
    result = cli("check")
    assert result.code == EXIT_SDK_ERROR
    assert "No image file: pass IMAGE." in result.err
    assert "[tool.tundravm] image" in result.err


def test_secrets_needs_an_action(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_:
        cli("secrets")
    assert exit_.value.code == EXIT_SDK_ERROR
    assert "deliver" in capsys.readouterr().err
