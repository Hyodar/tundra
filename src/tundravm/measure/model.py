"""The measurement record the per-scheme derivations return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MeasurementBackend = Literal["rtmr", "azure", "gcp"]
MeasurementSource = Literal["measured-boot", "dstack-mr", "placeholder"]
"""Where measurement values came from; ``placeholder`` values are not real measurements."""


class PlaceholderMeasurementWarning(UserWarning):
    """``measure(..., allow_placeholder=True)`` returned values no tool measured."""


@dataclass(frozen=True, slots=True)
class Measurements:
    """Measurement values plus their provenance.

    ``source`` names the tool that measured ``artifact`` (with ``tool_version``
    when the tool reports one), or ``"placeholder"`` for values derived from
    artifact digests that no attestation will ever reproduce.
    """

    backend: MeasurementBackend
    values: dict[str, str] = field(default_factory=dict)
    source: MeasurementSource = field(kw_only=True)
    tool_version: str | None = field(default=None, kw_only=True)
    artifact: str | None = field(default=None, kw_only=True)
