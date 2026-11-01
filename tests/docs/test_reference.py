"""The committed reference is what ``python -m tundravm._reference docs/reference`` writes."""

from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT
from tundravm import _reference

REFERENCE = REPO_ROOT / "docs" / "reference"
COMMITTED_PYTHON = re.compile(r"\(Python (\d+\.\d+)\)")


def _stale(name: str) -> str:
    committed = (REFERENCE / name).read_text(encoding="utf-8")
    fresh = _reference.render(name)
    if committed == fresh:
        return ""
    diff = difflib.unified_diff(
        committed.splitlines(),
        fresh.splitlines(),
        f"committed/{name}",
        f"fresh/{name}",
        lineterm="",
    )
    shown = "\n".join(list(diff)[:60])
    return f"docs/reference/{name} is stale: run `{_reference.REGENERATE}`\n{shown}"


@pytest.mark.parametrize("name", sorted(_reference.FILES))
def test_the_committed_reference_is_a_fresh_generation(name: str) -> None:
    header = (REFERENCE / name).read_text(encoding="utf-8").split("\n", 4)[2]
    match = COMMITTED_PYTHON.search(header)
    assert match is not None, f"docs/reference/{name} names no Python version in its header"
    if match.group(1) != _reference.PYTHON:
        pytest.skip(f"generated with Python {match.group(1)}; argparse lays help out per version")
    assert _stale(name) == ""


def test_the_generation_is_deterministic(tmp_path: Path) -> None:
    first = {path.name: path.read_text() for path in _reference.write(tmp_path / "a")}
    second = {path.name: path.read_text() for path in _reference.write(tmp_path / "b")}
    assert first == second
    assert set(first) == {"cli.md", "api.md", "lint.md"}


def test_every_command_and_action_has_a_section() -> None:
    from tundravm.cli import COMMANDS

    text = _reference.render("cli.md")
    for command in COMMANDS:
        assert f"\n## tundravm {command}\n" in text
    assert "\n### tundravm secrets deliver\n" in text
    assert "\n### tundravm secrets unlock\n" in text


def test_every_exported_name_has_an_entry() -> None:
    import importlib

    text = _reference.render("api.md")
    for module in _reference.MODULES:
        for name in importlib.import_module(module).__all__:
            assert f"\n### `{name}`\n" in text, f"{module}.{name} has no entry"


def test_the_width_does_not_leak_into_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "37")
    _reference.render("cli.md")
    import os

    assert os.environ["COLUMNS"] == "37"


def test_the_parser_has_exactly_the_twelve_commands() -> None:
    from tundravm.cli import COMMANDS, build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert tuple(sub.choices) == COMMANDS
    assert len(COMMANDS) == 12
