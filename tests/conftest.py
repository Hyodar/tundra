"""Shared test fixtures."""

from __future__ import annotations

import itertools
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest

from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative.model import Package, Recipe
from tundravm.testing import FakeMkosi, Profiles
from tundravm.testing._fake_sources import FakeSources
from tundravm.testing._model import CompiledTree, compile_tree


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """``-n auto`` spreads a run over the CPUs only when it deselects the integration tests:
    those build and boot real images and share the host's mkosi state, so they run serially."""
    if "not integration" in (config.option.markexpr or ""):
        return None
    return 0


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
def _private_package_cache(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Outside integration tests the host package cache is a fresh directory, never the
    user's ``~/.cache``."""
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))


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
    import tundravm._source as source_module

    def _refuse(source: object) -> str:
        raise AssertionError(
            f"test tried to resolve {source!r} over the network; pass resolver= to lock()"
        )

    monkeypatch.setattr(source_module, "default_resolver", _refuse)


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from *tmp_path*: lowering reads ``./build/tundravm.lock``; keep the repository's out."""
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """A fresh directory directly under the system temp dir, for a test that binds a Unix
    socket below it: ``tmp_path`` under ``-n auto`` (``.../popen-gwN/<test name>/``) leaves
    too few of a socket path's 108 bytes for a project's build tree."""
    path = Path(tempfile.mkdtemp(prefix="tvm-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def emitted_unit_corpus() -> tuple[tuple[str, str], ...]:
    """``tests.unit_corpus.emitted_units``: every unit file of every stage of the attested
    node's compiled tree, compiled once per session."""
    from tests.unit_corpus import emitted_units

    return emitted_units()
