"""Disk encryption module.

Builds a Go binary (placeholder: tdx-init repo) that handles LUKS
format/open at runtime, and registers the binary invocation into Init's
runtime-init script.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.modules.init import Init

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
DISK_ENCRYPTION_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class DiskEncryption:
    """LUKS2 disk encryption at boot time.

    Builds a Go binary from source (currently the tdx-init repo as a
    placeholder) and registers its invocation in the runtime-init script.
    """

    device: str = "/dev/vda3"
    mapper_name: str = "cryptroot"
    key_path: str = "/persistent/key"
    mount_point: str = "/persistent"
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH

    def apply(self, init: Init) -> None:
        """Register build artifacts and runtime invocation with *init*."""
        self._add_build(init)
        self._add_bash(init)
        init.add_packages("cryptsetup")

    def _add_build(self, init: Init) -> None:
        init.add_build_packages(*DISK_ENCRYPTION_BUILD_PACKAGES)
        build_cmd = (
            f"DISK_ENC_SRC=$BUILDDIR/disk-encryption-src && "
            f"if [ ! -d \"$DISK_ENC_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$DISK_ENC_SRC\"; "
            f"fi && "
            f"cd \"$DISK_ENC_SRC/init\" && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/disk-encryption ./cmd/main.go && "
            f"install -m 0755 ./build/disk-encryption "
            f"\"$DESTDIR/usr/bin/disk-encryption\""
        )
        init.add_build_hook("sh", "-c", build_cmd, shell=True)

    def _add_bash(self, init: Init) -> None:
        init.add_bash(
            f"/usr/bin/disk-encryption --device {self.device}"
            f" --mapper {self.mapper_name}"
            f" --key {self.key_path}"
            f" --mount {self.mount_point}\n",
            comment="disk encryption",
        )
