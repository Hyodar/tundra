"""Helpers for testing images: plans, lint assertions, golden trees, CLI runs.

:func:`plan` takes an :class:`~tundravm.Image` and returns a :class:`PlannedImage`: one
profile compiled in memory and read as values (packages, files, units, users, runtime-init
steps, hooks). :func:`assert_clean` and :func:`assert_diagnostic` check lint findings,
:func:`assert_tree` compares ``tundravm.compile(image)`` against a golden directory, and
:func:`read_lock` reads the lockfile those take. :class:`FakeMkosi` and
:class:`FakeSources` stand in for mkosi and the source fetches, so a real backend runs a
whole build with nothing forked or fetched; :func:`boot` boots a built image under QEMU
and reads its serial console; :func:`run_cli` runs the CLI in-process.

:func:`fragment_plan`, :func:`fake_fragment`, :func:`compile_tree`,
:func:`assert_tree_matches` and :func:`recipe_file` work on the internal model an image
lowers to (:mod:`tundravm.declarative`); they are for testing tundravm itself.

Importing this package does not import pytest. The fixtures live in
``tundravm.testing.pytest_plugin``, which pytest loads automatically once
tundravm is installed.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from tundravm.check import render
from tundravm.compiler import PAYLOAD
from tundravm.declarative import lifecycle
from tundravm.declarative.lifecycle import Tree, check_report, read_lock, read_tree
from tundravm.declarative.model import (
    BootStep,
    Check,
    Declaration,
    File,
    Fragment,
    Package,
    Ready,
    Recipe,
)
from tundravm.diagnostics import Diagnostic, Severity
from tundravm.diff import TreeDiff, _foreign_profile_globs, diff_trees
from tundravm.testing._boot import (
    BootResult,
    BootUnavailable,
    ConsoleScan,
    HttpResponse,
    boot,
    boot_unavailable,
    scan_console,
)
from tundravm.testing._fake_mkosi import FakeCall, FakeMkosi, build_unavailable
from tundravm.testing._fake_sources import FakeFetch, FakeSources
from tundravm.testing._image import (
    TEST_PLATFORM,
    ImageFile,
    InitStep,
    PlannedImage,
    fragment_plan,
    plan,
)
from tundravm.testing._units import UnitView

UPDATE_GOLDEN_ENV = "TUNDRAVM_UPDATE_GOLDEN"
_UNIFIED_LINES = 200
_UNIT_DIR = "mkosi.extra/usr/lib/systemd/system"

Variants = str | Sequence[str] | None
"""A profile selection: one name, several names, or ``None`` for every declared profile."""


class CompiledTree:
    """A compiled mkosi tree on disk, with readers for the files tests inspect.

    ``variants`` are the compiled profiles. Every ``variant`` argument names one of
    them and defaults to the first. A profile reads the payload (``payload/``) with
    its overlay (``payload/mkosi.profiles/<profile>/``) over it, as mkosi merges them:
    a path in the overlay wins, scripts and configuration accumulate.
    """

    __slots__ = ("default_variant", "root", "variants")

    def __init__(self, root: Path, variants: Sequence[str], default_variant: str = "default"):
        self.root = Path(root)
        self.variants = tuple(variants)
        if default_variant not in self.variants and self.variants:
            default_variant = self.variants[0]
        self.default_variant = default_variant

    def __fspath__(self) -> str:
        return os.fspath(self.root)

    def __repr__(self) -> str:
        return f"CompiledTree({str(self.root)!r}, variants={self.variants!r})"

    def layers(self, name: str | None = None) -> tuple[Path, ...]:
        """The configuration directories profile *name* builds from: the payload, then its
        overlay when it has one."""
        name = name or self.default_variant
        if name not in self.variants:
            raise KeyError(f"profile {name!r} was not compiled; compiled: {list(self.variants)}")
        payload = self.root / PAYLOAD
        overlay = payload / "mkosi.profiles" / name
        return (payload, overlay) if overlay.is_dir() else (payload,)

    def variant(self, name: str | None = None) -> Path:
        """The directory holding what profile *name* adds: its overlay, else the
        payload."""
        return self.layers(name)[-1]

    def path(self, relpath: str, variant: str | None = None) -> Path:
        """*relpath* in the profile's overlay when it is there, else in the payload."""
        for layer in reversed(self.layers(variant)):
            found = layer / relpath.lstrip("/")
            if found.exists() or found.is_symlink():
                return found
        return self.layers(variant)[0] / relpath.lstrip("/")

    def read(self, relpath: str, variant: str | None = None) -> str:
        return self.path(relpath, variant).read_text(encoding="utf-8")

    def exists(self, relpath: str, variant: str | None = None) -> bool:
        return self.path(relpath, variant).exists()

    def files(self, variant: str | None = None) -> list[str]:
        """Sorted POSIX paths of every file the profile reads, relative to its layer (the
        payload's own profile overlays left out)."""
        found: set[str] = set()
        for layer in self.layers(variant):
            for path in layer.rglob("*"):
                rel = path.relative_to(layer).as_posix()
                if not path.is_dir() and not rel.startswith("mkosi.profiles/"):
                    found.add(rel)
        return sorted(found)

    def unit(self, name: str, variant: str | None = None) -> str:
        """Systemd unit text; a bare name such as ``"app"`` means ``app.service``."""
        unit = name if "." in name else f"{name}.service"
        path = self.path(f"{_UNIT_DIR}/{unit}", variant)
        if not path.is_file():
            found = sorted(p.name for p in path.parent.glob("*")) if path.parent.is_dir() else []
            raise FileNotFoundError(f"no unit {unit!r} in {path.parent}; found: {found}")
        return path.read_text(encoding="utf-8")

    def scripts(self, phase: str, variant: str | None = None) -> list[Path]:
        """The profile's ``mkosi.<phase>.d/`` scripts in the order mkosi runs them."""
        return [
            script
            for layer in self.layers(variant)
            for script in sorted((layer / f"mkosi.{phase}.d").glob("*"))
        ]

    def script(self, phase: str, variant: str | None = None) -> str:
        """The text of the profile's *phase* scripts, in the order mkosi runs them."""
        found = self.scripts(phase, variant)
        if not found:
            phases = sorted(
                {
                    path.name.removeprefix("mkosi.").removesuffix(".d")
                    for layer in self.layers(variant)
                    for path in layer.glob("mkosi.*.d")
                    if any(path.iterdir())
                }
            )
            raise FileNotFoundError(
                f"no {phase!r} script for profile {variant!r}; scripts: {', '.join(phases)}"
            )
        return "\n".join(path.read_text(encoding="utf-8") for path in found)

    def runtime_init(self, variant: str | None = None, *, step: str | None = None) -> str:
        """The runtime-init script text: one *step*'s script, or every step in the order
        ``runtime-init.service`` lists them (``/usr/lib/runtime-init/<step>``)."""
        if step is not None:
            return self.read(f"mkosi.extra/usr/lib/runtime-init/{step}", variant)
        aggregate = self.unit("runtime-init.service", variant)
        after = next(line for line in aggregate.splitlines() if line.startswith("After="))
        steps = [
            unit.removeprefix("runtime-init-").removesuffix(".service")
            for unit in after[6:].split()
        ]
        return "".join(self.runtime_init(variant, step=name) for name in steps)

    def conf(self, variant: str | None = None) -> str:
        """The profile's payload configuration as mkosi reads it, one text: the payload's
        ``mkosi.conf``, its ``mkosi.conf.d/*.conf`` and the overlay's ``mkosi.conf``."""
        texts: list[str] = []
        for layer in self.layers(variant):
            texts.append((layer / "mkosi.conf").read_text(encoding="utf-8"))
            texts += [p.read_text(encoding="utf-8") for p in sorted(layer.glob("mkosi.conf.d/*"))]
        return "\n".join(texts)

    def main_conf(self) -> str:
        """The root ``mkosi.conf``: the settings every image shares and the QEMU disk."""
        return (self.root / "mkosi.conf").read_text(encoding="utf-8")


