"""Helpers for testing recipes and fragments: plans, lint assertions, golden trees, CLI runs.

:func:`fragment_plan` puts a fragment on a minimal bootable platform and
:func:`plan` takes a whole recipe; both return an :class:`Image`, one variant
compiled in memory and read as values (packages, files, units, users, init
steps, hooks). :func:`assert_clean` and :func:`assert_diagnostic` check lint
findings, :func:`assert_tree` and :func:`assert_tree_matches` compare against
a golden directory, :func:`compile_tree` writes a tree to disk,
:func:`fake_bake` and :func:`bake_in_process` produce simulated artifacts,
:func:`fake_fragment` builds a small :class:`~tundravm.Fragment` to compose with, and
:func:`boot` boots a baked image under QEMU and reads its serial console.

Importing this package does not import pytest. The fixtures live in
``tundravm.testing.pytest_plugin``, which pytest loads automatically once
tundravm is installed.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import textwrap
from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from tundravm.check import render
from tundravm.declarative import lifecycle
from tundravm.declarative.lifecycle import (
    INPROCESS,
    MANIFEST_KEY,
    Artifact,
    Backend,
    Lock,
    Tree,
    check_report,
    read_artifacts,
    read_tree,
    variant_provenance,
    variant_tree_digest,
)
from tundravm.declarative.model import (
    BootStep,
    Check,
    Declaration,
    File,
    Fragment,
    Package,
    Ready,
    Recipe,
    Target,
)
from tundravm.diagnostics import Diagnostic, Severity
from tundravm.diff import TreeDiff, _foreign_profile_globs, diff_trees
from tundravm.models import BAKE_RESULT_FILENAME, ArtifactRef, BakeResult, ProfileBuildResult
from tundravm.testing._boot import (
    BootResult,
    BootUnavailable,
    ConsoleScan,
    HttpResponse,
    boot,
    boot_unavailable,
    scan_console,
)
from tundravm.testing._image import (
    TEST_PLATFORM,
    Image,
    ImageFile,
    InitStep,
    fragment_plan,
    plan,
)
from tundravm.testing._units import UnitView

UPDATE_GOLDEN_ENV = "TUNDRAVM_UPDATE_GOLDEN"
_UNIFIED_LINES = 200
_UNIT_DIR = "mkosi.extra/usr/lib/systemd/system"

Variants = str | Sequence[str] | None
"""A variant selection: one name, several names, or ``None`` for every declared variant."""


class CompiledTree:
    """A compiled mkosi tree on disk, with readers for the files tests inspect.

    ``variants`` are the compiled variant directories. Every ``variant``
    argument names one of them and defaults to the first.
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

    def variant(self, name: str | None = None) -> Path:
        """Directory holding variant *name*'s mkosi.conf, extra, skeleton and scripts."""
        name = name or self.default_variant
        if name not in self.variants:
            raise KeyError(f"variant {name!r} was not compiled; compiled: {list(self.variants)}")
        native = self.root / "mkosi.profiles" / name
        return native if native.is_dir() and not (self.root / name).is_dir() else self.root / name

    def path(self, relpath: str, variant: str | None = None) -> Path:
        return self.variant(variant) / relpath.lstrip("/")

    def read(self, relpath: str, variant: str | None = None) -> str:
        return self.path(relpath, variant).read_text(encoding="utf-8")

    def exists(self, relpath: str, variant: str | None = None) -> bool:
        return self.path(relpath, variant).exists()

    def files(self, variant: str | None = None) -> list[str]:
        """Sorted POSIX paths of every file in the variant, relative to its directory."""
        base = self.variant(variant)
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if not p.is_dir())

    def unit(self, name: str, variant: str | None = None) -> str:
        """Systemd unit text; a bare name such as ``"app"`` means ``app.service``."""
        unit = name if "." in name else f"{name}.service"
        path = self.path(f"{_UNIT_DIR}/{unit}", variant)
        if not path.is_file():
            found = sorted(p.name for p in path.parent.glob("*")) if path.parent.is_dir() else []
            raise FileNotFoundError(f"no unit {unit!r} in {path.parent}; found: {found}")
        return path.read_text(encoding="utf-8")

    def script(self, phase: str, variant: str | None = None) -> str:
        """Text of ``scripts/NN-<phase>.sh``."""
        scripts = self.variant(variant) / "scripts"
        matches = sorted(scripts.glob(f"[0-9][0-9]-{phase}.sh"))
        if not matches:
            found = sorted(p.name for p in scripts.glob("*.sh")) if scripts.is_dir() else []
            raise FileNotFoundError(f"no {phase!r} script in {scripts}; found: {found}")
        return matches[0].read_text(encoding="utf-8")

    def runtime_init(self, variant: str | None = None, *, step: str | None = None) -> str:
        """The runtime-init script text: one *step*'s script, or every step in run order.

        Under ``nethermind-v1`` that is ``/usr/bin/runtime-init``; the current
        dialect writes ``/usr/lib/runtime-init/<step>`` per step, joined here in
        the order ``runtime-init.service`` lists them.
        """
        if step is not None:
            return self.read(f"mkosi.extra/usr/lib/runtime-init/{step}", variant)
        if self.exists("mkosi.extra/usr/bin/runtime-init", variant):
            return self.read("mkosi.extra/usr/bin/runtime-init", variant)
        aggregate = self.unit("runtime-init.service", variant)
        after = next(line for line in aggregate.splitlines() if line.startswith("After="))
        steps = [
            unit.removeprefix("runtime-init-").removesuffix(".service")
            for unit in after[6:].split()
        ]
        return "".join(self.runtime_init(variant, step=name) for name in steps)

    def conf(self, variant: str | None = None) -> str:
        return self.read("mkosi.conf", variant)


