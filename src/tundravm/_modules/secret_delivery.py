"""Secret delivery module."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from tundravm._modules.base import TUNDRA_TOOLS, Module
from tundravm._modules.disk_encryption import DiskEncryption, DiskSpec
from tundravm._source import GitSource, GoBuild, Install, SourceBuild
from tundravm._units.render import render
from tundravm.diagnostics import Diagnostic
from tundravm.errors import ValidationError
from tundravm.models import SecretSpec, ships_unit

if TYPE_CHECKING:
    from tundravm.declarative._lowered import Lowered

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_CONFIG_PATH = "/etc/tdx/secrets.yaml"
SECRET_DELIVERY_DEFAULT_MANIFEST_PATH = "/etc/tdx/secrets.json"
SECRET_ENV_DIRECTORY = "/run/secrets"
"""Where a service's ``SecretEnv`` values are delivered, one ``<service>.env`` file each."""
_UNIT_DIRECTORY = "/usr/lib/systemd/system"

BUILTIN_SECRET_PROVIDER = "tundra-tools"
"""The provider of a ``Secrets`` without ``delivery=``: tundra-tools' ``secret-delivery``."""

BUILTIN_SECRET_CAPABILITIES: Mapping[str, bool] = MappingProxyType(
    {"ssh-key": True, "file": False, "env": False}
)
"""What the built-in provider writes at boot, by target kind.

``ssh-key``: one ed25519 public key POSTed to ``host:port``, written to
``<ssh_dir>/authorized_keys`` and ``key_path`` (tundra-tools ``pkg/secrets``
``Manager.Setup``). The binary never reads the manifest, so ``file``
(``SecretFile``) and ``env`` (``SecretEnv``) targets are not written.
"""

_TARGET_NOUNS = {"file": "file", "env": "environment variable"}


