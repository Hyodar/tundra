"""Helpers for testing recipes and modules: compile, lint, golden trees, CLI runs.

The declarative helpers work on lifecycle values: :func:`assert_clean` and
:func:`assert_diagnostic` take the diagnostics :func:`tundravm.lint` returns,
:func:`assert_tree` compares a :class:`~tundravm.Tree` with a golden directory
and :func:`fake_bake` turns a tree into a simulated :class:`~tundravm.Artifact`.
They also accept a ``Recipe`` (linted for you) or a lowered ``Image``.

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
from collections.abc import Iterator, Mapping, Sequence
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Self, cast, overload

from tundravm.backends.inprocess import InProcessBackend
from tundravm.check import Diagnostic, Level, render
from tundravm.declarative import Recipe
from tundravm.declarative.lifecycle import (
    INPROCESS,
    MANIFEST_KEY,
    Artifact,
    Tree,
    check_report,
    compile_image,
    read_artifacts,
    read_tree,
    variant_names,
)
from tundravm.declarative.model import Diagnostic as Finding
from tundravm.declarative.model import Target
from tundravm.diff import TreeDiff, _foreign_profile_globs, diff_trees
from tundravm.models import BAKE_RESULT_FILENAME, ArtifactRef, BakeResult, ProfileBuildResult
from tundravm.modules.base import Module

if TYPE_CHECKING:
    from tundravm.image import Image

UPDATE_GOLDEN_ENV = "TUNDRAVM_UPDATE_GOLDEN"
_UNIFIED_LINES = 200
_UNIT_DIR = "mkosi.extra/usr/lib/systemd/system"


class CompiledTree:
    """A compiled mkosi tree on disk, with readers for the files tests inspect.

    Every ``profile`` argument defaults to the image's default profile when it
    was compiled, otherwise to the first compiled profile.
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

    def profile(self, name: str | None = None) -> Path:
        """Directory holding *name*'s mkosi.conf, extra, skeleton and scripts."""
        name = name or self.default_profile
        if name not in self.profiles:
            raise KeyError(f"profile {name!r} was not compiled; compiled: {list(self.profiles)}")
        native = self.root / "mkosi.profiles" / name
        return native if native.is_dir() and not (self.root / name).is_dir() else self.root / name

    def path(self, relpath: str, profile: str | None = None) -> Path:
        return self.profile(profile) / relpath.lstrip("/")

    def read(self, relpath: str, profile: str | None = None) -> str:
        return self.path(relpath, profile).read_text(encoding="utf-8")

    def exists(self, relpath: str, profile: str | None = None) -> bool:
        return self.path(relpath, profile).exists()

    def files(self, profile: str | None = None) -> list[str]:
        """Sorted POSIX paths of every file in the profile, relative to its directory."""
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


@contextlib.contextmanager
def _selected(image: Image, profiles: Sequence[str] | None) -> Iterator[None]:
    if profiles is None:
        yield
        return
    names = (profiles,) if isinstance(profiles, str) else tuple(profiles)
    unknown = [name for name in names if name not in image.state.profiles]
    if unknown:
        raise ValueError(f"unknown profile(s) {unknown}; declared: {sorted(image.state.profiles)}")
    with image.profiles(*names):
        yield


def _profile_list(profiles: Sequence[str] | None) -> list[str] | None:
    if profiles is None:
        return None
    return [profiles] if isinstance(profiles, str) else list(profiles)


def compile_tree(
    image: Image | Recipe, *, profiles: Sequence[str] | None = None, path: Path | None = None
) -> CompiledTree:
    """Compile *profiles* into *path* or a fresh temp dir.

    For a ``Recipe``, *profiles* are variant names (default: every variant); for an
    ``Image``, the default is its active profiles. The image's compile cache is
    left as it was, so a later ``bake()`` is unaffected.
    """
    root = Path(path) if path is not None else Path(tempfile.mkdtemp(prefix="tundravm-tree-"))
    if isinstance(image, Recipe):
        names = variant_names(image, _profile_list(profiles))
        from tundravm.declarative import lower

        tree = compile_image(lower(image, variants=names), names, locked=None)
        tree.write(root)
        return CompiledTree(root, tree.variants, default_profile=tree.variants[0])
    saved = (image._last_compile_digest, image._last_compile_path, image._last_compile_emission)
    try:
        with _selected(image, profiles):
            result = image.compile(root, force=True)
    finally:
        (
            image._last_compile_digest,
            image._last_compile_path,
            image._last_compile_emission,
        ) = saved
    return CompiledTree(root, result.profiles, default_profile=image.default_profile)


AnyDiagnostic = Diagnostic | Finding


def _findings(
    subject: Image | Recipe | Sequence[AnyDiagnostic], profiles: Sequence[str] | None
) -> Sequence[AnyDiagnostic]:
    if isinstance(subject, Recipe):
        return check_report(subject, None, variants=_profile_list(profiles))
    if isinstance(subject, (list, tuple)):
        return subject
    image = cast("Image", subject)
    return image.check(profiles=_profile_list(profiles))


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
    profiles: Sequence[str] | None = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> Sequence[Finding]: ...
