"""Project configuration: ``[tool.tundravm]`` of the nearest ``pyproject.toml`` and ``TUNDRAVM_*``.

Three values, each resolved the same way: an explicit command-line argument, then the
environment (``TUNDRAVM_IMAGE``, ``TUNDRAVM_BUILD``, ``TUNDRAVM_BACKEND``), then the table,
then the built-in default. ``image`` is the image file (``image = "prover.py"``), ``build``
the build root (``build = "build"``: the lockfile ``build/tundravm.lock``, the build
directories ``build/<image>/production/<build-id>/``), ``backend`` the build backend
(``local``, ``lima``, ``inprocess``). Relative paths in the table are relative to the
``pyproject.toml``; in the environment, to the working directory. Lookup walks up from the
working directory to the first ``pyproject.toml`` that has the table. Instances live in
``.tundravm/instances/`` beside it (in the working directory without one).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from .errors import ValidationError

PYPROJECT = "pyproject.toml"
TABLE = "tool.tundravm"

ConfigKey = Literal["image", "build", "backend"]
CONFIG_KEYS: tuple[ConfigKey, ...] = get_args(ConfigKey)
PATH_KEYS: frozenset[str] = frozenset({"image", "build"})
ENVIRONMENT: dict[ConfigKey, str] = {
    "image": "TUNDRAVM_IMAGE",
    "build": "TUNDRAVM_BUILD",
    "backend": "TUNDRAVM_BACKEND",
}
"""The variable that sets each key for one shell or CI job."""

RENAMED_KEYS: dict[str, str] = {"recipe": "image", "out": "build"}
"""Keys earlier versions read, with the key that replaced each."""
REMOVED_KEYS: dict[str, str] = {
    "tree": "pass the committed tree to `tundravm check --tree DIR`",
    "lockfile": "the lockfile is BUILD/tundravm.lock, beside the build directories",
    "require": "a build directory records what was checked; `tundravm verify` checks it",
    "strict_toolchain": "`tundravm check` reports toolchain drift; the lock pins the toolchain",
}
"""Keys that no longer exist, with where their job went."""

BACKEND_KINDS: tuple[str, ...] = ("local", "lima", "inprocess")

Origin = Literal["argument", "environment", "pyproject", "default"]

DEFAULT_BUILD = "build"
INSTANCES = Path(".tundravm") / "instances"
"""Where ``run`` and ``deploy`` keep one directory per instance, under the project root."""

RESOLUTION_HELP = (
    "IMAGE defaults to [tool.tundravm] image in the nearest pyproject.toml, or "
    "$TUNDRAVM_IMAGE; the build root to its `build` key or $TUNDRAVM_BUILD (default: "
    "build); the backend to $TUNDRAVM_BACKEND or its `backend` key (default: local on "
    "Linux x86-64, lima elsewhere)."
)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The ``[tool.tundravm]`` table of *path*."""

    path: Path
    values: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.path.parent

    def get(self, key: ConfigKey) -> str | None:
        return self.values.get(key)


@dataclass(frozen=True, slots=True)
class Settings:
    """What a command runs against: the image file, the build root, the backend kind, and
    where each came from; *root* is the project directory (instances live there)."""

    image: Path | None
    build: Path
    backend: str | None
    root: Path
    origins: dict[str, Origin] = field(default_factory=dict)

    @property
    def lockfile(self) -> Path:
        """``BUILD/tundravm.lock``: the one lockfile every command reads."""
        return self.build / "tundravm.lock"

    @property
    def instances(self) -> Path:
        return self.root / INSTANCES


