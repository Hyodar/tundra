"""Shared test fixtures."""

from __future__ import annotations

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
    import tundravm.source as source_module

    def _refuse(source: object) -> str:
        raise AssertionError(
            f"test tried to resolve {source!r} over the network; pass resolver= to lock()"
        )

    monkeypatch.setattr(source_module, "default_resolver", _refuse)
