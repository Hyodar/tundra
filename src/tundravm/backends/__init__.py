"""Build backend interfaces and implementations."""

from .base import BuildBackend, MountSpec, Requirement, collect_artifacts
from .inprocess import InProcessBackend
from .lima import LimaMkosiBackend
from .local_linux import LocalLinuxBackend
from .nix import NixMkosiBackend

__all__ = [
    "BuildBackend",
    "InProcessBackend",
    "LimaMkosiBackend",
    "LocalLinuxBackend",
    "MountSpec",
    "NixMkosiBackend",
    "Requirement",
    "collect_artifacts",
]
