"""Module protocol for reusable SDK configuration bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .devtools import Devtools
from .disk_encryption import DiskEncryption
from .init import Init
from .key_generation import KeyGeneration
from .secret_delivery import (
    GLOBAL_ENV_RELATIVE_PATH,
    SecretDelivery,
    SecretDeliveryValidation,
    SecretsRuntimeArtifacts,
)
from .tdxs import Tdxs

if TYPE_CHECKING:
    from tdx.image import Image


class Module(Protocol):
    def setup(self, image: Image) -> None:
        """One-time build/package setup for a module."""

    def install(self, image: Image) -> None:
        """Per-image runtime configuration for a module."""


__all__ = [
    "DiskEncryption",
    "Devtools",
    "GLOBAL_ENV_RELATIVE_PATH",
    "Init",
    "KeyGeneration",
    "Module",
    "SecretDelivery",
    "SecretDeliveryValidation",
    "SecretsRuntimeArtifacts",
    "Tdxs",
]