@overload
def assert_clean(
    subject: Image | Recipe,
    /,
    *,
    profiles: Sequence[str] | None = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> list[Diagnostic]: ...
def assert_clean(
    subject: Image | Recipe | Sequence[Finding],
    /,
    *,
    profiles: Sequence[str] | None = None,
    allow: Sequence[str] = (),
    strict: bool | None = None,
) -> Sequence[AnyDiagnostic]:
    """Fail on error-level findings (warnings too with *strict*) whose code is not in *allow*.

    *subject* is the diagnostics ``tundravm.lint()`` returned (*strict* defaults to
    true), or a ``Recipe``/``Image`` to lint (*strict* defaults to false). Returns
    every diagnostic, allowed or not.
    """
    given = isinstance(subject, (list, tuple))
    strict = given if strict is None else strict
    diagnostics = _findings(subject, profiles)
    levels = {"error", "warning"} if strict else {"error"}
    failing = [d for d in diagnostics if d.code not in allow and d.level in levels]
    if failing:
        raise AssertionError(
            f"recipe has {len(failing)} unexpected finding(s):\n{_render(failing)}"
        )
    return list(diagnostics) if not given else diagnostics


@overload
def assert_diagnostic(
    diagnostics: Sequence[Finding],
    code: str,
    /,
    *,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> Finding: ...
@overload
def assert_diagnostic(
    diagnostics: Image | Recipe,
    code: str,
    /,
    *,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> Diagnostic: ...
def assert_diagnostic(
    diagnostics: Image | Recipe | Sequence[Finding],
    code: str,
    /,
    *,
    level: Level | None = None,
    profile: str | None = None,
    variant: str | None = None,
    subject: str | None = None,
) -> AnyDiagnostic:
    """Return the first diagnostic matching every given field, else fail listing them all.

    *diagnostics* is what ``tundravm.lint()`` returned, or a ``Recipe``/``Image`` to
    lint. ``profile`` is an alias of ``variant``.
    """
    wanted_subject = subject
    variant = profile if profile is not None else variant
    scope = None if variant is None or isinstance(diagnostics, (list, tuple)) else [variant]
    found = _findings(diagnostics, scope)
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


def assert_tree_matches(
    image: Image,
    golden_dir: str | Path,
    *,
    profiles: Sequence[str] | None = None,
    update: bool | None = None,
) -> TreeDiff:
    """Compare the compiled tree with *golden_dir*; rewrite it instead when updating.

    *update* defaults to ``TUNDRAVM_UPDATE_GOLDEN=1`` in the environment. Profile
    directories in *golden_dir* that were not compiled are neither compared nor
    touched. Returns the diff from the golden tree to the compiled one.
    """
    golden = Path(golden_dir)
    if update is None:
        update = os.environ.get(UPDATE_GOLDEN_ENV) == "1"
    with tempfile.TemporaryDirectory(prefix="tundravm-golden-") as tmp:
        tree = compile_tree(image, profiles=profiles, path=Path(tmp))
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


def bake_in_process(
    image: Image,
    *,
    build_dir: Path | None = None,
    profiles: Sequence[str] | None = None,
) -> BakeResult:
    """Bake with ``InProcessBackend`` into *build_dir* or a temp dir; the backend is restored."""
    destination = (
        Path(build_dir)
        if build_dir is not None
        else Path(tempfile.mkdtemp(prefix="tundravm-bake-"))
    )
    original = image.backend
    image.backend = InProcessBackend()
    try:
        with _selected(image, profiles):
            return image.bake(destination)
    finally:
        image.backend = original


class FakeModule(Module):
    """Configurable module for tests; records the profiles it was applied to.

    Each instance gets its own subclass, so fakes can require each other::

        a = FakeModule("a")
        b = FakeModule("b", requires=(a,))
        img.apply(a, b)  # FakeModule("b") alone raises ValidationError
    """

    applied_to: list[str]
    packages: tuple[str, ...]
    files: dict[str, str]
    script: str | None

    _GENERATED: ClassVar[bool] = False

    def __new__(
        cls,
        name: str = "fake",
        *,
        packages: Sequence[str] = (),
        files: Mapping[str, str] | None = None,
        init_script: str | None = None,
        init_priority: int | None = 50,
        requires: Sequence[type[Module] | Module] = (),
    ) -> Self:
        base = cls.__mro__[1] if cls._GENERATED else cls
        namespace: dict[str, object] = {
            "name": name,
            "requires": tuple(r if isinstance(r, type) else type(r) for r in requires),
            "init_priority": init_priority,
            "_GENERATED": True,
            "__module__": base.__module__,
            "__qualname__": f"{base.__qualname__}[{name}]",
        }
        generated = type(f"{base.__name__}[{name}]", (base,), namespace)
        return cast(Self, object.__new__(generated))

    def __init__(
        self,
        name: str = "fake",
        *,
        packages: Sequence[str] = (),
        files: Mapping[str, str] | None = None,
        init_script: str | None = None,
        init_priority: int | None = 50,
        requires: Sequence[type[Module] | Module] = (),
    ) -> None:
        self.packages = tuple(packages)
        self.files = dict(files or {})
        self.script = init_script
        self.applied_to = []

    def __repr__(self) -> str:
        return f"FakeModule({self.name!r})"

    def configure(self, image: Image) -> None:
        if self.packages:
            image.install(*self.packages)
        for path, content in self.files.items():
            image.file(path, content=content)
        self.applied_to.extend(image._active_profiles)
        priority: int | None = getattr(self, "init_priority", None)
        if self.script and priority is not None:
            image.runtime_init(self.script, priority=priority)


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
    "FakeModule",
    "assert_clean",
    "assert_diagnostic",
    "assert_tree",
    "assert_tree_matches",
    "bake_in_process",
    "compile_tree",
    "fake_bake",
    "recipe_file",
    "run_cli",
]
