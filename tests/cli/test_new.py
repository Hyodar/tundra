"""``tundravm new NAME [--ci]``: stage 1 of the attested node, its project and its pipeline."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.cli.conftest import Cli
from tests.cli.test_doctor import TOOLS, _host
from tests.cli.test_grammar import REMOVED
from tundravm.backends.local_linux import DEBIAN_KEYRING
from tundravm.capabilities import build_capability, go_capability
from tundravm.cli import EXIT_ERROR, EXIT_OK
from tundravm.recipe import load_file
from tundravm.templates import WORKFLOW_PATH, render_workflow


@pytest.fixture
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_new_writes_a_project_whose_table_names_the_image(here: Path, cli: Cli) -> None:
    result = cli("new", "demo")
    assert result.code == EXIT_OK, result.err
    root = here / "demo"
    for name in ("demo.py", "tests/test_demo.py", "pyproject.toml", "README.md", ".gitignore"):
        assert (root / name).is_file(), name
        assert f"created demo/{name}" in result.out
    assert not (root / ".github").exists() and not (root / "policies").exists()
    table = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["tundravm"]
    assert table == {"image": "demo.py", "build": "build"}
    assert load_file(root / "demo.py").variants == ("production", "dev")
    assert "check demo.py:" in result.out
    assert "uv run tundravm build" in result.out
    assert "/.tundravm/" in (root / ".gitignore").read_text()
    readme = (root / "README.md").read_text()
    assert "tundravm build --tree-only mkosi" in readme and "tundravm dev" in readme


def test_the_ci_workflow_runs_the_project_it_was_written_for(
    here: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli("new", "vault", "--ci").code == EXIT_OK
    root = here / "vault"
    workflow = yaml.safe_load((root / WORKFLOW_PATH).read_text())
    jobs = workflow["jobs"]
    assert workflow[True]["push"]["tags"] == ["v*"] and "pull_request" in workflow[True]
    assert workflow["permissions"] == {"contents": "read"}
    assert {name: job.get("needs") for name, job in jobs.items()} == {
        "check": None,
        "build": "check",
        "rebuild": "build",
        "release": ["build", "rebuild"],
    }
    assert jobs["release"]["permissions"] == {
        "contents": "write",
        "id-token": "write",
        "attestations": "write",
    }
    runs = {
        name: [line.strip() for step in job["steps"] for line in step.get("run", "").splitlines()]
        for name, job in jobs.items()
    }
    for job, line in (
        ("check", "uv run tundravm check --tree mkosi"),
        ("check", "uv run tundravm lock --check"),
        ("build", "uv run tundravm build"),
        ("build", "uv run tundravm verify latest"),
        ("rebuild", 'uv run tundravm verify --rebuild "$RUNNER_TEMP"/release/*/'),
        ("release", 'if [ "$ATTESTED" = true ]; then'),  # no verify line without attestation
    ):
        assert line in runs[job], (job, line)
    assert any(line.startswith("xargs gh release create") for line in runs["release"])
    steps = {step["name"]: step for step in jobs["release"]["steps"]}
    assert steps["Attest the provenance of the released files"]["id"] == "attest"
    assert (
        steps["Create the release"]["env"]["ATTESTED"] == "${{ steps.attest.outcome == 'success' }}"
    )
    uses = [step["uses"] for job in jobs.values() for step in job["steps"] if "uses" in step]
    assert "actions/attest-build-provenance@v4.2.2" in uses
    for action in uses:
        assert re.fullmatch(r"[\w-]+/[\w-]+@v\d+\.\d+\.\d+", action), action
    text = "\n".join(line for lines in runs.values() for line in lines)
    for verb in REMOVED:
        assert not re.search(rf"tundravm {verb}\b", text), verb
    monkeypatch.chdir(root)
    for argv in (
        ("lock",),
        ("build", "--tree-only", "mkosi"),
        ("check", "--tree", "mkosi"),
        ("lock", "--check"),
    ):
        result = cli(*argv)
        assert result.code == EXIT_OK, (argv, result.out, result.err)


def test_the_ci_runner_installs_what_doctor_asks_of_a_bare_ubuntu_host() -> None:
    bare = {tool: line for tool, line in TOOLS.items() if tool != "go"}
    apparmor = {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns": "1"}
    fixes = []
    for files in ((), (DEBIAN_KEYRING,)):
        host, _ = _host(bare, files=files, userns=False, proc=apparmor)
        fixes += [build_capability(host, "local").fix, go_capability(host).fix]
    workflow = render_workflow()
    (installs,) = {line.strip() for line in workflow.splitlines() if "apt-get install" in line}
    commands = [fix.split(" (")[0] for fix in fixes if fix]
    assert len(commands) == 4
    for command in commands:
        if command.startswith("sudo apt install "):
            assert set(command.split()[3:]) <= set(installs.split()[5:]), command
        else:
            assert command in workflow, command


def test_a_new_project_is_ruff_format_clean_at_its_own_setting(here: Path, cli: Cli) -> None:
    assert cli("new", "demo").code == EXIT_OK
    root = here / "demo"
    assert tomllib.loads((root / "pyproject.toml").read_text())["tool"]["ruff"] == {
        "line-length": 100
    }
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("name", ["Demo", "1x", "a/b", "x" * 70])
def test_a_bad_name_is_refused(here: Path, cli: Cli, name: str) -> None:
    result = cli("new", name)
    assert result.code == EXIT_ERROR
    assert "Invalid project name" in result.err


def test_a_non_empty_directory_is_refused(here: Path, cli: Cli) -> None:
    (here / "demo").mkdir()
    (here / "demo" / "keep.txt").write_text("mine")
    result = cli("new", "demo")
    assert result.code == EXIT_ERROR
    assert "exists and is not empty" in result.err
    assert sorted(p.name for p in (here / "demo").iterdir()) == ["keep.txt"]


def test_an_empty_directory_is_used(here: Path, cli: Cli) -> None:
    (here / "demo").mkdir()
    assert cli("new", "demo").code == EXIT_OK


def test_the_generated_tests_use_the_public_api_and_pass(
    here: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli("new", "demo").code == EXIT_OK
    path = here / "demo" / "tests" / "test_demo.py"
    tree = ast.parse(path.read_text())
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported - {"os", "pathlib"} == {"tundravm", "tundravm.testing"}
    spec = importlib.util.spec_from_file_location("generated_test_demo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tests = [getattr(module, name) for name in dir(module) if name.startswith("test_")]
    assert len(tests) == 3
    monkeypatch.setenv("TUNDRAVM_UPDATE_GOLDEN", "1")
    module.test_matches_committed_tree()
    monkeypatch.delenv("TUNDRAVM_UPDATE_GOLDEN")
    for test in tests:
        test()
