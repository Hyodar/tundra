"""Instances: what ``run`` and ``deploy`` started, one directory each under ``.tundravm/instances``.

``<instance-id>/instance.json`` (schema :data:`SCHEMA`) is the receipt: the build it runs
(path, id, kind), the security mode, the platform, its endpoints and, for a local VM, the
QEMU process; ``events.jsonl`` appends one record per thing done to it (launched,
attested, delivered, unlocked, stopped, upgraded), never a secret value; ``console.log`` is
the serial console of a local VM. An instance id is ``<image>-<n>``.

:func:`launch` is ``run``: the build's ``qemu.raw`` through OVMF from its ESP (the disk
path), on a snapshot so the build directory is never written, with every TCP port the
image's firewall accepts or its services declare (the manifest's ``ports``) forwarded from
``127.0.0.1`` (the same port when it is free and
unprivileged; the management port :data:`MANAGEMENT_PORT` among them). ``simulated`` uses
KVM when ``/dev/kvm`` is usable, TCG otherwise, and appends the console settings to the
UKI's command line through SMBIOS; ``tdx`` boots TDVF with a ``tdx-guest`` object and
appends nothing, and is refused where ``doctor --for tdx`` is not ready.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

from .builds import Build
from .deploy import qemu as qemu_paths
from .errors import DeploymentError, StateError, ValidationError

SCHEMA = "tundravm.instance/1"
INSTANCE = "instance.json"
EVENTS = "events.jsonl"
CONSOLE = "console.log"
MONITOR = "monitor.sock"
PIDFILE = "qemu.pid"
KNOWN_HOSTS = "known_hosts"
MANAGEMENT_PORT = 8745
SSH_PORT = 22
QEMU = "qemu-system-x86_64"
VOLUME_SIZE = 16 << 30
"""The size of each sparse volume disk a local instance gets."""
CONSOLE_CMDLINE = (
    "console=ttyS0",
    "systemd.show_status=yes",
    "systemd.log_color=no",
    "systemd.journald.forward_to_console=1",
)

Mode = Literal["simulated", "tdx"]
MODES: tuple[Mode, ...] = ("simulated", "tdx")


@dataclass(slots=True)
class Instance:
    """One instance directory and its receipt."""

    path: Path
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data["id"])

    @property
    def platform(self) -> str:
        return str(self.data["platform"])

    @property
    def mode(self) -> Mode:
        mode = self.data["mode"]
        assert mode in MODES
        return mode  # type: ignore[no-any-return]

    @property
    def build(self) -> Build:
        return Build.open(Path(self.data["build"]["path"]))

    @property
    def host(self) -> str:
        return str(self.data.get("address") or "127.0.0.1")

    def port(self, guest: int) -> int | None:
        """The host port that reaches *guest* (the guest port itself on a cloud VM)."""
        ports = self.data.get("ports") or {}
        found = ports.get(str(guest))
        return None if found is None else int(found)

    def save(self) -> None:
        text = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
        scratch = self.path / f".{INSTANCE}.tmp"
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, self.path / INSTANCE)

    def event(self, kind: str, **fields: object) -> dict[str, object]:
        """Append one receipt to ``events.jsonl``: *kind*, the time, *fields*."""
        record: dict[str, object] = {"event": kind, "at": now(), **fields}
        with (self.path / EVENTS).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def events(self) -> list[dict[str, Any]]:
        path = self.path / EVENTS
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create(root: Path, build: Build, *, kind: str, platform: str, mode: Mode) -> Instance:
    """A new instance directory for *build* under *root*: ``<image>-<n>``."""
    root.mkdir(parents=True, exist_ok=True)
    taken = {p.name for p in root.iterdir()}
    number = 1
    while f"{build.image}-{number}" in taken:
        number += 1
    name = f"{build.image}-{number}"
    path = root / name
    path.mkdir()
    instance = Instance(
        path,
        {
            "schema": SCHEMA,
            "id": name,
            "kind": kind,
            "platform": platform,
            "mode": mode,
            "state": "created",
            "created_at": now(),
            "build": {
                "path": str(build.path.resolve()),
                "id": build.id,
                "image": build.image,
                "profile": build.profile,
                "kind": build.kind,
            },
        },
    )
    instance.save()
    return instance


def open_instance(root: Path, name: str) -> Instance:
    path = root / name
    if not (path / INSTANCE).is_file():
        known = sorted(p.name for p in root.iterdir()) if root.is_dir() else []
        raise StateError(
            f"No instance {name!r} under {root}.",
            hint=f"Instances: {', '.join(known)}." if known else "Start one with `tundravm run`.",
            context={"instances": str(root)},
        )
    data = json.loads((path / INSTANCE).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise StateError(f"{path / INSTANCE} is not a {SCHEMA} receipt.", hint="Start it again.")
    return Instance(path, data)


# ── local QEMU ───────────────────────────────────────────────────────────


def free_port(preferred: int | None = None) -> int:
    """*preferred* when it is unprivileged and free on 127.0.0.1, else any free port."""
    if preferred is not None and preferred >= 1024:
        with contextlib.suppress(OSError), socket.socket() as probe:
            probe.bind(("127.0.0.1", preferred))
            return preferred
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def forwards(build: Build) -> dict[int, int]:
    """Guest TCP port to host port for every port the image accepts or declares."""
    guest_ports = set(build.tcp_ports()) | {MANAGEMENT_PORT}
    return {port: free_port(port) for port in sorted(guest_ports)}


def _firmware(mode: Mode) -> tuple[str, str | None]:
    """``(code, vars)`` for OVMF, ``(tdvf, None)`` for TDX."""
    if mode == "tdx":
        paths = qemu_paths.TDVF_PATHS
        found = next((p for p in paths if Path(p).is_file()), None)
        if found is None:
            raise DeploymentError(
                "No TDVF firmware found.",
                hint="Install OVMF built with TDX support (`tundravm doctor --for tdx`).",
                context={"searched": ", ".join(paths)},
            )
        return found, None
    for code, store in zip(qemu_paths.OVMF_CODE_PATHS, qemu_paths.OVMF_VARS_PATHS, strict=True):
        if Path(code).is_file() and Path(store).is_file():
            return code, store
    raise DeploymentError(
        "No OVMF firmware found.",
        hint="Install ovmf (`tundravm doctor --for run`).",
        context={"searched": ", ".join(qemu_paths.OVMF_CODE_PATHS)},
    )


def kvm_usable() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def qemu_command(
    instance: Instance,
    disk: Path,
    *,
    mode: Mode,
    ports: Mapping[int, int],
    volumes: Sequence[tuple[str, Path]],
    memory: str = "2G",
    cpus: int = 2,
) -> list[str]:
    """The QEMU argv of a local instance (see the module docstring)."""
    code, store = _firmware(mode)
    kvm = mode == "tdx" or kvm_usable()
    machine = f"q35,accel={'kvm' if kvm else 'tcg'}"
    if mode == "tdx":
        machine += ",kernel-irqchip=split,confidential-guest-support=tdx0"
    cmd = [QEMU, "-machine", machine]
    if mode == "tdx":
        cmd += ["-object", "tdx-guest,id=tdx0"]
    cmd += ["-cpu", "host" if kvm else "max", "-m", memory, "-smp", str(cpus), "-no-reboot"]
    if store is None:
        cmd += ["-bios", code]
    else:
        copy = instance.path / "ovmf-vars.fd"
        if not copy.exists():
            shutil.copyfile(store, copy)
        cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={code}"]
        cmd += ["-drive", f"if=pflash,format=raw,file={copy}"]
    cmd += ["-drive", f"file={disk},format=raw,if=virtio,snapshot=on"]
    for index, (serial, path) in enumerate(volumes):
        cmd += ["-drive", f"file={path},if=none,id=vol{index},format=raw"]
        cmd += ["-device", f"virtio-blk-pci,drive=vol{index},serial={serial}"]
    if mode == "simulated":
        extra = " ".join(CONSOLE_CMDLINE).replace(",", ",,")
        cmd += ["-smbios", f"type=11,value=io.systemd.stub.kernel-cmdline-extra={extra}"]
    hostfwd = "".join(f",hostfwd=tcp:127.0.0.1:{h}-:{g}" for g, h in sorted(ports.items()))
    cmd += ["-nic", f"user,model=virtio-net-pci{hostfwd}"]
    cmd += ["-display", "none", "-serial", f"file:{instance.path / CONSOLE}"]
    cmd += ["-monitor", f"unix:{instance.path / MONITOR},server,nowait"]
    cmd += ["-pidfile", str(instance.path / PIDFILE)]
    return cmd


def tdx_refusal() -> str | None:
    """Why ``run --mode tdx`` cannot run here (``doctor --for tdx``), ``None`` when it can."""
    from .capabilities import Host, run_capabilities

    row = next(r for r in run_capabilities(Host()) if r.name == "run:tdx")
    return None if row.verdict == "ready" else f"{row.verdict}: {row.detail}"


def launch(
    root: Path,
    build: Build,
    *,
    mode: Mode,
    detach: bool,
    out: TextIO,
    memory: str = "2G",
    cpus: int = 2,
) -> Instance:
    """``run``: start *build*'s ``qemu.raw`` as a new instance; see the module docstring."""
    if mode == "tdx":
        refused = tdx_refusal()
        if refused is not None:
            raise DeploymentError(
                f"run --mode tdx needs an Intel TDX host ({refused}).",
                hint="Run `tundravm doctor --for tdx` for the missing prerequisite, or use "
                "--mode simulated (no TDX, verdicts are `simulated`).",
            )
    if mode == "simulated" and build.production:
        print(
            "note: a production build in simulated mode: its services wait for a TDX quote "
            "that never comes; only a dev build (--dev) runs its services simulated",
            file=sys.stderr,
        )
    disk = build.artifact("qemu")
    if shutil.which(QEMU) is None:
        raise DeploymentError(
            f"{QEMU} is not installed.",
            hint="Install qemu-system-x86 (`tundravm doctor --for run`).",
        )
    instance = create(root, build, kind="run", platform="qemu", mode=mode)
    ports = forwards(build)
    volumes: list[tuple[str, Path]] = []
    for volume in build.runtime().get("volumes", ()):
        path = instance.path / f"volume-{volume['name']}.img"
        with path.open("wb") as handle:
            handle.truncate(VOLUME_SIZE)
        volumes.append((str(volume["serial"]), path))
    cmd = qemu_command(
        instance, disk, mode=mode, ports=ports, volumes=volumes, memory=memory, cpus=cpus
    )
    stderr = (instance.path / "qemu.stderr").open("wb")
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stderr,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        stderr.close()
    instance.data.update(
        {
            "state": "running",
            "pid": process.pid,
            "command": cmd,
            "ports": {str(guest): host for guest, host in ports.items()},
            "volumes": {serial: str(path) for serial, path in volumes},
            "console": CONSOLE,
        }
    )
    instance.save()
    instance.event("launched", mode=mode, build=build.id, pid=process.pid)
    time.sleep(0.5)
    if process.poll() is not None:
        instance.data["state"] = "exited"
        instance.save()
        text = (instance.path / "qemu.stderr").read_text(encoding="utf-8", errors="replace")
        raise DeploymentError(
            f"QEMU exited at once (status {process.returncode}).",
            hint="Read its output below and `tundravm doctor --for run`.",
            context={"stderr": text[-2000:], "instance": instance.id},
        )
    print(describe(instance), file=out)
    if detach:
        return instance
    _follow(instance, process, out)
    return instance


