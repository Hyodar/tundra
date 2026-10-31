"""``tundravm new NAME [--template service|prover] [--ci]``."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_grammar import REMOVED
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.recipe import load_file


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
    assert "tundravm build --tree-only mkosi" in readme and "tundravm run" in readme


def test_the_prover_template_with_ci(here: Path, cli: Cli) -> None:
    result = cli("new", "prover", "--template", "prover", "--ci")
    assert result.code == EXIT_OK, result.err
    root = here / "prover"
    policies = (root / "policies" / "README.md").read_text()
    assert "measurements.json" in policies and "platform_approval" in policies
    workflow = (root / ".github" / "workflows" / "tundravm.yml").read_text()
    for verb in ("check", "lock", "build", "verify", "show"):
        assert f"tundravm {verb} " in workflow or f"tundravm {verb}\n" in workflow, verb
    for verb in REMOVED:
        assert not re.search(rf"tundravm {verb}\b", workflow), verb
    header = (root / "prover.py").read_text().split('"""')[1]
    assert "tundravm check prover.py" in header and "tundravm build prover.py" in header


@pytest.mark.parametrize("name", ["Demo", "1x", "a/b", "x" * 70])
def test_a_bad_name_is_refused(here: Path, cli: Cli, name: str) -> None:
    result = cli("new", name)
    assert result.code == EXIT_SDK_ERROR
    assert "Invalid project name" in result.err


def test_a_non_empty_directory_is_refused(here: Path, cli: Cli) -> None:
    (here / "demo").mkdir()
    (here / "demo" / "keep.txt").write_text("mine")
    result = cli("new", "demo")
    assert result.code == EXIT_SDK_ERROR
    assert "exists and is not empty" in result.err
    assert sorted(p.name for p in (here / "demo").iterdir()) == ["keep.txt"]


def test_an_empty_directory_is_used(here: Path, cli: Cli) -> None:
    (here / "demo").mkdir()
    assert cli("new", "demo").code == EXIT_OK
