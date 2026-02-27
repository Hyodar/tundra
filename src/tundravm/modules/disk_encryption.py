"""Disk encryption module."""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING

from tundravm.build_cache import Build, Cache

if TYPE_CHECKING:
    from tundravm.image import Image

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
DISK_ENCRYPTION_DEFAULT_BRANCH = "main"
DISK_ENCRYPTION_CONFIG_PATH = "/etc/tdx/disk-setup.yaml"


@dataclass(slots=True)
class DiskEncryption:
    """LUKS2 disk encryption at boot time."""

    device: str = "/dev/vda3"
    mapper_name: str = "cryptroot"
    key_path: str = "/persistent/key"
    mount_point: str = "/persistent"
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, config file, and init script to *image*."""
        image.build_install(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.install("cryptsetup")

        clone_dir = Build.build_path("disk-encryption")
        chroot_dir = Build.chroot_path("disk-encryption")
        cache = Cache.declare(
            f"disk-encryption-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("disk-encryption/build/disk-setup"),
                    dest=Build.dest_path("usr/bin/disk-setup"),
                    name="disk-setup",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/disk-setup ./cmd/disk-setup"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        image.file(DISK_ENCRYPTION_CONFIG_PATH, content=self._render_config())

        image.add_init_script(self._render_init_script(), priority=20)

    def _render_config(self) -> str:
        strategy_block = dedent("""\
            strategy: "largest"
            format: "on_fail"
            encryption_key: "key_persistent"
            mount_at: "{mount_point}"
            dirs: ["ssh", "data", "logs"]
        """).format(mount_point=self.mount_point)
        if self.device:
            strategy_block = dedent("""\
                strategy: "pathglob"
                strategy_config:
                  pattern: "{device}"
                format: "on_fail"
                encryption_key: "key_persistent"
                mount_at: "{mount_point}"
                dirs: ["ssh", "data", "logs"]
            """).format(device=self.device, mount_point=self.mount_point)

        return dedent(
            """\
            disks:
              disk_persistent:
            """
        ) + "\n".join(f"    {line}" for line in strategy_block.strip().splitlines()) + "\n"

    def _render_init_script(self) -> str:
        return dedent(f"""\
            if [ -z "${{DISK_ENCRYPTION_KEY:-}}" ] && [ -f "{self.key_path}" ]; then
                export DISK_ENCRYPTION_KEY="$(tr -d '\\n' < "{self.key_path}")"
            fi
            /usr/bin/disk-setup setup {DISK_ENCRYPTION_CONFIG_PATH}
        """)
