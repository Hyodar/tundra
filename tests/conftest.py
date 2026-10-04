"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm.backends.inprocess import InProcessBackend


@pytest.fixture(autouse=True)
def _plain_format_outside_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``--format auto`` on ``text`` when the suite itself runs in GitHub Actions."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def inprocess_backend() -> InProcessBackend:
    """Provide an in-process backend for tests that call bake()."""
    return InProcessBackend()


@pytest.fixture(autouse=True)
def _no_network_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test resolves source pins over the network by accident."""
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


@pytest.fixture(autouse=True)
def _host_has_pefile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's ``pefile`` out of the local backend's tools-tree choice."""
    monkeypatch.setattr("tundravm.backends.local_linux.host_has_pefile", lambda: True)
