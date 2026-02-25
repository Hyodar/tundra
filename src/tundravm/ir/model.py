"""IR dataclasses shared by compiler and validators."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from tundravm.models import Arch, Phase


@dataclass(frozen=True, slots=True)
class Command:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    shell: bool = False


@dataclass(slots=True)
class ProfileIR:
    name: str
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    phases: dict[Phase, list[Command]] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    services: list[str] = field(default_factory=list)
    users: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    builds: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImageIR:
    base: str
    arch: Arch
    default_profile: str
    profiles: dict[str, ProfileIR] = field(default_factory=dict)
