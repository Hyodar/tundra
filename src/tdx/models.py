"""Core typed dataclasses for recipe state and build/deploy requests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Arch = Literal["x86_64", "aarch64"]
OutputTarget = Literal["qemu", "azure", "gcp"]
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
    uid: int | None = None
    gid: int | None = None
    shell: str = "/usr/sbin/nologin"


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    enabled: bool = True
    wants: tuple[str, ...] = ()


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

    @classmethod
    def file(cls, path: str, *, mode: str = "0400") -> SecretTarget:
        return cls(kind="file", location=path, mode=mode, scope="service")

    @classmethod
    def env(cls, name: str, *, scope: Literal["service", "global"] = "service") -> SecretTarget:
        return cls(kind="env", location=name, mode="0400", scope=scope)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str
    required: bool = True
    schema: SecretSchema | None = None
    targets: tuple[SecretTarget, ...] = ()


@dataclass(slots=True)
class ProfileState:
    name: str
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    phases: dict[Phase, list[CommandSpec]] = field(default_factory=dict)
    repositories: list[RepositorySpec] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    templates: list[TemplateEntry] = field(default_factory=list)
    users: list[UserSpec] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    partitions: list[PartitionSpec] = field(default_factory=list)
    hooks: list[HookSpec] = field(default_factory=list)
    secrets: list[SecretSpec] = field(default_factory=list)


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
    "DeployRequest",
    "DeployResult",
    "FileEntry",
    "HookSpec",
    "OutputTarget",
    "PartitionSpec",
    "Phase",
    "ProfileBuildResult",
    "ProfileState",
    "RepositorySpec",
    "RecipeState",
    "SecretSchema",
    "SecretSpec",
    "SecretTarget",
    "ServiceSpec",
    "TemplateEntry",
    "UserSpec",
]
