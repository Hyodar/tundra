"""The external command runner every deploy adapter calls, and the names a deploy draws,
both replaceable: tests answer the commands, ``deploy --plan`` records them."""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
"""Runs one argv and returns its result; adapters take one to make their commands assertable."""


def run_captured(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* with its output captured as text."""
    return subprocess.run(list(cmd), capture_output=True, text=True, check=False)


def stderr_of(result: subprocess.CompletedProcess[str]) -> str:
    """The first 2000 characters of *result*'s stderr (empty when not captured)."""
    return result.stderr[:2000] if result.stderr else ""


@dataclass(frozen=True, slots=True)
class Fresh:
    """What a deploy draws anew each time: a hex suffix of a length (resource names, the
    deployment id) and the epoch seconds of an Azure image version."""

    hex: Callable[[int], str] = lambda length: uuid.uuid4().hex[:length]
    epoch: Callable[[], str] = lambda: str(int(time.time()))


RANDOM = Fresh()
PLANNED = Fresh(hex=lambda length: "X" * length, epoch=lambda: "EPOCH")
"""The plan's placeholders: ``XXXXXXXX`` and ``EPOCH`` stand for what the deploy draws."""
