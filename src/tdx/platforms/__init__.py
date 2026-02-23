"""Platform profiles for the TDX VM SDK."""

from __future__ import annotations

from .azure import AzurePlatform
from .gcp import GcpPlatform

__all__ = [
    "AzurePlatform",
    "GcpPlatform",
]
