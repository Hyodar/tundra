"""Secret delivery module.

Builds a Go binary that handles secret delivery at runtime, and registers
its invocation into the runtime-init script via ``image.add_init_script()``.

At build time, serializes the declared secrets (schemas + targets) into
``/etc/tdx/secrets.json`` so the Go binary knows what to expect and where
to write values at boot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache
from tundravm.errors import ValidationError
from tundravm.models import SecretSchema, SecretSpec, SecretTarget

if TYPE_CHECKING:
    from tundravm.image import Image

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

    Declare secrets via ``.secret()``, then call ``.apply(img)`` to build
    the Go binary, write ``/etc/tdx/secrets.json``, and register the
    init script.  The Go binary reads the config at boot to validate
    incoming secrets and write them to the declared targets.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH
    _secrets: list[SecretSpec] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def secret(
        self,
        name: str,
        *,
        required: bool = True,
        schema: SecretSchema | None = None,
        targets: tuple[SecretTarget, ...] = (),
    ) -> SecretSpec:
        """Declare an expected secret with validation schema and targets."""
        if not name:
            raise ValidationError(
                "secret() requires a non-empty secret name.",
            )
        if not targets:
            raise ValidationError(
                "secret() requires at least one delivery target.",
            )
        entry = SecretSpec(
            name=name,
            required=required,
            schema=schema,
            targets=targets,
        )
        self._secrets.append(entry)
        return entry

    def apply(self, image: Image) -> None:
        """Add build hook, packages, config file, and init script."""
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
        clone_dir = Build.build_path("secret-delivery")
        chroot_dir = Build.chroot_path("secret-delivery")
        cache = Cache.declare(
            f"secret-delivery-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("secret-delivery/init/build/secret-delivery"),
                    dest=Build.dest_path("usr/bin/secret-delivery"),
                    name="secret-delivery",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir}/init && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/secret-delivery ./cmd/main.go"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

    def _add_config(self, image: Image) -> None:
        """Write the JSON config file and register secrets on the image."""
        # Push secrets into the profile state for lockfile hashing
        for profile in image._iter_active_profiles():
            for spec in self._secrets:
                profile.secrets.append(spec)

        secrets = {s.name: s for s in self._secrets}
        config = _render_config(
            secrets,
            method=self.method,
            port=self.port,
        )
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
