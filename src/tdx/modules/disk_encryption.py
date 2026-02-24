"""Disk encryption module.

Builds a Go binary (placeholder: tdx-init repo) that handles LUKS
format/open at runtime, and registers the binary invocation into the
runtime-init script via ``image.add_init_script()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tdx.build_cache import Build, Cache

if TYPE_CHECKING:
    from tdx.image import Image

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

    def apply(self, image: Image) -> None:
        """Add build hook, packages, and init script to *image*."""
        image.build_install(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.install("cryptsetup")

        clone_dir = Build.build_path("disk-encryption")
        chroot_dir = Build.chroot_path("disk-encryption")
        cache = Cache.declare(
            f"disk-encryption-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("disk-encryption/init/build/disk-encryption"),
                    dest=Build.dest_path("usr/bin/disk-encryption"),
                    name="disk-encryption",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir}/init && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/disk-encryption ./cmd/main.go"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

        image.add_init_script(
            f"/usr/bin/disk-encryption"
            f" --device {self.device}"
            f" --mapper {self.mapper_name}"
            f" --key {self.key_path}"
            f" --mount {self.mount_point}\n",
            priority=20,
        )
