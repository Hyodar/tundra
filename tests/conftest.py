"""Shared test fixtures."""

from __future__ import annotations

import pytest

from tdx.backends.inprocess import InProcessBackend


@pytest.fixture
def inprocess_backend() -> InProcessBackend:
    """Provide an in-process backend for tests that call bake()."""
    return InProcessBackend()
