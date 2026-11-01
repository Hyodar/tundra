"""Pytest fixtures for testing tundravm images.

Registered as the ``tundravm`` plugin through the ``pytest11`` entry point, so
the fixtures are available in any project that has tundravm installed. Disable
it with ``pytest -p no:tundravm``.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import pytest

from tundravm.declarative.model import Package, Recipe
from tundravm.testing import (
    BootResult,
    BootUnavailable,
    CompiledTree,
    FakeMkosi,
    Profiles,
    compile_tree,
)
from tundravm.testing import boot as _boot
from tundravm.testing import run_cli as _run_cli
from tundravm.testing._boot import Bootable


class CompileFactory(Protocol):
    def __call__(self, recipe: Recipe, profiles: Profiles = None) -> CompiledTree: ...


CliRunner = Callable[..., tuple[int, str, str]]
BootRunner = Callable[..., BootResult]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "boot: boots a built image under QEMU (needs qemu-system-x86_64 and OVMF)"
    )


@pytest.fixture
def recipe() -> Recipe:
    """A minimal lint-clean internal model named ``test``: one ``default`` profile, the
    kernel package. For testing tundravm itself; an image test builds an ``Image``.
    """
    return Recipe(name="test", items=(Package("linux-image-amd64"),))


@pytest.fixture
def compiled(tmp_path: Path) -> CompileFactory:
    """Factory: ``compiled(recipe, profiles=None)`` compiles the internal model's profiles
    (:func:`~tundravm.testing.compile_tree`) into a new dir under ``tmp_path``."""
    counter = itertools.count()

    def factory(recipe: Recipe, profiles: Profiles = None) -> CompiledTree:
        return compile_tree(recipe, profiles=profiles, path=tmp_path / f"compiled-{next(counter)}")

    return factory


@pytest.fixture
def run_cli() -> CliRunner:
    """``run_cli(*argv) -> (exit_code, stdout, stderr)`` for the ``tundravm`` CLI."""

    def runner(*argv: str | os.PathLike[str]) -> tuple[int, str, str]:
        return _run_cli(*argv)

    return runner


@pytest.fixture
def boot(tmp_path: Path) -> BootRunner:
    """``boot(artifact, **kwargs) -> BootResult``: :func:`tundravm.testing.boot` with the
    console log under ``tmp_path``; skips the test when QEMU or OVMF is missing."""
    counter = itertools.count()

    def runner(artifact: Bootable, **kwargs: Any) -> BootResult:
        kwargs.setdefault("log", tmp_path / f"boot-{next(counter)}" / "console.log")
        try:
            return _boot(artifact, **kwargs)
        except BootUnavailable as exc:
            pytest.skip(f"cannot boot here: {exc}")

    return runner


@pytest.fixture
def fake_mkosi(monkeypatch: pytest.MonkeyPatch) -> FakeMkosi:
    """A :class:`~tundravm.testing.FakeMkosi` in place of mkosi for this test: backends run
    for real, nothing forks, ``build`` writes placeholder outputs. Configure it by setting
    its fields (``summary``, ``outputs``, ``fail``, ``sudo``)."""
    return FakeMkosi().install(monkeypatch.setattr)


__all__ = [
    "BootRunner",
    "CliRunner",
    "CompileFactory",
    "boot",
    "compiled",
    "fake_mkosi",
    "recipe",
    "run_cli",
]
