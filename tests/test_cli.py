"""Tests for the ``tundravm`` command-line interface."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.platforms import AzurePlatform

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl", "jq")
img.file("/etc/motd", content="hello\\n")
img.user("app", system=True)
img.service("app", command="/usr/bin/app")
img.targets("qemu")
with img.profile("azure"):
    AzurePlatform().apply(img)
"""


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    build_dir = tmp_path / "build"
    path.write_text(f"BUILD_DIR = {str(build_dir)!r}\n" + RECIPE, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def test_inspect_json_holds_the_sha256_digest(recipe: Path) -> None:
    code, out = run("inspect", str(recipe), "--json")
    assert code == EXIT_OK
    assert re.fullmatch(r"[0-9a-f]{64}", json.loads(out)["digest"])


def test_digest_is_stable_and_variant_sensitive(recipe: Path) -> None:
    def digest(*extra: str) -> str:
        return str(json.loads(run("inspect", str(recipe), "--json", *extra)[1])["digest"])

    a, b, c = digest(), digest(), digest("--variant", "azure")
    assert a == b
    assert a != c


def test_inspect_text_and_json(recipe: Path) -> None:
    code, out = run("inspect", str(recipe))
    assert code == EXIT_OK
    assert "curl" in out
    assert "/etc/motd" in out

    code, out = run("inspect", str(recipe), "--json", "--variant", "default", "--variant", "azure")
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "azure"}
    assert "curl" in payload["default"]["packages"]


def test_compile_writes_tree(recipe: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "mkosi-out"
    code, out = run("compile", str(recipe), "--out", str(out_dir))
    assert code == EXIT_OK
    assert f"compiled {out_dir}" in out
    assert "variants: default" in out
    assert out_dir.exists()
    assert any(out_dir.rglob("mkosi.conf"))


def test_compile_defaults_to_build_dir(recipe: Path, tmp_path: Path) -> None:
    code, out = run("compile", str(recipe))
    assert code == EXIT_OK
    assert str(tmp_path / "build" / "mkosi") in out


def test_lock_then_frozen_bake(recipe: Path, tmp_path: Path) -> None:
    code, out = run("lock", str(recipe))
    assert code == EXIT_OK
    lock_path = Path(out.split("locked ", 1)[1].strip())
    assert lock_path.exists()

    code, out = run("bake", str(recipe), "--lockfile", str(lock_path))
    assert code == EXIT_OK
    assert re.search(r"^default\s+qemu\s+\S+disk\.qcow2\s", out, re.M)


def test_frozen_bake_without_lock_fails_with_code(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run("bake", str(recipe), "--lockfile", str(tmp_path / "missing.lock"))
    assert code == EXIT_SDK_ERROR
    assert "E_LOCKFILE" in capsys.readouterr().err


def test_lock_then_bake_every_variant(recipe: Path) -> None:
    variants = ("--variant", "default", "--variant", "azure")
    code, out = run("lock", str(recipe), *variants)
    assert code == EXIT_OK
    assert "locked " in out
    code, out = run("bake", str(recipe), *variants)
    assert code == EXIT_OK
    assert re.search(r"^azure\s+azure\s", out, re.M)
    assert re.search(r"^default\s+qemu\s", out, re.M)


def test_unknown_variant_is_reported(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run("inspect", str(recipe), "--variant", "gcp")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "Unknown variant(s): gcp" in err
    assert "azure" in err and "default" in err


def test_attr_flag_selects_factory(tmp_path: Path) -> None:
    recipe = tmp_path / "r.py"
    recipe.write_text(
        "from tundravm import Image\n"
        "def small():\n    i = Image(); i.install('a'); return i\n"
        "def big():\n    i = Image(); i.install('b'); return i\n",
        encoding="utf-8",
    )
    code, out = run("inspect", str(recipe), "--attr", "big", "--json")
    assert code == EXIT_OK
    assert json.loads(out)["variants"]["default"]["packages"] == ["b"]


def test_missing_recipe_exits_with_sdk_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run("inspect", str(tmp_path / "nope.py"), "--json")
    assert code == EXIT_SDK_ERROR
    assert "E_VALIDATION" in capsys.readouterr().err


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_init_writes_loadable_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "my_node.py"
    code, out = run(
        "init", str(tmp_path), "--name", "my_node", "--backend", "inprocess", "--base", "debian/sid"
    )
    assert code == EXIT_OK
    assert f"created {target}" in out
    source = target.read_text(encoding="utf-8")
    assert 'base="debian/sid"' in source
    assert "InProcessBackend()" in source

    code, out = run("inspect", str(target), "--json")
    assert code == EXIT_OK
    payload = json.loads(out)["variants"]
    assert set(payload) == {"default", "dev"}
    assert payload["default"]["base"] == "debian/sid"

    assert run("lock", str(target))[0] == EXIT_OK
    code, out = run("bake", str(target), "--out", str(tmp_path / "out"))
    assert code == EXIT_OK
    assert re.search(r"^default\s+qemu\s", out, re.M)


def test_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "r.py"
    target.write_text("x = 1\n", encoding="utf-8")
    code, _ = run("init", str(tmp_path), "--name", "r")
    assert code == EXIT_SDK_ERROR
    assert "Refusing to overwrite" in capsys.readouterr().err
    code, _ = run("init", str(tmp_path), "--name", "r", "--force")
    assert code == EXIT_OK
    assert "recipe = Recipe(" in target.read_text(encoding="utf-8")


@pytest.mark.parametrize("backend", ["lima", "nix", "local", "inprocess"])
def test_init_templates_compile_for_every_backend(tmp_path: Path, backend: str) -> None:
    target = tmp_path / f"{backend}.py"
    assert run("init", str(tmp_path), "--name", backend, "--backend", backend)[0] == EXIT_OK
    compile(target.read_text(encoding="utf-8"), str(target), "exec")
