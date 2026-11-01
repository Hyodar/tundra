"""Test helpers for the internal model an image lowers to (:mod:`tundravm.declarative`).

tundravm's own tests use them; an image's tests use :mod:`tundravm.testing`. Nothing here
is public: :func:`fragment_plan` plans internal declarations on :data:`TEST_PLATFORM`,
:func:`fake_fragment` builds a small group of them, :func:`compile_tree` writes a recipe's
tree as a :class:`CompiledTree`, :func:`assert_tree_matches` compares it with a golden
directory, and :func:`lint_model` lints a recipe the way ``tundravm check`` does.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path

from tundravm.compiler import PAYLOAD
from tundravm.declarative import lifecycle
from tundravm.declarative.lifecycle import Lock, check_report
from tundravm.declarative.model import (
    BootStep,
    Check,
    Declaration,
    File,
    Fragment,
    Package,
    Ready,
    Recipe,
    Variant,
)
from tundravm.declarative.utils import Network
from tundravm.diagnostics import Diagnostic
from tundravm.diff import TreeDiff, _foreign_profile_globs, diff_trees

from . import UPDATE_GOLDEN_ENV, Profiles
from ._image import PlannedImage, planned_model

_UNIFIED_LINES = 200
_UNIT_DIR = "mkosi.extra/usr/lib/systemd/system"

TEST_PLATFORM = Fragment("test-platform", (Package("linux-image-amd64"), Network()))
"""What :func:`fragment_plan` boots on by default: the kernel package and a DHCP
network, so ``kernel-missing`` and ``network-missing`` stay quiet and the findings of what is
under test stand alone."""


class CompiledTree:
    """A compiled mkosi tree on disk, with readers for the files tests inspect.

    ``profiles`` are the compiled profiles. Every ``profile`` argument names one of
    them and defaults to the first. A profile reads the payload (``payload/``) with
    its overlay (``payload/mkosi.profiles/<profile>/``) over it, as mkosi merges them:
    a path in the overlay wins, scripts and configuration accumulate.
    """

    __slots__ = ("default_profile", "root", "profiles")

    def __init__(self, root: Path, profiles: Sequence[str], default_profile: str = "default"):
        self.root = Path(root)
        self.profiles = tuple(profiles)
        if default_profile not in self.profiles and self.profiles:
            default_profile = self.profiles[0]
        self.default_profile = default_profile

    def __fspath__(self) -> str:
        return os.fspath(self.root)

    def __repr__(self) -> str:
        return f"CompiledTree({str(self.root)!r}, profiles={self.profiles!r})"

    def layers(self, name: str | None = None) -> tuple[Path, ...]:
        """The configuration directories profile *name* builds from: the payload, then its
        overlay when it has one."""
        name = name or self.default_profile
        if name not in self.profiles:
            raise KeyError(f"profile {name!r} was not compiled; compiled: {list(self.profiles)}")
        payload = self.root / PAYLOAD
        overlay = payload / "mkosi.profiles" / name
        return (payload, overlay) if overlay.is_dir() else (payload,)

    def profile(self, name: str | None = None) -> Path:
        """The directory holding what profile *name* adds: its overlay, else the
        payload."""
        return self.layers(name)[-1]

    def path(self, relpath: str, profile: str | None = None) -> Path:
        """*relpath* in the profile's overlay when it is there, else in the payload."""
        for layer in reversed(self.layers(profile)):
            found = layer / relpath.lstrip("/")
            if found.exists() or found.is_symlink():
                return found
        return self.layers(profile)[0] / relpath.lstrip("/")

    def read(self, relpath: str, profile: str | None = None) -> str:
        return self.path(relpath, profile).read_text(encoding="utf-8")

    def exists(self, relpath: str, profile: str | None = None) -> bool:
        return self.path(relpath, profile).exists()

    def files(self, profile: str | None = None) -> list[str]:
        """Sorted POSIX paths of every file the profile reads, relative to its layer (the
        payload's own profile overlays left out)."""
        found: set[str] = set()
        for layer in self.layers(profile):
            for path in layer.rglob("*"):
                rel = path.relative_to(layer).as_posix()
                if not path.is_dir() and not rel.startswith("mkosi.profiles/"):
                    found.add(rel)
        return sorted(found)

    def unit(self, name: str, profile: str | None = None) -> str:
        """Systemd unit text; a bare name such as ``"app"`` means ``app.service``."""
        unit = name if "." in name else f"{name}.service"
        path = self.path(f"{_UNIT_DIR}/{unit}", profile)
        if not path.is_file():
            found = sorted(p.name for p in path.parent.glob("*")) if path.parent.is_dir() else []
            raise FileNotFoundError(f"no unit {unit!r} in {path.parent}; found: {found}")
        return path.read_text(encoding="utf-8")

    def scripts(self, phase: str, profile: str | None = None) -> list[Path]:
        """The profile's ``mkosi.<phase>.d/`` scripts in the order mkosi runs them."""
        return [
            script
            for layer in self.layers(profile)
            for script in sorted((layer / f"mkosi.{phase}.d").glob("*"))
        ]

    def script(self, phase: str, profile: str | None = None) -> str:
        """The text of the profile's *phase* scripts, in the order mkosi runs them."""
        found = self.scripts(phase, profile)
        if not found:
            phases = sorted(
                {
                    path.name.removeprefix("mkosi.").removesuffix(".d")
                    for layer in self.layers(profile)
                    for path in layer.glob("mkosi.*.d")
                    if any(path.iterdir())
                }
            )
            raise FileNotFoundError(
                f"no {phase!r} script for profile {profile!r}; scripts: {', '.join(phases)}"
            )
        return "\n".join(path.read_text(encoding="utf-8") for path in found)

    def runtime_init(self, profile: str | None = None, *, step: str | None = None) -> str:
        """The runtime-init script text: one *step*'s script, or every step in the order
        ``runtime-init.service`` lists them (``/usr/lib/runtime-init/<step>``)."""
        if step is not None:
            return self.read(f"mkosi.extra/usr/lib/runtime-init/{step}", profile)
        aggregate = self.unit("runtime-init.service", profile)
        after = next(line for line in aggregate.splitlines() if line.startswith("After="))
        steps = [
            unit.removeprefix("runtime-init-").removesuffix(".service")
            for unit in after[6:].split()
        ]
        return "".join(self.runtime_init(profile, step=name) for name in steps)

    def conf(self, profile: str | None = None) -> str:
        """The profile's payload configuration as mkosi reads it, one text: the payload's
        ``mkosi.conf``, its ``mkosi.conf.d/*.conf`` and the overlay's ``mkosi.conf``."""
        texts: list[str] = []
        for layer in self.layers(profile):
            texts.append((layer / "mkosi.conf").read_text(encoding="utf-8"))
            texts += [p.read_text(encoding="utf-8") for p in sorted(layer.glob("mkosi.conf.d/*"))]
        return "\n".join(texts)

    def main_conf(self) -> str:
        """The root ``mkosi.conf``: the settings every image shares and the QEMU disk."""
        return (self.root / "mkosi.conf").read_text(encoding="utf-8")


