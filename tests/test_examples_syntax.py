import ast
from pathlib import Path


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
            if isinstance(node, ast.ImportFrom) and node.module == "tundravm":
                names = {alias.name for alias in node.names}
                assert "Image" not in names, f"{path} imports the fluent Image"
            if isinstance(node, ast.ImportFrom) and node.module == "tundravm.modules":
                raise AssertionError(f"{path} imports fluent modules")
