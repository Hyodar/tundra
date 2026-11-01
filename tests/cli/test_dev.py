"""``dev``: build the dev profile (or reuse the latest build, saying why), boot it under the
fake QEMU, probe it, follow the console, stop on Ctrl-C."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.cli.conftest import Cli, receipt
from tundravm import instances
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, EXIT_OK

LOCAL = """\
from tundravm import Image, Service
from tundravm.components import DevAccess
from tundravm.sources import GoBinary, Local

image = Image(
    "demo",
    services=[Service("app", [GoBinary("app", Local("app"))])],
    dev=DevAccess(password="tdx"),
)
"""
"""An image with a Local source, whose content the lock pins."""


def _probe_lines(build: Build, failing: str | None = None) -> str:
    """What the probe of *build* prints when every check passes but *failing*."""
    checks = [str(row["check"]) for row in build.manifest["probe"]]
    lines = [
        f"tundravm-probe: {name} {'fail bad' if name == failing else 'ok fine'}" for name in checks
    ]
    failed = int(failing is not None)
    lines.append(f"tundravm-probe: done {len(checks) - failed} ok {failed} fail")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("failing", "code"),
    [
        (None, EXIT_OK),
        ("service:app.service", EXIT_FAILURE),
        ("the probe did not finish", EXIT_FAILURE),
    ],
    ids=["every-check-ok", "a-failed-check", "the-probe-did-not-finish"],
)
def test_no_follow_reuses_the_build_and_exits_with_the_probes_verdict(
    dev_built: Build,
    project: Path,
    cli: Cli,
    qemu: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing: str | None,
    code: int,
) -> None:
    if failing == "the probe did not finish":
        monkeypatch.setattr(instances, "PROBE_TIMEOUT", 0.05)
    else:
        monkeypatch.setenv("FAKE_QEMU_CONSOLE", _probe_lines(dev_built, failing))
    result = cli("dev", "--no-follow")
    assert result.code == code, result.err
    assert result.out.startswith(f"reusing build/demo/development/{dev_built.id}\n")
    assert "instance demo-1: simulated (development build" in result.out
    assert result.out.splitlines()[-1] == "stop it with `tundravm run stop demo-1`"
    assert failing is None or failing in result.out
    assert receipt(project, "demo-1")["state"] == "running"
    assert cli("run", "stop", "demo-1").out.strip() == "stopped demo-1"


def _edit_image(project: Path, cli: Cli) -> None:
    (project / "demo.py").write_text(LOCAL.replace('"demo",', '"demo", packages=["jq"],'))
    assert cli("lock").code == EXIT_OK


def _edit_local_source(project: Path, cli: Cli) -> None:
    (project / "app" / "main.go").write_text("package main\n\nfunc main() { println() }\n")


def _edit_lock(project: Path, cli: Cli) -> None:
    lock = project / "build" / "tundravm.lock"
    payload = json.loads(lock.read_text())
    payload["toolchain"]["mkosi"] = "mkosi 99.0"
    lock.write_text(json.dumps(payload, indent=2) + "\n")


@pytest.mark.parametrize(
    ("change", "line"),
    [
        (None, "reusing build/demo/development/"),
        (_edit_image, "building: image changed"),
        (_edit_local_source, "building: Local source app changed"),
        (_edit_lock, "building: lock changed"),
    ],
    ids=["nothing", "the-image", "a-local-source", "the-lock"],
)
def test_the_latest_dev_build_is_reused_until_what_it_was_built_from_changes(
    project: Path,
    cli: Cli,
    qemu: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Path, Cli], None] | None,
    line: str,
) -> None:
    monkeypatch.setattr(instances, "PROBE_TIMEOUT", 0.05)
    (project / "demo.py").write_text(LOCAL, encoding="utf-8")
    (project / "app").mkdir()
    (project / "app" / "main.go").write_text("package main\n\nfunc main() {}\n")
    first = cli("dev", "--no-follow")
    assert first.out.startswith("building: no dev build yet\nlock written: build/tundravm.lock")
    assert "\nbuilt build/demo/development/" in first.out
    cli("run", "stop", "demo-1")
    built = [p.name for p in (project / "build" / "demo" / "development").iterdir()]
    if change is not None:
        change(project, cli)
    again = cli("dev", "--no-follow")
    assert again.out.startswith(line), again.out
    cli("run", "stop", "demo-2")
    now = [p.name for p in (project / "build" / "demo" / "development").iterdir()]
    assert (len(now) == len(built)) is (change is None)


def test_ctrl_c_stops_the_instance_and_keeps_the_build(
    dev_built: Build, project: Path, cli: Cli, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_QEMU_CONSOLE", _probe_lines(dev_built))
    real_follow, real_alive = instances.follow, instances.alive

    def follow(instance: instances.Instance, out: object, **kwargs: object) -> bool:
        def interrupted(instance: instances.Instance) -> bool:
            monkeypatch.setattr(instances, "alive", real_alive)
            raise KeyboardInterrupt

        monkeypatch.setattr(instances, "alive", interrupted)
        return real_follow(instance, out, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(instances, "follow", follow)
    result = cli("dev")
    assert result.code == EXIT_OK, result.err
    assert "following the console (`tundravm run logs demo-1` has it from boot)" in result.out
    assert result.out.splitlines()[-1] == "stopped demo-1"
    assert receipt(project, "demo-1")["state"] == "stopped"
    assert dev_built.path.is_dir()


def test_a_host_that_cannot_run_the_mode_is_refused_before_the_build(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "tdx_refusal", lambda: "no Intel TDX host: kvm_intel tdx is off")
    result = cli("dev", "--mode", "tdx")
    assert result.code == EXIT_ERROR
    assert "dev --mode tdx cannot run here" in result.err and "doctor --for tdx" in result.err
    assert not (project / "build").exists()


@pytest.mark.parametrize(
    ("lacking", "line", "fix"),
    [
        (("qemu",), "no qemu-system-x86_64", "sudo apt install qemu-system-x86"),
        (("ovmf",), "no OVMF firmware", "sudo apt install ovmf"),
        (
            ("qemu", "ovmf"),
            "no qemu-system-x86_64, no OVMF firmware",
            "sudo apt install qemu-system-x86 ovmf",
        ),
    ],
    ids=["no-qemu", "no-ovmf", "neither"],
)
def test_a_host_without_qemu_or_ovmf_hears_every_package_before_the_build(
    project: Path,
    cli: Cli,
    qemu: Path,
    monkeypatch: pytest.MonkeyPatch,
    lacking: tuple[str, ...],
    line: str,
    fix: str,
) -> None:
    if "qemu" in lacking:
        monkeypatch.setenv("PATH", "/nonexistent")
    if "ovmf" in lacking:
        monkeypatch.setattr("tundravm.deploy.qemu.OVMF_CODE_PATHS", ("/nonexistent",))
    result = cli("dev")
    assert result.code == EXIT_ERROR
    assert f"dev cannot boot the image here: {line}.\nfix: {fix}, then" in result.err
    assert not (project / "build").exists()
