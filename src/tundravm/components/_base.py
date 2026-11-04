"""What the baseline components share."""

from __future__ import annotations

from typing import Any, Literal

from tundravm.declarative import model as dm


def _diagnostic(code: str, severity: Literal["error", "warning", "info"], message: str) -> Any:
    return dm.Diagnostic(code, severity, message)
