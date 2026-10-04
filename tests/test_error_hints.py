"""Every ``raise <TdxError subclass>(...)`` in ``src/tundravm`` tells the user what to do next."""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
from pathlib import Path
from types import ModuleType

import tundravm
from tundravm.errors import TdxError

SRC = Path(tundravm.__file__).parent

# (path relative to src/tundravm, enclosing function, error class) for raises that may
# omit hint=. Every entry needs a comment saying why no hint can help; keep it empty.
ALLOWLIST: frozenset[tuple[str, str, str]] = frozenset()


def _modules() -> list[ModuleType]:
    found = [tundravm]
    for info in pkgutil.walk_packages(tundravm.__path__, prefix="tundravm."):
        # Importing tundravm.__main__ runs the CLI on sys.argv; its source is still scanned.
        if info.name != "tundravm.__main__":
            found.append(importlib.import_module(info.name))
    return found


def _error_classes(modules: list[ModuleType]) -> frozenset[str]:
    names: set[str] = set()
    for module in modules:
        for name, value in vars(module).items():
            if inspect.isclass(value) and issubclass(value, TdxError):
                names.add(name)
                names.add(value.__name__)
    return frozenset(names)


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _raises(path: Path, errors: frozenset[str]) -> list[tuple[int, str, str, ast.Call]]:
    """``(line, enclosing function, error class, call)`` for each TdxError raise in *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str, str, ast.Call]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.Raise) and isinstance(child.exc, ast.Call):
                name = _called_name(child.exc)
                if name in errors:
                    found.append((child.lineno, scope or "<module>", name, child.exc))
            visit(child, inner)

    visit(tree, "")
    return found


def test_every_sdk_error_raise_passes_a_hint() -> None:
    modules = _modules()
    errors = _error_classes(modules)
    assert {"TdxError", "ValidationError", "LockfileError"} <= errors

    total = 0
    missing: list[str] = []
    allowed: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        for line, scope, name, call in _raises(path, errors):
            total += 1
            if any(keyword.arg == "hint" for keyword in call.keywords):
                continue
            key = (relative, scope, name)
            if key in ALLOWLIST:
                allowed.add(key)
                continue
            missing.append(f"src/tundravm/{relative}:{line} {scope}: raise {name}(...)")

    assert total > 100, f"found only {total} TdxError raises; is the scan broken?"
    assert missing == [], "raises without hint=:\n" + "\n".join(missing)
    assert allowed == set(ALLOWLIST), f"stale allowlist entries: {set(ALLOWLIST) - allowed}"
