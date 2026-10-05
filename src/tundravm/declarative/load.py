"""``load()``: import a recipe file and return the ``Recipe`` it binds."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .model import Recipe


def load(
    path: str | Path,
    *,
    attribute: str | None = "recipe",
    extra_paths: Sequence[str | Path] = (),
) -> Recipe:
    """The ``Recipe`` that the Python file at *path* binds to *attribute*.

    *attribute* may also name a zero-argument factory returning the recipe.
    ``attribute=None`` discovers it like the CLI does: a module-level
    ``recipe``, else the only ``Recipe`` value, else the only zero-argument
    ``build()`` factory. The file's directory and *extra_paths* are importable
    while it runs.
    """
    from tundravm.recipe import load_recipe

    return load_recipe(path, attribute=attribute, extra_paths=extra_paths)


__all__ = ["load"]
