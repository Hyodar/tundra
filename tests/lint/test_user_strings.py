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
    "removed --out flag (--tree, --build, --output)": re.compile(r"--out\b"),
    "removed --against flag (--tree)": re.compile(r"--against\b"),
    "removed deploy --target flag (TARGET)": re.compile(r"\bdeploy\b[^\n]*--target\b"),
    "removed MANIFEST positional (--build)": re.compile(r"\b(?:measure|deploy|sbom) MANIFEST\b"),
    "removed verify --artifacts flag (--build)": re.compile(r"\bverify\b[^\n]*--artifacts\b"),
    "removed clean part flags (--what)": re.compile(
        r"\bclean\b[^\n]*--(?:sources|tree|artifacts|state|all)\b"
    ),
    "removed --json shorthand (--format json)": re.compile(
        r"\b(?:inspect|lint)\b[^\n]*--json\b(?!-)"
    ),
    "elided measure flags (--build, --variant)": re.compile(r"\bmeasure \.\.\."),
}
"""Phrases a hint, message or template must not contain, by what they name."""

RENAMED = {
    "Recipe.common (Recipe.items)": re.compile(r"\b[Rr]ecipe\.common\b|\bcommon="),
    "Variant parent= (inherits=)": re.compile(r"\bparent="),
    "Build cache_key= (cache_namespace=)": re.compile(r"\bcache_key="),
    "Directory (FileTree)": re.compile(r"\bDirectory\("),
    "assert_clean strict= (warnings=)": re.compile(r"\bassert_clean\([^)]*\bstrict="),
}
"""Removed recipe-authoring spellings, by their replacement."""
RENAMED_KEYWORDS = {
    "Variant": {"target": "outputs=", "targets": "outputs=", "parent": "inherits="},
    "Build": {"recipe": "using=", "cache_key": "cache_namespace="},
    "Recipe": {"common": "items="},
}
"""Keyword arguments of these calls that no longer exist, with their replacement."""
DOCS = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("*.md"))]
"""The current docs; ``docs/design`` keeps the dated design records as written."""


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


def _removed_keywords(text: str) -> Iterator[str]:
    """``Callee(kw=)`` for each removed keyword passed directly to a ``RENAMED_KEYWORDS`` call."""
    for callee, renamed in RENAMED_KEYWORDS.items():
        for match in re.finditer(rf"(?<![\w.]){callee}\(", text):
            depth, index = 1, match.end()
            while index < len(text) and depth:
                char = text[index]
                depth += (char in "([{") - (char in ")]}")
                keyword = re.match(r"(\w+)=(?!=)", text[index:]) if depth == 1 else None
                if keyword and text[index - 1] in "(, \n" and keyword[1] in renamed:
                    yield f"{callee}({keyword[1]}=) is {renamed[keyword[1]]}"
                index += 1


def _renamed(text: str) -> list[str]:
    hits = [what for what, pattern in RENAMED.items() if pattern.search(text)]
    return [*hits, *_removed_keywords(text)]


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


def test_package_strings_use_the_current_recipe_spellings() -> None:
    found = [
        f"{path.relative_to(PACKAGE)}:{line}: {what}"
        for path in sorted(PACKAGE.rglob("*.py"))
        for line, text in _runtime_strings(ast.parse(path.read_text(encoding="utf-8")))
        for what in _renamed(text)
    ]
    assert found == []


@pytest.mark.parametrize(
    "path",
    [*sorted(EXAMPLES.rglob("*.py")), *DOCS],
    ids=lambda path: str(path.relative_to(REPO_ROOT)),
)
def test_examples_and_docs_use_the_current_recipe_spellings(path: Path) -> None:
    assert _renamed(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "source",
    [
        'Variant("dev", parent="default")',
        'Variant("cloud", targets=("azure",))',
        'Variant(\n    "x",\n    add=Fragment("a", (Package("b"),)),\n    target="qemu",\n)',
        'Recipe("r", common=Fragment("c"))',
        "recipe.common.items",
        'Build("b", GIT, recipe=Go(output="b"), install=(Install("b", "/b"),))',
        'Build("b", GIT, script="make", cache_key="b")',
        'Directory("/opt", Path("src"))',
        "assert_clean(RECIPE, strict=True)",
    ],
)
def test_renamed_guard_catches_the_removed_spellings(source: str) -> None:
    assert _renamed(source)


def test_renamed_guard_passes_the_current_spellings() -> None:
    source = (
        'Recipe("r", items=(packages("curl"),), variants=(Variant("d", outputs=("qemu",)),))\n'
        'Build("b", GIT, using=Go(output="b"), cache_namespace="b", install=(Install("b", "/"),))\n'
        'Secret("s", targets=(SecretFile("/s"),))\nFileTree("/opt", Path("src"))\n'
        "ArtifactRef(target='qemu', path=p)\nassert_clean(RECIPE, warnings='allow')\n"
    )
    assert _renamed(source) == []
