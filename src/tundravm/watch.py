"""``tundravm watch``: re-check a recipe whenever its sources change.

The sources are the recipe file and every ``.py`` under its directory, minus
hidden directories, ``__pycache__``, installed packages and the build output.
They are polled by mtime and size; on a change the modules the recipe imported
from that directory are dropped from ``sys.modules`` so the next load imports
them from their current source, then the check runs again and prints one line.
"""

from __future__ import annotations

import importlib
import itertools
import os
import sys
import time
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

Stamp = tuple[int, int] | None
"""A file's ``(st_mtime_ns, st_size)``, or ``None`` when it does not exist."""

_PACKAGE_DIR = Path(__file__).resolve().parent
_ENVIRONMENT_DIRS = frozenset({"site-packages", "dist-packages"})


def stamp(path: Path) -> Stamp:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _pruned(name: str) -> bool:
    return name.startswith(".") or name == "__pycache__" or name in _ENVIRONMENT_DIRS


def python_files(root: Path, skip: Collection[Path] = ()) -> Iterator[Path]:
    """Every ``.py`` under *root*, outside hidden directories, ``__pycache__``,
    installed packages and the directories in *skip*."""
    for parent, dirs, names in os.walk(root):
        here = Path(parent)
        dirs[:] = sorted(d for d in dirs if not _pruned(d) and here / d not in skip)
        yield from (here / name for name in sorted(names) if name.endswith(".py"))


def imported_under(root: Path) -> dict[str, Path]:
    """Module name -> source file of every loaded module whose file lies under *root*.

    tundravm itself and installed packages (a project ``.venv``, ``site-packages``)
    are left out: reloading them would split class identities.
    """
    prefixes = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    found: dict[str, Path] = {}
    for name, module in list(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str) or not filename.endswith(".py"):
            continue
        path = Path(filename).resolve()
        if (
            not path.is_relative_to(root)
            or path.is_relative_to(_PACKAGE_DIR)
            or _ENVIRONMENT_DIRS & set(path.parts)
            or any(path.is_relative_to(prefix) for prefix in prefixes)
        ):
            continue
        found[name] = path
    return found


@dataclass(slots=True)
class Sources:
    """The recipe file and the ``.py`` files under its directory.

    *skip* holds directories left out of the walk (the build output, so ``--write``
    does not wake the loop); *modules* the ones the recipe imported from there.
    """

    recipe: Path
    skip: set[Path] = field(default_factory=set)
    modules: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def of(cls, recipe: Path) -> Sources:
        return cls(recipe.resolve())

    def ignore(self, *dirs: Path) -> None:
        self.skip.update(path.resolve() for path in dirs)

    def files(self) -> tuple[Path, ...]:
        return tuple(dict.fromkeys((self.recipe, *python_files(self.recipe.parent, self.skip))))

    def stamps(self) -> dict[Path, Stamp]:
        return {path: stamp(path) for path in self.files()}

    def forget(self) -> None:
        """Drop the tracked modules from ``sys.modules`` so the next load re-imports them."""
        for name in self.modules:
            sys.modules.pop(name, None)
        importlib.invalidate_caches()

    def refresh(self) -> None:
        """Track what the last load imported, keeping modules a failed load did not reach."""
        self.modules.update(imported_under(self.recipe.parent))


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def verdict(*, errors: int, warnings: int, tree: str, drifted: int = 0) -> str:
    """``lint 0 errors 1 warning; tree stale (2 files)``.

    *drifted* sections (lockfile drift being every error) add ``; lock drifted (N sections)``
    before the tree half.
    """
    lock = f"; lock drifted ({plural(drifted, 'section')})" if drifted else ""
    return f"lint {plural(errors, 'error')} {plural(warnings, 'warning')}{lock}; {tree}"


def lint_failure(*, errors: int, code: str, message: str) -> str:
    """``lint 1 error (app-privileged-port: ...)``: lint errors that keep the recipe from
    lowering, so no tree is checked; *code* and *message* are the first finding's."""
    return f"lint {plural(errors, 'error')} ({code}: {message})"


def tree_verdict(*, changed: int, exists: bool, wrote: Path | None = None) -> str:
    """The tree half of the line: ``tree up to date``, ``tree stale (N files)``,
    ``tree missing (N files to write)`` or, after a write, ``wrote PATH``."""
    if changed == 0:
        return "tree up to date"
    if wrote is not None:
        return f"wrote {wrote}"
    files = plural(changed, "file")
    return f"tree stale ({files})" if exists else f"tree missing ({files} to write)"


@dataclass(slots=True)
class Watch:
    """The polling loop: run *check* once, then again whenever a source changes.

    *check* loads the recipe and returns the verdict line; *ticks* bounds the loop
    (one poll per item; default forever) and *sleep* waits *interval* seconds
    before each poll. Ctrl-C ends the loop with exit code 0.
    """

    sources: Sources
    check: Callable[[], str]
    out: TextIO
    interval: float = 1.0
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = datetime.now
    seen: dict[Path, Stamp] = field(default_factory=dict)

    def run(self, ticks: Iterable[object] | None = None) -> int:
        try:
            self.cycle()
            for _ in itertools.count() if ticks is None else ticks:
                self.sleep(self.interval)
                if self.sources.stamps() != self.seen:
                    self.cycle()
        except KeyboardInterrupt:
            print("stopped", file=self.out, flush=True)
        return 0

    def cycle(self) -> None:
        before = self.sources.stamps()
        self.sources.forget()
        line = self.check()
        self.sources.refresh()
        self.seen = {
            path: before[path] if path in before else stamp(path) for path in self.sources.files()
        }
        print(f"{self.clock():%H:%M:%S} {line}", file=self.out, flush=True)


__all__ = [
    "Sources",
    "Stamp",
    "Watch",
    "imported_under",
    "lint_failure",
    "plural",
    "python_files",
    "stamp",
    "tree_verdict",
    "verdict",
]
