"""Fetch request models used by integrity-checked input retrieval."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    sha256: str


@dataclass(frozen=True, slots=True)
class GitFetchRequest:
    repo: str
    ref: str
    tree_hash: str


__all__ = ["FetchRequest", "GitFetchRequest"]