def _names(variants: Variants) -> list[str] | None:
    if variants is None:
        return None
    return [variants] if isinstance(variants, str) else list(variants)


def compile_tree(
    recipe: Recipe, *, variants: Variants = None, path: str | Path | None = None
) -> CompiledTree:
    """Compile the profiles *variants* (default: all) of *recipe* into *path* or a temp dir.

    *recipe* is the internal model an image lowers to; for an :class:`~tundravm.Image`,
    ``tundravm.compile(image).write(path)`` does the same. Unknown profile names raise
    ``ValidationError``. No lockfile is consulted, so source builds use their refs.
    """
    root = Path(path) if path is not None else Path(tempfile.mkdtemp(prefix="tundravm-tree-"))
    tree = lifecycle.compile(recipe, variants=_names(variants))
    tree.write(root)
    return CompiledTree(root, tree.variants, default_variant=tree.variants[0])


Lintable = Recipe | PlannedImage | Sequence[Diagnostic]
"""What the assertions check: a :class:`PlannedImage` to lint, findings, or the internal
model an image lowers to."""


def _findings(subject: Lintable, variants: Variants) -> Sequence[Diagnostic]:
    if isinstance(subject, Recipe):
        return check_report(subject, None, variants=_names(variants))
    if variants is not None:
        raise TypeError("variants= selects the profiles to lint; this input already chose one")
    return subject.lint() if isinstance(subject, PlannedImage) else subject


