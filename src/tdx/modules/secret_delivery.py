"""Secret delivery module.

Builds a Go binary that handles secret delivery at runtime, and registers
its invocation into the runtime-init script via ``image.add_init_script()``.

At build time, serializes the declared secrets (schemas + targets) into
``/etc/tdx/secrets.json`` so the Go binary knows what to expect and where
to write values at boot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tdx.models import SecretSpec

if TYPE_CHECKING:
    from tdx.image import Image

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
SECRET_DELIVERY_DEFAULT_BRANCH = "main"
SECRET_DELIVERY_CONFIG_PATH = "/etc/tdx/secrets.json"


@dataclass(slots=True)
class SecretDelivery:
    """Boot-time secret delivery phase.

    Builds a Go binary from source and registers its invocation in the
    runtime-init script.  Collects ``img.secret()`` declarations and writes
    ``/etc/tdx/secrets.json`` — the Go binary reads this at boot to know
    which secrets to expect, how to validate them, and where to write them.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH

    def apply(self, image: Image) -> None:
        """Add build hook, packages, config file, and init script to *image*."""
        image.build_install(*SECRET_DELIVERY_BUILD_PACKAGES)
        image.install("python3")

        self._add_build_hook(image)
        self._add_config(image)

        image.add_init_script(
            f"/usr/bin/secret-delivery"
            f" --config {SECRET_DELIVERY_CONFIG_PATH}"
            f" --method {self.method}"
            f" --port {self.port}\n",
            priority=30,
        )

    def _add_build_hook(self, image: Image) -> None:
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

    def _add_config(self, image: Image) -> None:
        """Collect declared secrets and write the JSON config file."""
        secrets: dict[str, SecretSpec] = {}
        for profile in image._iter_active_profiles():
            for spec in profile.secrets:
                secrets[spec.name] = spec

        config = _render_config(secrets, method=self.method, port=self.port)
        image.file(SECRET_DELIVERY_CONFIG_PATH, content=config)


def _render_config(
    secrets: dict[str, SecretSpec],
    *,
    method: str,
    port: int,
) -> str:
    """Serialize secret declarations to JSON for the Go binary."""
    entries = []
    for spec in sorted(secrets.values(), key=lambda s: s.name):
        entry: dict[str, object] = {
            "name": spec.name,
            "required": spec.required,
        }
        if spec.schema is not None:
            schema: dict[str, object] = {"kind": spec.schema.kind}
            if spec.schema.min_length is not None:
                schema["min_length"] = spec.schema.min_length
            if spec.schema.max_length is not None:
                schema["max_length"] = spec.schema.max_length
            if spec.schema.pattern is not None:
                schema["pattern"] = spec.schema.pattern
            if spec.schema.enum:
                schema["enum"] = list(spec.schema.enum)
            entry["schema"] = schema

        targets = []
        for t in spec.targets:
            target: dict[str, str] = {"kind": t.kind, "location": t.location}
            if t.kind == "file":
                target["mode"] = t.mode
                if t.owner is not None:
                    target["owner"] = t.owner
            if t.kind == "env":
                target["scope"] = t.scope
            targets.append(target)
        entry["targets"] = targets
        entries.append(entry)

    payload = {
        "method": method,
        "port": port,
        "secrets": entries,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
