"""Disk encryption module.

Builds a Go binary (placeholder: tdx-init repo) that handles LUKS
format/open at runtime, and registers the binary invocation into the
runtime-init script via ``image.add_init_script()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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

        cache_name = f"disk-encryption-{self.source_branch}"
        c = image.caches
        restore = c.get(cache_name).copy_file("disk-encryption", "$DESTDIR/usr/bin/disk-encryption")
        store = c.create(cache_name).add_file("disk-encryption", "./build/disk-encryption")
        build_cmd = (
            f"if {c.has(cache_name)}; then "
            f'echo "Using cached disk-encryption"; '
            f"{restore}; "
            f"else "
            f"DISK_ENC_SRC=$BUILDDIR/disk-encryption-src && "
            f'if [ ! -d "$DISK_ENC_SRC" ]; then '
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "$DISK_ENC_SRC"; '
            f"fi && "
            f'cd "$DISK_ENC_SRC/init" && '
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/disk-encryption ./cmd/main.go && "
            f"{store} && "
            f'install -m 0755 ./build/disk-encryption "$DESTDIR/usr/bin/disk-encryption"; '
            f"fi"
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

        image.add_init_script(
            f"/usr/bin/disk-encryption"
            f" --device {self.device}"
            f" --mapper {self.mapper_name}"
            f" --key {self.key_path}"
            f" --mount {self.mount_point}\n",
            priority=20,
        )
