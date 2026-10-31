"""``run``: a local instance of a build under a fake QEMU, its receipt and its actions."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.helpers import fake_ovmf
from tundravm import instances
from tundravm.builds import Build
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR

FAKE_QEMU = """\
#!{python}
import os, signal, socket, sys, time
argv = sys.argv[1:]
def value(flag):
    return argv[argv.index(flag) + 1]
console = value("-serial").removeprefix("file:")
monitor = value("-monitor").removeprefix("unix:").split(",")[0]
with open(value("-pidfile"), "w") as handle:
    handle.write(str(os.getpid()))
with open(console, "w") as handle:
    handle.write("[    1.0] Kernel command line: console=ttyS0\\nReached target minimal.target\\n")
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
server = socket.socket(socket.AF_UNIX)
server.bind(monitor)
server.listen(1)
while True:
    conn, _ = server.accept()
    if b"quit" in conn.recv(64):
        sys.exit(0)
"""


@pytest.fixture
def qemu(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A fake ``qemu-system-x86_64`` first on PATH, and fake OVMF images."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "qemu-system-x86_64"
    script.write_text(FAKE_QEMU.format(python=sys.executable))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake_ovmf(tmp_path, monkeypatch)
    started: list[int] = []
    real = instances.launch

    def launch(*args: object, **kwargs: object) -> instances.Instance:
        found = real(*args, **kwargs)  # type: ignore[arg-type]
        started.append(int(found.data["pid"]))
        return found

    monkeypatch.setattr(instances, "launch", launch)
    yield script
    for pid in started:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _receipt(project: Path, name: str) -> dict[str, object]:
    path = project / ".tundravm" / "instances" / name / "instance.json"
    payload: dict[str, object] = json.loads(path.read_text())
    return payload


def test_run_detached_writes_a_receipt_and_never_the_build(
    dev_built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    before = {p: p.stat().st_mtime for p in dev_built.path.rglob("*")}
    result = cli("run", "--mode", "simulated", "--dev", "--detach")
    assert result.code == EXIT_OK, result.err
    assert result.out.startswith(f"instance demo-1: simulated (development build {dev_built.id})")
    assert "verdict: simulated" in result.out
    assert "stop it with `tundravm run stop demo-1`" in result.out
    receipt = _receipt(project, "demo-1")
    assert receipt["schema"] == "tundravm.instance/1"
    assert receipt["mode"] == "simulated" and receipt["platform"] == "qemu"
    assert receipt["state"] == "running" and receipt["kind"] == "run"
    build = receipt["build"]
    assert isinstance(build, dict) and build["id"] == dev_built.id
    ports = receipt["ports"]
    assert isinstance(ports, dict) and {"8080", "8745"} <= set(ports)
    command = receipt["command"]
    assert isinstance(command, list)
    disk = f"file={dev_built.path.resolve() / 'qemu.raw'},format=raw,if=virtio,snapshot=on"
    assert disk in command
    assert any(
        str(arg).startswith("type=11,value=io.systemd.stub.kernel-cmdline-extra=")
        for arg in command
    )
    events = (project / ".tundravm/instances/demo-1/events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["event"] == "launched"
    assert {p: p.stat().st_mtime for p in dev_built.path.rglob("*")} == before
    assert not any(
        p.name.startswith("qemu") for p in dev_built.path.iterdir() if p.name != "qemu.raw"
    )


def test_status_logs_mode_and_stop(dev_built: Build, project: Path, cli: Cli, qemu: Path) -> None:
    assert cli("run", "--mode", "simulated", "--dev", "--detach").code == EXIT_OK
    status = cli("run", "status", "demo-1")
    assert status.code == EXIT_OK
    assert "state          running" in status.out
    assert "control_plane  not listening" in status.out
    report = json.loads(cli("run", "status", "demo-1", "--json").out)
    assert report["schema"] == "tundravm.run/1" and report["state"] == "running"
    logs = cli("run", "logs", "demo-1")
    assert "Reached target minimal.target" in logs.out
    mode = cli("run", "mode", "demo-1")
    assert "simulated on qemu, development build: no TDX" in mode.out
    assert json.loads(cli("run", "mode", "demo-1", "--json").out)["production_evidence"] is False
    stopped = cli("run", "stop", "demo-1")
    assert stopped.out.strip() == "stopped demo-1"
    assert _receipt(project, "demo-1")["state"] == "stopped"
    assert cli("run", "stop", "demo-1").out.strip() == "was not running: demo-1"
    after = json.loads(cli("run", "status", "demo-1", "--json").out)
    assert after["state"] == "stopped" and "control_plane" not in after


def test_a_second_run_is_another_instance(
    dev_built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    assert cli("run", "--mode", "simulated", "--dev", "--detach").code == EXIT_OK
    result = cli("run", dev_built.id, "--mode", "simulated", "--detach")
    assert result.code == EXIT_OK, result.err
    assert result.out.startswith("instance demo-2:")
    for name in ("demo-1", "demo-2"):
        cli("run", "stop", name)


def test_a_production_build_runs_simulated_with_a_note(
    built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    result = cli("run", "--mode", "simulated", "--detach")
    assert result.code == EXIT_OK, result.err
    assert "wait for a TDX quote that never comes" in result.err
    ssh = cli("run", "ssh", "demo-1")
    assert ssh.code == EXIT_SDK_ERROR
    assert "runs a production build: it has no debug access" in ssh.err
    cli("run", "stop", "demo-1")


def test_tdx_mode_is_refused_without_a_tdx_host(
    built: Build, project: Path, cli: Cli, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "tdx_refusal", lambda: "unavailable: no Intel TDX host")
    result = cli("run", "--mode", "tdx")
    assert result.code == EXIT_SDK_ERROR
    assert "run --mode tdx needs an Intel TDX host (unavailable: no Intel TDX host)" in result.err
    assert not (project / ".tundravm" / "instances").exists()


def test_tdx_mode_boots_tdvf_without_console_settings(
    built: Build,
    project: Path,
    cli: Cli,
    qemu: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(instances, "tdx_refusal", lambda: None)
    tdvf = tmp_path / "OVMF.inteltdx.fd"
    tdvf.write_bytes(b"tdvf")
    monkeypatch.setattr("tundravm.deploy.qemu.TDVF_PATHS", (str(tdvf),))
    assert cli("run", "--mode", "tdx", "--detach").code == EXIT_OK
    command = _receipt(project, "demo-1")["command"]
    assert isinstance(command, list)
    assert (
        "tdx-guest,id=tdx0" in command
        and ["-bios", str(tdvf)] == command[command.index("-bios") : command.index("-bios") + 2]
    )
    assert "-smbios" not in command
    assert any("confidential-guest-support=tdx0" in str(arg) for arg in command)
    cli("run", "stop", "demo-1")


def test_actions_need_an_existing_instance(project: Path, cli: Cli) -> None:
    missing = cli("run", "status")
    assert missing.code == EXIT_SDK_ERROR and "`run status` needs INSTANCE" in missing.err
    unknown = cli("run", "logs", "nope")
    assert unknown.code == EXIT_SDK_ERROR and "No instance 'nope'" in unknown.err


def test_a_missing_qemu_is_named(
    built: Build, project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    result = cli("run", "--mode", "simulated")
    assert result.code == EXIT_SDK_ERROR and "qemu-system-x86_64 is not installed" in result.err


def test_a_volume_gets_a_disk_of_its_own(prover: Path, cli: Cli, qemu: Path) -> None:
    assert cli("build", "--dev").code == EXIT_OK
    assert cli("run", "--mode", "simulated", "--dev", "--detach").code == EXIT_OK
    receipt = _receipt(prover, "prover-1")
    volumes = receipt["volumes"]
    assert isinstance(volumes, dict) and list(volumes) == ["tundravm-data"]
    command = receipt["command"]
    assert isinstance(command, list)
    assert "virtio-blk-pci,drive=vol0,serial=tundravm-data" in command
    time.sleep(0)
    cli("run", "stop", "prover-1")
