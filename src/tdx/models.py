"""Core typed dataclasses for recipe state and build/deploy requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Arch = Literal["x86_64", "aarch64"]
OutputTarget = Literal["qemu", "azure", "gcp"]
SecurityProfile = Literal["strict", "default", "none"]
RestartPolicy = Literal["always", "on-failure", "no"]

DEFAULT_DEBLOAT_PATHS_REMOVE = (
    "/etc/machine-id",
    "/etc/ssh/ssh_host_*_key*",
    "/usr/lib/modules",
    "/usr/lib/pcrlock.d",
    "/usr/lib/systemd/catalog",
    "/usr/lib/systemd/network",
    "/usr/lib/systemd/user",
    "/usr/lib/systemd/user-generators",
    "/usr/lib/tmpfiles.d",
    "/usr/lib/udev/hwdb.bin",
    "/usr/lib/udev/hwdb.d",
    "/usr/share/bash-completion",
    "/usr/share/bug",
    "/usr/share/debconf",
    "/usr/share/doc",
    "/usr/share/gcc",
    "/usr/share/gdb",
    "/usr/share/info",
    "/usr/share/initramfs-tools",
    "/usr/share/lintian",
    "/usr/share/locale",
    "/usr/share/man",
    "/usr/share/menu",
    "/usr/share/mime",
    "/usr/share/perl5/debconf",
    "/usr/share/polkit-1",
    "/usr/share/systemd",
    "/usr/share/zsh",
    "/etc/credstore",
    "/etc/systemd/network",
)

DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP = (
    "basic.target",
    "local-fs-pre.target",
    "local-fs.target",
    "minimal.target",
    "network-online.target",
    "slices.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald-dev-log.socket",
    "systemd-journald.service",
    "systemd-journald.socket",
    "systemd-remount-fs.service",
    "systemd-sysctl.service",
)

DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP = (
    "journalctl",
    "systemctl",
    "systemd",
    "systemd-tty-ask-password-agent",
)

# Legacy aliases for backward compatibility with existing tests
DEFAULT_DEBLOAT_REMOVE = (
    "cloud-init",
    "man-db",
    "info",
)
DEFAULT_DEBLOAT_MASK = (
    "debug-shell.service",
    "systemd-networkd-wait-online.service",
)

Phase = Literal[
    "sync",
    "skeleton",
    "prepare",
    "build",
    "extra",
    "postinst",
    "finalize",
    "postoutput",
    "clean",
    "repart",
    "boot",
]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    shell: bool = False


@dataclass(frozen=True, slots=True)
class RepositorySpec:
    name: str
    url: str
    suite: str | None = None
    components: tuple[str, ...] = ()
    keyring: str | None = None
    priority: int = 100


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    content: str
    mode: str = "0644"


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    path: str
    template: str
    variables: Mapping[str, str] = field(default_factory=dict)
    rendered: str = ""
    mode: str = "0644"


@dataclass(frozen=True, slots=True)
class UserSpec:
    name: str
    system: bool = False
    home: str | None = None
    shell: str = "/usr/sbin/nologin"
    uid: int | None = None
    gid: int | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    exec: tuple[str, ...] = ()
    user: str | None = None
    after: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    restart: RestartPolicy = "no"
    enabled: bool = True
    extra_unit: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    security_profile: SecurityProfile = "default"


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    name: str
    size: str
    mount: str
    fs: str = "ext4"


@dataclass(frozen=True, slots=True)
class HookSpec:
    phase: Phase
    command: CommandSpec
    after_phase: Phase | None = None


@dataclass(frozen=True, slots=True)
class SecretSchema:
    kind: Literal["string", "json"] = "string"
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecretTarget:
    kind: Literal["file", "env"]
    location: str
    mode: str = "0400"
    scope: Literal["service", "global"] = "service"
    owner: str | None = None

    @classmethod
    def file(
        cls,
        path: str,
        *,
        mode: str = "0400",
        owner: str | None = None,
    ) -> SecretTarget:
        return cls(kind="file", location=path, mode=mode, scope="service", owner=owner)

    @classmethod
    def env(cls, name: str, *, scope: Literal["service", "global"] = "service") -> SecretTarget:
        return cls(kind="env", location=name, mode="0400", scope=scope)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str
    required: bool = True
    schema: SecretSchema | None = None
    targets: tuple[SecretTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class DebloatConfig:
    enabled: bool = True
    paths_remove: tuple[str, ...] = DEFAULT_DEBLOAT_PATHS_REMOVE
    paths_skip: tuple[str, ...] = ()
    paths_remove_extra: tuple[str, ...] = ()
    paths_skip_for_profiles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    systemd_minimize: bool = True
    systemd_units_keep: tuple[str, ...] = DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP
    systemd_units_keep_extra: tuple[str, ...] = ()
    systemd_bins_keep: tuple[str, ...] = DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP
    clean_var_dirs: tuple[str, ...] = ("/var/log", "/var/cache")

    @property
    def effective_paths_remove(self) -> tuple[str, ...]:
        """Paths to remove = default + extra - skipped - profile-conditional."""
        skip_set = set(self.paths_skip)
        # Also exclude paths that are conditionally skipped for profiles
        for _profile, paths in self.paths_skip_for_profiles:
            skip_set.update(paths)
        combined = list(self.paths_remove) + list(self.paths_remove_extra)
        return tuple(sorted(set(p for p in combined if p not in skip_set)))

    @property
    def profile_conditional_paths(self) -> dict[str, tuple[str, ...]]:
        """Paths that should only be removed when a specific profile is NOT active."""
        result: dict[str, list[str]] = {}
        all_paths = set(self.paths_remove) | set(self.paths_remove_extra)
        for profile_name, paths in self.paths_skip_for_profiles:
            for p in paths:
                if p in all_paths:
                    result.setdefault(profile_name, []).append(p)
        return {k: tuple(sorted(v)) for k, v in result.items()}

    @property
    def effective_units_keep(self) -> tuple[str, ...]:
        """Units to keep = default + extra."""
        return tuple(sorted(set(self.systemd_units_keep) | set(self.systemd_units_keep_extra)))


@dataclass(frozen=True, slots=True)
class Kernel:
    version: str | None = None
    config_file: str | Path | None = None
    cmdline: str | None = None
    tdx: bool = False
    source_repo: str = "https://github.com/gregkh/linux"

    @classmethod
    def generic(cls, version: str, *, cmdline: str | None = None) -> Kernel:
        return cls(version=version, cmdline=cmdline)

    @classmethod
    def from_config(cls, config_file: str) -> Kernel:
        return cls(config_file=config_file)

    @classmethod
    def tdx_kernel(
        cls,
        version: str,
        *,
        cmdline: str | None = None,
        config_file: str | Path | None = None,
        source_repo: str = "https://github.com/gregkh/linux",
    ) -> Kernel:
        return cls(
            version=version,
            tdx=True,
            cmdline=cmdline,
            config_file=config_file,
            source_repo=source_repo,
        )


@dataclass(slots=True)
class ProfileState:
    name: str
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    build_sources: list[tuple[str, str]] = field(default_factory=list)
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    phases: dict[Phase, list[CommandSpec]] = field(default_factory=dict)
    repositories: list[RepositorySpec] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    skeleton_files: list[FileEntry] = field(default_factory=list)
    templates: list[TemplateEntry] = field(default_factory=list)
    users: list[UserSpec] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    partitions: list[PartitionSpec] = field(default_factory=list)
    hooks: list[HookSpec] = field(default_factory=list)
    secrets: list[SecretSpec] = field(default_factory=list)
    debloat: DebloatConfig = field(default_factory=DebloatConfig)
    # Legacy fields kept for backward compat during transition
    debloat_enabled: bool = True
    debloat_remove: tuple[str, ...] = DEFAULT_DEBLOAT_REMOVE
    debloat_mask: tuple[str, ...] = DEFAULT_DEBLOAT_MASK


@dataclass(slots=True)
class RecipeState:
    base: str
    arch: Arch
    default_profile: str
    profiles: dict[str, ProfileState]

    @classmethod
    def initialize(cls, *, base: str, arch: Arch, default_profile: str) -> RecipeState:
        default = ProfileState(name=default_profile)
        return cls(
            base=base,
            arch=arch,
            default_profile=default_profile,
            profiles={default_profile: default},
        )

    def ensure_profile(self, name: str) -> ProfileState:
        if name not in self.profiles:
            self.profiles[name] = ProfileState(name=name)
        return self.profiles[name]


@dataclass(frozen=True, slots=True)
class BakeRequest:
    profile: str
    build_dir: Path
    emit_dir: Path
    output_targets: tuple[OutputTarget, ...] = ("qemu",)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    target: OutputTarget
    path: Path
    digest: str | None = None


@dataclass(slots=True)
class ProfileBuildResult:
    profile: str
    artifacts: dict[OutputTarget, ArtifactRef] = field(default_factory=dict)
    report_path: Path | None = None


@dataclass(slots=True)
class BakeResult:
    profiles: dict[str, ProfileBuildResult] = field(default_factory=dict)

    def artifact_for(self, *, profile: str, target: OutputTarget) -> ArtifactRef | None:
        profile_result = self.profiles.get(profile)
        if profile_result is None:
            return None
        return profile_result.artifacts.get(target)


@dataclass(frozen=True, slots=True)
class DeployRequest:
    profile: str
    target: OutputTarget
    artifact_path: Path
    parameters: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeployResult:
    target: OutputTarget
    deployment_id: str
    endpoint: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


__all__ = [
    "Arch",
    "ArtifactRef",
    "BakeRequest",
    "BakeResult",
    "CommandSpec",
    "DebloatConfig",
    "DEFAULT_DEBLOAT_MASK",
    "DEFAULT_DEBLOAT_PATHS_REMOVE",
    "DEFAULT_DEBLOAT_REMOVE",
    "DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP",
    "DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP",
    "DeployRequest",
    "DeployResult",
    "FileEntry",
    "HookSpec",
    "Kernel",
    "OutputTarget",
    "PartitionSpec",
    "Phase",
    "ProfileBuildResult",
    "ProfileState",
    "RepositorySpec",
    "RecipeState",
    "RestartPolicy",
    "SecretSchema",
    "SecretSpec",
    "SecretTarget",
    "SecurityProfile",
    "ServiceSpec",
    "TemplateEntry",
    "UserSpec",
]
