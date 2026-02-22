"""Module protocol for reusable SDK configuration bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .init import (
    GLOBAL_ENV_RELATIVE_PATH,
    HttpPostDeliveryConfig,
    HttpPostSecretDelivery,
    Init,
    SecretDeliveryValidation,
    SecretsRuntimeArtifacts,
)

if TYPE_CHECKING:
    from tdx.image import Image


class Module(Protocol):
    def setup(self, image: Image) -> None:
        """One-time build/package setup for a module."""

    def install(self, image: Image) -> None:
        """Per-image runtime configuration for a module."""


__all__ = [
    "HttpPostDeliveryConfig",
    "HttpPostSecretDelivery",
    "Init",
    "Module",
    "GLOBAL_ENV_RELATIVE_PATH",
    "SecretDeliveryValidation",
    "SecretsRuntimeArtifacts",
]