def _follow(instance: Instance, process: subprocess.Popen[bytes], out: TextIO) -> None:
    """Copy the console to *out* until QEMU exits; Ctrl-C stops the instance."""
    console = instance.path / CONSOLE
    offset = 0
    try:
        while True:
            if console.is_file():
                with console.open("rb") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                offset += len(chunk)
                if chunk:
                    out.write(chunk.decode("utf-8", errors="replace"))
                    out.flush()
            if process.poll() is not None:
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        stop(instance)
        return
    instance.data["state"] = "exited"
    instance.save()
    instance.event("exited", status=process.returncode)


def describe(instance: Instance) -> str:
    """The lines ``run`` prints: the instance, its mode, what reaches what."""
    build = instance.data["build"]
    lines = [
        f"instance {instance.id}: {instance.data['mode']} ({build['kind']} build {build['id']})",
    ]
    for guest, host in sorted((instance.data.get("ports") or {}).items(), key=lambda p: int(p[0])):
        role = {str(MANAGEMENT_PORT): "management", str(SSH_PORT): "ssh"}.get(guest, "service")
        lines.append(f"  :{guest:<5} -> {instance.host}:{host}  ({role})")
    if instance.data["mode"] == "simulated":
        lines.append("  verdict: simulated (no TDX: never production evidence)")
    lines.append(f"  console: {instance.path / CONSOLE}")
    return "\n".join(lines)