def _names(profiles: Profiles) -> list[str] | None:
    if profiles is None:
        return None
    return [profiles] if isinstance(profiles, str) else list(profiles)


def compile_tree(
    recipe: Recipe, *, profiles: Profiles = None, path: str | Path | None = None
) -> CompiledTree:
    """Compile *profiles* (default: every profile) of *recipe* into *path* or a temp dir.

    *recipe* is the internal model an image lowers to; for an :class:`~tundravm.Image`,
    ``tundravm.compile(image).write(path)`` does the same. Unknown profile names raise
    ``ValidationError``. No lockfile is consulted, so source builds use their refs.
    """
    root = Path(path) if path is not None else Path(tempfile.mkdtemp(prefix="tundravm-tree-"))
    tree = lifecycle.compile(recipe, variants=_names(profiles))
    tree.write(root)
    return CompiledTree(root, tree.profiles, default_profile=tree.profiles[0])


def assert_tree_matches(
    recipe: Recipe,
    golden_dir: str | Path,
    *,
    profiles: Profiles = None,
    update: bool | None = None,
) -> TreeDiff:
    """Compare the compiled tree of *recipe* (the internal model) with *golden_dir*.

    Unlike :func:`assert_tree`, a mismatch shows a unified diff; when updating it rewrites
    *golden_dir* instead. *update* defaults to ``TUNDRAVM_UPDATE_GOLDEN=1`` in the
    environment. Profile overlays in *golden_dir* that were not compiled are neither
    compared nor touched. Returns the diff from the golden tree to the compiled one.
    """
    golden = Path(golden_dir)
    if update is None:
        update = os.environ.get(UPDATE_GOLDEN_ENV) == "1"
    with tempfile.TemporaryDirectory(prefix="tundravm-golden-") as tmp:
        tree = compile_tree(recipe, profiles=profiles, path=Path(tmp))
        ignore = _foreign_profile_globs(golden, tree.profiles)
        diff = diff_trees(golden, tree.root, ignore=ignore)
        if update:
            if not diff.is_clean:
                _replace_tree(tree.root, golden, ignore)
            return diff
    if diff.is_clean:
        return diff
    lines = diff.unified().splitlines(keepends=True)
    shown = "".join(lines[:_UNIFIED_LINES])
    if len(lines) > _UNIFIED_LINES:
        shown += f"... {len(lines) - _UNIFIED_LINES} more diff lines\n"
    raise AssertionError(
        f"compiled tree differs from {golden}\n{diff.stat()}\n{shown}\n"
        f"Re-run with {UPDATE_GOLDEN_ENV}=1 to accept the compiled tree."
    )


