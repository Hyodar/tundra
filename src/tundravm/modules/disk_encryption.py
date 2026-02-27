"""Disk encryption module.

Configures ``tdx-init`` disk settings and installs a compatibility shim
command for runtime-init ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tundravm.modules._tdx_init import (
    ensure_tdx_init_build,
    ensure_tdx_init_config,
    write_tdx_init_config,
)

if TYPE_CHECKING:
    from tundravm.image import Image

DISK_ENCRYPTION_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
DISK_ENCRYPTION_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class DiskEncryption:
    """LUKS2 disk encryption at boot time.

    Configures disk strategy in ``/etc/tdx-init/config.yaml`` and registers
    a compatibility command in runtime-init ordering.
    """

    device: str = "/dev/vda3"
    mapper_name: str = "cryptroot"
    key_path: str = "/persistent/key"
    mount_point: str = "/persistent"
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Ensure tdx-init is built and disk settings are configured."""
        ensure_tdx_init_build(
            image,
            source_repo=self.source_repo,
            source_ref=self.source_branch,
        )
        image.install("cryptsetup")

        config = ensure_tdx_init_config(image)
        disks = config.setdefault("disks", {})
        disk_persistent = disks.setdefault("disk_persistent", {})
        disk_persistent["strategy"] = "pathglob" if self.device else "largest"
        if self.device:
            disk_persistent["strategy_config"] = {"path_glob": self.device}
        else:
            disk_persistent["strategy_config"] = {}
        disk_persistent["format"] = "on_fail"
        disk_persistent["encryption_key"] = "key_persistent"
        disk_persistent["mount_at"] = self.mount_point
        write_tdx_init_config(image, config)

        image.file(
            "/usr/bin/disk-encryption",
            content=_compat_disk_encryption_script(),
            mode="0755",
        )

        image.add_init_script(
            f"/usr/bin/disk-encryption"
            f" --device {self.device}"
            f" --mapper {self.mapper_name}"
            f" --key {self.key_path}"
            f" --mount {self.mount_point}\n",
            priority=20,
        )


def _compat_disk_encryption_script() -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "# Compatibility shim: disk setup is handled by tdx-init.\n"
        "exit 0\n"
    )
