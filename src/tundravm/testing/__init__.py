"""Helpers for testing images: plans, lint assertions, golden trees, CLI runs.

:func:`plan` takes an :class:`~tundravm.Image` (or one
:class:`~tundravm.components.Component`) and returns a :class:`PlannedImage`: one profile
compiled in memory and read as values (packages, files, units, users, runtime-init steps,
hooks). :func:`assert_clean` and :func:`assert_diagnostic` check lint findings,
:func:`assert_tree` compares ``tundravm.compile(image)`` against a golden directory, and
:func:`read_lock` reads the lockfile those take. :class:`FakeMkosi` and
:class:`FakeSources` stand in for mkosi and the source fetches, so a real backend runs a
whole build with nothing forked or fetched; :func:`boot` boots a built image under QEMU
and reads its serial console; :func:`run_cli` runs the CLI in-process, on an image
:func:`image_file` writes.

Importing this package does not import pytest. The fixtures live in
``tundravm.testing.pytest_plugin``, which pytest loads automatically once
tundravm is installed.
"""

from __future__ import annotations

import contextlib
import io
import os
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from tundravm._image._image import Image
from tundravm.check import render
from tundravm.declarative.lifecycle import Tree, check_report, read_lock, read_tree
from tundravm.diagnostics import Diagnostic, Severity
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
from tundravm.testing._image import ImageFile, InitStep, PlannedImage, plan
from tundravm.testing._units import UnitView

UPDATE_GOLDEN_ENV = "TUNDRAVM_UPDATE_GOLDEN"
_UNIFIED_LINES = 200

Profiles = str | Sequence[str] | None
"""A profile selection: one name, several names, or ``None`` for every declared profile."""


Lintable = Image | PlannedImage | Sequence[Diagnostic]
"""What the assertions check: an image (every profile, or those *profiles* names), one
planned profile, or findings."""


def _names(profiles: Profiles) -> list[str] | None:
    if profiles is None:
        return None
    return [profiles] if isinstance(profiles, str) else list(profiles)


def _findings(subject: Lintable, profiles: Profiles) -> Sequence[Diagnostic]:
    if isinstance(subject, Image):
        from tundravm._image import recipe_of

        return check_report(recipe_of(subject), None, variants=_names(profiles))
    if profiles is not None:
        raise TypeError("profiles= selects the profiles to lint; this input already chose one")
    return subject.lint() if isinstance(subject, PlannedImage) else subject


def _render(diagnostics: Sequence[Diagnostic]) -> str:
    return render(diagnostics) if diagnostics else "no findings"


def assert_clean(
    subject: Lintable,
    /,
    *,
    warnings: Literal["fail", "allow"] = "fail",
    allow: Sequence[str] = (),
    profiles: Profiles = None,
) -> Sequence[Diagnostic]:
    """Fail on every error and warning finding whose code is not in *allow*.

    *subject* is a :class:`PlannedImage` (``plan(image, profile)``, linted for its
    profile), the diagnostics :meth:`PlannedImage.lint` returned, or an image (linted for
    *profiles*, default: all). ``warnings="allow"`` fails on errors only. Returns every
    diagnostic, allowed or not: *subject* itself for diagnostics.
    """
    diagnostics = _findings(subject, profiles)
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
    profiles: Profiles = None,
    severity: Severity | None = None,
    profile: str | None = None,
    subject: str | None = None,
    location: str | None = None,
) -> Diagnostic:
    """Return the first diagnostic matching every given field, else fail listing them all.

    *diagnostics* is a :class:`PlannedImage` to lint, a sequence of diagnostics (what
    :meth:`PlannedImage.lint` returned), or an image to lint for the profiles *profiles*
    (default: *profile* alone when given, else all). *profile* names the
    profile, ``""`` the image as a whole; *location* the source position (``file:line``).
    """
    wanted_subject = subject
    if profiles is None and profile and isinstance(diagnostics, Image):
        profiles = [profile]
    found = _findings(diagnostics, profiles)
    for d in found:
        if (
            d.code == code
            and (severity is None or d.severity == severity)
            and (profile is None or d.profile == profile)
            and (wanted_subject is None or d.subject == wanted_subject)
            and (location is None or d.location == location)
        ):
            return d
    wanted = ", ".join(
        f"{key}={value!r}"
        for key, value in (
            ("code", code),
            ("severity", severity),
            ("profile", profile),
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


def image_file(tmp_path: Path, source: str, name: str = "image.py") -> Path:
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
    "UPDATE_GOLDEN_ENV",
    "BootResult",
    "BootUnavailable",
    "ConsoleScan",
    "FakeCall",
    "FakeFetch",
    "FakeMkosi",
    "FakeSources",
    "HttpResponse",
    "ImageFile",
    "InitStep",
    "Lintable",
    "PlannedImage",
    "Profiles",
    "UnitView",
    "assert_clean",
    "assert_diagnostic",
    "assert_tree",
    "boot",
    "boot_unavailable",
    "build_unavailable",
    "image_file",
    "plan",
    "read_lock",
    "run_cli",
    "scan_console",
]
