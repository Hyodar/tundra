"""Key generation module.

Builds a Go binary (placeholder: tdx-init repo) that handles key generation
at runtime, and registers the binary invocation into the runtime-init script
via ``image.add_init_script()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tdx.build_cache import Build, Cache

if TYPE_CHECKING:
    from tdx.image import Image

KEY_GENERATION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

KEY_GENERATION_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
KEY_GENERATION_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class KeyGeneration:
    """Generate a cryptographic key at boot time.

    Builds a Go binary from source (currently the tdx-init repo as a
    placeholder) and registers its invocation in the runtime-init script.
    """

    strategy: Literal["tpm", "random"] = "tpm"
    output: str = "/persistent/key"
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, packages, and init script to *image*."""
        image.build_install(*KEY_GENERATION_BUILD_PACKAGES)

        clone_dir = Build.build_path("key-generation")
        chroot_dir = Build.chroot_path("key-generation")
        cache = Cache.declare(
            f"key-generation-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("key-generation/init/build/key-generation"),
                    dest=Build.dest_path("usr/bin/key-generation"),
                    name="key-generation",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir}/init && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/key-generation ./cmd/main.go"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd), shell=True)

        image.add_init_script(
            f"/usr/bin/key-generation --strategy {self.strategy} --output {self.output}\n",
            priority=10,
        )
