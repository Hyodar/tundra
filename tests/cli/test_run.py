"""``run``: a local instance of a build under a fake QEMU, its receipt and its actions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.conftest import receipt as _receipt
from tundravm import instances
from tundravm._modules import instance_key
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_OK


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
    assert isinstance(ports, dict) and "8080" in ports and "8745" not in ports
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
    assert "instance_key" not in status.out
    pub = "02" + "cd" * 32
    with (project / ".tundravm/instances/demo-1/console.log").open("a") as console:
        console.write(f"[    2.0] sh[201]: {instance_key.CONSOLE}{pub}\r\n")
    assert f"instance_key   {pub}" in cli("run", "status", "demo-1").out
    assert _receipt(project, "demo-1")["instance_key"] == pub
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


def test_a_production_build_runs_simulated_fail_closed(
    built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    """It still boots (the console shows the gate refusing), and status and mode say its
    services are fail-closed."""
    result = cli("run", "--mode", "simulated", "--detach")
    assert result.code == EXIT_OK, result.err
    assert "simulated mode is fail-closed: tdx-evidence.service finds no TDX" in result.err
    status = cli("run", "status", "demo-1")
    assert f"services       {instances.FAIL_CLOSED}" in status.out
    report = json.loads(cli("run", "status", "demo-1", "--json").out)
    assert report["services"] == instances.FAIL_CLOSED
    mode = cli("run", "mode", "demo-1")
    assert instances.FAIL_CLOSED in mode.out
    ssh = cli("run", "ssh", "demo-1")
    assert ssh.code == EXIT_ERROR
    assert "runs a production build: it has no debug access" in ssh.err
    cli("run", "stop", "demo-1")


def test_a_development_build_runs_simulated_without_the_fail_closed_line(
    dev_built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    assert cli("run", "--mode", "simulated", "--dev", "--detach").code == EXIT_OK
    status = cli("run", "status", "demo-1")
    assert "fail-closed" not in status.out
    assert "services" not in json.loads(cli("run", "status", "demo-1", "--json").out)
    cli("run", "stop", "demo-1")


def test_tdx_mode_is_refused_without_a_tdx_host(
    built: Build, project: Path, cli: Cli, qemu: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(instances, "tdx_refusal", lambda: "no Intel TDX host: kvm_intel tdx is off")
    result = cli("run", "--mode", "tdx")
    assert result.code == EXIT_ERROR
    assert "run --mode tdx cannot run here: no Intel TDX host: kvm_intel tdx is off." in result.err
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
    assert missing.code == EXIT_ERROR and "`run status` needs INSTANCE" in missing.err
    unknown = cli("run", "logs", "nope")
    assert unknown.code == EXIT_ERROR and "No instance 'nope'" in unknown.err


def test_a_missing_qemu_is_named(
    built: Build, project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    result = cli("run", "--mode", "simulated")
    assert result.code == EXIT_ERROR and "qemu-system-x86_64 is not installed" in result.err


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


def test_run_probe_reads_the_probe_off_the_console_and_refuses_production(
    dev_built: Build, project: Path, cli: Cli, qemu: Path
) -> None:
    """``run probe`` renders each check's last line (the journal forwards each a second
    time, a kernel line may cut one), exits 1 on a ``fail`` and records ``probe.json``; a
    production build has no probe, so ``run --probe`` refuses it before booting."""
    checks = [row["check"] for row in dev_built.manifest["probe"]]
    assert checks[:2] == ["mode", "rtmr"] and "service:app.service" in checks
    assert cli("run", "--mode", "simulated", "--dev", "--detach").code == EXIT_OK
    lines = [f"tundravm-probe: {name} ok fine" for name in checks if name != "rtmr"]
    lines += [
        "tundravm-probe: rtmr skip no TDX",
        "[    3.1] sh[201]: tundravm-probe: service:app.service ok active",
        "tundravm-probe: service:app.service fail failed (exit-code): journalctl -u app.ser"
        "[    3.2] virtio_net virtio0 enp0s1: renamed\n"
        "vice says why",
        f"tundravm-probe: done {len(checks) - 2} ok 1 fail",
    ]
    with (project / ".tundravm/instances/demo-1/console.log").open("a") as console:
        console.write("\n".join(lines) + "\n")
    result = cli("run", "probe", "demo-1")
    assert result.code == 1, result.err
    rows = result.out.splitlines()
    assert rows[0].split() == ["check", "verdict", "detail"]
    assert rows[1].split() == ["mode", "ok", "fine"]
    table = {row.split()[0]: row.split()[1:] for row in rows[1 : len(checks) + 1]}
    assert table["rtmr"] == ["skip", "no", "TDX"]
    failed = next(row for row in rows if row.startswith("service:app.service"))
    assert failed.split()[1:3] == ["fail", "failed"]
    assert failed.endswith("journalctl -u app.service says why")
    assert rows[-2] == f"{len(checks) - 2} ok, 1 fail, 1 skip"
    record = json.loads((project / ".tundravm/instances/demo-1/probe.json").read_text())
    assert record["ok"] is False and record["done"] == {"ok": len(checks) - 2, "fail": 1}
    assert {r["check"] for r in record["results"]} == set(checks)
    cli("run", "stop", "demo-1")
    refused = cli("run", "--mode", "simulated", "--probe")
    assert refused.code == EXIT_ERROR
    assert "is a production build: it carries no boot probe" in refused.err
    assert "tundravm run --mode simulated --dev --probe" in refused.err
    assert not (project / ".tundravm/instances/demo-2").exists()


def test_run_forwards_what_the_input_chain_accepts_not_where_the_output_chain_sends(
    tmp_path: Path,
) -> None:
    conf = tmp_path / "tree/payload/mkosi.extra/etc/nftables.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(
        "table inet filter {\n\tchain input {\n\t\ttcp dport 8745 accept\n\t}\n"
        "\tchain output {\n\t\tip daddr 127.0.0.1 tcp dport 19582 accept\n\t}\n}\n"
    )
    assert Build(tmp_path, {"ports": [8080]}).tcp_ports() == (8080, 8745)


@pytest.mark.parametrize(
    ("rules", "declared", "forwarded"),
    [
        ("", [8080], [8080]),
        ('tcp dport 8745 accept comment "secret-delivery"', [8080], [8080, 8745]),
        ("tcp dport { 22, 8545 } accept", [], [22, 8545]),
    ],
)
def test_forwards_open_only_what_the_firewall_accepts_or_a_service_declares(
    tmp_path: Path, rules: str, declared: list[int], forwarded: list[int]
) -> None:
    conf = tmp_path / "tree/payload/mkosi.extra/etc/nftables.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(f"table inet filter {{\n\tchain input {{\n\t\t{rules}\n\t}}\n}}\n")
    build = Build(tmp_path, {"ports": declared})
    assert sorted(instances.forwards(build)) == forwarded
