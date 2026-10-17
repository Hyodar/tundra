"""`tundravm init` scaffolds: the four templates, the project files and the generated tests."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import tundravm
from tests.helpers import REPO_ROOT, run_main
from tundravm import templates
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.templates import (
    EXPECTED_FINDINGS,
    TEMPLATE_VARIANTS,
    TEMPLATES,
    TUNDRAVM_REPOSITORY,
    TundravmSource,
    render_pyproject,
    render_recipe_template,
    source_from_direct_url,
)
from tundravm.testing import UPDATE_GOLDEN_ENV

NAME = "node"
COMMIT = "258d089fdc105803ce0080a40aaa35138e5f2cb6"
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
        code, compiled = run_main("compile", recipe_path.name, "--tree", tree)
        assert code == EXIT_OK, compiled
    first, second = snapshot(project / "a"), snapshot(project / "b")
    assert first and first == second
    assert run_main("compile", recipe_path.name, "--tree", "a", "--check")[0] == EXIT_OK

    again = tmp_path / "again"
    scaffold(again, "--template", template)
    generated = (f"{NAME}.py", f"tests/test_{NAME}.py", "pyproject.toml", "README.md")
    before, after = snapshot(project), snapshot(again)
    assert {path: after[path] for path in generated} == {path: before[path] for path in generated}


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_every_template_installs_the_distribution_kernel(tmp_path: Path, template: str) -> None:
    scaffold(tmp_path, "--template", template)
    recipe = tundravm.load_recipe(tmp_path / f"{NAME}.py")
    kernel = tundravm.Package("linux-image-amd64")
    for variant in recipe.variants:
        assert kernel in tundravm.resolve(recipe, variant=variant.name).items, variant.name

    items = tuple(item for item in recipe.items if item != kernel)
    stripped = dataclasses.replace(recipe, items=items)
    assert "kernel-missing" in {d.code for d in tundravm.lint(stripped)}


@pytest.mark.parametrize("template", list(TEMPLATES))
def test_render_is_a_pure_function_of_its_arguments(template: str) -> None:
    args = {"title": "n", "filename": "n.py", "base": "debian/trixie", "backend": "lima"}
    first = render_recipe_template(**args, template=template)
    assert first == render_recipe_template(**args, template=template)
    assert "$" not in first


def test_service_app_fragment_carries_its_lint_rule(tmp_path: Path) -> None:
    scaffold(tmp_path)
    recipe = tundravm.load_recipe(tmp_path / f"{NAME}.py")
    app: Any = recipe.items[-1]  # the template's App(...)
    assert type(app).__name__ == "App"
    config = {f.name: getattr(app, f.name) for f in dataclasses.fields(app) if f.init}
    assert config == {"version": "0.1.0", "port": 8080}
    privileged = dataclasses.replace(app, port=80)
    items = (*recipe.items[:-1], privileged)
    found = tundravm.lint(dataclasses.replace(recipe, items=items))
    assert {d.code for d in found} == {"app-privileged-port"}


def test_scaffold_writes_the_project_files_and_lists_next_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        templates, "tundravm_source", lambda: TundravmSource(TUNDRAVM_REPOSITORY, COMMIT, True)
    )
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
    assert f'dependencies = ["tundravm @ git+{TUNDRAVM_REPOSITORY}@{COMMIT}"]' in pyproject
    assert 'dev = ["pytest"]' in pyproject
    assert '[tool.pytest.ini_options]\npythonpath = ["."]\n' in pyproject
    readme = [line for line in (tmp_path / "README.md").read_text().splitlines() if line]
    assert len(readme) == 5 and "service template" in readme[0]
    assert "uv run pytest tests" in readme[3]
    test_module = (tmp_path / "tests" / "test_my_node.py").read_text(encoding="utf-8")
    assert "from tundravm.testing import" in test_module
    assert all(name in test_module for name in ("compile_tree", "assert_clean", "assert_tree"))
    assert "TUNDRAVM_UPDATE_GOLDEN=1" in test_module
    steps = [line.split()[1:3] for line in out.splitlines() if re.match(r"  \d\. ", line)]
    assert steps == [
        ["tundravm", "lock"],
        ["tundravm", "compile"],
        ["uv", "run"],
        ["tundravm", "ci"],
        ["tundravm", "bake"],
    ]
    assert "  1. tundravm lock my-node.py" in out
    assert "record recipe sections and pin source repositories/downloads" in out
    assert "  2. tundravm compile my-node.py --tree mkosi" in out
    assert "  3. uv run pytest tests" in out
    assert "  4. tundravm ci my-node.py --tree mkosi" in out
    assert "  5. tundravm bake my-node.py --build build" in out
    assert f"  `uv sync` installs the pinned dependency, tundravm at {COMMIT[:12]}" in out
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


def test_existing_pyproject_is_kept_with_a_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = TundravmSource(TUNDRAVM_REPOSITORY, COMMIT, True)
    monkeypatch.setattr(templates, "tundravm_source", lambda: source)
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "theirs"\n', encoding="utf-8")
    out = scaffold(tmp_path)
    assert pyproject.read_text() == '[project]\nname = "theirs"\n'
    assert f"kept {pyproject} (already exists)" in out
    note = next(line for line in out.splitlines() if line.startswith("note:"))
    assert f"`{source.install}` and `uv add --dev pytest`" in note
    assert "uv sync" not in out  # the dependency is not in their pyproject.toml yet


def test_an_unpinned_dependency_is_noted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = TundravmSource(TUNDRAVM_REPOSITORY, "master", False)
    monkeypatch.setattr(templates, "tundravm_source", lambda: source)
    out = scaffold(tmp_path)
    assert source.note is not None
    assert f"note: {source.note}" in out.splitlines()
    assert "  `uv sync` installs the dependency, tundravm at master" in out.splitlines()


def _direct_url(**record: object) -> str:
    return json.dumps(record)


def test_a_git_install_pins_its_commit_and_repository() -> None:
    raw = _direct_url(
        url="https://github.com/someone/tundra",
        vcs_info={"vcs": "git", "commit_id": COMMIT, "requested_revision": "master"},
    )
    source = source_from_direct_url(raw)
    assert source == TundravmSource("https://github.com/someone/tundra", COMMIT, pinned=True)
    assert source.requirement == f"tundravm @ git+https://github.com/someone/tundra@{COMMIT}"
    assert source.install == f'uv add "{source.requirement}"'
    assert source.note is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        _direct_url(url="https://example.com/tundravm-0.1.0.tar.gz", archive_info={}),
        _direct_url(url="https://github.com/Hyodar/tundra", vcs_info={"vcs": "git"}),
        _direct_url(url="file:///nowhere", vcs_info={"vcs": "git", "commit_id": COMMIT}),
        _direct_url(url="file:///nowhere/at/all", dir_info={"editable": True}),
    ],
)
def test_an_unknown_commit_follows_the_default_branch_with_a_note(raw: str | None) -> None:
    source = source_from_direct_url(raw)
    assert source == TundravmSource(TUNDRAVM_REPOSITORY, "master", pinned=False)
    assert source.note is not None and "follows master" in source.note
    pyproject = render_pyproject(name="node", source=source)
    assert f'dependencies = ["tundravm @ git+{TUNDRAVM_REPOSITORY}@master"]' in pyproject
    assert "could not tell which commit\n# it ran from, so this follows master." in pyproject


def _git(cwd: Path, *args: str) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def test_an_editable_checkout_pins_head_only_once_a_remote_branch_has_it(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "tundra"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "first")
    head = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "remote", "add", "origin", "git@github.com:someone/tundra.git")
    raw = _direct_url(url=checkout.as_uri(), dir_info={"editable": True})
    assert not source_from_direct_url(raw).pinned  # HEAD is on no remote yet

    _git(checkout, "update-ref", "refs/remotes/origin/master", head)
    source = source_from_direct_url(raw)
    assert source == TundravmSource("https://github.com/someone/tundra", head, pinned=True)
    pyproject = render_pyproject(name="node", source=source)
    assert f'dependencies = ["tundravm @ git+https://github.com/someone/tundra@{head}"]' in (
        pyproject
    )
    assert "# tundravm is not on PyPI: this pins the commit `tundravm init` ran from." in (
        pyproject
    )


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
    assert run_main("compile", f"{NAME}.py", "--tree", "mkosi")[0] == EXIT_OK
    passed = pytest_in_project()
    assert passed.returncode == 0, passed.stdout
    assert "3 passed" in passed.stdout
