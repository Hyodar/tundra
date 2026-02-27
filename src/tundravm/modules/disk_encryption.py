"""Disk encryption module."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache

if TYPE_CHECKING:
    from tundravm.image import Image

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
DISK_ENCRYPTION_DEFAULT_BRANCH = "master"
DISK_ENCRYPTION_DEFAULT_CONFIG_PATH = "/etc/tdx/disk-setup.yaml"
DEFAULT_DISK_DIRS = ("ssh", "data", "logs")


@dataclass(slots=True)
class DiskEncryption:
    """LUKS2 disk encryption at boot time."""

    device: str = "/dev/vda3"
    mapper_name: str | None = None
    key_path: str = "/persistent/key"
    mount_point: str = "/persistent"
    disk_name: str = "disk_persistent"
    key_name: str = "key_persistent"
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    dirs: tuple[str, ...] = DEFAULT_DISK_DIRS
    config_path: str = DISK_ENCRYPTION_DEFAULT_CONFIG_PATH
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, config file, and init script to *image*."""
        image.build_install(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.install("cryptsetup")

        clone_dir = Build.build_path("disk-encryption")
        chroot_dir = Build.chroot_path("disk-encryption")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("disk-encryption/build/disk-setup"),
                    dest=Build.dest_path("usr/bin/disk-setup"),
                    name="disk-setup",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {shlex.quote(self.source_branch)} "
            f'{shlex.quote(self.source_repo)} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            "mkdir -p ./build && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/disk-setup ./cmd/disk-setup"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        image.file(self.config_path, content=self._render_config())
        image.add_init_script(self._render_init_script(), priority=20)

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"disk-encryption-{repo_hash}-{self.source_branch}"

    def _render_config(self) -> str:
        disk_key = self.disk_name
        lines = [
            "disks:",
            f"  {disk_key}:",
        ]
        if self.device:
            lines.extend(
                (
                    '    strategy: "pathglob"',
                    "    strategy_config:",
                    f'      pattern: "{self.device}"',
                )
            )
        else:
            lines.append('    strategy: "largest"')
        lines.extend(
            (
                f'    format: "{self.format_policy}"',
                f'    encryption_key: "{self.key_name}"',
                f'    mount_at: "{self.mount_point}"',
                f"    dirs: {json.dumps(list(self.dirs))}",
            )
        )
        return "\n".join(lines) + "\n"

    def _generated_mapper_name(self) -> str:
        return f"crypt_disk_{self.disk_name}"

    def _render_init_script(self) -> str:
        lines = [
            f'if [ -z "${{DISK_ENCRYPTION_KEY:-}}" ] && [ -f "{self.key_path}" ]; then',
            f'    export DISK_ENCRYPTION_KEY="$(tr -d \'\\n\' < "{self.key_path}")"',
            "fi",
            f"/usr/bin/disk-setup setup {shlex.quote(self.config_path)}",
        ]
        if self.mapper_name and self.mapper_name != self._generated_mapper_name():
            generated_mapper = self._generated_mapper_name()
            generated_mapper_path = shlex.quote(f"/dev/mapper/{generated_mapper}")
            generated_mapper_name = shlex.quote(generated_mapper)
            requested_mapper_name = shlex.quote(self.mapper_name)
            lines.extend(
                (
                    f"if [ -e {generated_mapper_path} ]; then",
                    f"    cryptsetup rename {generated_mapper_name} {requested_mapper_name}",
                    "fi",
                )
            )
        return "\n".join(lines) + "\n"
