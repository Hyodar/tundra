"""``bake(verify_reproducible=True)``, ``bake --verify-reproducible``: two builds must match."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import Backend, ReproducibilityError, bake, lock
from tundravm.backends.base import MountSpec, Requirement
from tundravm.backends.inprocess import InProcessBackend
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import Fragment, Package, Recipe, Variant, lower
from tundravm.declarative.lifecycle import bake_image
from tundravm.models import REPRODUCE_DIRNAME, BakeRequest, BakeResult

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "repro",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(Variant("default", targets=("qemu", "azure")),),
)
"""


def _recipe() -> Recipe:
    return Recipe(
        "repro",
        Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
        variants=(Variant("default", targets=("qemu", "azure")),),
    )


@dataclass
class FlippingBackend:
    """The in-process backend; a build under ``.reproduce`` flips a byte of its qemu image."""

    name: str = "inprocess"
    inner: InProcessBackend = field(default_factory=InProcessBackend)
    builds: list[Path] = field(default_factory=list)

    def requirements(self) -> tuple[Requirement, ...]:
        return ()

    def mount_plan(self, request: BakeRequest) -> tuple[MountSpec, ...]:
        return self.inner.mount_plan(request)

    def prepare(self, request: BakeRequest) -> None:
        self.inner.prepare(request)

    def execute(self, request: BakeRequest) -> BakeResult:
        self.builds.append(request.build_dir)
        result = self.inner.execute(request)
        if request.build_dir.name == REPRODUCE_DIRNAME:
            path = result.profiles[request.profile].artifacts["qemu"].path
            data = bytearray(path.read_bytes())
            data[0] ^= 0x01
            path.write_bytes(bytes(data))
        return result

    def cleanup(self, request: BakeRequest) -> None:
        del request


def _declarative(out: Path) -> dict[str, object]:
    payload = json.loads((out / "bake-result.json").read_text(encoding="utf-8"))
    block = payload["declarative"]
    assert isinstance(block, dict)
    return block


def test_inprocess_bake_is_reproducible_and_records_it(tmp_path: Path) -> None:
    recipe = _recipe()
    out = tmp_path / "build"
    artifacts = bake(
        recipe,
        lock=lock(recipe),
        backend=Backend("inprocess"),
        out=out,
        verify_reproducible=True,
    )
    assert {a.target for a in artifacts} == {"qemu", "azure"}
    assert _declarative(out)["reproducible"] is True
    assert not (out / REPRODUCE_DIRNAME).exists()


def test_a_bake_without_the_check_records_nothing(tmp_path: Path) -> None:
    recipe = _recipe()
    bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), out=tmp_path / "build")
    assert "reproducible" not in _declarative(tmp_path / "build")


def test_a_differing_second_build_fails_with_a_table_and_keeps_it(tmp_path: Path) -> None:
    recipe = _recipe()
    out = tmp_path / "build"
    sources = out / ".sources" / "tool-0123456789ab-cdef0123"
    sources.mkdir(parents=True)
    (sources / "main.go").write_text("package main\n", encoding="utf-8")
    backend = FlippingBackend()
    with pytest.raises(ReproducibilityError) as caught:
        bake_image(
            lower(recipe),
            None,
            locked=lock(recipe),
            backend=backend,
            out=out,
            verify_reproducible=True,
        )
    error = caught.value
    assert error.code == "E_REPRODUCIBILITY"
    scratch = out / REPRODUCE_DIRNAME
    assert backend.builds == [out, scratch]
    lines = str(error).splitlines()
    assert lines[0] == (
        "The bake is not reproducible: 1 of 2 artifact(s) differ between two builds."
    )
    header, azure, qemu = lines[1:4]
    assert header.split() == ["variant", "target", "first", "second"]
    assert azure.split()[:2] == ["default", "azure"] and azure.split()[-1] == "match"
    assert qemu.split()[:2] == ["default", "qemu"] and qemu.split()[-1] == "mismatch"
    assert qemu.split()[2] != qemu.split()[3]
    assert error.hint is not None
    assert f"tundravm diff RECIPE --against {scratch / 'mkosi'}" in error.hint
    assert "SOURCE_DATE_EPOCH" in error.hint and "snapshot" in error.hint
    assert error.context == {"second_build": str(scratch), "differ": "default/qemu"}
    assert _declarative(out)["reproducible"] is False
    copied = scratch / ".sources" / sources.name / "main.go"
    assert copied.read_text(encoding="utf-8") == "package main\n"
    assert copied.stat().st_ino == (sources / "main.go").stat().st_ino
    assert (scratch / "mkosi").is_dir()


def test_cli_bake_verify_reproducible_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    code, out = run_main("bake", str(recipe), "-q", "--verify-reproducible")
    assert code == EXIT_OK, out
    assert "reproducible: yes (2 artifacts match a second build)" in out
    code, out = run_main("status", str(recipe))
    assert code == EXIT_OK
    artifact_lines = [line for line in out.splitlines() if line.startswith("artifact")]
    assert len(artifact_lines) == 2
    assert all("  reproducible  " in line for line in artifact_lines)
    code, out = run_main("status", str(recipe), "--format", "json")
    items = json.loads(out)["artifacts"]["items"]
    assert [item["reproducible"] for item in items] == [True, True]
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    code, out = run_main("status", str(recipe), "--format", "json")
    assert [item["reproducible"] for item in json.loads(out)["artifacts"]["items"]] == [
        None,
        None,
    ]


def test_cli_reports_e_reproducibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = RECIPE.replace(
        "backend = InProcessBackend()",
        "from tests.lifecycle.test_verify_reproducible import FlippingBackend\n"
        "backend = FlippingBackend()",
    )
    recipe = write_recipe_file(tmp_path, source, monkeypatch)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    code, _ = run_main("bake", str(recipe), "-q", "--verify-reproducible")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_REPRODUCIBILITY]: The bake is not reproducible" in err
    assert "default  qemu" in err and "mismatch" in err
    code, out = run_main("status", str(recipe))
    assert "not reproducible" in out