def alive(instance: Instance) -> bool:
    pid = instance.data.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmdline = Path(f"/proc/{pid}/cmdline")
    if cmdline.is_file():
        return str(instance.path) in cmdline.read_bytes().decode(errors="replace")
    return True


def reachable(host: str, port: int | None, timeout: float = 1.0) -> bool:
    if port is None:
        return False
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=timeout):
        return True
    return False


def readiness(instance: Instance) -> dict[str, object]:
    """Control-plane and application readiness, as two states (plan 6.3): the management
    port answering, and every other forwarded service port answering."""
    ports = {int(g): int(h) for g, h in (instance.data.get("ports") or {}).items()}
    control = reachable(instance.host, ports.get(MANAGEMENT_PORT))
    services = {
        g: reachable(instance.host, h)
        for g, h in ports.items()
        if g not in (MANAGEMENT_PORT, SSH_PORT)
    }
    return {
        "control_plane": "listening" if control else "not listening",
        "application": "listening"
        if services and all(services.values())
        else ("none declared" if not services else "not listening"),
        "ports": {str(port): ok for port, ok in sorted(services.items())},
    }


def status(instance: Instance) -> dict[str, object]:
    """``run status``: the process, then readiness when it runs."""
    running = alive(instance)
    if not running and instance.data.get("state") == "running":
        instance.data["state"] = "exited"
        instance.save()
    report: dict[str, object] = {
        "id": instance.id,
        "mode": instance.data["mode"],
        "state": "running" if running else str(instance.data.get("state")),
        "build": instance.data["build"],
    }
    if running:
        report.update(readiness(instance))
    return report


