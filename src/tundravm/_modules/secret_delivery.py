"""Secret delivery module."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tundravm._modules.base import TUNDRA_TOOLS, Module
from tundravm._modules.disk_encryption import DiskEncryption, DiskSpec
from tundravm._source import GitSource, GoBuild, Install, SourceBuild
from tundravm.check import Diagnostic
from tundravm.errors import ValidationError
from tundravm.models import SecretSpec

if TYPE_CHECKING:
    from tundravm.declarative._lowered import Lowered

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_CONFIG_PATH = "/etc/tdx/secrets.yaml"
SECRET_DELIVERY_DEFAULT_MANIFEST_PATH = "/etc/tdx/secrets.json"
SECRET_DELIVERY_INIT_PRIORITY = 30


@dataclass(slots=True)
class SecretDelivery(Module):
    """Boot-time secret delivery phase.

    *secrets* are the expected secrets, each with at least one delivery target.
    *store_at* names the ``DiskEncryption`` disk (a name or the ``DiskSpec``) that
    received secrets are stored on; ``check()`` warns when no disk applied to the
    profile has that name (``secret-store-undefined``).
    """

    secrets: tuple[SecretSpec, ...] = ()
    method: Literal["http_post"] = "http_post"
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_dir: str = "/root/.ssh"
    key_path: str | None = "/etc/root_key"
    store_at: str | DiskSpec | None = "disk_persistent"
    config_path: str = SECRET_DELIVERY_DEFAULT_CONFIG_PATH
    manifest_path: str = SECRET_DELIVERY_DEFAULT_MANIFEST_PATH
    source: GitSource = TUNDRA_TOOLS

    def __post_init__(self) -> None:
        self.secrets = tuple(self.secrets)
        for spec in self.secrets:
            if not spec.name:
                raise ValidationError(
                    "SecretDelivery requires non-empty secret names.",
                    hint="Give each Secret() a name, e.g. Secret('api-token', ...).",
                )
            if not spec.targets:
                raise ValidationError(
                    f"secret {spec.name!r} requires at least one delivery target.",
                    hint="Deliver it with SecretFile(PATH) or SecretEnv(NAME) in targets=.",
                )

    @property
    def store_disk(self) -> str | None:
        """Name of the disk secrets are stored on (``store_at`` as a name)."""
        if isinstance(self.store_at, DiskSpec):
            return self.store_at.name
        return self.store_at or None

    def check(self, image: Lowered, profile: str) -> Iterator[Diagnostic]:
        store = self.store_disk
        if store is None:
            return
        disks = {
            disk.name
            for module in image.applied_modules(profile, inherited=True)
            if isinstance(module, DiskEncryption)
            for disk in module.disks
        }
        if store in disks:
            return
        declared = ", ".join(sorted(disks)) or "none"
        yield Diagnostic(
            level="warning",
            code="secret-store-undefined",
            message=(
                f"secrets are stored on disk {store!r}, which no Disk in this variant declares"
            ),
            hint=(
                f"Declared disks: {declared}. Declare Disk({store!r}, ...) in this variant, "
                "pass Secrets(store=) one of the declared disks, or store=None."
            ),
            profile=profile,
            subject=store,
        )

    def init_script(self) -> str:
        """The runtime-init step: ``secret-delivery setup`` on the config (priority 30)."""
        return f"/usr/bin/secret-delivery setup {shlex.quote(self.config_path)}\n"

    def source_spec(self) -> SourceBuild:
        """The ``secret-delivery`` source build from ``source``.

        ``mark_unpinned=False`` keeps the unpinned hook byte-identical to the
        hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="secret-delivery",
            source=self.source,
            build=GoBuild(package="./cmd/secret-delivery", output="secret-delivery"),
            install=(Install.artifact("/usr/bin/secret-delivery"),),
            mark_unpinned=False,
        )

    def render_manifest(self) -> str:
        """The JSON manifest of the expected secrets and their delivery targets."""
        return _render_manifest_json(
            self.secrets, method=self.method, host=self.host, port=self.port
        )

    def render_config(self) -> str:
        """The ``secret-delivery`` YAML config: where to listen, where to store."""
        if self.method != "http_post":
            raise ValidationError(
                "Only http_post secret delivery is supported.",
                hint="Secrets() delivers over HTTP POST only.",
            )

        lines = [
            "ssh:",
            '  strategy: "webserver"',
            "  strategy_config:",
            f'    server_url: "{self.host}:{self.port}"',
            f'  dir: "{self.ssh_dir}"',
        ]
        if self.key_path:
            lines.append(f'  key_path: "{self.key_path}"')
        if self.store_disk:
            lines.append(f'  store_at: "{self.store_disk}"')
        return "\n".join(lines) + "\n"


def _render_manifest_json(
    secrets: tuple[SecretSpec, ...],
    *,
    method: str,
    host: str,
    port: int,
) -> str:
    entries = []
    for spec in sorted(secrets, key=lambda s: s.name):
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
        for target_spec in spec.targets:
            target: dict[str, str] = {
                "kind": target_spec.kind,
                "location": target_spec.location,
            }
            if target_spec.kind == "file":
                target["mode"] = target_spec.mode
                if target_spec.owner is not None:
                    target["owner"] = target_spec.owner
            if target_spec.kind == "env":
                target["scope"] = target_spec.scope
            targets.append(target)
        entry["targets"] = targets
        entries.append(entry)

    payload = {
        "method": method,
        "host": host,
        "port": port,
        "secrets": entries,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
