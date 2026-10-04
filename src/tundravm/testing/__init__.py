"""Helpers for testing recipes and fragments: compile, lint, golden trees, CLI runs.

Every helper takes declarative values: a :class:`~tundravm.Recipe`, the
:class:`~tundravm.Tree` :func:`tundravm.compile` returns, or the diagnostics
:func:`tundravm.lint` returns. :func:`compile_tree` writes a recipe's tree to
disk with readers for the files tests inspect, :func:`assert_clean` and
:func:`assert_diagnostic` check lint results, :func:`assert_tree` and
:func:`assert_tree_matches` compare against a golden directory,
:func:`fake_bake` and :func:`bake_in_process` produce simulated artifacts, and
:func:`fake_fragment` builds a small :class:`~tundravm.Fragment` to compose with.

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
from typing import cast, overload

from tundravm.check import Diagnostic, Level, render
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
)
from tundravm.declarative.model import (
    Check,
    Declaration,
    File,
    Fragment,
    Init,
    Package,
    Recipe,
    Target,
)
from tundravm.declarative.model import Diagnostic as Finding
from tundravm.diff import TreeDiff, _foreign_profile_globs, diff_trees
from tundravm.models import BAKE_RESULT_FILENAME, ArtifactRef, BakeResult, ProfileBuildResult

UPDATE_GOLDEN_ENV = "TUNDRAVM_UPDATE_GOLDEN"
_UNIFIED_LINES = 200
_UNIT_DIR = "mkosi.extra/usr/lib/systemd/system"

Variants = str | Sequence[str] | None
"""A variant selection: one name, several names, or ``None`` for every declared variant."""


class CompiledTree:
    """A compiled mkosi tree on disk, with readers for the files tests inspect.

    ``profiles`` (alias ``variants``) are the compiled variant directories.
    Every ``profile`` argument names one of them and defaults to the first.
    """

    __slots__ = ("default_profile", "profiles", "root")

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

    @property
    def variants(self) -> tuple[str, ...]:
        """The compiled variants (same as ``profiles``)."""
        return self.profiles

    def profile(self, name: str | None = None) -> Path:
        """Directory holding variant *name*'s mkosi.conf, extra, skeleton and scripts."""
        name = name or self.default_profile
        if name not in self.profiles:
            raise KeyError(f"variant {name!r} was not compiled; compiled: {list(self.profiles)}")
        native = self.root / "mkosi.profiles" / name
        return native if native.is_dir() and not (self.root / name).is_dir() else self.root / name

    def path(self, relpath: str, profile: str | None = None) -> Path:
        return self.profile(profile) / relpath.lstrip("/")

    def read(self, relpath: str, profile: str | None = None) -> str:
        return self.path(relpath, profile).read_text(encoding="utf-8")

    def exists(self, relpath: str, profile: str | None = None) -> bool:
        return self.path(relpath, profile).exists()

    def files(self, profile: str | None = None) -> list[str]:
        """Sorted POSIX paths of every file in the variant, relative to its directory."""
        base = self.profile(profile)
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*") if not p.is_dir())

    def unit(self, name: str, profile: str | None = None) -> str:
        """Systemd unit text; a bare name such as ``"app"`` means ``app.service``."""
        unit = name if "." in name else f"{name}.service"
        path = self.path(f"{_UNIT_DIR}/{unit}", profile)
        if not path.is_file():
            found = sorted(p.name for p in path.parent.glob("*")) if path.parent.is_dir() else []
            raise FileNotFoundError(f"no unit {unit!r} in {path.parent}; found: {found}")
        return path.read_text(encoding="utf-8")

    def script(self, phase: str, profile: str | None = None) -> str:
        """Text of ``scripts/NN-<phase>.sh``."""
        scripts = self.profile(profile) / "scripts"
        matches = sorted(scripts.glob(f"[0-9][0-9]-{phase}.sh"))
        if not matches:
            found = sorted(p.name for p in scripts.glob("*.sh")) if scripts.is_dir() else []
            raise FileNotFoundError(f"no {phase!r} script in {scripts}; found: {found}")
        return matches[0].read_text(encoding="utf-8")

    def runtime_init(self, profile: str | None = None) -> str:
        return self.read("mkosi.extra/usr/bin/runtime-init", profile)

    def conf(self, profile: str | None = None) -> str:
        return self.read("mkosi.conf", profile)


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
    return CompiledTree(root, tree.variants, default_profile=tree.variants[0])


