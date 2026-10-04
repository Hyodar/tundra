"""Pytest fixtures for tundravm recipes and fragments.

Registered as the ``tundravm`` plugin through the ``pytest11`` entry point, so
the fixtures are available in any project that has tundravm installed. Disable
it with ``pytest -p no:tundravm``.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest

from tundravm.declarative.model import Fragment, Recipe
from tundravm.testing import CompiledTree, Variants, compile_tree
from tundravm.testing import run_cli as _run_cli


class CompileFactory(Protocol):
    def __call__(self, recipe: Recipe, variants: Variants = None) -> CompiledTree: ...


CliRunner = Callable[..., tuple[int, str, str]]


@pytest.fixture
def recipe() -> Recipe:
    """A minimal ``Recipe(name="test", common=Fragment("test"))`` with one ``default`` variant."""
    return Recipe(name="test", common=Fragment("test"))


@pytest.fixture
def compiled(tmp_path: Path) -> CompileFactory:
    """Factory: ``compiled(recipe, variants=None)`` compiles into a new dir under ``tmp_path``."""
    counter = itertools.count()

    def factory(recipe: Recipe, variants: Variants = None) -> CompiledTree:
        return compile_tree(recipe, variants=variants, path=tmp_path / f"compiled-{next(counter)}")

    return factory


@pytest.fixture
def run_cli() -> CliRunner:
    """``run_cli(*argv) -> (exit_code, stdout, stderr)`` for the ``tundravm`` CLI."""

    def runner(*argv: str | os.PathLike[str]) -> tuple[int, str, str]:
        return _run_cli(*argv)

    return runner


__all__ = ["CliRunner", "CompileFactory", "compiled", "recipe", "run_cli"]
