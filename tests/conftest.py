"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm.backends.inprocess import InProcessBackend


@pytest.fixture(autouse=True)
def _plain_format_outside_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``--format auto`` on ``text`` when the suite itself runs in GitHub Actions."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def _simulated_backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that binds no ``backend`` bakes in process outside integration tests.

    The loader picks this host's backend for such a file (local on Linux x86-64, Lima
    elsewhere); a unit test never builds a real image by accident.
    """
    if request.node.get_closest_marker("integration") is None:
        monkeypatch.setenv("TUNDRAVM_BACKEND", "inprocess")


@pytest.fixture
def inprocess_backend() -> InProcessBackend:
    """Provide an in-process backend for tests that call bake()."""
    return InProcessBackend()


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
