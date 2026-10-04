"""User-facing strings must not cite the removed fluent API or CLI verbs."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

import tundravm
from tests.helpers import REPO_ROOT

PACKAGE = Path(tundravm.__file__).parent
EXAMPLES = REPO_ROOT / "examples"

DENYLIST = {
    "fluent method call": re.compile(r"\bimg\.[a-z_]+\("),
    "fluent profile block": re.compile(r"\bwith img\b"),
    "removed `check` verb": re.compile(r"\btundravm check\b"),
    "frozen bake flag": re.compile(r"\bbake\([^)]*\bfrozen=True\b"),
    "internal source class": re.compile(r"\b(?:Http|Git)Source\("),
    "removed init hook": re.compile(r"\badd_init_script\b"),
    "module application": re.compile(r"\.apply\("),
    "fluent Image constructor": re.compile(r"\bImage\("),
    "internal module spec": re.compile(r"\b(?:Key|Disk|Secret)Spec\("),
    "internal secret target": re.compile(r"\bSecretTarget\."),
}
"""Phrases a hint, message or template must not contain, by what they name."""


def _runtime_strings(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of string constants that can reach output, minus bare string statements."""
    bare = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in bare:
            yield node.lineno, node.value


def _hits(text: str) -> list[str]:
    return [what for what, pattern in DENYLIST.items() if pattern.search(text)]


def _package_findings() -> list[str]:
    found: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, text in _runtime_strings(tree):
            for what in _hits(text):
                found.append(f"{path.relative_to(PACKAGE)}:{line}: {what}: {text[:80]!r}")
    return found


def test_package_strings_cite_only_the_declarative_api() -> None:
    assert _package_findings() == []


def test_examples_cite_only_the_declarative_api() -> None:
    found = [
        f"{path.relative_to(EXAMPLES)}:{number}: {what}"
        for path in sorted(EXAMPLES.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for what in _hits(line)
    ]
    assert found == []


@pytest.mark.parametrize(
    "source",
    [
        'error(hint="Call bake(frozen=True) first.")',
        'error(hint=f"img.apply({theirs}(), {mine}())")',
        "error(hint='pin it with HttpSource(sha256=...)')",
        "MESSAGE = 'see `tundravm check RECIPE`'",
    ],
)
def test_denylist_catches_fluent_hints(source: str) -> None:
    assert any(_hits(text) for _, text in _runtime_strings(ast.parse(source)))


def test_bare_string_statements_are_documentation() -> None:
    tree = ast.parse('"""Call img.apply(x)."""\nNAME = 1\n"""Attribute doc: Image()."""\n')
    assert list(_runtime_strings(tree)) == []
