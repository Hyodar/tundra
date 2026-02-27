"""Key generation module.

Configures ``tdx-init`` key settings and installs a compatibility shim command
for runtime-init ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tundravm.modules._tdx_init import (
    ensure_tdx_init_build,
    ensure_tdx_init_config,
    write_tdx_init_config,
)

if TYPE_CHECKING:
    from tundravm.image import Image

KEY_GENERATION_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
KEY_GENERATION_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class KeyGeneration:
    """Generate a cryptographic key at boot time.

    Configures key strategy in ``/etc/tdx-init/config.yaml`` and registers
    a compatibility command in runtime-init ordering.
    """

    strategy: Literal["tpm", "random"] = "tpm"
    output: str = "/persistent/key"
    source_repo: str = KEY_GENERATION_DEFAULT_REPO
    source_branch: str = KEY_GENERATION_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Ensure tdx-init is built and key settings are configured."""
        ensure_tdx_init_build(
            image,
            source_repo=self.source_repo,
            source_ref=self.source_branch,
        )

        config = ensure_tdx_init_config(image)
        keys = config.setdefault("keys", {})
        key_persistent = keys.setdefault("key_persistent", {})
        key_persistent["strategy"] = "random"
        key_persistent["tpm"] = self.strategy == "tpm"
        write_tdx_init_config(image, config)

        image.file(
            "/usr/bin/key-generation",
            content=_compat_key_generation_script(),
            mode="0755",
        )

        image.add_init_script(
            f"/usr/bin/key-generation --strategy {self.strategy} --output {self.output}\n",
            priority=10,
        )


def _compat_key_generation_script() -> str:
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "# Compatibility shim: key setup is handled by tdx-init.\n"
        "exit 0\n"
    )
