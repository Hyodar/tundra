"""Pytest fixtures for tundravm recipes and modules.

Registered as the ``tundravm`` plugin through the ``pytest11`` entry point, so
the fixtures are available in any project that has tundravm installed. Disable
it with ``pytest -p no:tundravm``.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import pytest

from tundravm.backends.inprocess import InProcessBackend
from tundravm.image import Image
from tundravm.testing import CompiledTree, compile_tree
from tundravm.testing import run_cli as _run_cli


class CompileFactory(Protocol):
    def __call__(self, image: Image, profiles: Sequence[str] | None = None) -> CompiledTree: ...


CliRunner = Callable[..., tuple[int, str, str]]


@pytest.fixture
def image() -> Image:
    """A fresh ``Image(reproducible=True)`` with no backend."""
    return Image(reproducible=True)


@pytest.fixture
def inprocess_image(tmp_path: Path) -> Image:
    """An ``Image`` that bakes with ``InProcessBackend`` into ``tmp_path / "build"``."""
    return Image(backend=InProcessBackend(), build_dir=tmp_path / "build")


@pytest.fixture
def compiled(tmp_path: Path) -> CompileFactory:
    """Factory: ``compiled(image, profiles=None)`` compiles into a new dir under ``tmp_path``."""
    counter = itertools.count()

    def factory(image: Image, profiles: Sequence[str] | None = None) -> CompiledTree:
        return compile_tree(image, profiles=profiles, path=tmp_path / f"compiled-{next(counter)}")

    return factory


@pytest.fixture
def run_cli() -> CliRunner:
    """``run_cli(*argv) -> (exit_code, stdout, stderr)`` for the ``tundravm`` CLI."""

    def runner(*argv: str | os.PathLike[str]) -> tuple[int, str, str]:
        return _run_cli(*argv)

    return runner


__all__ = ["CliRunner", "CompileFactory", "compiled", "image", "inprocess_image", "run_cli"]
