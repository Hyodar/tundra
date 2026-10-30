"""Declarations as Python source: what ``tundravm units convert`` prints."""

from __future__ import annotations

import keyword
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from ._component import Composite
from .model import (
    DECLARATION_TYPES,
    BootStep,
    Edit,
    File,
    Hook,
    Mount,
    Ref,
    Remove,
    Service,
    Task,
    Unit,
    key_fields,
)
from .systemd import Hardening

_PUBLIC_NAMES = {"GoBuild": "Go", "CargoBuild": "Cargo", "DotnetBuild": "Dotnet"}
"""The ``tundravm`` names of the build recipes (their classes are the compiler's)."""


@dataclass
class _Writer:
    """Renders declaration values as Python source, hoisting long text into constants."""

    tree: Path
    constants: dict[str, str] = field(default_factory=dict)
    """Text -> constant name."""
    names: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    utils: set[str] = field(default_factory=set)
    paths: bool = False
    tree_paths: bool = False

    def constant(self, text: str, hint: str) -> str:
        if text in self.constants:
            return self.constants[text]
        base = re.sub(r"[^A-Za-z0-9]+", "_", hint).strip("_").upper() or "TEXT"
        if base[0].isdigit() or keyword.iskeyword(base.lower()):
            base = f"_{base}"
        name, index = base, 2
        while name in self.names or name in ("TREE", "Path"):
            name, index = f"{base}_{index}", index + 1
        self.names.add(name)
        self.constants[text] = name
        return name

    def value(self, value: object, *, hint: str = "", field_name: str = "") -> str:
        if isinstance(value, str):
            if value.count("\n") >= 2 or ("\n" in value and len(value) > 60):
                return self.constant(value, hint or field_name)
            return repr(value)
        if isinstance(value, bool) or value is None:
            return repr(value)
        if isinstance(value, int):
            return oct(value) if field_name == "mode" else repr(value)
        if isinstance(value, Path):
            try:
                rel = value.relative_to(self.tree.resolve())
            except ValueError:
                self.paths = True
                return f"Path({str(value)!r})"
            self.tree_paths = True
            return f"TREE / {rel.as_posix()!r}"
        if isinstance(value, tuple):
            parts = [self.value(v, hint=hint, field_name=field_name) for v in value]
            return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
        if isinstance(value, Mapping):
            items = (f"{self.value(k)}: {self.value(v, hint=hint)}" for k, v in value.items())
            return "{" + ", ".join(items) + "}"
        if isinstance(value, (Edit, Remove)):
            return self.change(value)
        if is_dataclass(value) and not isinstance(value, type):
            return self.call(value)
        raise TypeError(f"cannot render {value!r}")

    def ref(self, ref: Ref) -> str:
        """``Unit.ref('ssh.service')``: the key fields as the constructor takes them."""
        self.imports.add(ref.kind)
        cls = next(t for t in DECLARATION_TYPES if t.__name__ == ref.kind)
        key = dict(zip(key_fields(ref.kind), ref.identity, strict=True))
        args = [
            repr(key[f.name]) if f.default is MISSING else f"{f.name}={key[f.name]!r}"
            for f in fields(cls)
            if f.name in key and (f.default is MISSING or f.default != key[f.name])
        ]
        return f"{ref.kind}.ref({', '.join(args)})"

    def change(self, change: Edit | Remove) -> str:
        """``File.ref('/etc/motd').edit(content=...)`` or ``Unit.ref(...).remove()``."""
        head = self.ref(change.target)
        if isinstance(change, Remove):
            return f"{head}.remove()"
        hint = (change.target.identity or (change.target.kind,))[-1].rpartition("/")[2]
        values = (
            f"{name}={self.value(value, hint=hint, field_name=name)}"
            for name, value in change.fields.items()
        )
        return f"{head}.edit({', '.join(values)})"

    def call(self, obj: Any) -> str:
        cls = type(obj)
        if isinstance(obj, Hardening):
            preset = next(
                (n for n in ("strict", "relaxed", "none") if getattr(cls, n)() == obj), ""
            )
            if preset:
                self.imports.add("Hardening")
                return f"Hardening.{preset}()"
        name = _PUBLIC_NAMES.get(cls.__name__, cls.__name__)
        if isinstance(obj, Composite):
            self.utils.add(name)
        else:
            self.imports.add(name)
        hint = _hint(obj)
        args: list[str] = []
        positional = True
        for item in fields(obj):
            if not item.init:
                continue
            value = getattr(obj, item.name)
            if item.default is MISSING and item.default_factory is MISSING:
                text = self.value(value, hint=hint, field_name=item.name)
                args.append(text if positional and not item.kw_only else f"{item.name}={text}")
                continue
            positional = False
            default = item.default if item.default is not MISSING else item.default_factory()  # type: ignore[misc]
            if value == default and type(value) is type(default):
                continue
            args.append(f"{item.name}={self.value(value, hint=hint, field_name=item.name)}")
        return f"{name}({', '.join(args)})"


def _hint(obj: object) -> str:
    match obj:
        case File(path=path):
            return path.rpartition("/")[2] or path
        case Unit(name=name) | Service(name=name) | Task(name=name):
            return name
        case Mount(where=where):
            return where.rpartition("/")[2]
        case Hook(name=name):
            return f"{name}_script"
        case BootStep(name=name):
            return name
    return type(obj).__name__


def declarations_source(decls: Sequence[object], *, names: Sequence[str] = ()) -> str:
    """Python source binding each of *decls* (``tundravm units convert``), ruff-formatted;
    *names* are the variable names (default: ``unit``, ``unit_2`` ...)."""
    writer = _Writer(tree=Path.cwd())
    body = [
        f"{names[i] if i < len(names) else 'unit' + ('_' + str(i + 1) if i else '')} = "
        + writer.value(decl)
        for i, decl in enumerate(decls)
    ]
    import tundravm

    top = sorted(n for n in writer.imports if hasattr(tundravm, n))
    rest = sorted(n for n in writer.imports if not hasattr(tundravm, n))
    lines = [f"from tundravm.declarative import {', '.join(top)}"] if top else []
    if rest:
        lines.append(f"from tundravm.declarative import {', '.join(rest)}")
    lines.append("")
    for text, name in writer.constants.items():
        lines += [f"{name} = {_block(text)}", ""]
    source, _ = _ruff_format("\n".join([*lines, *body]) + "\n", None)
    return source


def _block(text: str) -> str:
    if "\r" in text or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return repr(text)
    body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    return '"""\\\n' + body + '"""'


def _ruff_format(source: str, output: Path | None) -> tuple[str, str | None]:
    """*source* formatted by ruff, or as is with a note when ruff is not available."""
    commands: list[list[str]] = [[sys.executable, "-m", "ruff"]]
    found = shutil.which("ruff")
    if found:
        commands.append([found])
    filename = str(output) if output is not None else "recipe.py"
    for command in commands:
        try:
            done = subprocess.run(
                [*command, "format", "--stdin-filename", filename, "-"],
                input=source,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and done.stdout:
            return done.stdout, None
    return source, "ruff is not available: the module is not formatted (run `ruff format` on it)"


__all__ = ["declarations_source"]
