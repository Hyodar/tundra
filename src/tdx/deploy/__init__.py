"""Deployment adapter protocol and result models."""

from __future__ import annotations

from typing import Protocol

from tdx.models import DeployRequest, DeployResult


class DeployAdapter(Protocol):
    name: str

    def deploy(self, request: DeployRequest) -> DeployResult:
        """Deploy a previously baked artifact."""


__all__ = ["DeployAdapter", "DeployRequest", "DeployResult"]
