"""Secret delivery module.

Builds a Go binary that handles secret delivery at runtime, and registers
its invocation into the runtime-init script via ``image.add_init_script()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tdx.image import Image

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
SECRET_DELIVERY_DEFAULT_BRANCH = "main"


@dataclass(slots=True)
class SecretDelivery:
    """Boot-time secret delivery phase.

    Builds a Go binary from source and registers its invocation in the
    runtime-init script. The binary handles secret validation and
    materialization at boot time.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, packages, and init script to *image*."""
        image.build_install(*SECRET_DELIVERY_BUILD_PACKAGES)
        image.install("python3")

        build_cmd = (
            f"SECRET_DEL_SRC=$BUILDDIR/secret-delivery-src && "
            f"if [ ! -d \"$SECRET_DEL_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$SECRET_DEL_SRC\"; "
            f"fi && "
            f"cd \"$SECRET_DEL_SRC/init\" && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/secret-delivery ./cmd/main.go && "
            f"install -m 0755 ./build/secret-delivery "
            f"\"$DESTDIR/usr/bin/secret-delivery\""
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

        image.add_init_script(
            f"/usr/bin/secret-delivery"
            f" --method {self.method}"
            f" --port {self.port}\n",
            priority=30,
        )
