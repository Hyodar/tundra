"""Tests for the ``tundravm`` command-line interface."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main

RECIPE = """
from tundravm import File, Fragment, Package, Recipe, Service, User, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "cli",
    Fragment(
        "common",
        items=(
            Package("curl"),
            Package("jq"),
            Package("linux-image-amd64"),
            File("/etc/motd", "hello\\n"),
            User("app", system=True),
            Service("app", "/usr/bin/app"),
        ),
    ),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)
"""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from *tmp_path* so its build dir is ``tmp_path / "build"``."""
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def test_inspect_json_holds_the_sha256_digest(recipe: Path) -> None:
    code, out = run_main("inspect", str(recipe), "--json")
    assert code == EXIT_OK
    assert re.fullmatch(r"[0-9a-f]{64}", json.loads(out)["digest"])


def test_digest_is_stable_and_variant_sensitive(recipe: Path) -> None:
    def digest(*extra: str) -> str:
        return str(json.loads(run_main("inspect", str(recipe), "--json", *extra)[1])["digest"])

    a, b, c = digest(), digest(), digest("--variant", "azure")
    assert a == b
    assert a != c
    assert digest("--variant", "default", "--variant", "azure") == a


def test_inspect_text_and_json(recipe: Path) -> None:
    code, out = run_main("inspect", str(recipe))
    assert code == EXIT_OK
    assert "curl" in out
    assert "/etc/motd" in out

    code, out = run_main(
        "inspect", str(recipe), "--json", "--variant", "default", "--variant", "azure"
    )
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "azure"}
    assert "curl" in payload["default"]["packages"]


def test_compile_writes_tree(recipe: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "mkosi-out"
    code, out = run_main("compile", str(recipe), "--out", str(out_dir))
    assert code == EXIT_OK
    assert f"compiled {out_dir}" in out
    assert "variants: default, azure" in out
    assert out_dir.exists()
    assert any(out_dir.rglob("mkosi.conf"))


def test_compile_defaults_to_build_dir(recipe: Path, tmp_path: Path) -> None:
    code, out = run_main("compile", str(recipe), "--variant", "default")
    assert code == EXIT_OK
    assert f"compiled {Path('build') / 'mkosi'}\n" in out
    assert "variants: default\n" in out
    assert (tmp_path / "build" / "mkosi" / "default" / "mkosi.conf").is_file()


def test_lock_then_frozen_bake(recipe: Path, tmp_path: Path) -> None:
    code, out = run_main("lock", str(recipe))
    assert code == EXIT_OK
    lock_path = Path(out.split("locked ", 1)[1].strip())
    assert lock_path.exists()

    code, out = run_main("bake", str(recipe), "--lockfile", str(lock_path))
    assert code == EXIT_OK
    assert re.search(r"^default\s+qemu\s+\S+disk\.qcow2\s", out, re.M)


def test_frozen_bake_without_lock_fails_with_code(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("bake", str(recipe), "--lockfile", str(tmp_path / "missing.lock"))
    assert code == EXIT_SDK_ERROR
    assert "E_LOCKFILE" in capsys.readouterr().err


def test_lock_then_bake_every_variant(recipe: Path) -> None:
    variants = ("--variant", "default", "--variant", "azure")
    code, out = run_main("lock", str(recipe), *variants)
    assert code == EXIT_OK
    assert "locked " in out
    code, out = run_main("bake", str(recipe), *variants)
    assert code == EXIT_OK
    assert re.search(r"^azure\s+azure\s", out, re.M)
    assert re.search(r"^default\s+qemu\s", out, re.M)


def test_unknown_variant_is_reported(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run_main("inspect", str(recipe), "--variant", "gcp")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "Unknown variant(s): gcp" in err
    assert "azure" in err and "default" in err


def test_attr_flag_selects_factory(tmp_path: Path) -> None:
    recipe = tmp_path / "r.py"
    recipe.write_text(
        "from tundravm import Fragment, Package, Recipe\n"
        "def small():\n    return Recipe('r', Fragment('c', items=(Package('a'),)))\n"
        "def big():\n    return Recipe('r', Fragment('c', items=(Package('b'),)))\n",
        encoding="utf-8",
    )
    code, out = run_main("inspect", str(recipe), "--attr", "big", "--json")
    assert code == EXIT_OK
    assert json.loads(out)["variants"]["default"]["packages"] == ["b"]


def test_missing_recipe_exits_with_sdk_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("inspect", str(tmp_path / "nope.py"), "--json")
    assert code == EXIT_SDK_ERROR
    assert "E_VALIDATION" in capsys.readouterr().err


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_init_writes_loadable_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "my_node.py"
    code, out = run_main(
        "init", str(tmp_path), "--name", "my_node", "--backend", "inprocess", "--base", "debian/sid"
    )
    assert code == EXIT_OK
    assert f"created {target}" in out
    source = target.read_text(encoding="utf-8")
    assert 'base="debian/sid"' in source
    assert "InProcessBackend()" in source

    code, out = run_main("inspect", str(target), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "dev"}
    assert payload["default"]["base"] == "debian/sid"

    assert run_main("lock", str(target))[0] == EXIT_OK
    code, out = run_main("bake", str(target), "--out", str(tmp_path / "out"))
    assert code == EXIT_OK
    assert re.search(r"^default\s+qemu\s", out, re.M)


def test_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "r.py"
    target.write_text("x = 1\n", encoding="utf-8")
    code, _ = run_main("init", str(tmp_path), "--name", "r")
    assert code == EXIT_SDK_ERROR
    assert "Refusing to overwrite" in capsys.readouterr().err
    code, _ = run_main("init", str(tmp_path), "--name", "r", "--force")
    assert code == EXIT_OK
    assert "recipe = Recipe(" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("backend", ["lima", "nix", "local", "inprocess"])
def test_init_templates_compile_for_every_backend(tmp_path: Path, backend: str) -> None:
    target = tmp_path / f"{backend}.py"
    assert run_main("init", str(tmp_path), "--name", backend, "--backend", backend)[0] == EXIT_OK
    compile(target.read_text(encoding="utf-8"), str(target), "exec")
