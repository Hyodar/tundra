"""``bake(verify_reproducible=True)``, ``bake --verify-reproducible``: two builds must match."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tundravm.backends.base import MountSpec, Requirement
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.declarative import (
    Backend,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
    lower,
)
from tundravm.declarative.lifecycle import bake_image, read_artifacts
from tundravm.errors import BackendExecutionError, ReproducibilityError
from tundravm.models import REPRODUCE_DIRNAME, BakeRequest, BakeResult

RECIPE = """
from tundravm.declarative import Fragment, Package, Recipe, Variant
from tundravm.backends.local_linux import LocalLinuxBackend

backend = LocalLinuxBackend()
recipe = Recipe(
    "repro",
    items=(Package("curl"), Package("linux-image-amd64")),
    variants=(Variant("default", outputs=("qemu", "azure")),),
)
"""


def _recipe() -> Recipe:
    return Recipe(
        "repro",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu", "azure")),),
    )


@dataclass
class FlippingBackend:
    """The Linux backend; a build under ``.reproduce`` flips a byte of its qemu image."""

    name: str = "local_linux"
    inner: LocalLinuxBackend = field(default_factory=LocalLinuxBackend)
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


@dataclass
class FailingSecondBuild(FlippingBackend):
    """The second build reads the first build's record, then fails with *error*."""

    error: BaseException = field(
        default_factory=lambda: BackendExecutionError("mkosi exited 1", hint="Read its log.")
    )
    seen: list[dict[str, object]] = field(default_factory=list)

    def execute(self, request: BakeRequest) -> BakeResult:
        if request.build_dir.name == REPRODUCE_DIRNAME:
            self.seen.append(_declarative(request.build_dir.parent))
            raise self.error
        return self.inner.execute(request)


def _declarative(out: Path) -> dict[str, object]:
    payload = json.loads((out / "bake-result.json").read_text(encoding="utf-8"))
    block = payload["declarative"]
    assert isinstance(block, dict)
    return block


def test_a_bake_is_reproducible_and_records_it(tmp_path: Path) -> None:
    recipe = _recipe()
    out = tmp_path / "build"
    artifacts = bake(
        recipe,
        lock=lock(recipe),
        backend=Backend("local"),
        build=out,
        verify_reproducible=True,
    )
    assert {a.target for a in artifacts} == {"qemu", "azure"}
    assert _declarative(out)["reproducibility"] == {
        "state": "matched",
        "second_build": None,
        "differ": [],
        "error": None,
    }
    assert not (out / REPRODUCE_DIRNAME).exists()


def test_a_bake_without_the_check_records_nothing(tmp_path: Path) -> None:
    recipe = _recipe()
    bake(recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "build")
    assert "reproducibility" not in _declarative(tmp_path / "build")


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
            build=out,
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
    assert f"tundravm check RECIPE --tree {scratch / 'mkosi'}" in error.hint
    assert "SOURCE_DATE_EPOCH" in error.hint and "snapshot" in error.hint
    assert error.context == {"second_build": str(scratch), "differ": "default/qemu"}
    assert _declarative(out)["reproducibility"] == {
        "state": "different",
        "second_build": ".reproduce",
        "differ": ["default/qemu"],
        "error": None,
    }
    copied = scratch / ".sources" / sources.name / "main.go"
    assert copied.read_text(encoding="utf-8") == "package main\n"
    assert copied.stat().st_ino == (sources / "main.go").stat().st_ino
    assert (scratch / "mkosi").is_dir()


@pytest.mark.parametrize(
    ("error", "state", "message"),
    [
        (BackendExecutionError("mkosi exited 1", hint="x"), "error", "BackendExecutionError: "),
        (KeyboardInterrupt(), "interrupted", None),
    ],
)
def test_a_failed_second_build_keeps_the_first_builds_record(
    tmp_path: Path, error: BaseException, state: str, message: str | None
) -> None:
    recipe = _recipe()
    out = tmp_path / "build"
    backend = FailingSecondBuild(error=error)
    with pytest.raises(type(error)):
        bake_image(
            lower(recipe), None, locked=lock(recipe), backend=backend, build=out,
            verify_reproducible=True,
        )  # fmt: skip
    (running,) = backend.seen
    assert running["reproducibility"] == {
        "state": "running",
        "second_build": ".reproduce",
        "differ": [],
        "error": None,
    }
    assert running["recipe_digest"] and "tree_digest" not in running  # per variant now
    payload = json.loads((out / "bake-result.json").read_text(encoding="utf-8"))
    own = payload["variants"]["default"]["declarative"]
    assert own["tree_digest"] and own["reproducibility"]["state"] == state
    recorded = _declarative(out)
    assert recorded["reproducibility"]["state"] == state  # type: ignore[index]
    error_text = recorded["reproducibility"]["error"]  # type: ignore[index]
    assert error_text is None if message is None else str(error_text).startswith(message)
    assert {k: v for k, v in recorded.items() if k != "reproducibility"} == {
        k: v for k, v in running.items() if k != "reproducibility"
    }
    assert [a.target for a in read_artifacts(out)] == ["azure", "qemu"]
