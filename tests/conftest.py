"""Shared test fixtures."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Protocol

import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.model import Package, Recipe
from tundravm.testing import FakeMkosi, Profiles
from tundravm.testing._fake_sources import FakeSources
from tundravm.testing._model import CompiledTree, compile_tree


class CompileFactory(Protocol):
    def __call__(self, recipe: Recipe, profiles: Profiles = None) -> CompiledTree: ...


@pytest.fixture
def recipe() -> Recipe:
    """A minimal lint-clean internal model named ``test``: one ``default`` profile, the
    kernel package."""
    return Recipe(name="test", items=(Package("linux-image-amd64"),))


@pytest.fixture
def compiled(tmp_path: Path) -> CompileFactory:
    """Factory: ``compiled(recipe, profiles=None)`` compiles the internal model's profiles
    (``compile_tree``) into a new dir under ``tmp_path``."""
    counter = itertools.count()

    def factory(recipe: Recipe, profiles: Profiles = None) -> CompiledTree:
        return compile_tree(recipe, profiles=profiles, path=tmp_path / f"compiled-{next(counter)}")

    return factory


@pytest.fixture(autouse=True)
def _plain_format_outside_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``--format auto`` on ``text`` when the suite itself runs in GitHub Actions."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def _fake_process_boundary(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside integration tests a build runs the Linux backend against a fake mkosi and
    fake source checkouts: the whole build path runs, nothing forks, fetches or needs
    privileges, and a unit test never builds a real image by accident (on any host).
    Tests of the boundary itself are marked ``boundary``."""
    if request.node.get_closest_marker("integration") is not None:
        return
    if request.node.get_closest_marker("boundary") is not None:
        return
    monkeypatch.setenv("TUNDRAVM_BACKEND", "local")
    monkeypatch.setattr(LocalLinuxBackend, "_ensure_linux", lambda self: None)
    FakeMkosi().install(monkeypatch.setattr)
    FakeSources().install(monkeypatch.setattr)


@pytest.fixture(autouse=True)
def _no_network_resolution(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test resolves source pins over the network by accident.

    Integration tests (marked ``integration``) build real images and may resolve
    and fetch sources for real, so the guard does not apply to them.
    """
    if request.node.get_closest_marker("integration") is not None:
        return
    pin_tundra_tools_only(monkeypatch)


def pin_tundra_tools_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the implied evidence service's source to a fixed commit and refuse every
    other network resolution. Module-scoped fixtures that run ``lock`` call this
    themselves: the autouse guard above is function-scoped and is not active yet when
    they are set up (tundra-tools is a private repository, so CI cannot reach it)."""
    import tundravm._source as source_module
    from tundravm.declarative.utils import TUNDRA_TOOLS

    def _refuse(source: object) -> str:
        # Every image implies the evidence service, which builds tundra-tools, so a
        # `lock` of any image reaches that one source. It resolves to a fixed commit
        # here; anything else is a test reaching the network by accident.
        if getattr(source, "url", None) == TUNDRA_TOOLS.url:
            return TUNDRA_TOOLS_COMMIT
        raise AssertionError(
            f"test tried to resolve {source!r} over the network; pass resolver= to lock()"
        )

    monkeypatch.setattr(source_module, "default_resolver", _refuse)


TUNDRA_TOOLS_COMMIT = "1" * 40
"""What the hermetic resolver pins the implied evidence service's source to."""


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from *tmp_path*: lowering reads ``./build/tundravm.lock``; keep the repository's out."""
    monkeypatch.chdir(tmp_path)
