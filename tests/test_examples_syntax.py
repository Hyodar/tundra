import ast
from pathlib import Path


def test_examples_are_syntax_valid() -> None:
    examples = sorted(Path("examples").glob("*.py"))
    assert examples

    for path in examples:
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path))
