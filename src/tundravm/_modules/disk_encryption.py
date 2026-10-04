"""Disk encryption module."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm._modules.base import TUNDRA_TOOLS, Module
from tundravm._modules.key_generation import KeyGeneration, KeySpec, validate_entry_name
from tundravm._source import GitSource, GoBuild, Install, SourceBuild
from tundravm.check import Diagnostic
from tundravm.errors import ValidationError

if TYPE_CHECKING:
    from tundravm._image import Image

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_CONFIG_PATH = "/etc/tdx/disk-setup.yaml"
DISK_ENCRYPTION_INIT_PRIORITY = 20
DEFAULT_DISK_DIRS = ("ssh", "data", "logs")


@dataclass(frozen=True, slots=True)
class DiskSpec:
    """A managed encrypted or plain disk.

    A disk is encrypted when it has a key: ``key=`` a :class:`KeySpec` (read from
    ``key.output``, which ``key_path`` then holds), ``key_path`` alone, or
    ``key_name`` (written to the config as ``encryption_key``). ``device=None``
    picks the largest unpartitioned disk.
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

    def __post_init__(self) -> None:
        validate_entry_name(self.name, kind="disk")
        key = self.key
        if key is not None:
            if key.output is None:
                raise ValidationError(
                    f"disk {self.name!r}: key {key.name!r} has no output path to read.",
                    hint=f"Declare the key with output=..., e.g. KeySpec({key.name!r}, "
                    "output='/run/keys/disk').",
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
                    "Plain disks cannot request custom mapper names.",
                    context={"disk": self.name, "mapper_name": self.mapper_name},
                )

    @property
    def encrypted(self) -> bool:
        """True when the disk has a key (``key_name`` or ``key_path``)."""
        return self.key_name is not None or self.key_path is not None

    @property
    def generated_mapper_name(self) -> str:
        """The mapper name ``disk-setup`` opens the disk under."""
        return f"crypt_disk_{self.name}"


@dataclass(slots=True)
class DiskEncryption(Module):
    """Configure one or more disks via ``tundra-tools`` ``disk-setup``.

    ``check()`` verifies that the keys disks reference through ``key=`` or
    ``key_name`` are declared by a ``KeyGeneration`` applied to the profile.
    """

    disks: tuple[DiskSpec, ...]
    config_path: str = DISK_ENCRYPTION_DEFAULT_CONFIG_PATH
    source: GitSource = TUNDRA_TOOLS

    def __post_init__(self) -> None:
        self.disks = tuple(self.disks)
        if not self.disks:
            raise ValidationError("DiskEncryption requires at least one disk definition.")

        names: set[str] = set()
        mount_points: set[str] = set()
        mapper_names: set[str] = set()
        env_key_names: set[str] = set()
        for spec in self.disks:
            if spec.name in names:
                raise ValidationError(f"Duplicate disk name {spec.name!r}.")
            names.add(spec.name)
            if spec.mount_at in mount_points:
                raise ValidationError(
                    "Each managed disk must use a unique mount point.",
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
                    "Each encrypted disk must use a unique mapper name.",
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

    def configure(self, image: Image) -> None:
        """Build disk-setup, write the aggregate disk config, run it at boot (priority 20)."""
        image.build_packages(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.build_from(self.source_spec())
        image.install("cryptsetup")
        image.file(self.config_path, content=self._render_config())
        image.runtime_init(self._render_init_script(), priority=DISK_ENCRYPTION_INIT_PRIORITY)

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        keys: dict[str, KeySpec] = {}
        for module in image.applied_modules(profile, inherited=True):
            if isinstance(module, KeyGeneration):
                keys.update((spec.name, spec) for spec in module.keys)
        for disk in self.disks:
            key_ref = disk.key_name or (disk.key.name if disk.key is not None else None)
            if key_ref is None:
                continue
            key = keys.get(key_ref)
            if key is None:
                declared = ", ".join(sorted(keys)) or "none"
                yield Diagnostic(
                    level="error",
                    code="disk-key-undefined",
                    message=(
                        f"disk {disk.name!r} uses key {key_ref!r}, which no "
                        "KeyGeneration in this profile declares"
                    ),
                    hint=(
                        f"Declared keys: {declared}. Add KeySpec({key_ref!r}, ...) "
                        "to a KeyGeneration applied to this profile, or fix the key."
                    ),
                    profile=profile,
                    subject=disk.name,
                )
            elif disk.key_path is not None and key.output != disk.key_path:
                written = key.output or "no file (output is unset)"
                yield Diagnostic(
                    level="warning",
                    code="disk-key-path-mismatch",
                    message=(
                        f"disk {disk.name!r} reads its key from {disk.key_path}, but key "
                        f"{key.name!r} is written to {written}"
                    ),
                    hint=(
                        f"Set KeySpec({key.name!r}, output={disk.key_path!r}) or align key_path."
                    ),
                    profile=profile,
                    subject=disk.name,
                )

    def source_spec(self) -> SourceBuild:
        """The ``disk-setup`` source build from ``source``.

        ``mark_unpinned=False`` keeps the unpinned hook byte-identical to the
        hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="disk-encryption",
            source=self.source,
            build=GoBuild(package="./cmd/disk-setup", output="disk-setup"),
            install=(Install.artifact("/usr/bin/disk-setup"),),
            mark_unpinned=False,
        )

    def _render_config(self) -> str:
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
        return "\n".join(lines) + "\n"

    def _strategy_lines(self, spec: DiskSpec) -> tuple[str, ...]:
        if spec.device is not None:
            return (
                '    strategy: "pathglob"',
                "    strategy_config:",
                f'      pattern: "{spec.device}"',
            )
        return ('    strategy: "largest"',)

    def _render_init_script(self) -> str:
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
