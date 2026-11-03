"""Secret delivery: the attested endpoint (``tools/cmd/attested-endpoint``) of a ``Secrets``.

The endpoint serves ``GET /evidence`` (a quote binding the nonce, its TLS key, the SSH host
keys, the instance public key and each attested TLS port's certificate key) and takes
``POST /credentials``, ``/unlock`` and ``/ssh-key`` only on the TLS session that fetched
that evidence (plan section 6.3).
"""

from __future__ import annotations

import json
import posixpath
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from tundravm._modules import attested_tls
from tundravm._modules.base import Module, tools_source
from tundravm._modules.disk_encryption import (
    OWNER_DIRECTORY,
    OWNER_UNIT,
    DiskSpec,
)
from tundravm._source import GitSource, GoBuild, Install, LocalSource, SourceBuild
from tundravm._units.render import render
from tundravm.errors import ValidationError
from tundravm.models import SecretSpec, ServiceSpec

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

BUILTIN_SECRET_PROVIDER = "tundravm"
"""The provider of a ``Secrets`` without ``delivery=``: the attested endpoint from ``tools/``."""

ENDPOINT_UNIT = "tundravm-endpoint.service"
"""The attested endpoint's unit: the control plane, which no runtime-init step holds up."""
QUOTE_SOCKET = "/var/tdxs.sock"
"""The evidence service's socket the endpoint asks for quotes."""
SSH_HOST_KEYS = "/etc/ssh/ssh_host_*_key.pub"
"""The SSH host public keys every quote binds (none in an image without SSH)."""
INSTANCE_PUBLIC_KEY = "/run/tundravm/instance-key.pub"
"""The instance public key every quote binds, in an image that generates one."""
RUNTIME = "/run/tundravm"
LOGIN_KEYS = "/etc/ssh/authorized_keys.d"
"""Each login's authorized keys, where the endpoint writes the owner login's (its default)."""
MODE_FILE = "/run/tundravm/mode"
"""What the execution gate recorded, which the evidence carries."""

