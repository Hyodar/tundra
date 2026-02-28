"""Module protocol for reusable SDK configuration bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .devtools import Devtools
from .disk_encryption import DiskEncryption
from .init import Init
from .key_generation import KeyGeneration
from .secret_delivery import SecretDelivery
from .tdxs import Tdxs

if TYPE_CHECKING:
    from tundravm.image import Image


class Module(Protocol):
    """Two-phase module: setup() for build-time, install() for runtime.

    Modules like Tdxs, Devtools, Raiko, TaikoClient, and Nethermind
    conform to this protocol. They also provide a convenience ``apply()``
    method that calls both phases.
    """

    def setup(self, image: Image) -> None:
        """One-time build/package setup for a module."""

    def install(self, image: Image) -> None:
        """Per-image runtime configuration for a module."""


class InitModule(Protocol):
    """Single-phase module for boot-time init scripts.

    Init modules like KeyGeneration, DiskEncryption, and SecretDelivery
    use ``apply()`` to register config files and init script fragments
    that are composed into ``/usr/bin/runtime-init`` at compile time.
    """

    def apply(self, image: Image) -> None:
        """Register config and init script fragments on the image."""


__all__ = [
    "DiskEncryption",
    "Devtools",
    "Init",
    "InitModule",
    "KeyGeneration",
    "Module",
    "SecretDelivery",
    "Tdxs",
]
