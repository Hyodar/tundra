"""Build backend interfaces and implementations."""

from .base import BuildBackend, MountSpec, collect_artifacts
from .inprocess import InProcessBackend
from .lima import LimaMkosiBackend
from .local_linux import LocalLinuxBackend
from .nix import NixMkosiBackend

# Backward-compat alias
LimaBackend = LimaMkosiBackend

__all__ = [
    "BuildBackend",
    "InProcessBackend",
    "LimaBackend",
    "LimaMkosiBackend",
    "LocalLinuxBackend",
    "MountSpec",
    "NixMkosiBackend",
    "collect_artifacts",
]
