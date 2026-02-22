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


@dataclass(slots=True)
class ProfileState:
    name: str
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    phases: dict[Phase, list[CommandSpec]] = field(default_factory=dict)


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
    "OutputTarget",
    "Phase",
    "ProfileBuildResult",
    "ProfileState",
    "RecipeState",
]
