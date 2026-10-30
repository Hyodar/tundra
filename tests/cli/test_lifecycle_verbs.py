"""CLI lifecycle across processes: bake, then measure/deploy in fresh ``main`` calls."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.deploy.qemu import QemuDeployAdapter

RECIPE = """
from tundravm.declarative import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "lifecycle",
    items=(Package("curl"), Package("linux-image-amd64")),
    variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
)
"""

BUILD = Path("build")
"""Recipe files bake into ``build/`` under the working directory (the fixture's tmp_path)."""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from *tmp_path* so its build dir is ``tmp_path / "build"``."""
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


BOTH = ("--variant", "default", "--variant", "azure")
DEFAULT = ("--variant", "default")


def test_bake_prints_next_deploy_hint(recipe: Path, tmp_path: Path) -> None:
    code, out = run_main("bake", str(recipe), *DEFAULT)
    assert code == EXIT_OK
    assert out.splitlines()[-1] == f"next: tundravm deploy qemu --build {BUILD} --variant default"


def test_measure_after_bake_in_separate_invocation(recipe: Path, tmp_path: Path) -> None:
    assert run_main("bake", str(recipe), *BOTH)[0] == EXIT_OK
    manifest = str(tmp_path / "build")

    code, out = run_main(
        "measure",
        "--build",
        manifest,
        "--variant",
        "default",
        "--scheme",
        "qemu",
        "--format",
        "json",
        "--allow-placeholder",
    )
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["scheme"] == "qemu"
    assert payload["tool"] == "placeholder"
    assert sorted(payload["values"]) == ["RTMR1", "RTMR2"]
    assert sorted(payload["unverifiable"]) == ["MRTD", "RTMR0"]

    code, out = run_main(
        "measure",
        "--build",
        manifest,
        "--scheme",
        "azure",
        "--variant",
        "azure",
        "--allow-placeholder",
    )
    assert code == EXIT_OK
    assert out.startswith("measurements azure (azure)\nsource: placeholder (")


def test_measure_refuses_simulated_artifact_without_allow_placeholder(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("bake", str(recipe), *DEFAULT)[0] == EXIT_OK
    capsys.readouterr()
    code, _ = run_main("measure", "--build", str(tmp_path / "build"), "--scheme", "qemu")
    assert code == EXIT_SDK_ERROR
    assert "simulated artifact" in capsys.readouterr().err


def test_measure_without_bake_reports_state_error(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("measure", "--build", str(tmp_path / "build"), "--scheme", "qemu")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "E_STATE" in err
    assert "tundravm bake first" in err


def test_measure_needs_a_variant_when_several_are_baked(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main("bake", str(recipe), *BOTH)
    code, _ = run_main(
        "measure", "--build", str(tmp_path / "build"), "--scheme", "qemu", "--allow-placeholder"
    )
    assert code == EXIT_SDK_ERROR
    assert "pass --variant NAME" in capsys.readouterr().err


SSH_RECIPE = RECIPE.replace(
    'items=(Package("curl"),',
    'items=(SshAccess(authorized_keys=("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA me",)), '
    'Package("curl"),',
).replace(
    "from tundravm import", "from tundravm.declarative.utils import SshAccess\nfrom tundravm import"
)
"""``RECIPE`` with ``SshAccess``: its artifacts record the SSH port deploy forwards."""


def test_deploy_qemu_after_bake_in_separate_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = write_recipe_file(tmp_path, SSH_RECIPE, monkeypatch)
    assert run_main("bake", str(recipe), *BOTH)[0] == EXIT_OK
    launched: list[list[str]] = []

    def fake_qemu(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        launched.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(
        "tundravm.declarative.lifecycle.get_adapter",
        lambda target: QemuDeployAdapter(runner=fake_qemu),
    )
    command = (
        "deploy",
        "qemu",
        "--build",
        str(tmp_path / "build"),
        "--variant",
        "default",
        "--param",
        "memory=4GiB",
        "--param",
        "cpus=3",
        "--param",
        "ssh_port=2299",
    )
    assert run_main(*command)[0] == EXIT_SDK_ERROR  # in-process artifacts are simulated
    assert not launched

    code, out = run_main(*command, "--allow-simulated-artifact")

    assert code == EXIT_OK
    (argv,) = launched
    disk = tmp_path / "build" / "default" / "disk.qcow2"
    assert argv[argv.index("-m") + 1] == "4GiB"
    assert argv[argv.index("-smp") + 1] == "3"
    assert f"file={disk},format=qcow2,if=virtio" in argv
    assert "user,id=net0,hostfwd=tcp::2299-:22" in argv
    lines = out.splitlines()
    assert lines[0] == "deployed default to qemu"
    assert any(line.split() == ["endpoint", "ssh://localhost:2299"] for line in lines)
    assert any(line.split() == ["artifact_path", str(disk)] for line in lines)


def test_deploy_rejects_malformed_param(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main("bake", str(recipe))
    manifest = str(tmp_path / "build")
    code, _ = run_main("deploy", "qemu", "--build", manifest, "--param", "oops")
    assert code == EXIT_SDK_ERROR
    assert "KEY=VALUE" in capsys.readouterr().err
    code, _ = run_main("deploy", "qemu", "--build", manifest, "--param", "vcpus=3")
    assert code == EXIT_SDK_ERROR
    assert "Unknown qemu parameter(s): vcpus" in capsys.readouterr().err


def test_deploy_refuses_a_firmware_param_that_is_not_a_file(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main("bake", str(recipe))
    manifest = str(tmp_path / "build")
    missing = tmp_path / "TDVF.fd"
    argv = (
        "deploy",
        "qemu",
        "--build",
        manifest,
        "--variant",
        "default",
        "--allow-simulated-artifact",
    )
    code, _ = run_main(*argv, "--param", "tdx=true", "--param", f"firmware={missing}")
    assert code == EXIT_SDK_ERROR
    assert f"QEMU firmware not found: {missing}" in capsys.readouterr().err


def test_measure_reads_the_build_dir_manifest(recipe: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "elsewhere"
    code, _ = run_main("bake", str(recipe), "--build", str(out_dir), *DEFAULT)
    assert code == EXIT_OK
    assert (out_dir / "bake-result.json").exists()

    code, _ = run_main("measure", "--build", str(tmp_path / "build"), "--scheme", "qemu")
    assert code == EXIT_SDK_ERROR

    code, out = run_main(
        "measure",
        "--build",
        str(out_dir),
        "--scheme",
        "qemu",
        "--format",
        "json",
        "--allow-placeholder",
    )
    assert code == EXIT_OK
    assert json.loads(out)["scheme"] == "qemu"
