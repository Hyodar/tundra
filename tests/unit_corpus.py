"""Every systemd unit text in the repository, for the unit text and unit lint tests.

Sources: the committed attested node tree, the unit constants of modules, examples and templates,
and the emitted tree of every example, the attested node and ``init`` template.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

from tests.helpers import ATTESTED_NODE, EXAMPLE_RECIPES, REPO_ROOT

_UNIT_PATH = re.compile(r"(^|/)(usr/lib|etc)/systemd/system/")
_UNIT_SUFFIX = re.compile(r"\.(service|socket|target|timer|mount|path|conf)$")


def is_unit_file(path: str) -> bool:
    """Whether *path* (relative to a tree root) is a unit or drop-in under a systemd directory."""
    return bool(_UNIT_PATH.search(path) and _UNIT_SUFFIX.search(path))


def golden_units() -> Iterator[tuple[str, str]]:
    """``(path, text)`` for every unit file in the committed attested node tree."""
    root = ATTESTED_NODE / "mkosi"
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


def recipe_paths() -> dict[str, Path]:
    """Every stage of the attested node and its ``image.py``, by name."""
    paths = {path.stem: path for path in EXAMPLE_RECIPES}
    paths["attested-node"] = ATTESTED_NODE / "image.py"
    return paths


@cache
def emitted_units() -> tuple[tuple[str, str], ...]:
    """``(recipe:path, text)`` for every unit file of every recipe's emitted tree."""
    from tundravm.declarative import compile, load

    found: list[tuple[str, str]] = []
    for name, path in sorted(recipe_paths().items()):
        tree = compile(load(path, extra_paths=[REPO_ROOT]))
        for entry in tree.entries:
            if entry.content is not None and entry.symlink is None and is_unit_file(entry.path):
                found.append((f"{name}:{entry.path}", entry.content.decode("utf-8")))
    return tuple(found)
