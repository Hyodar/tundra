"""QEMU deployment adapter.

Launches a QEMU VM with the baked artifact, supporting:
- OVMF firmware for UEFI boot (TDVF via ``-bios`` for a TDX guest)
- UKI (Unified Kernel Image) via -kernel flag
- TDX confidential computing support
- Port forwarding for SSH and every declared ``Secrets`` port
- A detached VM with a serial log and a monitor socket, or one attached to the terminal
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, DeployResult

from ._run import CommandRunner, run_attached, run_captured, stderr_of

# Common OVMF firmware paths
OVMF_CODE_PATHS = (
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/edk2/ovmf/OVMF_CODE.fd",
    "/usr/share/qemu/OVMF_CODE.fd",
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
)

OVMF_VARS_PATHS = (
    "/usr/share/OVMF/OVMF_VARS.fd",
    "/usr/share/edk2/ovmf/OVMF_VARS.fd",
    "/usr/share/qemu/OVMF_VARS.fd",
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
)

# A TDX guest boots TDVF (OVMF built with TDX support) as one image through -bios.
TDVF_PATHS = (
    "/usr/share/ovmf/OVMF.fd",
    "/usr/share/edk2/ovmf/OVMF.inteltdx.fd",
    "/usr/share/OVMF/OVMF.fd",
    "/usr/share/qemu/OVMF.fd",
)

SERIAL_LOG = "qemu-serial.log"
MONITOR_SOCKET = "qemu.monitor"
PIDFILE = "qemu.pid"
VARS_COPY = "qemu-ovmf-vars.fd"

# sockaddr_un.sun_path holds 108 bytes, the terminating NUL included.
_UNIX_PATH_MAX = 107

QemuRunner = CommandRunner


def _find_firmware(paths: tuple[str, ...], name: str) -> str:
    for path in paths:
        if Path(path).exists():
            return path
    raise DeploymentError(
        f"OVMF firmware not found: {name}",
        hint="Install OVMF/edk2 package for UEFI boot support.",
        context={"searched_paths": ", ".join(paths)},
    )


@dataclass(slots=True)
class QemuDeployAdapter:
    name: str = "qemu"
    qemu_binary: str = "qemu-system-x86_64"
    extra_args: list[str] = field(default_factory=list)
    # None: require qemu_binary on PATH; a detached launch captures QEMU's output,
    # an attached one leaves the terminal to QEMU.
    runner: QemuRunner | None = None

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"qemu-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        memory = params.pop("memory", "2G")
        cpus = params.pop("cpus", "2")
        ssh_port = params.pop("ssh_port", "2222")
        enable_tdx = params.pop("tdx", "false").lower() == "true"
        daemonize = params.pop("daemonize", "true").lower() == "true"
        forwards = parse_forwards(params.pop("forward", ""))
        hosts = [ssh_port, *(str(host) for host, _ in forwards)]
        if len(set(hosts)) != len(hosts):
            raise DeploymentError(
                "Two QEMU port forwards use the same host port.",
                hint="Give every forward (and ssh_port) its own host port.",
                context={"ssh_port": ssh_port, "forward": format_forwards(forwards)},
            )
        artifact_path = request.artifact_path
        if not artifact_path.exists():
            raise DeploymentError(
                "Artifact path does not exist.",
                hint="Run bake() successfully before deploy().",
                context={"artifact_path": str(artifact_path)},
            )

        # Check if QEMU is available
        if self.runner is None and shutil.which(self.qemu_binary) is None:
            raise DeploymentError(
                f"QEMU binary not found: {self.qemu_binary}",
                hint="Install QEMU and ensure it is in PATH.",
                context={"binary": self.qemu_binary},
            )

        # Determine if this is a UKI (.efi) or disk image
        suffixes = artifact_path.suffixes
        is_uki = ".efi" in suffixes or artifact_path.suffix == ".efi"
        machine = "q35,accel=kvm"
        if enable_tdx:
            machine = f"{machine},kernel-irqchip=split,confidential-guest-support=tdx0"

        cmd: list[str] = [self.qemu_binary, "-machine", machine]
        if enable_tdx:
            cmd.extend(["-object", "tdx-guest,id=tdx0"])
        cmd.extend(["-cpu", "host", "-m", memory, "-smp", cpus, "-no-reboot"])

        # Firmware: TDVF through -bios for TDX (it cannot run from pflash), else OVMF pflash
        if enable_tdx:
            cmd.extend(["-bios", _find_firmware(TDVF_PATHS, "TDVF (OVMF.fd with TDX support)")])
        elif is_uki:
            ovmf_code = _find_firmware(OVMF_CODE_PATHS, "OVMF_CODE")
            # The VARS store is written to: boot from a per-variant copy, not /usr/share
            ovmf_vars = _run_dir(artifact_path.absolute(), request.profile) / VARS_COPY
            if not ovmf_vars.exists():
                shutil.copyfile(_find_firmware(OVMF_VARS_PATHS, "OVMF_VARS"), ovmf_vars)
            cmd.extend(
                [
                    "-drive",
                    f"file={ovmf_code},if=pflash,format=raw,readonly=on",
                    "-drive",
                    f"file={ovmf_vars},if=pflash,format=raw",
                ]
            )
        if is_uki:
            cmd.extend(["-kernel", str(artifact_path)])
        else:
            disk_format = _disk_format_for_path(artifact_path)
            cmd.extend(["-drive", f"file={artifact_path},format={disk_format},if=virtio"])

        # User-mode networking: ssh_port to 22, then every other forward
        hostfwd = "".join(f",hostfwd=tcp::{host}-:{guest}" for host, guest in forwards)
        cmd.extend(
            [
                "-netdev",
                f"user,id=net0,hostfwd=tcp::{ssh_port}-:22{hostfwd}",
                "-device",
                "virtio-net-pci,netdev=net0",
            ]
        )

        state: dict[str, str] = {}
        if daemonize:
            # -nographic and a stdio serial cannot be combined with -daemonize, and a
            # daemonized QEMU changes to /, so these paths are absolute
            run_dir = _run_dir(artifact_path.absolute(), request.profile)
            state = {
                "serial_log": str(run_dir / SERIAL_LOG),
                "monitor": str(run_dir / MONITOR_SOCKET),
                "pidfile": str(run_dir / PIDFILE),
            }
            if len(state["monitor"].encode()) > _UNIX_PATH_MAX:
                raise DeploymentError(
                    "The QEMU monitor socket path is too long for a unix socket.",
                    hint="Bake into a shorter --out directory, or attach instead.",
                    context={"monitor": state["monitor"]},
                )
            cmd.extend(
                [
                    "-display",
                    "none",
                    "-serial",
                    f"file:{state['serial_log']}",
                    "-monitor",
                    f"unix:{state['monitor']},server,nowait",
                    "-daemonize",
                    "-pidfile",
                    state["pidfile"],
                ]
            )
        else:
            # Attached: the console and the monitor (Ctrl-A C) share this terminal
            cmd.extend(["-nographic", "-serial", "mon:stdio"])

        # Extra args
        cmd.extend(self.extra_args)

        # Launch
        runner = self.runner
        if runner is None:
            runner = run_captured if daemonize else run_attached
        result = runner(cmd)

        if result.returncode != 0:
            raise DeploymentError(
                "QEMU launch failed." if daemonize else "QEMU exited with an error.",
                hint="Check QEMU output and ensure KVM is available.",
                context={
                    "returncode": str(result.returncode),
                    "stderr": stderr_of(result),
                    "command": " ".join(cmd),
                },
            )

        metadata = {
            "artifact_path": str(artifact_path),
            "memory": memory,
            "cpus": cpus,
            "ssh_port": ssh_port,
            "tdx": str(enable_tdx).lower(),
            "is_uki": str(is_uki).lower(),
            **({"forward": format_forwards(forwards)} if forwards else {}),
            **state,
            **params,
        }

        return DeployResult(
            target="qemu",
            deployment_id=deployment_id,
            endpoint=f"ssh://localhost:{ssh_port}",
            metadata=metadata,
        )


def parse_forwards(text: str) -> tuple[tuple[int, int], ...]:
    """``HOST:GUEST[,HOST:GUEST...]`` as ``(host, guest)`` port pairs."""
    pairs: list[tuple[int, int]] = []
    for item in filter(None, (part.strip() for part in text.split(","))):
        host, sep, guest = item.partition(":")
        try:
            pair = (int(host), int(guest))
        except ValueError:
            pair = (0, 0)
        if not sep or not all(0 < port < 65536 for port in pair):
            raise DeploymentError(
                f"Invalid QEMU port forward {item!r}.",
                hint="Write each forward as HOST:GUEST ports, e.g. forward=8443:443,9000:9000.",
                context={"forward": text},
            )
        pairs.append(pair)
    return tuple(pairs)


def format_forwards(pairs: tuple[tuple[int, int], ...]) -> str:
    """*pairs* as ``HOST:GUEST,...``, the form :func:`parse_forwards` reads."""
    return ",".join(f"{host}:{guest}" for host, guest in pairs)


def _run_dir(artifact_path: Path, variant: str) -> Path:
    """``OUT/<variant>``: the artifact's nearest ancestor named *variant*, else its directory."""
    return next((p for p in artifact_path.parents if p.name == variant), artifact_path.parent)


def _disk_format_for_path(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".qcow2"):
        return "qcow2"
    if lower.endswith(".vhd"):
        return "vpc"
    return "raw"
