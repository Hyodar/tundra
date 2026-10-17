"""``[tool.tundravm]``: init writes it, recipe commands read it, ``tundravm config`` shows it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.project import find_project

TABLE = """[tool.tundravm]
recipe = "node.py"
tree = "mkosi"
build = "build"
lockfile = "build/tundravm.lock"
backend = "inprocess"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scaffolded ``node`` project; the test runs from inside it."""
    code, out = run_main(
        "init", str(tmp_path), "--name", "node", "--backend", "inprocess", "--no-doctor"
    )
    assert code == EXIT_OK, out
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_writes_the_table(project: Path) -> None:
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert pyproject.endswith("\n\n" + TABLE)
    found = find_project()
    assert found is not None and found.path == project / "pyproject.toml"


def test_init_prints_the_table_for_an_existing_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "theirs"\n', encoding="utf-8")
    code, out = run_main(
        "init", str(tmp_path), "--name", "node", "--backend", "inprocess", "--no-doctor"
    )
    assert code == EXIT_OK
    assert "note: add this table to it so commands run without RECIPE:\n" + TABLE in out + "\n"


def test_status_lint_and_compile_check_run_without_recipe(project: Path) -> None:
    code, out = run_main("status")
    assert code == EXIT_OK
    assert out.startswith("recipe    ok       node.py  digest ")
    assert "tree      missing  mkosi: not compiled" in out
    assert out.rstrip().endswith("next: tundravm lock")
    assert run_main("lint") == (EXIT_OK, "no findings\n")
    assert run_main("compile")[0] == EXIT_OK
    assert (project / "mkosi").is_dir() and not (project / "build" / "mkosi").exists()
    assert run_main("compile", "--check") == (EXIT_OK, "tree is up to date with the recipe\n")
    assert run_main("lock")[0] == EXIT_OK
    assert (project / "build" / "tundravm.lock").is_file()
    assert run_main("ci")[0] == EXIT_OK
    status = json.loads(run_main("status", "--format", "json")[1])
    assert status["recipe"]["path"] == "node.py"
    assert status["tree"]["verdict"] == "ok"
    assert status["next"] == "tundravm bake"


def test_the_table_is_found_from_a_subdirectory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "tests")
    code, out = run_main("config", "--format", "json")
    assert code == EXIT_OK
    values = json.loads(out)["values"]
    recipe = (project / "node.py").resolve()
    assert values["recipe"] == {"value": str(recipe), "origin": "pyproject", "exists": True}
    assert run_main("lint")[0] == EXIT_OK


def test_explicit_flags_override_the_table(project: Path) -> None:
    code, out = run_main("compile", "--tree", "elsewhere")
    assert code == EXIT_OK
    assert (project / "elsewhere").is_dir() and not (project / "mkosi").exists()
    code, out = run_main("config", "--build", "o", "--backend", "lima", "--format", "json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["pyproject"] == str(project / "pyproject.toml")
    assert payload["values"] == {
        "recipe": {"value": "node.py", "origin": "pyproject", "exists": True},
        "tree": {"value": "mkosi", "origin": "pyproject"},
        "build": {"value": "o", "origin": "flag"},
        "lockfile": {"value": "build/tundravm.lock", "origin": "pyproject", "exists": False},
        "backend": {"value": "lima", "origin": "flag"},
    }


def test_another_explicit_recipe_uses_the_built_in_defaults(project: Path) -> None:
    (project / "other.py").write_text((project / "node.py").read_text(), encoding="utf-8")
    assert run_main("compile", "other.py")[0] == EXIT_OK
    assert (project / "build" / "mkosi").is_dir() and not (project / "mkosi").exists()
    out = run_main("config", "other.py")[1]
    assert "  recipe    other.py" in out and "  tree      build/mkosi" in out
    assert "  build     build                default" in out


def test_config_text_names_each_origin(project: Path) -> None:
    code, out = run_main("config")
    assert code == EXIT_OK
    assert out.splitlines()[1:] == [
        "  recipe    node.py              pyproject",
        "  tree      mkosi                pyproject",
        "  build     build                pyproject",
        "  lockfile  build/tundravm.lock  pyproject; does not exist; run `tundravm lock`",
        "  backend   inprocess            pyproject",
    ]
    assert run_main("lock")[0] == EXIT_OK
    assert "  lockfile  build/tundravm.lock  pyproject\n" in run_main("config")[1]


def test_config_reports_the_backend_the_recipe_file_binds(project: Path) -> None:
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('backend = "inprocess"\n', ""),
        encoding="utf-8",
    )
    values = json.loads(run_main("config", "--format", "json")[1])["values"]
    assert values["backend"] == {"value": "inprocess", "origin": "recipe"}
    assert run_main("config")[1].splitlines()[-1] == (
        "  backend   inprocess            recipe (the recipe file's `backend`)"
    )


def test_missing_table_is_the_recipe_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        run_main("lint")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "error: the following arguments are required: recipe" in err
    assert "[tool.tundravm]" in err
    code, out = run_main("config")
    assert code == EXIT_OK
    assert out.startswith("pyproject: none (no [tool.tundravm] table here or above)\n")


def test_a_bad_table_is_a_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "x.py"\noutput = "build"\n', encoding="utf-8"
    )
    assert run_main("status")[0] == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "Unknown [tool.tundravm] key(s)" in err and "output" in err
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "x.py"\nbackend = "qemu"\n', encoding="utf-8"
    )
    (tmp_path / "x.py").write_text("", encoding="utf-8")
    assert run_main("bake")[0] == EXIT_SDK_ERROR
    assert "is not a build backend" in capsys.readouterr().err


def test_bake_uses_the_configured_build(project: Path) -> None:
    assert run_main("lock")[0] == EXIT_OK
    assert run_main("bake", "-q")[0] == EXIT_OK
    assert (project / "build" / "bake-result.json").is_file()


def test_the_renamed_out_key_names_its_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "x.py"\nout = "build"\n', encoding="utf-8"
    )
    assert run_main("status")[0] == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_VALIDATION]: [tool.tundravm] key(s) out in " in err
    assert "Hint: Rename out to build" in err


def test_a_configured_build_reaches_every_artifact_verb(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = project / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8").replace('build = "build"', 'build = "dist"')
    pyproject.write_text(text, encoding="utf-8")
    assert run_main("lock")[0] == EXIT_OK
    assert run_main("bake", "-q")[0] == EXIT_OK
    dist = Path("dist")
    assert (dist / "bake-result.json").is_file()
    assert not (project / "build" / "bake-result.json").exists()

    code, out = run_main("sbom", "--variant", "default")
    assert code == EXIT_OK and json.loads(out)["spdxVersion"]
    code, out = run_main(
        "measure", "--variant", "default", "--allow-placeholder", "--format", "json"
    )
    assert code == EXIT_OK
    assert Path(json.loads(out)["artifact"]).resolve().is_relative_to(dist.resolve())
    status = json.loads(run_main("status", "--format", "json")[1])
    assert status["artifacts"]["manifest"] == str(dist / "bake-result.json")
    run_main("evidence")
    assert (dist / "evidence" / "evidence.json").is_file()
    code, out = run_main("clean", "--artifacts", "--dry-run")
    assert code == EXIT_OK
    assert f"would remove {dist / 'bake-result.json'}" in out.splitlines()
    capsys.readouterr()
    assert run_main("deploy", "qemu", "--variant", "default")[0] == EXIT_SDK_ERROR
    assert "Refusing to deploy a simulated artifact." in capsys.readouterr().err