def find_project(start: Path | None = None) -> ProjectConfig | None:
    """The nearest ``pyproject.toml`` at or above *start* (default: CWD) with the table."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PYPROJECT
        if candidate.is_file():
            table = _table(candidate)
            if table is not None:
                return ProjectConfig(candidate, table)
    return None


def settings(image: Path | None = None, *, start: Path | None = None) -> Settings:
    """Resolve the image, build root and backend for a command given *image* (or none)."""
    project = find_project(start)
    origins: dict[str, Origin] = {}
    base = Path.cwd() if project is None else project.root

    def chosen(key: ConfigKey) -> tuple[str | None, Origin, Path]:
        env = os.environ.get(ENVIRONMENT[key], "").strip()
        if env:
            return env, "environment", Path.cwd()
        value = None if project is None else project.get(key)
        if value is not None:
            return value, "pyproject", base
        return None, "default", base

    found_image: Path | None = image
    if image is not None:
        origins["image"] = "argument"
    else:
        value, origin, where = chosen("image")
        if value is not None:
            found_image = _shown(where / value)
            origins["image"] = origin
    value, origin, where = chosen("build")
    build = _shown(where / (value or DEFAULT_BUILD))
    origins["build"] = origin
    backend, origin, _ = chosen("backend")
    if backend is not None and backend not in BACKEND_KINDS:
        source = ENVIRONMENT["backend"] if origin == "environment" else f"[{TABLE}] backend"
        raise ValidationError(
            f"{source} = {backend!r} is not a build backend.",
            hint=f"Use one of: {', '.join(BACKEND_KINDS)}.",
        )
    origins["backend"] = origin
    return Settings(found_image, build, backend, base, origins)


def _shown(path: Path) -> Path:
    """*path* relative to the working directory when it is below it, else absolute."""
    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        return absolute.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return absolute.resolve()


def _table(path: Path) -> dict[str, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        if "[tool.tundravm]" not in text:
            return None  # someone else's broken pyproject.toml: not ours to report
        raise ValidationError(
            f"Cannot read {path}: {exc}",
            hint="Fix the TOML syntax, or remove the file's [tool.tundravm] table.",
            context={"path": str(path)},
        ) from exc
    tool = data.get("tool")
    table = tool.get("tundravm") if isinstance(tool, dict) else None
    if not isinstance(table, dict):
        return None
    renamed = sorted(set(table) & set(RENAMED_KEYS))
    if renamed:
        raise ValidationError(
            f"[{TABLE}] key(s) {', '.join(renamed)} in {path} were renamed.",
            hint="Rename " + ", ".join(f"{old} to {RENAMED_KEYS[old]}" for old in renamed) + ".",
            context={"path": str(path)},
        )
    removed = sorted(set(table) & set(REMOVED_KEYS))
    if removed:
        raise ValidationError(
            f"[{TABLE}] key(s) {', '.join(removed)} in {path} no longer exist.",
            hint="; ".join(f"{key}: {REMOVED_KEYS[key]}" for key in removed) + ".",
            context={"path": str(path)},
        )
    values: dict[str, str] = {}
    for key, value in table.items():
        if key not in CONFIG_KEYS and not isinstance(value, dict):
            raise ValidationError(
                f"Unknown [{TABLE}] key {key!r} in {path}.",
                hint=f"[{TABLE}] accepts {', '.join(CONFIG_KEYS)} and the [{TABLE}.azure] and "
                f"[{TABLE}.gcp] deployment tables.",
                context={"path": str(path)},
            )
        if isinstance(value, dict):
            continue
        if not isinstance(value, str) or not value:
            raise ValidationError(
                f"[{TABLE}] {key} in {path} must be a non-empty string.",
                hint=f'Write it as {key} = "..."',
                context={"path": str(path)},
            )
        values[key] = value
    return values


def deployment_table(target: str, start: Path | None = None) -> dict[str, str]:
    """``[tool.tundravm.<target>]`` (``azure``, ``gcp``): the cloud settings ``deploy`` uses
    (``project``, ``bucket``, ``zone`` ...), overridden by ``TUNDRAVM_<TARGET>_<KEY>``."""
    found: dict[str, str] = {}
    project = find_project(start)
    if project is not None:
        data = tomllib.loads(project.path.read_text(encoding="utf-8"))
        table = data.get("tool", {}).get("tundravm", {}).get(target, {})
        if isinstance(table, dict):
            found.update({str(k): str(v) for k, v in table.items()})
    prefix = f"TUNDRAVM_{target.upper()}_"
    for name, value in os.environ.items():
        if name.startswith(prefix) and value:
            found[name.removeprefix(prefix).lower()] = value
    return found


def render_table(*, image: str, backend: str | None = None) -> str:
    """The ``[tool.tundravm]`` table ``new`` writes (a TOML snippet, trailing newline)."""
    return f'[{TABLE}]\nimage = "{image}"\nbuild = "build"\n' + (
        "" if backend is None else f'backend = "{backend}"\n'
    )


__all__ = [
    "BACKEND_KINDS",
    "CONFIG_KEYS",
    "ENVIRONMENT",
    "INSTANCES",
    "PATH_KEYS",
    "PYPROJECT",
    "REMOVED_KEYS",
    "RENAMED_KEYS",
    "RESOLUTION_HELP",
    "TABLE",
    "ConfigKey",
    "Origin",
    "ProjectConfig",
    "Settings",
    "deployment_table",
    "find_project",
    "render_table",
    "settings",
]
