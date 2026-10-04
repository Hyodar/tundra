"""Secret delivery module."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from tundravm.check import Diagnostic
from tundravm.errors import ValidationError
from tundravm.models import SecretSchema, SecretSpec, SecretTarget
from tundravm.modules.base import Module
from tundravm.modules.disk_encryption import DiskEncryption, DiskSpec
from tundravm.source import GitSource, GoBuild, SourceBuild

if TYPE_CHECKING:
    from tundravm.image import Image

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
SECRET_DELIVERY_DEFAULT_BRANCH = "master"
SECRET_DELIVERY_DEFAULT_CONFIG_PATH = "/etc/tdx/secrets.yaml"
SECRET_DELIVERY_DEFAULT_MANIFEST_PATH = "/etc/tdx/secrets.json"


@dataclass(slots=True)
class SecretDelivery(Module):
    """Boot-time secret delivery phase.

    *store_at* names the ``DiskEncryption`` disk (a name or the ``DiskSpec``) that
    received secrets are stored on; ``check()`` warns when no disk applied to the
    profile has that name (``secret-store-undefined``).
    """

    init_priority: ClassVar[int | None] = 30

    method: Literal["http_post"] = "http_post"
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_dir: str = "/root/.ssh"
    key_path: str | None = "/etc/root_key"
    store_at: str | DiskSpec | None = "disk_persistent"
    config_path: str = SECRET_DELIVERY_DEFAULT_CONFIG_PATH
    manifest_path: str = SECRET_DELIVERY_DEFAULT_MANIFEST_PATH
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
            raise ValidationError("secret() requires a non-empty secret name.")
        if not targets:
            raise ValidationError("secret() requires at least one delivery target.")
        entry = SecretSpec(
            name=name,
            required=required,
            schema=schema,
            targets=targets,
        )
        self._secrets.append(entry)
        return entry

    def with_secret(
        self,
        name: str,
        *,
        required: bool = True,
        schema: SecretSchema | None = None,
        targets: tuple[SecretTarget, ...] = (),
    ) -> Self:
        """Like :meth:`secret`, but return the module so declarations chain inline."""
        self.secret(name, required=required, schema=schema, targets=targets)
        return self

    @property
    def store_disk(self) -> str | None:
        """Name of the disk secrets are stored on (``store_at`` as a name)."""
        if isinstance(self.store_at, DiskSpec):
            return self.store_at.name
        return self.store_at or None

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
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
                f"secrets are stored on disk {store!r}, which no DiskEncryption "
                "in this profile declares"
            ),
            hint=(
                f"Declared disks: {declared}. Apply a DiskEncryption with "
                f"disk({store!r}, ...) to this profile, pass store_at= one of the declared "
                "disks, or store_at=None."
            ),
            profile=profile,
            subject=store,
        )

    def setup(self, image: Image) -> None:
        """Declare build packages and the secret-delivery build hook."""
        image.build_install(*SECRET_DELIVERY_BUILD_PACKAGES)
        image.source_build(self.source_spec())

    def install(self, image: Image) -> None:
        """Record the secrets on the active profiles and write the configs."""
        self._add_config(image)

    def init_script(self, image: Image) -> str:
        return f"/usr/bin/secret-delivery setup {shlex.quote(self.config_path)}\n"

    def source_spec(self) -> SourceBuild:
        """The ``secret-delivery`` source build from ``source_repo@source_branch``.

        ``mark_unpinned=False`` keeps the unpinned hook byte-identical to the
        hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="secret-delivery",
            source=GitSource(self.source_repo, self.source_branch),
            build=GoBuild(package="./cmd/secret-delivery", output="secret-delivery"),
            install_to="/usr/bin/secret-delivery",
            mark_unpinned=False,
        )

    def _add_config(self, image: Image) -> None:
        for profile in image._iter_active_profiles():
            for spec in self._secrets:
                profile.secrets.append(spec)

        image.file(self.config_path, content=self._render_yaml_config())
        image.file(
            self.manifest_path,
            content=_render_manifest_json(
                self._secrets,
                method=self.method,
                host=self.host,
                port=self.port,
            ),
        )

    def _render_yaml_config(self) -> str:
        if self.method != "http_post":
            raise ValidationError("Only http_post secret delivery is supported.")

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
    secrets: list[SecretSpec],
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
