"""Validation helpers for IR correctness."""

from __future__ import annotations

from tdx.errors import ValidationError
from tdx.ir.model import ImageIR


def validate_image_ir(ir: ImageIR) -> None:
    """Validate required structural constraints before compilation."""
    if ir.default_profile not in ir.profiles:
        raise ValidationError(
            "Default profile is missing from image IR.",
            hint="Declare the default profile in recipe state before compilation.",
            context={"profile": ir.default_profile, "operation": "validate_image_ir"},
        )
