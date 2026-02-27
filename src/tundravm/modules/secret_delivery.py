"""Secret delivery module."""

from __future__ import annotations

import hashlib
import json
import shlex
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

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
SECRET_DELIVERY_DEFAULT_BRANCH = "master"
SECRET_DELIVERY_DEFAULT_CONFIG_PATH = "/etc/tdx/secrets.yaml"
SECRET_DELIVERY_DEFAULT_MANIFEST_PATH = "/etc/tdx/secrets.json"


@dataclass(slots=True)
class SecretDelivery:
    """Boot-time secret delivery phase."""

    method: Literal["http_post"] = "http_post"
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_dir: str = "/root/.ssh"
    key_path: str | None = "/etc/root_key"
    store_at: str | None = "disk_persistent"
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

    def apply(self, image: Image) -> None:
        """Add build hook, configs, and init script."""
        image.build_install(*SECRET_DELIVERY_BUILD_PACKAGES)

        clone_dir = Build.build_path("secret-delivery")
        chroot_dir = Build.chroot_path("secret-delivery")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("secret-delivery/build/secret-delivery"),
                    dest=Build.dest_path("usr/bin/secret-delivery"),
                    name="secret-delivery",
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
            "-o ./build/secret-delivery ./cmd/secret-delivery"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        self._add_config(image)

        image.add_init_script(
            f"/usr/bin/secret-delivery setup {shlex.quote(self.config_path)}\n",
            priority=30,
        )

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"secret-delivery-{repo_hash}-{self.source_branch}"

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
        if self.store_at:
            lines.append(f'  store_at: "{self.store_at}"')
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