def _render(diagnostics: Sequence[Diagnostic]) -> str:
    return render(diagnostics) if diagnostics else "no findings"


def assert_clean(
    subject: Lintable,
    /,
    *,
    warnings: Literal["fail", "allow"] = "fail",
    allow: Sequence[str] = (),
    variants: Variants = None,
) -> Sequence[Diagnostic]:
    """Fail on every error and warning finding whose code is not in *allow*.

    *subject* is a :class:`PlannedImage` (``plan(image, profile)``, linted for its
    profile), the diagnostics :meth:`PlannedImage.lint` returned, or the internal model
    (linted for the profiles *variants*, default: all). ``warnings="allow"`` fails on
    errors only. Returns every diagnostic, allowed or not: *subject* itself for
    diagnostics.
    """
    diagnostics = _findings(subject, variants)
    severities = {"error", "warning"} if warnings == "fail" else {"error"}
    failing = [d for d in diagnostics if d.code not in allow and d.severity in severities]
    if failing:
        raise AssertionError(f"image has {len(failing)} unexpected finding(s):\n{_render(failing)}")
    return diagnostics


def assert_diagnostic(
    diagnostics: Lintable,
    code: str,
    /,
    *,
    variants: Variants = None,
    severity: Severity | None = None,
    variant: str | None = None,
    subject: str | None = None,
    location: str | None = None,
) -> Diagnostic:
    """Return the first diagnostic matching every given field, else fail listing them all.

    *diagnostics* is a :class:`PlannedImage` to lint, a sequence of diagnostics (what
    :meth:`PlannedImage.lint` returned), or the internal model to lint for the profiles
    *variants* (default: *variant* alone when given, else all). *variant* is the profile,
    ``""`` for the image as a whole; *location* the source position (``file:line``).
    """
    wanted_subject = subject
    if variants is None and variant and isinstance(diagnostics, Recipe):
        variants = [variant]
    found = _findings(diagnostics, variants)
    for d in found:
        if (
            d.code == code
            and (severity is None or d.severity == severity)
            and (variant is None or d.variant == variant)
            and (wanted_subject is None or d.subject == wanted_subject)
            and (location is None or d.location == location)
        ):
            return d
    wanted = ", ".join(
        f"{key}={value!r}"
        for key, value in (
            ("code", code),
            ("severity", severity),
            ("variant", variant),
            ("subject", wanted_subject),
            ("location", location),
        )
        if value is not None
    )
    raise AssertionError(f"no diagnostic with {wanted}; found:\n{_render(found)}")