def _names(variants: Variants) -> list[str] | None:
    if variants is None:
        return None
    return [variants] if isinstance(variants, str) else list(variants)


def compile_tree(
    recipe: Recipe, *, variants: Variants = None, path: str | Path | None = None
) -> CompiledTree:
    """Compile *variants* of *recipe* (default: every declared one) into *path* or a temp dir.

    Unknown variant names raise ``ValidationError``. No lockfile is consulted,
    so source builds use their refs.
    """
    root = Path(path) if path is not None else Path(tempfile.mkdtemp(prefix="tundravm-tree-"))
    tree = lifecycle.compile(recipe, variants=_names(variants))
    tree.write(root)
    return CompiledTree(root, tree.variants, default_variant=tree.variants[0])


Lintable = Recipe | Image | Sequence[Diagnostic]
"""What the assertions check: a recipe or an :class:`Image` to lint, or ``lint()``'s findings."""


def _findings(subject: Lintable, variants: Variants) -> Sequence[Diagnostic]:
    if isinstance(subject, Recipe):
        return check_report(subject, None, variants=_names(variants))
    if variants is not None:
        raise TypeError("variants= selects what a Recipe is linted for; this input already chose")
    return subject.lint() if isinstance(subject, Image) else subject


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

    *subject* is a ``Recipe`` (linted for *variants*, default: all), an
    :class:`Image` (linted for its variant) or the diagnostics ``lint()``
    returned. ``warnings="allow"`` fails on errors only. Returns every
    diagnostic, allowed or not: *subject* itself for diagnostics.
    """
    diagnostics = _findings(subject, variants)
    severities = {"error", "warning"} if warnings == "fail" else {"error"}
    failing = [d for d in diagnostics if d.code not in allow and d.severity in severities]
    if failing:
        raise AssertionError(
            f"recipe has {len(failing)} unexpected finding(s):\n{_render(failing)}"
        )
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

    *diagnostics* is a sequence of diagnostics (what ``tundravm.lint()``
    returned), an :class:`Image` to lint, or a ``Recipe`` to lint for
    *variants* (default: *variant* alone when given, else all). *variant* is
    the variant, ``""`` for the recipe as a whole; *location* the source
    position (``file:line``).
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

    Empty directories are ignored (git does not keep them). *update* (default:
    ``TUNDRAVM_UPDATE_GOLDEN=1``) writes *tree* to *golden* instead of comparing.
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
    """Compare *recipe*'s compiled tree with *golden_dir*; rewrite it instead when updating.

    Unlike :func:`assert_tree`, a mismatch shows a unified diff. *update*
    defaults to ``TUNDRAVM_UPDATE_GOLDEN=1`` in the environment. Variant
    directories in *golden_dir* that were not compiled are neither compared nor
    touched. Returns the diff from the golden tree to the compiled one.
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


