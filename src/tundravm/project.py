"""Project configuration: the ``[tool.tundravm]`` table of the nearest ``pyproject.toml``.

``tundravm init`` writes the table; every recipe command reads it so RECIPE and
the build paths can be omitted. Lookup starts in the working directory and
walks up to the first ``pyproject.toml`` that has a ``[tool.tundravm]`` table;
relative paths in it are relative to that file's directory. Precedence per
value: an explicit command-line flag, then the table, then the built-in default.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from .errors import ValidationError
from .evidence import EVIDENCE_REQUIREMENTS

PYPROJECT = "pyproject.toml"
TABLE = "tool.tundravm"

ConfigKey = Literal["recipe", "tree", "build", "lockfile", "backend"]
CONFIG_KEYS: tuple[ConfigKey, ...] = get_args(ConfigKey)
PATH_KEYS: frozenset[str] = frozenset({"recipe", "tree", "build", "lockfile"})
RENAMED_KEYS: dict[str, ConfigKey] = {"out": "build"}
"""Keys earlier versions read, with the key that replaced each."""

Origin = Literal["flag", "pyproject", "default", "recipe"]
"""Where a value came from; ``recipe``: the kind of the recipe file's ``backend``."""

DEFAULTS: dict[ConfigKey, str] = {
    "tree": "build/mkosi",
    "build": "build",
    "lockfile": "build/tundravm.lock",
}
"""Built-in defaults, relative to the recipe's directory (``backend``: the recipe file's)."""

RESOLUTION_HELP = (
    "Project configuration: RECIPE may be omitted when a pyproject.toml with a "
    "[tool.tundravm] table is found in the working directory or a parent (the "
    "nearest one with the table wins; `tundravm init` writes it). Its keys: recipe, "
    "tree (the compiled mkosi tree, every --tree), build (the build directory holding "
    "bake-result.json, every --build), lockfile (--lockfile; not clean's), backend "
    "(bake and doctor --backend) and require (a list of evidence checks, such as "
    '["reproducible", "lock"]: what status judges release readiness against and what '
    "evidence and release require when --require is omitted; default reproducible and "
    "lock; --require '' requires none) and strict_toolchain (true: `toolchain-drift`, the "
    "running build toolchain differing from the lockfile's, is an error in lint and lock "
    "--check, not a warning), with paths relative to the pyproject.toml. They apply "
    "to every command when RECIPE is omitted or names the table's recipe; commands "
    "without RECIPE (measure, deploy, sbom) always apply them. An explicit RECIPE or flag "
    "always wins; then the table; then the built-in default. A configured lockfile is "
    "never replaced by build/tundravm.lock: until `tundravm lock` writes it, commands say "
    "it does not exist and bake refuses. `tundravm config` prints the values the "
    "commands resolve (the recipe file's backend kind when none is set; whether the "
    "recipe and lockfile exist) and where each came from."
)


REQUIRE_KEY = "require"
"""The table's one list: the evidence checks ``status`` judges release readiness against
and ``evidence`` and ``release`` require when ``--require`` is omitted."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The ``[tool.tundravm]`` table of *path*, with its paths made absolute."""

    path: Path
    values: Mapping[str, str] = field(default_factory=dict)
    require: tuple[str, ...] | None = None
    """``require``: the checks a release must hold, in the order given; ``None`` when unset."""
    strict_toolchain: bool = False
    """``strict_toolchain = true``: ``toolchain-drift`` is an error, not a warning."""

    @property
    def root(self) -> Path:
        return self.path.parent

    def get(self, key: ConfigKey) -> str | None:
        return self.values.get(key)

    def path_of(self, key: ConfigKey) -> Path | None:
        """*key*'s value as a path relative to the working directory when inside it."""
        value = self.values.get(key)
        if value is None:
            return None
        return _shown(self.root / value)

    def default_path(self, key: ConfigKey) -> Path:
        """*key*'s path (:meth:`path_of`), else its built-in default beside the pyproject.toml."""
        configured = self.path_of(key)
        return configured if configured is not None else _shown(self.root / DEFAULTS[key])


