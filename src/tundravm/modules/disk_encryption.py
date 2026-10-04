"""Disk encryption module."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from tundravm.check import Diagnostic
from tundravm.errors import ValidationError
from tundravm.modules.base import Module
from tundravm.modules.key_generation import KeyGeneration, KeySpec
from tundravm.source import GitSource, GoBuild, SourceBuild

if TYPE_CHECKING:
    from tundravm.image import Image

DISK_ENCRYPTION_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

DISK_ENCRYPTION_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
DISK_ENCRYPTION_DEFAULT_BRANCH = "master"
DISK_ENCRYPTION_DEFAULT_CONFIG_PATH = "/etc/tdx/disk-setup.yaml"
DEFAULT_DISK_DIRS = ("ssh", "data", "logs")
ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class DiskSpec:
    """Specification for a managed encrypted or plain disk."""

    name: str
    device: str | None = "/dev/vda3"
    mapper_name: str | None = None
    key_path: str | None = None
    mount_point: str = "/persistent"
    key_name: str | None = None
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    dirs: tuple[str, ...] = DEFAULT_DISK_DIRS
    key: KeySpec | None = None


@dataclass(slots=True)
class DiskEncryption(Module):
    """Configure one or more disks via ``tundra-tools`` ``disk-setup``.

    Disks may be plain, keyed by ``key_path`` alone, keyed by a ``KeyGeneration``
    key spec via ``key=`` (reads ``key.output``), or by key name via ``key_name``
    (also written to the config as ``encryption_key``); ``check()`` verifies that
    ``key=`` and ``key_name`` keys are declared for the profile.
    """

    init_priority: ClassVar[int | None] = 20

    config_path: str = DISK_ENCRYPTION_DEFAULT_CONFIG_PATH
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH
    _disks: list[DiskSpec] = field(default_factory=list, init=False, repr=False)

    def disk(
        self,
        name: str,
        *,
        device: str | None = "/dev/vda3",
        mapper_name: str | None = None,
        key_path: str | None = None,
        mount_point: str = "/persistent",
        key_name: str | None = None,
        format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail",
        dirs: tuple[str, ...] = DEFAULT_DISK_DIRS,
        key: KeySpec | None = None,
    ) -> DiskSpec:
        """Register an additional disk definition.

        *key* is a spec returned by ``KeyGeneration.key()``: the disk reads it from
        ``key.output`` (so *key_path* defaults to it) and ``check()`` reports it when no
        ``KeyGeneration`` applied to the profile declares it.
        """
        if key is not None:
            if key.output is None:
                raise ValidationError(
                    f"disk {name!r}: key {key.name!r} has no output path to read.",
                    hint=f"Declare the key with output=..., e.g. keys.key({key.name!r}, "
                    "output='/run/keys/disk').",
                )
            if key_path is not None and key_path != key.output:
                raise ValidationError(
                    f"disk {name!r}: key_path {key_path!r} differs from key "
                    f"{key.name!r} output {key.output!r}.",
                    hint="Drop key_path=; it defaults to key.output.",
                )
            if key_name is not None and key_name != key.name:
                raise ValidationError(
                    f"disk {name!r}: key_name {key_name!r} differs from key {key.name!r}.",
                    hint="Pass key= or key_name=, not both.",
                )
            key_path = key.output
        spec = DiskSpec(
            name=name,
            device=device,
            mapper_name=mapper_name,
            key_path=key_path,
            mount_point=mount_point,
            key_name=key_name,
            format_policy=format_policy,
            dirs=dirs,
            key=key,
        )
        self._append_disk(spec)
        return spec

    def with_disk(
        self,
        name: str,
        *,
        device: str | None = "/dev/vda3",
        mapper_name: str | None = None,
        key_path: str | None = None,
        mount_point: str = "/persistent",
        key_name: str | None = None,
        format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail",
        dirs: tuple[str, ...] = DEFAULT_DISK_DIRS,
        key: KeySpec | None = None,
    ) -> Self:
        """Like :meth:`disk`, but return the module so declarations chain inline."""
        self.disk(
            name,
            device=device,
            mapper_name=mapper_name,
            key_path=key_path,
            mount_point=mount_point,
            key_name=key_name,
            format_policy=format_policy,
            dirs=dirs,
            key=key,
        )
        return self

    @property
    def disks(self) -> tuple[DiskSpec, ...]:
        """Registered disk definitions, in declaration order."""
        return tuple(self._disks)

    def setup(self, image: Image) -> None:
        """Validate disks, declare build packages and the disk-setup build hook."""
        self._validate()
        image.build_install(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.source_build(self.source_spec())

    def install(self, image: Image) -> None:
        """Install cryptsetup and write the aggregate disk config."""
        image.install("cryptsetup")
        image.file(self.config_path, content=self._render_config())

    def init_script(self, image: Image) -> str:
        return self._render_init_script()

    def check(self, image: Image, profile: str) -> Iterator[Diagnostic]:
        keys: dict[str, KeySpec] = {}
        for module in image.applied_modules(profile, inherited=True):
            if isinstance(module, KeyGeneration):
                keys.update((spec.name, spec) for spec in module.keys)
        for disk in self._disks:
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
                        f"Declared keys: {declared}. Add keys.key({key_ref!r}, ...) "
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
                    hint=f"Set keys.key({key.name!r}, output={disk.key_path!r}) or align key_path.",
                    profile=profile,
                    subject=disk.name,
                )

    def _append_disk(self, spec: DiskSpec) -> None:
        self._validate_name(spec.name, kind="disk")
        if any(existing.name == spec.name for existing in self._disks):
            raise ValidationError(f"Duplicate disk name {spec.name!r}.")
        self._disks.append(spec)

    def _validate(self) -> None:
        if not self._disks:
            raise ValidationError("DiskEncryption requires at least one disk definition.")

        mount_points: set[str] = set()
        mapper_names: set[str] = set()
        env_key_names: set[str] = set()
        for spec in self._disks:
            if spec.mount_point in mount_points:
                raise ValidationError(
                    "Each managed disk must use a unique mount point.",
                    context={"disk": spec.name, "mount_point": spec.mount_point},
                )
            mount_points.add(spec.mount_point)

            if not self._is_encrypted(spec):
                if spec.mapper_name is not None:
                    raise ValidationError(
                        "Plain disks cannot request custom mapper names.",
                        context={"disk": spec.name, "mapper_name": spec.mapper_name},
                    )
                if spec.key_path is not None:
                    raise ValidationError(
                        "Plain disks cannot declare encryption key paths.",
                        context={"disk": spec.name, "key_path": spec.key_path},
                    )
                continue

            if spec.key_path is None and spec.key_name is not None:
                env_key_names.add(spec.key_name)

            effective_mapper = spec.mapper_name or self._generated_mapper_name(spec.name)
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

    def source_spec(self) -> SourceBuild:
        """The ``disk-setup`` source build from ``source_repo@source_branch``.

        ``mark_unpinned=False`` keeps the unpinned hook byte-identical to the
        hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="disk-encryption",
            source=GitSource(self.source_repo, self.source_branch),
            build=GoBuild(package="./cmd/disk-setup", output="disk-setup"),
            install_to="/usr/bin/disk-setup",
            mark_unpinned=False,
        )

    def _render_config(self, disks: tuple[DiskSpec, ...] | None = None) -> str:
        disk_specs = disks or tuple(self._disks)
        lines = ["disks:"]
        for spec in disk_specs:
            lines.extend(
                (
                    f"  {spec.name}:",
                    *self._strategy_lines(spec),
                    f'    format: "{spec.format_policy}"',
                    f'    mount_at: "{spec.mount_point}"',
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

    def _generated_mapper_name(self, name: str) -> str:
        return f"crypt_disk_{name}"

    def _render_init_script(self) -> str:
        lines = [f"/usr/bin/disk-setup setup {shlex.quote(self.config_path)}"]
        for spec in self._disks:
            if self._is_encrypted(spec) and spec.mapper_name:
                generated_mapper = self._generated_mapper_name(spec.name)
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

    def _validate_name(self, name: str, *, kind: str) -> None:
        if not name:
            raise ValidationError(f"{kind} names must be non-empty.")
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            raise ValidationError(
                f"Invalid {kind} name {name!r}.",
                hint="Use only letters, numbers, dot, underscore, and dash.",
            )

    def _is_encrypted(self, spec: DiskSpec) -> bool:
        return spec.key_name is not None or spec.key_path is not None
