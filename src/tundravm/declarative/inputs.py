"""The host files a recipe reads: :func:`recipe_inputs`.

Every ``Path`` a declaration names is read when the recipe lowers (``File`` and
``Unit`` contents, ``Template`` sources, ``FileTree`` trees, ``Kernel``
configs), and ``Tdxs.from_policy`` reads its policy file when the recipe module
runs. Tools that must notice a change to what a recipe compiles to (``tundravm
watch``) poll these paths besides the recipe's own modules.
"""

from __future__ import annotations

from pathlib import Path

from .model import Edit, File, FileTree, Fragment, Kernel, Recipe, Template, Unit
from .utils import Tdxs

_READ_FIELDS = frozenset(
    {
        ("File", "content"),
        ("Unit", "content"),
        ("Template", "template"),
        ("Kernel", "config"),
        ("FileTree", "source"),
    }
)
"""The ``(type, field)`` pairs whose ``Path`` value lowering reads from the host."""


def recipe_inputs(recipe: Recipe) -> frozenset[Path]:
    """The absolute host paths *recipe*'s declarations read, in every variant.

    The platform's contribution (its ``items``, a ``Kernel`` among them, and
    what it adds itself) counts as resolution sees it, before the recipe's.

    A ``FileTree`` contributes its source directory (its members are inputs
    too: a caller stamping it walks it); relative paths are taken from the
    working directory, as lowering reads them. Paths that do not exist are kept:
    creating one changes what the recipe lowers to.
    """
    found: set[Path] = set()
    for item in (*recipe.platform.contribution(), *recipe.items):
        _visit(item, found)
    for variant in recipe.variants:
        _visit(variant.add, found)
        for change in variant.changes:
            if isinstance(change, Edit):
                found.update(
                    value
                    for name, value in change.fields.items()
                    if isinstance(value, Path) and (change.target.kind, name) in _READ_FIELDS
                )
    return frozenset(path.absolute() for path in found)


def _visit(item: object, found: set[Path]) -> None:
    match item:
        case Tdxs(policy_file=Path() as path):
            found.add(path)
    match item:
        case Fragment(items=items):
            for child in items:
                _visit(child, found)
        case (
            File(content=Path() as path)
            | Unit(content=Path() as path)
            | Template(template=Path() as path)
            | Kernel(config=Path() as path)
            | FileTree(source=path)
        ):
            found.add(path)


__all__ = ["recipe_inputs"]
