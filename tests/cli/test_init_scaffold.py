"""`tundravm init` scaffolds: the four templates, the project files and the generated tests."""

from __future__ import annotations

import dataclasses
import io
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import tundravm
from tests.helpers import REPO_ROOT, run_main
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.templates import (
    EXPECTED_FINDINGS,
    TEMPLATE_VARIANTS,
    TEMPLATES,
    render_recipe_template,
)
from tundravm.testing import UPDATE_GOLDEN_ENV

NAME = "node"
NO_DOCTOR = ("--backend", "inprocess", "--no-doctor")


def scaffold(project: Path, *flags: str, name: str = NAME) -> str:
    code, out = run_main("init", str(project), "--name", name, *NO_DOCTOR, *flags)
    assert code == EXIT_OK, out
    return out


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Every file under *root*: relative path -> (bytes, mode)."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_list_templates_prints_each_name_with_a_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    code, out = run_main("init", "--list-templates")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert [line.split()[0] for line in lines] == ["minimal", "service", "cloud", "prover"]
    assert all(TEMPLATES[line.split()[0]] in line for line in lines)
    assert lines[1].endswith("(default)")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_template_loads_lints_and_compiles_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    project = tmp_path / "project"
    out = scaffold(project, "--template", template)
    recipe_path = project / f"{NAME}.py"
    recipe = tundravm.load_recipe(recipe_path)
    assert tuple(v.name for v in recipe.variants) == TEMPLATE_VARIANTS[template]

    expected = set(EXPECTED_FINDINGS[template])
    assert {d.code for d in tundravm.lint(recipe)} == expected
    if not expected:
        assert f"lint {NAME}.py: no findings" in out

    monkeypatch.chdir(project)
    for tree in ("a", "b"):
        code, compiled = run_main("compile", recipe_path.name, "--out", tree)
        assert code == EXIT_OK, compiled
    first, second = snapshot(project / "a"), snapshot(project / "b")
    assert first and first == second
    assert run_main("compile", recipe_path.name, "--out", "a", "--check")[0] == EXIT_OK

    again = tmp_path / "again"
    scaffold(again, "--template", template)
    generated = (f"{NAME}.py", f"tests/test_{NAME}.py", "pyproject.toml", "README.md")
    before, after = snapshot(project), snapshot(again)
    assert {path: after[path] for path in generated} == {path: before[path] for path in generated}


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_render_is_a_pure_function_of_its_arguments(template: str) -> None:
    args = {"title": "n", "filename": "n.py", "base": "debian/trixie", "backend": "lima"}
    first = render_recipe_template(**args, template=template)
    assert first == render_recipe_template(**args, template=template)
    assert "$" not in first


def test_service_app_fragment_carries_its_lint_rule(tmp_path: Path) -> None:
    scaffold(tmp_path)
    recipe = tundravm.load_recipe(tmp_path / f"{NAME}.py")
    app: Any = recipe.common.items[-1]  # the template's App(...)
    assert type(app).__name__ == "App"
    config = {f.name: getattr(app, f.name) for f in dataclasses.fields(app) if f.init}
    assert config == {"version": "0.1.0", "port": 8080}
    privileged = dataclasses.replace(app, port=80)
    common = dataclasses.replace(recipe.common, items=(*recipe.common.items[:-1], privileged))
    found = tundravm.lint(dataclasses.replace(recipe, common=common))
    assert {d.code for d in found} == {"app-privileged-port"}


def test_scaffold_writes_the_project_files_and_lists_next_steps(tmp_path: Path) -> None:
    out = scaffold(tmp_path, name="my-node")
    created = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert created == [
        ".gitignore",
        "README.md",
        "my-node.py",
        "pyproject.toml",
        "tests/test_my_node.py",
    ]
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "my-node"' in pyproject and 'requires-python = ">=3.12"' in pyproject
    assert 'dependencies = ["tundravm"]' in pyproject and 'dev = ["pytest"]' in pyproject
    readme = [line for line in (tmp_path / "README.md").read_text().splitlines() if line]
    assert len(readme) == 5 and "service template" in readme[0]
    assert "uv run pytest tests" in readme[3]
    test_module = (tmp_path / "tests" / "test_my_node.py").read_text(encoding="utf-8")
    assert "from tundravm.testing import" in test_module
    assert all(name in test_module for name in ("compile_tree", "assert_clean", "assert_tree"))
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in test_module
    assert "  1. tundravm compile my-node.py --out mkosi" in out
    assert "  2. uv run pytest tests" in out
    assert "  5. tundravm bake my-node.py --out build" in out
    assert "note:" not in out


def test_scaffold_lints_then_lists_next_steps_then_probes(tmp_path: Path) -> None:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, f"{argv[0]} 1.0\n", "")

    out = io.StringIO()
    argv = ["init", str(tmp_path), "--name", NAME, "--backend", "nix"]
    code = main(argv, stdout=out, runner=runner)
    text = out.getvalue()
    assert code == EXIT_OK
    assert text.index("lint node.py:") < text.index("next") < text.index("checking the nix")


def test_scaffold_refuses_to_overwrite_and_never_replaces_project_files(tmp_path: Path) -> None:
    tests = tmp_path / "tests" / f"test_{NAME}.py"
    tests.parent.mkdir()
    tests.write_text("mine\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# mine\n", encoding="utf-8")
    code, _ = run_main("init", str(tmp_path), "--name", NAME, *NO_DOCTOR)
    assert code == EXIT_SDK_ERROR
    assert sorted(p.name for p in tmp_path.iterdir()) == ["README.md", "tests"]

    out = scaffold(tmp_path, "--force")
    assert f"overwrote {tests}" in out and "assert_tree" in tests.read_text()
    assert f"kept {tmp_path / 'README.md'} (already exists)" in out
    assert (tmp_path / "README.md").read_text() == "# mine\n"


def test_no_tests_skips_the_tests_module(tmp_path: Path) -> None:
    out = scaffold(tmp_path, "--no-tests")
    assert not (tmp_path / "tests").exists()
    assert "uv run pytest" not in out
    assert f"tundravm lint {NAME}.py" in (tmp_path / "README.md").read_text()
    assert "uv run pytest" not in (tmp_path / "README.md").read_text()


def test_existing_pyproject_is_kept_with_a_note(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "theirs"\n', encoding="utf-8")
    out = scaffold(tmp_path)
    assert pyproject.read_text() == '[project]\nname = "theirs"\n'
    assert f"kept {pyproject} (already exists)" in out
    assert "`uv add tundravm` and `uv add --dev pytest`" in out


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_generated_tests_pass_in_the_scratch_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: str
) -> None:
    project = tmp_path / "project"
    scaffold(project, "--template", template)
    env = {key: value for key, value in os.environ.items() if key != UPDATE_GOLDEN_ENV}
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project)]

    def pytest_in_project() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60
        )

    missing = pytest_in_project()
    assert missing.returncode == 1, missing.stdout
    assert f"{project / 'mkosi'} is missing" in missing.stdout
    assert "2 passed" in missing.stdout

    monkeypatch.chdir(project)
    assert run_main("compile", f"{NAME}.py", "--out", "mkosi")[0] == EXIT_OK
    passed = pytest_in_project()
    assert passed.returncode == 0, passed.stdout
    assert "3 passed" in passed.stdout
