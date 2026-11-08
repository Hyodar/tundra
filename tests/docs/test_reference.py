"""The committed reference is what ``python -m tundravm._reference docs/reference`` writes."""

from __future__ import annotations

import argparse
import collections.abc as abc
import difflib
import re
import sys
import typing
from collections.abc import Callable
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


def _signatures(text: str) -> str:
    """Every signature ``api.md`` prints: its code blocks and its member cells."""
    blocks = re.findall(r"^```python\n(.*?)^```", text, re.M | re.S)
    cells = re.findall(r"^\| `(.+?)` \|", text, re.M)
    return "\n".join([*blocks, *cells])


def _matches(pattern: str) -> Callable[[str], list[str]]:
    return lambda text: re.findall(rf".{{0,30}}{pattern}.{{0,30}}", _signatures(text))


def _aliases_without_entry(text: str) -> list[str]:
    """The names a signature uses that tundravm binds to a type alias (and to no class, which
    the name could mean instead) without an entry."""
    stdlib = {id(value) for module in (typing, abc) for value in vars(module).values()}
    aliases, classes = set(), set()
    for module in list(sys.modules.values()):
        if module.__name__.split(".")[0] != "tundravm":
            continue
        for name, value in vars(module).items():
            if isinstance(value, type):
                classes.add(name)
            elif id(value) not in stdlib and (
                isinstance(value, typing.TypeAliasType) or typing.get_origin(value) is not None
            ):
                aliases.add(name)
    used = set(re.findall(r"\b[A-Z]\w*", _signatures(text)))
    entries = set(re.findall(r"^### `([^`]+)`$", text, re.M))
    return sorted((used & (aliases - classes)) - entries)


@pytest.mark.parametrize(
    ("what", "offenders"),
    [
        ("an internal module alias", _matches(r"\b(?:dm|du)\.")),
        ("a marker's repr as a default", _matches(r"= <")),
        ("a private module path", _matches(r"\b_image\.|\b_values\b|\btundravm\._")),
        ("the probe's Report for ProbeReport", _matches(r"(?<!Probe)\bReport\b")),
        ("an alias without an entry", _aliases_without_entry),
    ],
)
def test_api_signatures_print_only_what_a_reader_can_use(
    what: str, offenders: Callable[[str], list[str]]
) -> None:
    assert offenders(_reference.render("api.md")) == [], f"api.md prints {what}"


def test_the_width_does_not_leak_into_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "37")
    _reference.render("cli.md")
    import os

    assert os.environ["COLUMNS"] == "37"


def test_the_parser_has_exactly_the_thirteen_commands() -> None:
    from tundravm.cli import COMMANDS, build_parser

    parser = build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    assert tuple(sub.choices) == COMMANDS
    assert len(COMMANDS) == 13
