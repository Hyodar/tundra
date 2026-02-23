"""Module protocol for reusable SDK configuration bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .devtools import Devtools
from .disk_encryption import DiskEncryption
from .init import (
    GLOBAL_ENV_RELATIVE_PATH,
    DiskEncryptionConfig,
    HttpPostDeliveryConfig,
    HttpPostSecretDelivery,
    Init,
    InitPhase,
    InitPhaseSpec,
    SecretDeliveryValidation,
    SecretsRuntimeArtifacts,
    SshKeyDeliveryConfig,
)
from .key_generation import KeyGeneration
from .nethermind import Nethermind
from .raiko import Raiko
from .secret_delivery import SecretDelivery
from .taiko_client import TaikoClient
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
    "DiskEncryptionConfig",
    "Devtools",
    "GLOBAL_ENV_RELATIVE_PATH",
    "HttpPostDeliveryConfig",
    "HttpPostSecretDelivery",
    "Init",
    "InitPhase",
    "InitPhaseSpec",
    "KeyGeneration",
    "Module",
    "Nethermind",
    "Raiko",
    "SecretDelivery",
    "SecretDeliveryValidation",
    "SecretsRuntimeArtifacts",
    "SshKeyDeliveryConfig",
    "TaikoClient",
    "Tdxs",
]
