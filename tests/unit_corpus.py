"""Every systemd unit text in the repository, for the unit text and unit lint tests.

Sources: the committed surge tree, the unit constants of modules, examples and templates,
and the emitted tree of every example, surge variant and ``init`` template.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, SURGE_EXAMPLE

_UNIT_PATH = re.compile(r"(^|/)(usr/lib|etc)/systemd/system/")
_UNIT_SUFFIX = re.compile(r"\.(service|socket|target|timer|mount|path|conf)$")


def is_unit_file(path: str) -> bool:
    """Whether *path* (relative to a tree root) is a unit or drop-in under a systemd directory."""
    return bool(_UNIT_PATH.search(path) and _UNIT_SUFFIX.search(path))


def golden_units() -> Iterator[tuple[str, str]]:
    """``(path, text)`` for every unit file in the committed surge golden tree."""
    root = SURGE_EXAMPLE / "mkosi"
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and not path.is_symlink() and is_unit_file(relative):
            yield f"golden:{relative}", path.read_text(encoding="utf-8")


def constant_units() -> Iterator[tuple[str, str]]:
    """``(name, text)`` for the unit text constants written in Python."""
    from tundravm._modules import devtools
    from tundravm.compiler.emit_mkosi import MINIMAL_TARGET_UNIT

    yield "MINIMAL_TARGET_UNIT", MINIMAL_TARGET_UNIT
    yield "SERIAL_CONSOLE_SERVICE", devtools.SERIAL_CONSOLE_SERVICE


def recipe_paths(scratch: Path) -> dict[str, Path]:
    """Every example recipe, the surge recipe and every ``init`` template, by name; the
    templates are rendered into *scratch*."""
    from tundravm.templates import TEMPLATES, render_recipe_template

    paths = {path.stem: path for path in EXAMPLE_RECIPES}
    paths["surge"] = SURGE_EXAMPLE / "image.py"
    for template in TEMPLATES:
        path = scratch / template / f"{template}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            render_recipe_template(
                title=template,
                filename=path.name,
                release="trixie",
                template=template,
            ),
            encoding="utf-8",
        )
        paths[f"init-{template}"] = path
    return paths


@cache
def emitted_units(scratch: Path) -> tuple[tuple[str, str], ...]:
    """``(recipe:path, text)`` for every unit file of every recipe's emitted tree."""
    from tundravm.declarative import compile, load

    found: list[tuple[str, str]] = []
    for name, path in sorted(recipe_paths(scratch).items()):
        tree = compile(load(path, extra_paths=[REPO_ROOT]))
        for entry in tree.entries:
            if entry.content is not None and entry.symlink is None and is_unit_file(entry.path):
                found.append((f"{name}:{entry.path}", entry.content.decode("utf-8")))
    return tuple(found)