def _shown(path: Path) -> Path:
    """*path* relative to the working directory when it is below it, else absolute."""
    absolute = path if path.is_absolute() else Path.cwd() / path
    try:
        return absolute.relative_to(Path.cwd())
    except ValueError:
        return absolute


def find_project(start: Path | None = None) -> ProjectConfig | None:
    """The nearest ``pyproject.toml`` at or above *start* (default: CWD) with the table."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / PYPROJECT
        if candidate.is_file():
            table = _table(candidate)
            if table is not None:
                values, require, strict = table
                return ProjectConfig(candidate, values, require, strict)
    return None


STRICT_TOOLCHAIN_KEY = "strict_toolchain"
"""The table's one boolean: ``toolchain-drift`` is an error when true."""


def _table(path: Path) -> tuple[dict[str, str], tuple[str, ...] | None, bool] | None:
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
            hint="Rename "
            + ", ".join(f"{old} to {RENAMED_KEYS[old]}" for old in renamed)
            + f' (e.g. {RENAMED_KEYS[renamed[0]]} = "build").',
            context={"path": str(path)},
        )
    unknown = sorted(set(table) - {*CONFIG_KEYS, REQUIRE_KEY, STRICT_TOOLCHAIN_KEY})
    if unknown:
        raise ValidationError(
            f"Unknown [{TABLE}] key(s) in {path}: {', '.join(unknown)}.",
            hint=f"[{TABLE}] accepts: "
            + ", ".join((*CONFIG_KEYS, REQUIRE_KEY, STRICT_TOOLCHAIN_KEY)),
            context={"path": str(path)},
        )
    require = _require(table.pop(REQUIRE_KEY), path) if REQUIRE_KEY in table else None
    strict = table.pop(STRICT_TOOLCHAIN_KEY, False)
    if not isinstance(strict, bool):
        raise ValidationError(
            f"[{TABLE}] {STRICT_TOOLCHAIN_KEY} in {path} must be true or false.",
            hint=f"Write it as {STRICT_TOOLCHAIN_KEY} = true",
            context={"path": str(path)},
        )
    values: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str) or not value:
            raise ValidationError(
                f"[{TABLE}] {key} in {path} must be a non-empty string.",
                hint=f'Write it as {key} = "..."',
                context={"path": str(path)},
            )
        values[key] = value
    return values, require, strict


def _require(value: object, path: Path) -> tuple[str, ...]:
    """``require`` as given: a list of evidence checks, each one known."""
    known = ", ".join(EVIDENCE_REQUIREMENTS)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(
            f"[{TABLE}] {REQUIRE_KEY} in {path} must be a list of strings.",
            hint=f'Write it as {REQUIRE_KEY} = ["reproducible", "lock"], from: {known}.',
            context={"path": str(path)},
        )
    unknown = [item for item in value if item not in EVIDENCE_REQUIREMENTS]
    if unknown:
        raise ValidationError(
            f"[{TABLE}] {REQUIRE_KEY} in {path} names unknown check(s): {', '.join(unknown)}.",
            hint=f"Require any of: {known}.",
            context={"path": str(path)},
        )
    return tuple(dict.fromkeys(value))


def render_table(*, recipe: str, backend: str) -> str:
    """The ``[tool.tundravm]`` table ``init`` writes (a TOML snippet, trailing newline)."""
    return (
        f"[{TABLE}]\n"
        f'recipe = "{recipe}"\n'
        f'tree = "mkosi"\n'
        f'build = "build"\n'
        f'lockfile = "build/tundravm.lock"\n'
        f'backend = "{backend}"\n'
    )


__all__ = [
    "CONFIG_KEYS",
    "DEFAULTS",
    "PATH_KEYS",
    "PYPROJECT",
    "RENAMED_KEYS",
    "REQUIRE_KEY",
    "RESOLUTION_HELP",
    "TABLE",
    "ConfigKey",
    "Origin",
    "ProjectConfig",
    "find_project",
    "render_table",
]