_HARDENING: tuple[tuple[str, str], ...] = (
    ("ProtectSystem", "strict"),
    ("ProtectHome", "no"),
    ("PrivateTmp", "yes"),
    ("PrivateDevices", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectControlGroups", "yes"),
    ("ProtectKernelLogs", "yes"),
    ("ProtectClock", "yes"),
    ("ProtectHostname", "yes"),
    ("RestrictSUIDSGID", "yes"),
    ("RestrictRealtime", "yes"),
    ("RestrictNamespaces", "yes"),
    ("LockPersonality", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
    ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6"),
    ("SystemCallArchitectures", "native"),
    ("CapabilityBoundingSet", "CAP_CHOWN"),
)
"""Root (it writes root-owned credentials, FIFOs and ``authorized_keys``) with ``CAP_CHOWN``
alone, for a ``SecretFile`` with an owner."""


@dataclass(slots=True)
class SecretDelivery(Module):
    """Boot-time secret delivery phase.

    *secrets* are the expected secrets, each with at least one delivery target.
    *store_at* names the ``DiskEncryption`` disk (a name or the ``DiskSpec``) that
    received secrets are stored on.

    An env target with a ``service`` goes to that service's environment file
    (:meth:`env_file`), which a drop-in makes the unit read
    (:meth:`service_dropins`). *env_suffix* tells apart the files of several deliveries
    in one profile.

    *provider* writes the targets: the built-in one, which writes files and never the
    environment, or an external one writing the kinds in *provider_supports*. The SSH
    key flow always runs through the built-in one.

    *volumes* are ``(name, pipe)`` pairs: the volumes whose passphrase ``POST /unlock``
    hands to ``key-gen``'s FIFO. *attested_tls* are the attested TLS ports whose
    certificate (:func:`tundravm._modules.attested_tls.certificate`) the quote binds.
    *owners* name the volumes whose header pins the operator's key: with one, every write
    must be signed by it (the first signed one pins its signer). The operator's SSH key goes
    to root's *ssh_dir*, or to the ``authorized_keys.d`` file of *ssh_login*; with neither
    the endpoint takes none.
    """

    secrets: tuple[SecretSpec, ...] = ()
    method: Literal["http_post"] = "http_post"
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_dir: str | None = "/root/.ssh"
    key_path: str | None = "/etc/root_key"
    ssh_login: str | None = None
    store_at: str | DiskSpec | None = "disk_persistent"
    config_path: str = SECRET_DELIVERY_DEFAULT_CONFIG_PATH
    manifest_path: str = SECRET_DELIVERY_DEFAULT_MANIFEST_PATH
    source: GitSource | LocalSource = field(default_factory=tools_source)
    env_suffix: str | None = None
    provider: str = BUILTIN_SECRET_PROVIDER
    provider_supports: tuple[str, ...] = ()
    volumes: tuple[tuple[str, str], ...] = ()
    attested_tls: tuple[int, ...] = ()
    owners: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.secrets = tuple(self.secrets)
        self.attested_tls = tuple(sorted(self.attested_tls))
        for spec in self.secrets:
            if not spec.name:
                raise ValidationError(
                    "SecretDelivery requires non-empty secret names.",
                    hint="Give each Secret() a name, e.g. Secret('api-token', ...).",
                )
            if not spec.targets:
                raise ValidationError(
                    f"secret {spec.name!r} requires at least one delivery target.",
                    hint="Bind the Secret in a Service: argv=[..., secret], environment= or "
                    "credentials=.",
                )

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
        """Whether something other than the attested endpoint writes the targets."""
        return self.provider != BUILTIN_SECRET_PROVIDER

    def files(self) -> list[tuple[str, str, str, str | None]]:
        """``(name, location, mode, owner)`` of each ``file`` target the endpoint writes."""
        if self.external:
            return []
        return [
            (spec.name, target.location, target.mode, target.owner)
            for spec in sorted(self.secrets, key=lambda s: s.name)
            for target in spec.targets
            if target.kind == "file"
        ]

    def init_script(self) -> str:
        """The ``secrets`` runtime-init step: wait until every required ``file`` target the
        endpoint writes has arrived, so ``secrets.ready`` means the image has its secrets.

        The endpoint itself is :meth:`service`, not a step: it serves from boot whatever the
        other steps wait for.
        """
        required = [
            target.location
            for spec in self.secrets
            if spec.required and not self.external
            for target in spec.targets
            if target.kind == "file"
        ]
        if not required:
            return "# secrets: nothing required arrives through the attested endpoint\n"
        test = " && ".join(f"[ -e {shlex.quote(path)} ]" for path in sorted(set(required)))
        wait = f"until {test}; do sleep 1; done\n"
        return f"# secrets: wait for the attested endpoint's deliveries\n{wait}"

    @property
    def unit(self) -> str:
        """The endpoint's unit (``tundravm-endpoint-<suffix>.service`` beside another)."""
        if self.env_suffix is None:
            return ENDPOINT_UNIT
        return f"tundravm-endpoint-{self.env_suffix}.service"

    def service(self) -> ServiceSpec:
        """The endpoint: started at boot, after the evidence socket and the keys it binds,
        waiting for no runtime-init step (the control plane must answer before them)."""
        owners = (OWNER_UNIT,) if self.owners else ()
        return ServiceSpec(
            name=self.unit,
            command=("/usr/bin/attested-endpoint", "--config", self.config_path),
            description="tundravm attested endpoint",
            after=(
                "network-online.target",
                "tdx-evidence.service",
                "tdxs.socket",
                "ssh-host-keys.service",
                "tundravm-instance-key.service",
                *(attested_tls.unit(port) for port in self.attested_tls),
                *owners,
            ),
            wants=("network-online.target", "tdxs.socket", *owners),
            type="notify",
            restart="on-failure",
            hardening=(*_HARDENING, ("ReadWritePaths", " ".join(self.write_paths()))),
            needs=(),
        )

    def write_paths(self) -> tuple[str, ...]:
        """The directories the endpoint writes (``-``: perhaps not there). ``/run/tundravm``
        exists once the execution gate, which the endpoint starts after, recorded its mode;
        root's SSH directory's parent is writable so the endpoint can make it, and a login's
        ``authorized_keys.d``, which the image makes."""
        found = {RUNTIME}
        if self.ssh_login is not None:
            found.add(LOGIN_KEYS)
        elif self.ssh_dir is not None:
            found.add(posixpath.dirname(self.ssh_dir.rstrip("/")) or "/")
            if self.key_path:
                found.add(posixpath.dirname(self.key_path))
        found.update(posixpath.dirname(location) for _, location, _, _ in self.files())
        found.update(posixpath.dirname(pipe) for _, pipe in self.volumes)
        outermost = [p for p in found if not any(p.startswith(f"{o}/") for o in found)]
        return tuple(f"-{path}" for path in sorted(outermost))

    def source_spec(self) -> SourceBuild:
        """The ``attested-endpoint`` source build from ``source``."""
        return SourceBuild(
            name="attested-endpoint",
            source=self.source,
            build=GoBuild(package="./cmd/attested-endpoint", output="attested-endpoint"),
            install=(Install.artifact("/usr/bin/attested-endpoint"),),
        )

    def render_manifest(self) -> str:
        """The JSON manifest of the expected secrets and their delivery targets."""
        return _render_manifest_json(
            self.secrets, method=self.method, host=self.host, port=self.port, env_file=self.env_file
        )

    def render_config(self) -> str:
        """The endpoint's YAML config: where it listens, what it binds and where it writes."""
        if self.method != "http_post":
            raise ValidationError(
                "Only http_post secret delivery is supported.",
                hint="Leave the delivery to tundravm: secrets arrive over the attested endpoint.",
            )

        lines = [
            f'listen: "{self.host}:{self.port}"',
            f'quote_socket: "{QUOTE_SOCKET}"',
            f'ssh_host_keys: "{SSH_HOST_KEYS}"',
            f'instance_key: "{INSTANCE_PUBLIC_KEY}"',
            f'mode_file: "{MODE_FILE}"',
        ]
        if self.ssh_login is not None:
            lines.extend(("ssh:", f'  login: "{self.ssh_login}"'))
        elif self.ssh_dir is not None:
            lines.extend(("ssh:", f'  dir: "{self.ssh_dir}"'))
            if self.key_path:
                lines.append(f'  key_path: "{self.key_path}"')
        files = self.files()
        lines.append("credentials:" if files else "credentials: []")
        for name, location, mode, owner in files:
            lines.extend((f'  - name: "{name}"', f'    path: "{location}"', f'    mode: "{mode}"'))
            if owner is not None:
                lines.append(f'    owner: "{owner}"')
        lines.append("volumes:" if self.volumes else "volumes: []")
        for name, pipe in self.volumes:
            lines.extend((f'  - name: "{name}"', f'    pipe: "{pipe}"'))
            if name in self.owners:
                lines.append(f'    owner: "{OWNER_DIRECTORY}/{name}"')
        lines.append("attested_tls:" if self.attested_tls else "attested_tls: []")
        for port in self.attested_tls:
            lines.extend(
                (f"  - port: {port}", f'    certificate: "{attested_tls.certificate(port)}"')
            )
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
