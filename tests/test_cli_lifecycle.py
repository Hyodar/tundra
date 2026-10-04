"""CLI lifecycle across processes: bake, then measure/deploy/doctor in fresh ``main`` calls."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, ProbeRunner, doctor, main
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.recipe import load_recipe

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.backends import NixMkosiBackend

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl")
img.output_targets("qemu")
with img.profile("azure"):
    img.output_targets("azure")

nix = Image(build_dir=BUILD_DIR, backend=NixMkosiBackend())
"""


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    path.write_text(f"BUILD_DIR = {str(tmp_path / 'build')!r}\n" + RECIPE, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def test_bake_prints_next_deploy_hint(recipe: Path) -> None:
    code, out = run("bake", str(recipe))
    assert code == EXIT_OK
    assert out.splitlines()[-1] == f"next: tundravm deploy {recipe} --target qemu"


def test_measure_after_bake_in_separate_invocation(recipe: Path) -> None:
    assert run("bake", str(recipe), "--all-profiles")[0] == EXIT_OK

    code, out = run("measure", str(recipe), "--backend", "rtmr", "--json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["backend"] == "rtmr"
    assert payload["values"]

    code, out = run("measure", str(recipe), "--backend", "azure", "--profile", "azure")
    assert code == EXIT_OK
    assert out.startswith("measurements azure (azure)\n")


def test_measure_without_bake_reports_state_error(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run("measure", str(recipe), "--backend", "rtmr")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "E_STATE" in err
    assert "tundravm bake first" in err


def test_measure_rejects_multiple_profiles(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run("bake", str(recipe), "--all-profiles")
    code, _ = run("measure", str(recipe), "--backend", "rtmr", "--all-profiles")
    assert code == EXIT_SDK_ERROR
    assert "exactly one profile" in capsys.readouterr().err


def test_deploy_qemu_after_bake_in_separate_invocation(
    recipe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run("bake", str(recipe))[0] == EXIT_OK
    launched: list[list[str]] = []

    def fake_qemu(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        launched.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(
        "tundravm.image.get_adapter", lambda target: QemuDeployAdapter(runner=fake_qemu)
    )

    code, out = run(
        "deploy",
        str(recipe),
        "--target",
        "qemu",
        "--memory",
        "4GiB",
        "--cpus",
        "3",
        "--param",
        "ssh_port=2299",
    )

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


def test_deploy_rejects_malformed_param(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run("bake", str(recipe))
    code, _ = run("deploy", str(recipe), "--target", "qemu", "--param", "oops")
    assert code == EXIT_SDK_ERROR
    assert "KEY=VALUE" in capsys.readouterr().err


def _runner(returncode: int) -> ProbeRunner:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        stdout = f"{argv[0]} 9.9.9\nsecond line\n" if returncode == 0 else ""
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "boom")

    return runner


def _missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(argv[0])


def test_doctor_with_recipe_ok(recipe: Path) -> None:
    img = load_recipe(recipe, attr="nix")
    out = io.StringIO()
    code = doctor(img, out, runner=_runner(0))
    lines = out.getvalue().splitlines()
    assert code == EXIT_OK
    assert lines[0].startswith("tundravm ")
    assert lines[1].startswith("python 3.")
    assert "backend nix_mkosi: available" in lines
    assert "  ok nix nix 9.9.9" in lines
    assert lines[-1].startswith("check: ")


@pytest.mark.parametrize("runner", [_runner(1), _missing])
def test_doctor_with_recipe_missing_tool_exits_1(recipe: Path, runner: ProbeRunner) -> None:
    img = load_recipe(recipe, attr="nix")
    out = io.StringIO()
    code = doctor(img, out, runner=runner)
    text = out.getvalue()
    assert code == EXIT_FAILURE
    assert "backend nix_mkosi: unavailable" in text
    assert "  missing nix — Install Nix" in text


def test_doctor_cli_inprocess_needs_nothing(recipe: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tundravm.cli.run_probe", _missing)
    code, out = run("doctor", str(recipe))
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
    code, out = run("doctor")
    assert code == EXIT_OK
    assert "backend lima_mkosi: unavailable" in out
    assert "backend nix_mkosi: available" in out
    assert "  ok nix nix (Nix) 2.24.0" in out
    assert "backend local_linux: unavailable" in out
    assert "  missing mkosi — " in out
    assert "check:" not in out


def test_measure_follows_bake_out_dir(recipe: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "elsewhere"
    code, _ = run("bake", str(recipe), "--out", str(out_dir))
    assert code == EXIT_OK
    assert (out_dir / "bake-result.json").exists()

    code, _ = run("measure", str(recipe), "--backend", "rtmr")
    assert code == EXIT_SDK_ERROR

    code, out = run("measure", str(recipe), "--backend", "rtmr", "--json", "--out", str(out_dir))
    assert code == EXIT_OK
    assert json.loads(out)["backend"] == "rtmr"
