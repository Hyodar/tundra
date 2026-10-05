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

PYPROJECT = "pyproject.toml"
TABLE = "tool.tundravm"

ConfigKey = Literal["recipe", "out", "tree", "lockfile", "backend"]
CONFIG_KEYS: tuple[ConfigKey, ...] = get_args(ConfigKey)
PATH_KEYS: frozenset[str] = frozenset({"recipe", "out", "tree", "lockfile"})

Origin = Literal["flag", "pyproject", "default"]

DEFAULTS: dict[ConfigKey, str] = {
    "out": "build",
    "tree": "build/mkosi",
    "lockfile": "build/tundravm.lock",
}
"""Built-in defaults, relative to the recipe's directory (``backend``: the recipe file's)."""

RESOLUTION_HELP = (
    "Project configuration: RECIPE may be omitted when a pyproject.toml with a "
    "[tool.tundravm] table is found in the working directory or a parent (the "
    "nearest one with the table wins; `tundravm init` writes it). Its keys: recipe, "
    "out (build output, bake/fetch/status/clean --out), tree (the mkosi tree, compile "
    "and ci --out, diff --against), lockfile (--lockfile; not clean's) and backend "
    "(bake and doctor --backend), with paths relative to the pyproject.toml. They apply "
    "when RECIPE is omitted or names the table's recipe. An explicit RECIPE or flag "
    "always wins; then the table; then the built-in default. `tundravm config` prints "
    "the effective values and where each came from."
)


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """The ``[tool.tundravm]`` table of *path*, with its paths made absolute."""

    path: Path
    values: Mapping[str, str] = field(default_factory=dict)

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
                return ProjectConfig(candidate, table)
    return None


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
    unknown = sorted(set(table) - set(CONFIG_KEYS))
    if unknown:
        raise ValidationError(
            f"Unknown [{TABLE}] key(s) in {path}: {', '.join(unknown)}.",
            hint=f"[{TABLE}] accepts: {', '.join(CONFIG_KEYS)}",
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
    return values


def render_table(*, recipe: str, backend: str) -> str:
    """The ``[tool.tundravm]`` table ``init`` writes (a TOML snippet, trailing newline)."""
    return (
        f"[{TABLE}]\n"
        f'recipe = "{recipe}"\n'
        f'out = "build"\n'
        f'tree = "mkosi"\n'
        f'lockfile = "build/tundravm.lock"\n'
        f'backend = "{backend}"\n'
    )


__all__ = [
    "CONFIG_KEYS",
    "DEFAULTS",
    "PATH_KEYS",
    "PYPROJECT",
    "RESOLUTION_HELP",
    "TABLE",
    "ConfigKey",
    "Origin",
    "ProjectConfig",
    "find_project",
    "render_table",
]
