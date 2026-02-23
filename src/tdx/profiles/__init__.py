"""Platform profile helpers for the TDX VM SDK."""

from __future__ import annotations

from .azure import apply_azure_profile
from .devtools import apply_devtools_profile
from .gcp import apply_gcp_profile

__all__ = [
    "apply_azure_profile",
    "apply_devtools_profile",
    "apply_gcp_profile",
]
