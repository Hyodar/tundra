"""Disk encryption module."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import Literal

from tundravm._modules.base import Module, tool_packages, tools_source
from tundravm._modules.key_generation import KeySpec, validate_entry_name
from tundravm._source import GitSource, GoBuild, Install, LocalSource, SourceBuild
from tundravm.errors import ValidationError
from tundravm.models import ServiceSpec

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_CONFIG_PATH = "/etc/tdx/disk-setup.yaml"
OWNER_DIRECTORY = "/run/tundravm/owner"
"""Where each owned volume's owner key is for the boot (``<volume>``, ``type base64
SHA256:...``): ``disk-setup owner`` copies it from the header, the endpoint pins it."""
OWNER_UNIT = "tundravm-volume-owner.service"
"""``disk-setup owner``: ready once the headers' owners are the boot's, before the attested
endpoint starts, then writing each pin or rotation the endpoint makes into the header."""
DEFAULT_DISK_DIRS = ("ssh", "data", "logs")
_OWNER_HARDENING: tuple[tuple[str, str], ...] = (
    ("PrivateNetwork", "yes"),
    ("ProtectHome", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectControlGroups", "yes"),
    ("RestrictAddressFamilies", "AF_UNIX"),
    ("RestrictNamespaces", "yes"),
    ("LockPersonality", "yes"),
    ("SystemCallArchitectures", "native"),
)
"""Root with the volumes' devices (it reads and writes their headers) and no network; no
``ProtectClock``, which would limit the devices to the clock's."""


@dataclass(frozen=True, slots=True)
class DiskSpec:
    """A managed encrypted or plain disk.

    A disk is encrypted when it has a key: ``key=`` a :class:`KeySpec` (read from
    ``key.output``, which ``key_path`` then holds), ``key_path`` alone, or
    ``key_name`` (written to the config as ``encryption_key``). ``device=None``
    lets ``disk-setup`` pick the largest whole ``/dev/sd*`` disk, the boot disk
    included (never a virtio ``/dev/vd*`` one). ``format_policy``: ``always``
    formats on every boot, ``on_initialize`` while ``disk-setup`` has not initialised
    the disk (an encrypted disk without its init token), ``on_fail`` when an
    encrypted disk is not LUKS yet; both format a plain disk that does not mount.
    ``never`` does not format. With *owner* its LUKS2 header pins the operator's SSH key
    (``tools/pkg/owner``): it is created and opened only for the boot's owner.
    """

    name: str
    device: str | None = field(default="/dev/vda3", kw_only=True)
    mapper_name: str | None = field(default=None, kw_only=True)
    key: KeySpec | None = field(default=None, kw_only=True)
    key_path: str | None = field(default=None, kw_only=True)
    key_name: str | None = field(default=None, kw_only=True)
    mount_at: str = field(default="/persistent", kw_only=True)
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = field(
        default="on_fail", kw_only=True
    )
    dirs: tuple[str, ...] = field(default=DEFAULT_DISK_DIRS, kw_only=True)
    owner: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        validate_entry_name(self.name, kind="disk")
        key = self.key
        if key is not None:
            if key.output is None:
                raise ValidationError(
                    f"disk {self.name!r}: key {key.name!r} has no output path to read.",
                    hint="Unlock the volume with OperatorKey(), BrokerKey(...) or TpmSeal(...).",
                )
            if self.key_path is not None and self.key_path != key.output:
                raise ValidationError(
                    f"disk {self.name!r}: key_path {self.key_path!r} differs from key "
                    f"{key.name!r} output {key.output!r}.",
                    hint="Drop key_path=; it defaults to key.output.",
                )
            if self.key_name is not None and self.key_name != key.name:
                raise ValidationError(
                    f"disk {self.name!r}: key_name {self.key_name!r} differs from key "
                    f"{key.name!r}.",
                    hint="Pass key= or key_name=, not both.",
                )
            object.__setattr__(self, "key_path", key.output)
        if not self.encrypted:
            if self.mapper_name is not None:
                raise ValidationError(
                    "a plain volume cannot name its device mapper.",
                    hint="Give the Volume an unlock= so it is encrypted.",
                    context={"disk": self.name, "mapper_name": self.mapper_name},
                )

    @property
    def encrypted(self) -> bool:
        """True when the disk has a key (``key_name`` or ``key_path``)."""
        return self.key_name is not None or self.key_path is not None

    @property
    def owner_path(self) -> str:
        """The boot's owner file of an owned disk."""
        return f"{OWNER_DIRECTORY}/{self.name}"

    @property
    def generated_mapper_name(self) -> str:
        """The mapper name ``disk-setup`` opens the disk under."""
        return f"crypt_disk_{self.name}"


