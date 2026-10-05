"""The external command runner every deploy adapter calls, replaceable in tests."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
"""Runs one argv and returns its result; adapters take one to make their commands assertable."""


def run_captured(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with its output captured as text."""
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def run_attached(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* in the foreground on this terminal (nothing captured)."""
    return subprocess.run(list(cmd), text=True, check=False)


def stderr_of(result: subprocess.CompletedProcess[str]) -> str:
    """The first 2000 characters of *result*'s stderr (empty when not captured)."""
    return result.stderr[:2000] if result.stderr else ""
