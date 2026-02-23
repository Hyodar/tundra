"""QEMU deployment adapter.

Launches a QEMU VM with the baked artifact, supporting:
- OVMF firmware for UEFI boot
- UKI (Unified Kernel Image) via -kernel flag
- TDX confidential computing support
- Port forwarding for SSH/HTTP access
- virtio-scsi for persistent storage
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from tdx.errors import DeploymentError
from tdx.models import DeployRequest, DeployResult

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

    def deploy(self, request: DeployRequest) -> DeployResult:
        deployment_id = f"qemu-{request.profile}-{uuid.uuid4().hex[:8]}"
        params = dict(request.parameters)

        memory = params.pop("memory", "2G")
        cpus = params.pop("cpus", "2")
        ssh_port = params.pop("ssh_port", "2222")
        enable_tdx = params.pop("tdx", "false").lower() == "true"
        daemonize = params.pop("daemonize", "true").lower() == "true"
        artifact_path = request.artifact_path

        # Check if QEMU is available
        if shutil.which(self.qemu_binary) is None:
            raise DeploymentError(
                f"QEMU binary not found: {self.qemu_binary}",
                hint="Install QEMU and ensure it is in PATH.",
                context={"binary": self.qemu_binary},
            )

        # Determine if this is a UKI (.efi) or disk image
        is_uki = str(artifact_path).endswith(".efi")

        # Build QEMU command
        cmd: list[str] = [
            self.qemu_binary,
            "-machine", "q35,accel=kvm",
            "-cpu", "host",
            "-m", memory,
            "-smp", cpus,
            "-nographic",
            "-serial", "mon:stdio",
            "-no-reboot",
        ]

        # UEFI firmware
        if is_uki:
            ovmf_code = _find_firmware(OVMF_CODE_PATHS, "OVMF_CODE")
            ovmf_vars = _find_firmware(OVMF_VARS_PATHS, "OVMF_VARS")
            cmd.extend([
                "-drive", f"file={ovmf_code},if=pflash,format=raw,readonly=on",
                "-drive", f"file={ovmf_vars},if=pflash,format=raw",
                "-kernel", str(artifact_path),
            ])
        else:
            # Disk image boot
            cmd.extend([
                "-drive", f"file={artifact_path},format=raw,if=virtio",
            ])

        # Networking with port forwarding
        cmd.extend([
            "-netdev", f"user,id=net0,hostfwd=tcp::{ssh_port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
        ])

        # TDX support
        if enable_tdx:
            cmd.extend([
                "-object", "tdx-guest,id=tdx0",
                "-machine", "confidential-guest-support=tdx0",
            ])

        # Daemonize
        if daemonize:
            pidfile = artifact_path.parent / f"{deployment_id}.pid"
            cmd.extend(["-daemonize", "-pidfile", str(pidfile)])

        # Extra args
        cmd.extend(self.extra_args)

        # Launch
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise DeploymentError(
                "QEMU launch failed.",
                hint="Check QEMU output and ensure KVM is available.",
                context={
                    "returncode": str(result.returncode),
                    "stderr": result.stderr[:2000] if result.stderr else "",
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
            **params,
        }

        return DeployResult(
            target="qemu",
            deployment_id=deployment_id,
            endpoint=f"ssh://localhost:{ssh_port}",
            metadata=metadata,
        )