def _replace_tree(source: Path, golden: Path, keep: Sequence[str]) -> None:
    """Delete golden files not matched by the *keep* globs, then copy *source* over."""
    if golden.is_dir():
        for path in sorted(golden.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink():
                if not any(path.iterdir()):
                    path.rmdir()
            elif not any(fnmatchcase(path.relative_to(golden).as_posix(), g) for g in keep):
                path.unlink()
    shutil.copytree(source, golden, symlinks=True, dirs_exist_ok=True)


def fake_fragment(
    name: str = "fake",
    *,
    packages: Sequence[str] = (),
    files: Mapping[str, str | bytes] | None = None,
    init: str | None = None,
    after: Sequence[Ready] = (),
    requires: Sequence[str | Fragment] = (),
    checks: Sequence[Check] = (),
) -> Fragment:
    """A small group of internal declarations, for testing how groups compose.

    It declares a ``Package`` per name in *packages*, a ``File`` per ``path: content`` in
    *files* and, when *init* is given, a runtime-init step called *name* running *init*
    after the *after* readiness handles. *requires* names the groups (or takes the groups
    themselves) that must be in the same profile, and *checks* run on every profile that
    includes it. A requirement left out is the lint finding ``component-requires-missing``.
    """
    items: list[Declaration] = [Package(package) for package in packages]
    items.extend(File(path, content) for path, content in (files or {}).items())
    if init is not None:
        items.append(BootStep(name, init, after=tuple(after)))
    return Fragment(
        name,
        tuple(items),
        requires=tuple(r.name if isinstance(r, Fragment) else r for r in requires),
        checks=tuple(checks),
    )


def fragment_plan(
    *items: Declaration | Fragment,
    include: Sequence[Declaration | Fragment] = (),
    base: Fragment | Recipe | None = None,
    profile: str = "default",
    lock: Lock | None = None,
) -> PlannedImage:
    """Internal declarations (or one group of them) planned on a minimal bootable platform.

    *include* adds what the items need but are not under test (a group their ``requires``
    names). *base* is the platform: a group (default :data:`TEST_PLATFORM`, the kernel
    package) under one ``qemu`` profile called *profile*, or a whole internal model whose
    items gain them.
    """
    if len(items) == 1 and isinstance(items[0], Fragment):
        under_test = items[0]
    else:
        under_test = Fragment("under-test", tuple(items))
    if isinstance(base, Recipe):
        items = (*base.items, *include, under_test)
        return planned_model(replace(base, items=items), profile, lock=lock)
    platform = TEST_PLATFORM if base is None else base
    recipe = Recipe(
        name="fragment-plan",
        items=(platform, *include, under_test),
        variants=(Variant(profile, outputs=("qemu",)),),
    )
    return planned_model(recipe, profile, lock=lock)


def lint_model(recipe: Recipe, profiles: Profiles = None) -> list[Diagnostic]:
    """*profiles* of *recipe* (default: all) linted as ``tundravm check`` lints them."""
    return check_report(recipe, None, variants=_names(profiles))
