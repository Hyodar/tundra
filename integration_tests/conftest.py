"""Shared helpers for integration tests."""

from __future__ import annotations

from pathlib import Path


def snapshot_tree(root: Path) -> dict[str, str]:
    """Capture every file under *root* as ``{relative_path: content}``."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                tree[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                tree[str(path.relative_to(root))] = "<binary>"
    return tree
