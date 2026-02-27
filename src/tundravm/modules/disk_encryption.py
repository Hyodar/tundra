"""Disk encryption module."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache
from tundravm.errors import ValidationError

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
    name: str
    device: str = "/dev/vda3"
    mapper_name: str | None = None
    key_path: str | None = "/persistent/key"
    mount_point: str = "/persistent"
    key_name: str | None = "key_persistent"
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    dirs: tuple[str, ...] = DEFAULT_DISK_DIRS


@dataclass(slots=True)
class DiskEncryption:
    """Configure one or more disks via ``tundra-tools`` ``disk-setup``."""

    device: str = "/dev/vda3"
    mapper_name: str | None = None
    key_path: str | None = "/persistent/key"
    mount_point: str = "/persistent"
    disk_name: str = "disk_persistent"
    key_name: str | None = "key_persistent"
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    dirs: tuple[str, ...] = DEFAULT_DISK_DIRS
    config_path: str = DISK_ENCRYPTION_DEFAULT_CONFIG_PATH
    source_repo: str = DISK_ENCRYPTION_DEFAULT_REPO
    source_branch: str = DISK_ENCRYPTION_DEFAULT_BRANCH
    _disks: list[DiskSpec] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self._append_disk(
            DiskSpec(
                name=self.disk_name,
                device=self.device,
                mapper_name=self.mapper_name,
                key_path=self.key_path,
                mount_point=self.mount_point,
                key_name=self.key_name,
                format_policy=self.format_policy,
                dirs=self.dirs,
            )
        )

    def disk(
        self,
        name: str,
        *,
        device: str = "/dev/vda3",
        mapper_name: str | None = None,
        key_path: str | None = "/persistent/key",
        mount_point: str = "/persistent",
        key_name: str | None = "key_persistent",
        format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail",
        dirs: tuple[str, ...] = DEFAULT_DISK_DIRS,
    ) -> DiskSpec:
        """Register an additional disk definition."""
        spec = DiskSpec(
            name=name,
            device=device,
            mapper_name=mapper_name,
            key_path=key_path,
            mount_point=mount_point,
            key_name=key_name,
            format_policy=format_policy,
            dirs=dirs,
        )
        self._append_disk(spec)
        return spec

    def apply(self, image: Image) -> None:
        """Add build hook, config files, and init script to *image*."""
        self._validate()
        image.build_install(*DISK_ENCRYPTION_BUILD_PACKAGES)
        image.install("cryptsetup")

        clone_dir = Build.build_path("disk-encryption")
        chroot_dir = Build.chroot_path("disk-encryption")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("disk-encryption/build/disk-setup"),
                    dest=Build.dest_path("usr/bin/disk-setup"),
                    name="disk-setup",
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
            "-o ./build/disk-setup ./cmd/disk-setup"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))
        image.file(self.config_path, content=self._render_config())
        for spec in self._disks:
            image.file(
                self._entry_config_path(spec.name),
                content=self._render_config((spec,)),
            )
        image.add_init_script(self._render_init_script(), priority=20)

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
        for spec in self._disks:
            if spec.mount_point in mount_points:
                raise ValidationError(
                    "Each managed disk must use a unique mount point.",
                    context={"disk": spec.name, "mount_point": spec.mount_point},
                )
            mount_points.add(spec.mount_point)

            if spec.key_name is None:
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

            effective_mapper = spec.mapper_name or self._generated_mapper_name(spec.name)
            if effective_mapper in mapper_names:
                raise ValidationError(
                    "Each encrypted disk must use a unique mapper name.",
                    context={"disk": spec.name, "mapper_name": effective_mapper},
                )
            mapper_names.add(effective_mapper)

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"disk-encryption-{repo_hash}-{self.source_branch}"

    def _entry_config_path(self, name: str) -> str:
        config_dir = (
            f"{self.config_path[:-5]}.d"
            if self.config_path.endswith(".yaml")
            else f"{self.config_path}.d"
        )
        return f"{config_dir}/{name}.yaml"

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
        return "\n".join(lines) + "\n"

    def _strategy_lines(self, spec: DiskSpec) -> tuple[str, ...]:
        if spec.device:
            return (
                '    strategy: "pathglob"',
                "    strategy_config:",
                f'      pattern: "{spec.device}"',
            )
        return ('    strategy: "largest"',)

    def _generated_mapper_name(self, name: str) -> str:
        return f"crypt_disk_{name}"

    def _render_init_script(self) -> str:
        lines: list[str] = []
        for spec in self._disks:
            if spec.key_name is None:
                lines.append("unset DISK_ENCRYPTION_KEY")
            elif spec.key_path is None:
                lines.extend(
                    (
                        'if [ -z "${DISK_ENCRYPTION_KEY:-}" ]; then',
                        f'    echo "missing DISK_ENCRYPTION_KEY for disk {spec.name}" >&2',
                        "    exit 1",
                        "fi",
                    )
                )
            else:
                key_path = shlex.quote(spec.key_path)
                lines.extend(
                    (
                        f"if [ ! -f {key_path} ]; then",
                        f'    echo "missing disk key file for {spec.name}: {spec.key_path}" >&2',
                        "    exit 1",
                        "fi",
                        f'export DISK_ENCRYPTION_KEY="$(tr -d \'\\n\' < {key_path})"',
                    )
                )

            lines.append(
                f"/usr/bin/disk-setup setup {shlex.quote(self._entry_config_path(spec.name))}"
            )

            if spec.key_name is not None and spec.mapper_name:
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
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    def _validate_name(self, name: str, *, kind: str) -> None:
        if not name:
            raise ValidationError(f"{kind} names must be non-empty.")
        if ENTRY_NAME_PATTERN.fullmatch(name) is None:
            raise ValidationError(
                f"Invalid {kind} name {name!r}.",
                hint="Use only letters, numbers, dot, underscore, and dash.",
            )
