"""Deployment adapter protocol and result models."""

from __future__ import annotations

from typing import Protocol

from tdx.errors import DeploymentError
from tdx.models import DeployRequest, DeployResult

from .azure import AzureDeployAdapter
from .gcp import GcpDeployAdapter
from .qemu import QemuDeployAdapter


class DeployAdapter(Protocol):
    name: str

    def deploy(self, request: DeployRequest) -> DeployResult:
        """Deploy a previously baked artifact."""


def get_adapter(target: str) -> DeployAdapter:
    if target == "qemu":
        return QemuDeployAdapter()
    if target == "azure":
        return AzureDeployAdapter()
    if target == "gcp":
        return GcpDeployAdapter()
    raise DeploymentError("Unsupported deploy target.", context={"target": target})


__all__ = [
    "AzureDeployAdapter",
    "DeployAdapter",
    "DeployRequest",
    "DeployResult",
    "GcpDeployAdapter",
    "QemuDeployAdapter",
    "get_adapter",
]
