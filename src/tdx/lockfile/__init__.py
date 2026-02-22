"""Lockfile model types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LockEntry:
    source: str
    digest: str


__all__ = ["LockEntry"]
