"""Every example is a documented recipe that loads, lints clean and compiles deterministically."""

import ast
import re
from pathlib import Path

import pytest

from tests.helpers import EXAMPLE_RECIPES, REPO_ROOT, run_main
from tundravm.backends import InProcessBackend
from tundravm.backends.inprocess import TARGET_FILENAMES
from tundravm.declarative import Recipe, compile, lint
from tundravm.recipe import load_image, load_recipe

INTERNAL_MODULES = ("tundravm._modules", "tundravm._options", "tundravm._source")

INHERENT_WARNINGS = frozenset({"source-unpinned", "disk-auto-format"})
"""Warning codes an example may report: unpinned refs clear with ``tundravm lock``; a
``Disk`` that picks its device at boot stays a warning until the recipe names one."""

COMMAND = re.compile(r"^\s+tundravm (\w+) (\S+)", re.MULTILINE)

EXAMPLES = pytest.mark.parametrize(
    "path", EXAMPLE_RECIPES, ids=lambda path: path.relative_to(REPO_ROOT).as_posix()
)


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _recipe(path: Path) -> Recipe:
    return load_recipe(path, extra_paths=[REPO_ROOT])


def test_examples_are_syntax_valid() -> None:
    examples = sorted(Path("examples").rglob("*.py"))
    assert examples

    for path in examples:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))


def test_examples_use_only_the_declarative_api() -> None:
    for path in sorted(Path("examples").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "tundravm",
                "tundravm.declarative",
            ):
                names = {alias.name for alias in node.names}
                assert "Image" not in names, f"{path} imports the fluent Image"
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                assert not module.startswith(INTERNAL_MODULES), f"{path} imports {module}"


@EXAMPLES
def test_example_docstring_teaches_and_lists_three_commands(path: Path) -> None:
    doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
    assert "Teaches" in doc, f"{path.name}: say what the example teaches"
    commands = COMMAND.findall(doc)
    assert len(commands) == 3, f"{path.name}: list three commands, found {commands}"
    recipes = {target for _, target in commands if target.startswith("examples/")}
    assert recipes == {_relative(path)}, f"{path.name}: commands name another recipe"


@EXAMPLES
def test_example_binds_a_recipe(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "recipe" in bound
    assert isinstance(_recipe(path), Recipe)


@EXAMPLES
def test_example_lints_without_errors(path: Path) -> None:
    found = lint(_recipe(path))
    assert [d for d in found if d.level == "error"] == []
    assert {d.code for d in found if d.level == "warning"} <= INHERENT_WARNINGS


@EXAMPLES
def test_example_compiles_deterministically(path: Path) -> None:
    first = compile(_recipe(path))
    second = compile(_recipe(path))
    assert first.entries
    assert (first.digest, first.entries) == (second.digest, second.entries)


@EXAMPLES
def test_teaching_examples_bake_in_process(path: Path) -> None:
    backend = load_image(path, extra_paths=[REPO_ROOT]).backend
    if path.name[0].isdigit():
        assert isinstance(backend, InProcessBackend), f"{path.name} teaches on inprocess"
    else:
        assert backend is None or not isinstance(backend, InProcessBackend)


@pytest.mark.parametrize(
    "path",
    [path for path in EXAMPLE_RECIPES if path.name[0].isdigit()],
    ids=lambda path: path.name,
)
def test_teaching_example_bake_is_reproducible(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    policy = _recipe(path).policy
    if policy is not None and policy.require_frozen_lock:
        code, _ = run_main("bake", str(path), "--pythonpath", str(REPO_ROOT), "-q")
        assert code != 0, f"{path.name}: the policy must refuse an unlocked bake"
        return
    baked = []
    for out in ("a", "b"):
        code, _ = run_main("bake", str(path), "--pythonpath", str(REPO_ROOT), "--out", out, "-q")
        assert code == 0
        baked.append(
            sorted(
                (artifact.relative_to(out).as_posix(), artifact.read_bytes())
                for artifact in Path(out).rglob("*")
                if artifact.name in TARGET_FILENAMES.values()
            )
        )
    assert baked[0] and baked[0] == baked[1]