AnyDiagnostic = Diagnostic | Finding
Diagnostics = Sequence[Finding] | Sequence[Diagnostic]


def _findings(subject: Recipe | Diagnostics, variants: Variants) -> Sequence[AnyDiagnostic]:
    if isinstance(subject, Recipe):
        return check_report(subject, None, variants=_names(variants))
    if variants is not None:
        raise TypeError("variants= selects what a Recipe is linted for; lint() already chose")
    return subject


def _variant(d: AnyDiagnostic) -> str:
    return d.variant if isinstance(d, Finding) else d.profile


def _render(diagnostics: Sequence[AnyDiagnostic]) -> str:
    if not diagnostics:
        return "no findings"
    if all(isinstance(d, Diagnostic) for d in diagnostics):
        return render(cast("Sequence[Diagnostic]", diagnostics))
    return "\n".join(
        f"{d.level} {d.code} [{_variant(d)}]{f' {d.subject}' if d.subject else ''}: {d.message}"
        for d in diagnostics
    )


@overload
def assert_clean(
    subject: Sequence[Finding],
    /,
    *,
    variants: Variants = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> Sequence[Finding]: ...
@overload
def assert_clean(
    subject: Recipe | Sequence[Diagnostic],
    /,
    *,
    variants: Variants = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> list[Diagnostic]: ...
def assert_clean(
    subject: Recipe | Diagnostics,
    /,
    *,
    variants: Variants = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> Sequence[AnyDiagnostic]:
    """Fail on error-level findings (warnings too with *strict*) whose code is not in *allow*.

    *subject* is a sequence of diagnostics, such as what ``tundravm.lint()``
    returned (*strict* defaults to true), or a ``Recipe`` to lint for
    *variants* (default: all; *strict* defaults to false). A recipe's findings
    are the compiler's report form, with hints. Returns every diagnostic,
    allowed or not.
    """
    given = not isinstance(subject, Recipe)
    strict = given if strict is None else strict
    diagnostics = _findings(subject, variants)
    levels = {"error", "warning"} if strict else {"error"}
    failing = [d for d in diagnostics if d.code not in allow and d.level in levels]
    if failing:
        raise AssertionError(
            f"recipe has {len(failing)} unexpected finding(s):\n{_render(failing)}"
        )
    return diagnostics if given else list(diagnostics)


@overload
def assert_diagnostic(
    diagnostics: Sequence[Finding],
    code: str,
    /,
    *,
    variants: Variants = None,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> Finding: ...
@overload
def assert_diagnostic(
    diagnostics: Recipe | Sequence[Diagnostic],
    code: str,
    /,
    *,
    variants: Variants = None,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> Diagnostic: ...
def assert_diagnostic(
    diagnostics: Recipe | Diagnostics,
    code: str,
    /,
    *,
    variants: Variants = None,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> AnyDiagnostic:
    """Return the first diagnostic matching every given field, else fail listing them all.

    *diagnostics* is a sequence of diagnostics (what ``tundravm.lint()``
    returned), or a ``Recipe`` to lint for *variants* (default: *variant*
    alone when given, else all). ``profile`` is an alias of ``variant``.
    """
    wanted_subject = subject
    variant = profile if profile is not None else variant
    if variants is None and variant is not None and isinstance(diagnostics, Recipe):
        variants = [variant]
    found = _findings(diagnostics, variants)
    for d in found:
        if (
            d.code == code
            and (level is None or d.level == level)
            and (variant is None or _variant(d) == variant)
            and (wanted_subject is None or (d.subject or None) == wanted_subject)
        ):
            return d
    wanted = ", ".join(
        f"{key}={value!r}"
        for key, value in (
            ("code", code),
            ("level", level),
            ("variant", variant),
            ("subject", wanted_subject),
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


_FAKE_FILENAMES: dict[str, str] = {
    "qemu": "disk.qcow2",
    "azure": "disk.vhd",
    "gcp": "disk.raw.tar.gz",
}


def fake_bake(tree: Tree, *, variant: str, target: Target, out: str | Path) -> Artifact:
    """A simulated artifact for *variant* derived from *tree*, recorded in ``out``'s manifest.

    Writes ``out/<variant>/<disk file>`` and ``out/bake-result.json`` (merging with
    one already there), so :func:`tundravm.read_artifacts` reads it back.
    Measurement and deployment refuse it unless told to allow placeholders.
    """
    base = Path(out)
    path = base / variant / _FAKE_FILENAMES[target]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"simulated: variant={variant} target={target} tree={tree.digest}\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = base / BAKE_RESULT_FILENAME
    result = BakeResult.load(base) if manifest.is_file() else BakeResult(backend=INPROCESS)
    profile = result.profiles.setdefault(variant, ProfileBuildResult(profile=variant))
    profile.artifacts[target] = ArtifactRef(target=target, path=path, digest=digest)
    result.save(base)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload[MANIFEST_KEY] = {"recipe_digest": "", "simulated": True, "tree_digest": tree.digest}
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return next(a for a in read_artifacts(manifest) if a.variant == variant and a.target == target)


def bake_in_process(
    recipe: Recipe,
    *,
    out: str | Path | None = None,
    variants: Variants = None,
    locked: Lock | None = None,
) -> tuple[Artifact, ...]:
    """Bake *variants* (default: all) with the in-process backend into *out* or a temp dir.

    The artifacts are simulated placeholders. Without *locked*, the recipe is
    locked offline first, which fails for a source build: pass a lock built
    with a ``resolver=`` for those.
    """
    names = _names(variants)
    destination = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="tundravm-bake-"))
    if locked is None:
        locked = lifecycle.lock(recipe, offline=True, variants=names)
    return lifecycle.bake(
        recipe, locked=locked, backend=Backend("inprocess"), out=destination, variants=names
    )


def fake_fragment(
    name: str = "fake",
    *,
    packages: Sequence[str] = (),
    files: Mapping[str, str | bytes] | None = None,
    init: str | None = None,
    priority: int = 50,
    requires: Sequence[str | Fragment] = (),
    checks: Sequence[Check] = (),
) -> Fragment:
    """A small :class:`~tundravm.Fragment` for testing how fragments compose.

    It declares a ``Package`` per name in *packages*, a ``File`` per
    ``path: content`` in *files* and, when *init* is given, an ``Init`` called
    *name* running *init* at *priority*. *requires* names the fragments
    (or takes the fragments themselves) that must be in the same variant,
    and *checks* run on every variant that includes it::

        a = fake_fragment("a")
        b = fake_fragment("b", requires=(a,))
        Recipe(name="t", common=Fragment("app", (b,)))  # lint: fragment-requires-missing
    """
    items: list[Declaration] = [Package(package) for package in packages]
    items.extend(File(path, content) for path, content in (files or {}).items())
    if init is not None:
        items.append(Init(name, init, priority=priority))
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
    "UPDATE_GOLDEN_ENV",
    "CompiledTree",
    "Variants",
    "assert_clean",
    "assert_diagnostic",
    "assert_tree",
    "assert_tree_matches",
    "bake_in_process",
    "compile_tree",
    "fake_bake",
    "fake_fragment",
    "recipe_file",
    "run_cli",
]
