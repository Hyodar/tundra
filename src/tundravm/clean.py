"""``tundravm clean``: remove the parts of a build output directory a bake leaves behind.

The parts are ``sources`` (``OUT/.sources``), ``tree`` (``OUT/mkosi``),
``artifacts`` (each ``OUT/<variant>/`` and ``OUT/bake-result.json``) and
``state`` (``OUT/.mkosi``, which holds the cached tools tree, the mkosi state
next to the tree's config, and ``OUT/.reproduce``, the second build a failed
``bake --verify-reproducible`` keeps). The lockfile is never one of them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Collection, Iterable, Sequence
from pathlib import Path
from shutil import which

from ._source import SOURCES_DIRNAME
from .backends.base import MKOSI_STATE_NAMES
from .backends.local_linux import STATE_DIRNAME
from .models import BAKE_RESULT_FILENAME, REPRODUCE_DIRNAME

PARTS: tuple[str, ...] = ("sources", "tree", "artifacts", "state")
TREE_DIRNAME = "mkosi"
RESERVED = frozenset({SOURCES_DIRNAME, TREE_DIRNAME, STATE_DIRNAME, REPRODUCE_DIRNAME})

SudoRunner = Callable[[Sequence[str]], int]
"""Runs ``sudo rm -rf -- PATH`` and returns its exit status."""


def run_sudo(argv: Sequence[str]) -> int:
    """Run *argv* attached to the terminal, so sudo can ask for a password."""
    return subprocess.run(list(argv), check=False).returncode


def sudo_runner() -> SudoRunner | None:
    """:func:`run_sudo` when ``sudo`` is on ``PATH``, else ``None``."""
    return run_sudo if which("sudo") is not None else None


def baked_variants(out: Path) -> tuple[str, ...]:
    """The variants ``OUT/bake-result.json`` records; empty when it is missing or unreadable."""
    try:
        payload = json.loads((out / BAKE_RESULT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    return tuple(sorted(str(name) for name in profiles)) if isinstance(profiles, dict) else ()


def clean_paths(out: Path, parts: Collection[str], *, variants: Iterable[str] = ()) -> list[Path]:
    """The existing paths under *out* that removing *parts* deletes, in removal order.

    Artifact directories are those of *variants* and of the variants the
    manifest records.
    """
    paths: list[Path] = []
    if "sources" in parts:
        paths.append(out / SOURCES_DIRNAME)
    if "tree" in parts:
        paths.append(out / TREE_DIRNAME)
    if "artifacts" in parts:
        names = sorted({*variants, *baked_variants(out)} - RESERVED)
        paths.extend(out / name for name in names if (out / name).is_dir())
        paths.append(out / BAKE_RESULT_FILENAME)
    if "state" in parts:
        paths.append(out / STATE_DIRNAME)
        paths.append(out / REPRODUCE_DIRNAME)
        if "tree" not in parts:
            paths.extend(_tree_state(out / TREE_DIRNAME))
    return [path for path in paths if path.exists() or path.is_symlink()]


def _tree_state(tree: Path) -> list[Path]:
    """mkosi's build state next to the config at *tree* and in its variant directories."""
    if not tree.is_dir():
        return []
    roots = [tree, *(child for child in sorted(tree.iterdir()) if child.is_dir())]
    return [
        root / name
        for root in roots
        for name in sorted(MKOSI_STATE_NAMES)
        if (root / name).exists() or (root / name).is_symlink()
    ]


def remove(path: Path, *, sudo: SudoRunner | None) -> str | None:
    """Delete *path*; ``None`` on success, else why it is still there.

    ``sudo rm -rf`` is used only when the user cannot delete it (root-owned
    mkosi output) and *sudo* is given.
    """
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return None
    except PermissionError as exc:
        if sudo is None:
            return f"{exc.filename or path} is not writable by this user and sudo is not available"
    except OSError as exc:
        return f"{exc.filename or path}: {exc.strerror or exc}"
    if sudo(["sudo", "rm", "-rf", "--", str(path)]) != 0 or path.exists():
        return f"`sudo rm -rf -- {path}` failed"
    return None


def owner_hint(failed: Sequence[Path]) -> str:
    """What to do about the paths ``remove`` left behind."""
    joined = " ".join(str(path) for path in failed)
    return f"hint: remove them as their owner, e.g. `sudo rm -rf -- {joined}`"


__all__ = [
    "PARTS",
    "SudoRunner",
    "baked_variants",
    "clean_paths",
    "owner_hint",
    "remove",
    "run_sudo",
    "sudo_runner",
]
