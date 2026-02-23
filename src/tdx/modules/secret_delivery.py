"""Secret delivery module.

Builds a Go binary (placeholder: tdx-init repo) that handles secret
delivery at runtime, and registers the binary invocation into Init's
runtime-init script.

The Python-side validation/materialization logic stays in Init — this
module only contributes the shell-level boot phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tdx.modules.init import Init

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

    Builds a Go binary from source (currently the tdx-init repo as a
    placeholder) and registers its invocation in the runtime-init script.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH

    def apply(self, init: Init) -> None:
        """Register build artifacts and runtime invocation with *init*."""
        self._add_build(init)
        self._add_bash(init)
        init.add_packages("python3")

    def _add_build(self, init: Init) -> None:
        init.add_build_packages(*SECRET_DELIVERY_BUILD_PACKAGES)
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
        init.add_build_hook("sh", "-c", build_cmd, shell=True)

    def _add_bash(self, init: Init) -> None:
        init.add_bash(
            f"/usr/bin/secret-delivery --method {self.method}"
            f" --port {self.port}\n",
            comment="secret delivery",
        )