def stop(instance: Instance, timeout: float = 10.0) -> bool:
    """Stop a local instance: ``quit`` on its monitor, then SIGTERM; ``False`` when it was
    not running."""
    if not alive(instance):
        instance.data["state"] = "stopped"
        instance.save()
        return False
    monitor = instance.path / MONITOR
    with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(2)
        conn.connect(str(monitor))
        conn.sendall(b"quit\n")
    deadline = time.monotonic() + timeout
    while alive(instance) and time.monotonic() < deadline:
        time.sleep(0.1)
    pid = instance.data.get("pid")
    if alive(instance) and isinstance(pid, int):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    instance.data["state"] = "stopped"
    instance.save()
    instance.event("stopped")
    return True


def logs(instance: Instance) -> Iterator[str]:
    """The console of a local instance."""
    path = instance.path / CONSOLE
    if not path.is_file():
        return iter(())
    return iter(path.read_text(encoding="utf-8", errors="replace").splitlines())


def ssh_permitted(instance: Instance) -> None:
    """Refuse ``ssh`` unless the build is a development one whose image runs SSH."""
    build = instance.data["build"]
    if build["kind"] != "development":
        raise ValidationError(
            f"{instance.id} runs a production build: it has no debug access.",
            hint="SSH on a production image is the control plane only; use `tundravm secrets "
            "deliver|unlock`. Build with --dev (DevAccess) for a shell.",
        )
    if instance.port(SSH_PORT) is None:
        raise ValidationError(
            f"{instance.id}'s image runs no SSH server.",
            hint="Give the image dev=DevAccess(ssh_keys=[...]) and build --dev.",
        )


def known_hosts(instance: Instance) -> Path:
    return instance.path / KNOWN_HOSTS


__all__ = [
    "CONSOLE",
    "EVENTS",
    "INSTANCE",
    "MANAGEMENT_PORT",
    "MODES",
    "SCHEMA",
    "SSH_PORT",
    "Instance",
    "Mode",
    "alive",
    "create",
    "describe",
    "forwards",
    "free_port",
    "launch",
    "logs",
    "open_instance",
    "qemu_command",
    "readiness",
    "reachable",
    "ssh_permitted",
    "status",
    "stop",
    "tdx_refusal",
]
