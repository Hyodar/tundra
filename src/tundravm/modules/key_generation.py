"""Key generation module."""

from __future__ import annotations

import hashlib
import shlex
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
KEY_GENERATION_DEFAULT_BRANCH = "master"
KEY_GENERATION_DEFAULT_CONFIG_PATH = "/etc/tdx/key-gen.yaml"
KEY_GENERATION_TPM_NV_INDEX = "0x1500016"
KEY_GENERATION_TPM_TCTI = "device:/dev/tpmrm0"


@dataclass(slots=True)
class KeyGeneration:
    """Generate a cryptographic key at boot time.

    The underlying ``tundra-tools`` binary supports the ``random`` and ``pipe``
    strategies, with TPM persistence controlled separately. ``strategy='tpm'``
    is kept as a compatibility alias for ``random`` with TPM persistence.
    """

    strategy: Literal["tpm", "random", "pipe"] = "tpm"
    output: str = "/persistent/key"
    key_name: str = "key_persistent"
    size: int = 64
    pipe_path: str | None = None
    persist_in_tpm: bool | None = None
    config_path: str = KEY_GENERATION_DEFAULT_CONFIG_PATH
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, config file, and init script to *image*."""
        image.build_install(*KEY_GENERATION_BUILD_PACKAGES)
        image.install("tpm2-tools")

        clone_dir = Build.build_path("key-generation")
        chroot_dir = Build.chroot_path("key-generation")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("key-generation/build/key-gen"),
                    dest=Build.dest_path("usr/bin/key-gen"),
                    name="key-gen",
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
            "-o ./build/key-gen ./cmd/key-gen"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        image.file(self.config_path, content=self._render_config())
        image.add_init_script(self._render_init_script(), priority=10)

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"key-generation-{repo_hash}-{self.source_branch}"

    def _tool_strategy(self) -> Literal["random", "pipe"]:
        if self.strategy == "pipe":
            return "pipe"
        return "random"

    def _tpm_enabled(self) -> bool:
        if self.persist_in_tpm is not None:
            return self.persist_in_tpm
        return self.strategy == "tpm"

    def _render_config(self) -> str:
        lines = [
            "keys:",
            f"  {self.key_name}:",
            f'    strategy: "{self._tool_strategy()}"',
            f'    tpm: {"true" if self._tpm_enabled() else "false"}',
        ]
        if self._tool_strategy() == "random":
            lines.append(f"    size: {self.size}")
        elif self.pipe_path:
            lines.append(f'    pipe_path: "{self.pipe_path}"')
        return "\n".join(lines) + "\n"

    def _render_init_script(self) -> str:
        output_path = self.output
        if self._tpm_enabled():
            return dedent(f"""\
                /usr/bin/key-gen setup {shlex.quote(self.config_path)}
                install -d -m 0700 "$(dirname "{output_path}")"
                export DISK_ENCRYPTION_KEY="$(
                    tpm2_nvread -C o -T {KEY_GENERATION_TPM_TCTI} \
                        {KEY_GENERATION_TPM_NV_INDEX} | tr -d '\\n'
                )"
                printf '%s\\n' "$DISK_ENCRYPTION_KEY" > "{output_path}"
                chmod 0600 "{output_path}"
            """)
        return dedent(f"""\
            /usr/bin/key-gen setup {shlex.quote(self.config_path)}
            if [ -f "{output_path}" ]; then
                export DISK_ENCRYPTION_KEY="$(tr -d '\\n' < "{output_path}")"
            else
                echo "key-gen did not persist a key;" >&2
                echo "set persist_in_tpm=True or provide {output_path}" >&2
                exit 1
            fi
        """)
