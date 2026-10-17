"""``tundravm clean``: remove the parts of a build output directory a bake leaves behind.

The parts (``clean --what``) are ``sources`` (``BUILD/.sources``), ``tree``
(``BUILD/mkosi``, the bake's own tree), ``artifacts`` (each ``BUILD/<variant>/`` and
``BUILD/bake-result.json``), ``evidence`` (``BUILD/evidence``, what ``tundravm
evidence`` writes), ``state`` (``BUILD/.mkosi``, which holds the cached tools tree,
the mkosi state next to the tree's config, and ``BUILD/.tundravm-hashes.json``, the
artifact sha256s ``status`` caches) and ``reproduce`` (``BUILD/.reproduce``,
the second build a failed ``bake --verify-reproducible`` keeps). The lockfile is never
one of them, and a release directory never goes: a part that holds one (``release
--releases`` pointed inside the build directory) is refused (:func:`holds_release`).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Collection, Iterable, Sequence
from pathlib import Path
from shutil import which

from ._hashes import HASH_CACHE_NAME
from ._source import SOURCES_DIRNAME
from .backends.base import MKOSI_STATE_NAMES
from .backends.local_linux import STATE_DIRNAME
from .models import BAKE_RESULT_FILENAME, REPRODUCE_DIRNAME

PARTS: tuple[str, ...] = ("sources", "tree", "artifacts", "evidence", "state", "reproduce")
"""What ``clean --what`` names, in removal order; ``all`` is every one."""
TREE_DIRNAME = "mkosi"
EVIDENCE_DIRNAME = "evidence"
"""``BUILD/evidence``: the directory ``tundravm evidence`` writes."""
RESERVED = frozenset(
    {SOURCES_DIRNAME, TREE_DIRNAME, STATE_DIRNAME, REPRODUCE_DIRNAME, EVIDENCE_DIRNAME}
)

SudoRunner = Callable[[Sequence[str]], int]
"""Runs ``sudo rm -rf -- PATH`` and returns its exit status."""


def run_sudo(argv: Sequence[str]) -> int:
    """Run *argv* attached to the terminal, so sudo can ask for a password."""
    return subprocess.run(list(argv), check=False).returncode


def sudo_runner() -> SudoRunner | None:
    """:func:`run_sudo` when ``sudo`` is on ``PATH``, else ``None``."""
    return run_sudo if which("sudo") is not None else None


def baked_variants(build: Path) -> tuple[str, ...]:
    """The variants ``BUILD/bake-result.json`` records; empty when it is missing or unreadable."""
    try:
        payload = json.loads((build / BAKE_RESULT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    variants = payload.get("variants") if isinstance(payload, dict) else None
    return tuple(sorted(str(name) for name in variants)) if isinstance(variants, dict) else ()


def clean_paths(build: Path, parts: Collection[str], *, variants: Iterable[str] = ()) -> list[Path]:
    """The existing paths under *build* that removing *parts* deletes, in removal order.

    Artifact directories are those of *variants* and of the variants the
    manifest records.
    """
    paths: list[Path] = []
    if "sources" in parts:
        paths.append(build / SOURCES_DIRNAME)
    if "tree" in parts:
        paths.append(build / TREE_DIRNAME)
    if "artifacts" in parts:
        names = sorted({*variants, *baked_variants(build)} - RESERVED)
        paths.extend(build / name for name in names if (build / name).is_dir())
        paths.append(build / BAKE_RESULT_FILENAME)
    if "evidence" in parts:
        paths.append(build / EVIDENCE_DIRNAME)
    if "state" in parts:
        paths.append(build / STATE_DIRNAME)
        paths.append(build / HASH_CACHE_NAME)
        if "tree" not in parts:
            paths.extend(_tree_state(build / TREE_DIRNAME))
    if "reproduce" in parts:
        paths.append(build / REPRODUCE_DIRNAME)
    return [path for path in paths if path.exists() or path.is_symlink()]


RELEASE_INDEX = "release.json"
"""The file that marks a release directory (``tundravm release``)."""
RELEASE_DEPTH = 3
"""How deep below a part :func:`holds_release` looks: a releases directory directly in
it, or one level further down."""


def holds_release(path: Path) -> Path | None:
    """A release directory at or under *path* (to :data:`RELEASE_DEPTH`), else ``None``.

    A release directory holds ``release.json`` with a ``tundravm.release/`` schema.
    """
    if not path.is_dir() or path.is_symlink():
        return None
    base = len(path.parts)
    for current, dirs, files in os.walk(path):
        here = Path(current)
        if RELEASE_INDEX in files and _is_release_index(here / RELEASE_INDEX):
            return here
        if len(here.parts) - base >= RELEASE_DEPTH:
            dirs.clear()
        dirs.sort()
    return None


def _is_release_index(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and str(payload.get("schema", "")).startswith(
        "tundravm.release/"
    )


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
    "EVIDENCE_DIRNAME",
    "PARTS",
    "SudoRunner",
    "baked_variants",
    "clean_paths",
    "holds_release",
    "owner_hint",
    "remove",
    "run_sudo",
    "sudo_runner",
]
