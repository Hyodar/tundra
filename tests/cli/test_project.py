"""``[tool.tundravm]`` and ``TUNDRAVM_*``: image, build root, backend; renamed and removed keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm.errors import ValidationError
from tundravm.project import deployment_table, find_project, render_table, settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TUNDRAVM_IMAGE", "TUNDRAVM_BUILD", "TUNDRAVM_BACKEND"):
        monkeypatch.delenv(name, raising=False)


def _table(root: Path, body: str) -> None:
    (root / "pyproject.toml").write_text(f"[tool.tundravm]\n{body}", encoding="utf-8")


def test_the_table_names_the_image_build_and_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _table(tmp_path, 'image = "prover.py"\nbuild = "out"\nbackend = "local"\n')
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "sub")
    found = settings()
    assert found.image == Path("../prover.py").resolve()
    assert found.build == (tmp_path / "out").resolve()
    assert found.backend == "local"
    assert found.lockfile == found.build / "tundravm.lock"
    assert found.instances == tmp_path.resolve() / ".tundravm" / "instances"
    assert found.origins == {"image": "pyproject", "build": "pyproject", "backend": "pyproject"}


def test_defaults_without_a_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    found = settings()
    assert found.image is None and found.backend is None
    assert found.build == Path("build")
    assert found.origins["build"] == "default"


def test_the_environment_wins_over_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _table(tmp_path, 'image = "a.py"\nbuild = "out"\nbackend = "lima"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUNDRAVM_IMAGE", "b.py")
    monkeypatch.setenv("TUNDRAVM_BUILD", "elsewhere")
    monkeypatch.setenv("TUNDRAVM_BACKEND", "inprocess")
    found = settings()
    assert found.image == Path("b.py")
    assert found.build == Path("elsewhere")
    assert found.backend == "inprocess"
    assert set(found.origins.values()) == {"environment"}


def test_an_argument_wins_over_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _table(tmp_path, 'image = "a.py"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUNDRAVM_IMAGE", "b.py")
    found = settings(Path("c.py"))
    assert found.image == Path("c.py") and found.origins["image"] == "argument"


def test_an_unknown_backend_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUNDRAVM_BACKEND", "nix")
    with pytest.raises(ValidationError, match="TUNDRAVM_BACKEND = 'nix' is not a build backend"):
        settings()


def test_recipe_was_renamed_to_image(tmp_path: Path) -> None:
    _table(tmp_path, 'recipe = "a.py"\n')
    with pytest.raises(ValidationError, match="were renamed") as caught:
        find_project(tmp_path)
    assert caught.value.hint == "Rename recipe to image."


@pytest.mark.parametrize(
    ("key", "hint"),
    [("tree", "check --tree DIR"), ("lockfile", "BUILD/tundravm.lock"), ("require", "verify")],
)
def test_removed_keys_say_where_their_job_went(tmp_path: Path, key: str, hint: str) -> None:
    _table(tmp_path, f'{key} = "x"\n')
    with pytest.raises(ValidationError, match="no longer exist") as caught:
        find_project(tmp_path)
    assert hint in (caught.value.hint or "")


def test_an_unknown_key_lists_the_accepted_ones(tmp_path: Path) -> None:
    _table(tmp_path, 'out_dir = "x"\n')
    with pytest.raises(ValidationError, match="Unknown \\[tool.tundravm\\] key 'out_dir'"):
        find_project(tmp_path)


def test_a_broken_table_is_reported_and_someone_elses_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.other\n", encoding="utf-8")
    assert find_project(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[tool.tundravm]\nimage = \n", encoding="utf-8")
    with pytest.raises(ValidationError, match="Cannot read"):
        find_project(tmp_path)


def test_deployment_tables_and_their_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _table(tmp_path, 'image = "a.py"\n\n[tool.tundravm.gcp]\nproject = "p"\nbucket = "b"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TUNDRAVM_GCP_ZONE", "europe-west4-a")
    assert deployment_table("gcp") == {"project": "p", "bucket": "b", "zone": "europe-west4-a"}
    assert deployment_table("azure") == {}


def test_render_table() -> None:
    assert render_table(image="demo.py") == '[tool.tundravm]\nimage = "demo.py"\nbuild = "build"\n'
    assert 'backend = "lima"' in render_table(image="demo.py", backend="lima")