@dataclass(slots=True)
class DiskEncryption(Module):
    """Configure one or more disks via ``disk-setup`` (``tools/``).

    ``check()`` verifies that the keys disks reference through ``key=`` or
    ``key_name`` are declared by a ``KeyGeneration`` applied to the profile.
    """

    disks: tuple[DiskSpec, ...]
    config_path: str = DISK_ENCRYPTION_DEFAULT_CONFIG_PATH
    source: GitSource | LocalSource = field(default_factory=tools_source)

    def __post_init__(self) -> None:
        self.disks = tuple(self.disks)
        if not self.disks:
            raise ValidationError(
                "volume encryption has no volume to encrypt.",
                hint='Keep state on a Volume("data", unlock=OperatorKey()).',
            )

        names: set[str] = set()
        mount_points: set[str] = set()
        mapper_names: set[str] = set()
        env_key_names: set[str] = set()
        for spec in self.disks:
            if spec.name in names:
                raise ValidationError(
                    f"Duplicate disk name {spec.name!r}.",
                    hint="Give each Volume its own name.",
                )
            names.add(spec.name)
            if spec.mount_at in mount_points:
                raise ValidationError(
                    "two volumes mount at one path.",
                    hint="Give each Volume its own mount=.",
                    context={"disk": spec.name, "mount_at": spec.mount_at},
                )
            mount_points.add(spec.mount_at)

            if not spec.encrypted:
                continue

            if spec.key_path is None and spec.key_name is not None:
                env_key_names.add(spec.key_name)

            effective_mapper = spec.mapper_name or spec.generated_mapper_name
            if effective_mapper in mapper_names:
                raise ValidationError(
                    "two encrypted volumes share a device mapper name.",
                    hint="Give each Volume its own name.",
                    context={"disk": spec.name, "mapper_name": effective_mapper},
                )
            mapper_names.add(effective_mapper)

        if len(env_key_names) > 1:
            raise ValidationError(
                "Multiple encrypted disks with distinct keys require key_path values.",
                hint=(
                    "disk-setup can only consume one environment-provided fallback key "
                    "per aggregate setup run."
                ),
            )

    def source_spec(self) -> SourceBuild:
        """The ``disk-setup`` source build from ``source``."""
        return SourceBuild(
            name="disk-encryption",
            source=self.source,
            build=GoBuild(package="./cmd/disk-setup", output="disk-setup"),
            install=(Install.artifact("/usr/bin/disk-setup"),),
        )

    def packages(self) -> tuple[str, ...]:
        """What the image installs for ``disk-setup`` and the step's mapper renames: the
        packages of the programs it runs (:data:`~tundravm._modules.base.TOOL_PROGRAMS`),
        ``e2fsprogs`` for the ``mkfs.ext4`` it formats with among them."""
        return tool_packages("disk-setup")

    def render_config(self) -> str:
        """The aggregate ``disk-setup`` config: one entry per disk."""
        lines = ["disks:"]
        for spec in self.disks:
            lines.extend(
                (
                    f"  {spec.name}:",
                    *self._strategy_lines(spec),
                    f'    format: "{spec.format_policy}"',
                    f'    mount_at: "{spec.mount_at}"',
                    f"    dirs: {json.dumps(list(spec.dirs))}",
                )
            )
            if spec.key_name is not None:
                lines.append(f'    encryption_key: "{spec.key_name}"')
            if spec.key_path is not None:
                lines.append(f'    encryption_key_path: "{spec.key_path}"')
            if spec.owner:
                lines.append(f'    owner: "{spec.owner_path}"')
        return "\n".join(lines) + "\n"

    def owner_service(self) -> ServiceSpec | None:
        """:data:`OWNER_UNIT` when a disk is owned: root, with the devices and no network,
        waiting for no runtime-init step (the endpoint it comes before waits for none)."""
        if not any(spec.owner for spec in self.disks):
            return None
        return ServiceSpec(
            name=OWNER_UNIT,
            command=("/usr/bin/disk-setup", "owner", self.config_path),
            description="tundravm volume owners",
            before=("tundravm-endpoint.service",),
            type="notify",
            restart="on-failure",
            hardening=_OWNER_HARDENING,
            needs=(),
        )

    def _strategy_lines(self, spec: DiskSpec) -> tuple[str, ...]:
        if spec.device is not None:
            return (
                '    strategy: "pathglob"',
                "    strategy_config:",
                f'      pattern: "{spec.device}"',
            )
        return ('    strategy: "largest"',)

    def render_init_script(self) -> str:
        """The ``disks`` runtime-init step: ``disk-setup``, then any mapper renames."""
        lines = [f"/usr/bin/disk-setup setup {shlex.quote(self.config_path)}"]
        for spec in self.disks:
            if spec.encrypted and spec.mapper_name:
                generated_mapper = spec.generated_mapper_name
                if spec.mapper_name != generated_mapper:
                    generated_mapper_path = shlex.quote(f"/dev/mapper/{generated_mapper}")
                    generated_mapper_name = shlex.quote(generated_mapper)
                    requested_mapper_name = shlex.quote(spec.mapper_name)
                    lines.extend(
                        (
                            f"if [ -e {generated_mapper_path} ]; then",
                            "    cryptsetup rename "
                            f"{generated_mapper_name} {requested_mapper_name}",
                            "fi",
                        )
                    )
        return "\n".join(lines) + "\n"