@dataclass(slots=True)
class SecretDelivery(Module):
    """Boot-time secret delivery phase.

    *secrets* are the expected secrets, each with at least one delivery target.
    *store_at* names the ``DiskEncryption`` disk (a name or the ``DiskSpec``) that
    received secrets are stored on; ``check()`` warns when no disk applied to the
    profile has that name (``secret-store-undefined``).

    An env target with a ``service`` goes to that service's environment file
    (:meth:`env_file`), which a drop-in makes the unit read
    (:meth:`service_dropins`); ``check()`` rejects a service the profile neither
    generates, ships nor enables (``secret-env-service-unknown``). *env_suffix*
    tells apart the files of several deliveries in one profile.

    *provider* writes the targets: the built-in one (:data:`BUILTIN_SECRET_CAPABILITIES`)
    or an external one writing the kinds in *provider_supports*; ``check()``
    rejects a target it does not write (``secret-target-unsupported``). The SSH
    key flow always runs through the built-in one.
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
    env_suffix: str | None = None
    provider: str = BUILTIN_SECRET_PROVIDER
    provider_supports: tuple[str, ...] = ()

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

    def env_file(self, service: str) -> str:
        """The file *service*'s (``app.service``) env targets are delivered to."""
        stem = service.removesuffix(".service")
        suffix = "" if self.env_suffix is None else f"-{self.env_suffix}"
        return f"{SECRET_ENV_DIRECTORY}/{stem}{suffix}.env"

    def env_services(self) -> dict[str, bool]:
        """Each service an env target names, and whether a required secret targets it."""
        services: dict[str, bool] = {}
        for spec in self.secrets:
            for target in spec.targets:
                if target.kind == "env" and target.service is not None:
                    services[target.service] = services.get(target.service, False) or spec.required
        return dict(sorted(services.items()))

    def service_dropins(self) -> dict[str, str]:
        """Drop-in path to text: each service of :meth:`env_services` reads its env file.

        The file is required when a required secret targets the service, so the
        unit does not start without it; otherwise a missing file is skipped.
        """
        name = "tundravm-secrets" if self.env_suffix is None else f"tundravm-{self.env_suffix}"
        return {
            f"{_UNIT_DIRECTORY}/{service}.d/{name}.conf": render(
                [
                    (
                        "Service",
                        [("EnvironmentFile", f"{'' if required else '-'}{self.env_file(service)}")],
                    )
                ]
            )
            for service, required in self.env_services().items()
        }

    @property
    def external(self) -> bool:
        """Whether something other than tundra-tools writes the targets."""
        return self.provider != BUILTIN_SECRET_PROVIDER

    def delivers(self, kind: str) -> bool:
        """Whether *provider* writes a target of *kind* (``file``, ``env``) at boot."""
        if self.external:
            return kind in self.provider_supports
        return BUILTIN_SECRET_CAPABILITIES.get(kind, False)

    def boot_targets(self) -> list[dict[str, object]]:
        """What the image expects at boot: the SSH key, then each secret's targets."""
        rows: list[dict[str, object]] = [
            {
                "secret": None,
                "kind": "ssh-key",
                "location": f"{self.ssh_dir.rstrip('/')}/authorized_keys",
                "provider": BUILTIN_SECRET_PROVIDER,
                "supported": True,
                "required": True,
            }
        ]
        for spec in sorted(self.secrets, key=lambda s: s.name):
            rows.extend(
                {
                    "secret": spec.name,
                    "kind": target.kind,
                    "location": target.location,
                    "provider": self.provider,
                    "supported": self.delivers(target.kind),
                    "required": spec.required,
                }
                for target in spec.targets
            )
        return rows

    def check(self, image: Lowered, profile: str) -> Iterator[Diagnostic]:
        yield from self._check_targets(profile)
        yield from self._check_services(image, profile)
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
            severity="warning",
            code="secret-store-undefined",
            message=(
                f"secrets are stored on disk {store!r}, which no Disk in this variant declares"
            ),
            hint=(
                f"Declared disks: {declared}. Declare Disk({store!r}, ...) in this variant, "
                "pass Secrets(store=) one of the declared disks, or store=None."
            ),
            variant=profile,
            subject=store,
        )

    def _check_targets(self, profile: str) -> Iterator[Diagnostic]:
        for spec in self.secrets:
            for target in spec.targets:
                if self.delivers(target.kind):
                    continue
                noun = _TARGET_NOUNS[target.kind]
                only = "" if self.external else " (it provisions only the SSH key)"
                yield Diagnostic(
                    severity="error",
                    code="secret-target-unsupported",
                    message=(
                        f"secret {spec.name!r} goes to {noun} {target.location}, which the "
                        f"{self.provider!r} provider does not write at boot{only}"
                    ),
                    hint=self._unsupported_hint(target.kind),
                    variant=profile,
                    subject=spec.name,
                )

    def _unsupported_hint(self, kind: str) -> str:
        if self.external:
            return (
                f"Add {kind!r} to ExternalDelivery({self.provider!r}, supports=) if it writes "
                "the target, or remove the target from the Secret."
            )
        return (
            f"Declare Secrets(delivery=ExternalDelivery('NAME', supports=({kind!r},))) naming "
            "what writes it at boot (a cloud secret store, a custom unit), or remove the "
            "target and reach the VM with the SSH key Secrets() delivers."
        )

    def _check_services(self, image: Lowered, profile: str) -> Iterator[Diagnostic]:
        state = image.state.effective_profile(profile)
        controlled = {spec.unit for spec in state.unit_states}
        for service in self.env_services():
            if service in controlled or ships_unit(state, service):
                continue
            yield Diagnostic(
                severity="error",
                code="secret-env-service-unknown",
                message=(
                    f"a SecretEnv delivers to service {service!r}, which this variant "
                    "neither generates, ships nor enables"
                ),
                hint=(
                    f"Declare Service({service.removesuffix('.service')!r}, ...) or "
                    f"Unit({service!r}, ...) in this variant, Unit({service!r}, "
                    "enabled=True) for a packaged unit, or fix the SecretEnv's service=."
                ),
                variant=profile,
                subject=service,
            )

    def init_script(self) -> str:
        """The ``secrets`` runtime-init step: ``secret-delivery setup`` on the config.

        It receives the SSH key and writes the configuration; an external
        provider's targets are waited for by its own ``delivery-<name>`` step.
        """
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
            self.secrets, method=self.method, host=self.host, port=self.port, env_file=self.env_file
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
    env_file: Callable[[str], str],
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
                if target_spec.service is not None:
                    target["service"] = target_spec.service
                    target["env_file"] = env_file(target_spec.service)
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