def assert_tree(tree: Tree, golden: str | Path, *, update: bool | None = None) -> None:
    """Fail unless *golden* holds exactly *tree*: every path, its bytes, exec bit and symlink.

    *tree* is what ``tundravm.compile(image, lock=...)`` returns. Empty directories are
    ignored (git does not keep them). *update* (default: ``TUNDRAVM_UPDATE_GOLDEN=1``)
    writes *tree* to *golden* instead of comparing.
    """
    root = Path(golden)
    if update is None:
        update = os.environ.get(UPDATE_GOLDEN_ENV) == "1"
    if update:
        tree.write(root)
        return
    expected = {e.path: e for e in tree.entries if e.content is not None or e.symlink}
    actual = {e.path: e for e in read_tree(root).entries if e.content is not None or e.symlink}
    problems: list[str] = []
    for path in sorted(expected.keys() | actual.keys()):
        want, have = expected.get(path), actual.get(path)
        if want is None:
            problems.append(f"+ {path} (only in {root})")
        elif have is None:
            problems.append(f"- {path} (missing from {root})")
        elif want.symlink != have.symlink:
            problems.append(f"~ {path}: symlink {have.symlink!r} != {want.symlink!r}")
        elif want.content != have.content:
            problems.append(f"~ {path}: content differs")
        elif bool(want.mode & 0o111) != bool(have.mode & 0o111):
            problems.append(f"~ {path}: exec bit {oct(have.mode)} != {oct(want.mode)}")
    if problems:
        shown = "\n".join(problems[:_UNIFIED_LINES])
        raise AssertionError(
            f"tree differs from {root} in {len(problems)} path(s):\n{shown}\n"
            f"Re-run with {UPDATE_GOLDEN_ENV}=1 to accept the compiled tree."
        )


def assert_tree_matches(
    recipe: Recipe,
    golden_dir: str | Path,
    *,
    variants: Variants = None,
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
        tree = compile_tree(recipe, variants=variants, path=Path(tmp))
        ignore = _foreign_profile_globs(golden, tree.variants)
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
    includes it. A requirement left out is the lint finding ``fragment-requires-missing``.
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


def recipe_file(tmp_path: Path, source: str, name: str = "recipe.py") -> Path:
    """Write dedented *source* (an image file) to ``tmp_path/name`` for CLI tests; return
    its path."""
    path = Path(tmp_path) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    return path


def run_cli(*argv: str | os.PathLike[str]) -> tuple[int, str, str]:
    """Run ``tundravm.cli.main`` in-process; returns ``(exit_code, stdout, stderr)``."""
    from tundravm.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main([os.fspath(arg) for arg in argv], stdout=out)
        except SystemExit as exc:
            if exc.code is None or isinstance(exc.code, int):
                code = exc.code or 0
            else:
                err.write(f"{exc.code}\n")
                code = 1
    return code, out.getvalue(), err.getvalue()


__all__ = [
    "TEST_PLATFORM",
    "UPDATE_GOLDEN_ENV",
    "BootResult",
    "BootUnavailable",
    "CompiledTree",
    "ConsoleScan",
    "FakeCall",
    "FakeFetch",
    "FakeMkosi",
    "FakeSources",
    "HttpResponse",
    "PlannedImage",
    "ImageFile",
    "InitStep",
    "Lintable",
    "UnitView",
    "Variants",
    "assert_clean",
    "assert_diagnostic",
    "assert_tree",
    "assert_tree_matches",
    "boot",
    "boot_unavailable",
    "build_unavailable",
    "compile_tree",
    "fake_fragment",
    "fragment_plan",
    "plan",
    "read_lock",
    "recipe_file",
    "run_cli",
    "scan_console",
]
