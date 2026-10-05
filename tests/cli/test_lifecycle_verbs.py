"""CLI lifecycle across processes: bake, then measure/deploy/doctor in fresh ``main`` calls."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, ProbeRunner, doctor
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.recipe import load_file

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "lifecycle",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)
"""

NIX_RECIPE = """
from tundravm import Fragment, Package, Recipe
from tundravm.backends import NixMkosiBackend

backend = NixMkosiBackend()
recipe = Recipe("nix", Fragment("common", items=(Package("curl"),)))
"""

BUILD = Path("build")
"""Recipe files bake into ``build/`` under the working directory (the fixture's tmp_path)."""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from *tmp_path* so its build dir is ``tmp_path / "build"``."""
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


@pytest.fixture
def nix_recipe(tmp_path: Path) -> Path:
    path = tmp_path / "nix.py"
    path.write_text(NIX_RECIPE, encoding="utf-8")
    return path


BOTH = ("--variant", "default", "--variant", "azure")
DEFAULT = ("--variant", "default")


def test_bake_prints_next_deploy_hint(recipe: Path, tmp_path: Path) -> None:
    code, out = run_main("bake", str(recipe), *DEFAULT)
    assert code == EXIT_OK
    manifest = BUILD / "bake-result.json"
    assert out.splitlines()[-1] == (
        f"next: tundravm deploy {manifest} --variant default --target qemu"
    )


def test_measure_after_bake_in_separate_invocation(recipe: Path, tmp_path: Path) -> None:
    assert run_main("bake", str(recipe), *BOTH)[0] == EXIT_OK
    manifest = str(tmp_path / "build" / "bake-result.json")

    code, out = run_main(
        "measure",
        manifest,
        "--variant",
        "default",
        "--scheme",
        "rtmr",
        "--json",
        "--allow-placeholder",
    )
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["scheme"] == "rtmr"
    assert payload["tool"] == "placeholder"
    assert payload["values"]

    code, out = run_main(
        "measure", manifest, "--scheme", "azure", "--variant", "azure", "--allow-placeholder"
    )
    assert code == EXIT_OK
    assert out.startswith("measurements azure (azure)\nsource: placeholder (")


def test_measure_refuses_simulated_artifact_without_allow_placeholder(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_main("bake", str(recipe), *DEFAULT)[0] == EXIT_OK
    capsys.readouterr()
    code, _ = run_main("measure", str(tmp_path / "build"), "--scheme", "rtmr")
    assert code == EXIT_SDK_ERROR
    assert "simulated artifact" in capsys.readouterr().err


def test_measure_without_bake_reports_state_error(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("measure", str(tmp_path / "build"), "--scheme", "rtmr")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "E_STATE" in err
    assert "tundravm bake first" in err


def test_measure_needs_a_variant_when_several_are_baked(
    recipe: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main("bake", str(recipe), *BOTH)
    code, _ = run_main(
        "measure", str(tmp_path / "build"), "--scheme", "rtmr", "--allow-placeholder"
    )
    assert code == EXIT_SDK_ERROR
    assert "pass --variant NAME" in capsys.readouterr().err


def test_deploy_qemu_after_bake_in_separate_invocation(
    recipe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        str(tmp_path / "build" / "bake-result.json"),
        "--variant",
        "default",
        "--target",
        "qemu",
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
    code, _ = run_main("deploy", manifest, "--target", "qemu", "--param", "oops")
    assert code == EXIT_SDK_ERROR
    assert "KEY=VALUE" in capsys.readouterr().err
    code, _ = run_main("deploy", manifest, "--target", "qemu", "--param", "vcpus=3")
    assert code == EXIT_SDK_ERROR
    assert "Unknown qemu parameter(s): vcpus" in capsys.readouterr().err


def _runner(returncode: int) -> ProbeRunner:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        stdout = f"{argv[0]} 9.9.9\nsecond line\n" if returncode == 0 else ""
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "boom")

    return runner


def _missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(argv[0])


def test_doctor_with_recipe_ok(nix_recipe: Path) -> None:
    out = io.StringIO()
    code = doctor(load_file(nix_recipe), out, runner=_runner(0))
    lines = out.getvalue().splitlines()
    assert code == EXIT_OK
    assert lines[0].startswith("tundravm ")
    assert lines[1].startswith("python 3.")
    assert "backend nix_mkosi: available" in lines
    assert "  ok nix nix 9.9.9" in lines
    assert lines[-1].startswith("lint: ")


@pytest.mark.parametrize("runner", [_runner(1), _missing])
def test_doctor_with_recipe_missing_tool_exits_1(nix_recipe: Path, runner: ProbeRunner) -> None:
    out = io.StringIO()
    code = doctor(load_file(nix_recipe), out, runner=runner)
    text = out.getvalue()
    assert code == EXIT_FAILURE
    assert "backend nix_mkosi: unavailable" in text
    assert "  missing nix — Install Nix" in text


def test_doctor_cli_inprocess_needs_nothing(recipe: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tundravm.cli.run_probe", _missing)
    code, out = run_main("doctor", str(recipe))
    assert code == EXIT_OK
    assert "backend inprocess: available" in out
    assert "  no external tools required" in out


def test_doctor_without_recipe_probes_every_real_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def only_nix(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] != "nix":
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(list(argv), 0, "nix (Nix) 2.24.0\n", "")

    monkeypatch.setattr("tundravm.cli.run_probe", only_nix)
    code, out = run_main("doctor")
    assert code == EXIT_OK
    assert "backend lima_mkosi: unavailable" in out
    assert "backend nix_mkosi: available" in out
    assert "  ok nix nix (Nix) 2.24.0" in out
    assert "backend local_linux: unavailable" in out
    assert "  missing mkosi — " in out
    assert "lint:" not in out


def test_measure_reads_the_bake_out_dir_manifest(recipe: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "elsewhere"
    code, _ = run_main("bake", str(recipe), "--out", str(out_dir), *DEFAULT)
    assert code == EXIT_OK
    assert (out_dir / "bake-result.json").exists()

    code, _ = run_main("measure", str(tmp_path / "build"), "--scheme", "rtmr")
    assert code == EXIT_SDK_ERROR

    for manifest in (out_dir, out_dir / "bake-result.json"):
        code, out = run_main(
            "measure", str(manifest), "--scheme", "rtmr", "--json", "--allow-placeholder"
        )
        assert code == EXIT_OK
        assert json.loads(out)["scheme"] == "rtmr"


def test_doctor_local_marks_tools_tree_tools_optional_with_the_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_ukify(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[0] in ("ukify", "systemd-repart"):
            raise FileNotFoundError(argv[0])
        return subprocess.CompletedProcess(list(argv), 0, f"{argv[0]} 26\n", "")

    monkeypatch.setattr("tundravm.cli.run_probe", no_ukify)
    code, out = run_main("doctor", "--backend", "local")
    assert code == EXIT_OK
    assert "backend local_linux: available" in out
    fix = 'mkosi can use its own tools tree: add Setting("Build", "ToolsTree", ("default",))'
    assert f"  missing (optional) ukify — {fix} to the recipe, or install systemd-ukify" in out
    assert "  missing (optional) systemd-repart — " in out
    assert "  ok apt apt 26" in out


CLOUD_RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends import LocalLinuxBackend

backend = LocalLinuxBackend()
recipe = Recipe(
    "cloud",
    Fragment("common", items=(Package("systemd"),)),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
    ),
)
"""


def test_doctor_probes_the_cloud_tools_of_azure_variants(tmp_path: Path) -> None:
    path = write_recipe_file(tmp_path, CLOUD_RECIPE)
    out = io.StringIO()
    code = doctor(load_file(path), out, runner=_missing)
    text = out.getvalue()
    assert "cloud image tools:\n  missing (optional) qemu-img — " in text
    assert "install qemu-utils, or bake --variant default" in text
    assert "sgdisk" not in text
    assert code == EXIT_FAILURE  # mkosi itself is missing; the cloud tool stays optional


def test_doctor_skips_cloud_tools_without_cloud_variants(tmp_path: Path) -> None:
    source = CLOUD_RECIPE.replace(
        '        Variant("azure", parent="default", target="azure"),\n', ""
    )
    out = io.StringIO()
    doctor(load_file(write_recipe_file(tmp_path, source)), out, runner=_runner(0))
    assert "cloud image tools" not in out.getvalue()