_FAKE_FILENAMES: dict[str, str] = {
    "qemu": "disk.qcow2",
    "azure": "disk.vhd",
    "gcp": "disk.raw.tar.gz",
}


def fake_bake(tree: Tree, *, variant: str, target: Target, build: str | Path) -> Artifact:
    """A simulated artifact for *variant* derived from *tree*, recorded in *build*'s manifest.

    Writes ``build/<variant>/<disk file>`` and ``build/bake-result.json`` (merging with
    one already there), so :func:`tundravm.read_artifacts` reads it back.
    ``measure`` refuses it without ``allow_placeholder``, ``deploy`` without ``allow_simulated``.
    """
    base = Path(build)
    path = base / variant / _FAKE_FILENAMES[target]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"simulated: variant={variant} target={target} tree={tree.digest}\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = base / BAKE_RESULT_FILENAME
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    result = BakeResult.load(base) if manifest.is_file() else BakeResult(backend=INPROCESS)
    profile = result.profiles.setdefault(variant, ProfileBuildResult(profile=variant))
    profile.artifacts[target] = ArtifactRef(target=target, path=path, digest=digest)
    result.save(base)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[MANIFEST_KEY] = {"recipe_digest": "", "simulated": True}
    payload["variants_baked"] = [variant]
    for name, entry in payload["variants"].items():
        entry[MANIFEST_KEY] = dict(variant_provenance(previous, name))
    payload["variants"][variant][MANIFEST_KEY] = {
        **payload[MANIFEST_KEY],
        "tree_digest": variant_tree_digest(tree, variant),
    }
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return next(a for a in read_artifacts(base) if a.variant == variant and a.target == target)


def bake_in_process(
    recipe: Recipe,
    *,
    build: str | Path | None = None,
    variants: Variants = None,
    lock: Lock | None = None,
) -> tuple[Artifact, ...]:
    """Bake *variants* (default: all) with the in-process backend into *build* or a temp dir.

    The artifacts are simulated placeholders. Without *lock*, the recipe is
    locked offline first, which fails for a source build: pass a lock built
    with a ``resolver=`` for those.
    """
    names = _names(variants)
    destination = (
        Path(build) if build is not None else Path(tempfile.mkdtemp(prefix="tundravm-bake-"))
    )
    if lock is None:
        lock = lifecycle.lock(recipe, offline=True, variants=names)
    return lifecycle.bake(
        recipe, lock=lock, backend=Backend("inprocess"), build=destination, variants=names
    )


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
    """A small :class:`~tundravm.Fragment` for testing how fragments compose.

    It declares a ``Package`` per name in *packages*, a ``File`` per
    ``path: content`` in *files* and, when *init* is given, a ``BootStep`` called
    *name* running *init* after the *after* readiness handles. *requires* names the fragments
    (or takes the fragments themselves) that must be in the same variant,
    and *checks* run on every variant that includes it::

        a = fake_fragment("a")
        b = fake_fragment("b", requires=(a,))
        Recipe(name="t", items=(b,))  # lint: fragment-requires-missing
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
    """Write dedented *source* to ``tmp_path/name`` for CLI tests and return its path."""
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
    "HttpResponse",
    "Image",
    "ImageFile",
    "InitStep",
    "Lintable",
    "UnitView",
    "Variants",
    "assert_clean",
    "assert_diagnostic",
    "assert_tree",
    "assert_tree_matches",
    "bake_in_process",
    "boot",
    "boot_unavailable",
    "compile_tree",
    "fake_bake",
    "fake_fragment",
    "fragment_plan",
    "plan",
    "recipe_file",
    "run_cli",
    "scan_console",
]
