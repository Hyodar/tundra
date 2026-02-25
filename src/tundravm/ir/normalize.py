"""Normalization helpers for the IR."""

from __future__ import annotations

from tundravm.ir.model import ImageIR, ProfileIR


def ensure_default_profile(ir: ImageIR) -> ImageIR:
    """Ensure the IR always has an entry for the declared default profile."""
    if ir.default_profile not in ir.profiles:
        ir.profiles[ir.default_profile] = ProfileIR(name=ir.default_profile)
    return ir
