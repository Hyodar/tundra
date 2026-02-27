"""Key generation module."""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache

if TYPE_CHECKING:
    from tundravm.image import Image

KEY_GENERATION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

KEY_GENERATION_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
KEY_GENERATION_DEFAULT_BRANCH = "main"
KEY_GENERATION_CONFIG_PATH = "/etc/tdx/key-gen.yaml"


@dataclass(slots=True)
class KeyGeneration:
    """Generate a cryptographic key at boot time."""

    strategy: Literal["tpm", "random"] = "tpm"
    output: str = "/persistent/key"
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, config file, and init script to *image*."""
        image.build_install(*KEY_GENERATION_BUILD_PACKAGES)
        image.install("tpm2-tools")

        clone_dir = Build.build_path("key-generation")
        chroot_dir = Build.chroot_path("key-generation")
        cache = Cache.declare(
            f"key-generation-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("key-generation/build/key-gen"),
                    dest=Build.dest_path("usr/bin/key-gen"),
                    name="key-gen",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/key-gen ./cmd/key-gen"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        image.file(KEY_GENERATION_CONFIG_PATH, content=self._render_config())

        image.add_init_script(self._render_init_script(), priority=10)

    def _render_config(self) -> str:
        return dedent(f"""\
            keys:
              key_persistent:
                strategy: "random"
                tpm: {"true" if self.strategy == "tpm" else "false"}
                size: 64
        """)

    def _render_init_script(self) -> str:
        output_path = self.output
        if self.strategy == "tpm":
            return dedent(f"""\
                /usr/bin/key-gen setup {KEY_GENERATION_CONFIG_PATH}
                install -d -m 0700 "$(dirname "{output_path}")"
                export DISK_ENCRYPTION_KEY="$(
                    tpm2_nvread -C o -T device:/dev/tpmrm0 0x1500016 | tr -d '\\n'
                )"
                printf '%s\\n' "$DISK_ENCRYPTION_KEY" > "{output_path}"
                chmod 0600 "{output_path}"
            """)
        return dedent(f"""\
            /usr/bin/key-gen setup {KEY_GENERATION_CONFIG_PATH}
            if [ -f "{output_path}" ]; then
                export DISK_ENCRYPTION_KEY="$(tr -d '\\n' < "{output_path}")"
            fi
        """)
