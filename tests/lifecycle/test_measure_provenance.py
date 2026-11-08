"""Measurements are replayed from the artifact, never derived from its digest."""

from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence

from tundravm.cli import doctor

# --- CLI ---------------------------------------------------------------------


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(argv[0])


def test_doctor_lists_the_built_in_measurement() -> None:
    out = io.StringIO()
    doctor(out, runner=_probe)  # build rows are blocked without the probed tools
    lines = out.getvalue().splitlines()

    assert (
        "verify:qemu      ready (built in: each build predicts the registers a QEMU TD reports)"
        in lines
    )


def test_doctor_for_gcp_needs_no_measurement_tool() -> None:
    out = io.StringIO()
    doctor(out, area="gcp", runner=_probe)
    assert "verify:gcp       ready (built-in replay" in out.getvalue()
