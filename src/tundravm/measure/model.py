"""The measurement record the per-scheme derivations return."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

MeasurementBackend = Literal["qemu", "gcp", "azure", "portable"]
"""The measurement schemes: a host family, or ``portable`` (image-only registers)."""
MeasurementSource = Literal["tundravm-measure"]
"""Where measurement values came from: tundravm's own replay of the boot event log."""
MEASURE_TOOL = "tundravm-measure"
MEASURE_VERSION = "1.0+attest-12f1e29.dstack-mr-gcp-b16e08b"
"""The replay's version and the pinned flashbots tools it ports (``flake.nix`` pins)."""


@dataclass(frozen=True, slots=True)
class Measurements:
    """Accepted register values plus their provenance.

    ``registers`` maps each register to its accepted values (a hex string, or a dict
    with ``value`` and ``firmware``/``label``); ``unverifiable`` maps the registers the
    scheme cannot predict offline to the reason. ``extra`` holds scheme fields
    (``sources``, ``firmware_table``, ``vtpm_pcrs``).
    """

    backend: MeasurementBackend
    registers: dict[str, list[str | dict[str, str]]] = field(default_factory=dict)
    unverifiable: dict[str, str] = field(default_factory=dict)
    extra: dict[str, object] = field(default_factory=dict)
    source: MeasurementSource = field(kw_only=True)
    tool_version: str | None = field(default=None, kw_only=True)
    artifact: str | None = field(default=None, kw_only=True)
