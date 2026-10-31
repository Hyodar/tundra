"""Measurements are replayed from the artifact, never derived from its digest."""

from __future__ import annotations

import io
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm.cli import doctor
from tundravm.declarative import (
    Artifact,
    Backend,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
    measure,
)
from tundravm.declarative.lifecycle import Scheme
from tundravm.errors import MeasurementError
from tundravm.measure import MEASURE_VERSION


def _baked(tmp_path: Path) -> tuple[Artifact, ...]:
    """The artifacts of a qemu ``default`` and a ``gcp`` variant, built against the fake
    mkosi (placeholder files, no UKI)."""
    recipe = Recipe(
        "measured",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
    )
    return bake(recipe, lock=lock(recipe), backend=Backend("local"), build=tmp_path / "build")


def _default(tmp_path: Path) -> Artifact:
    return next(a for a in _baked(tmp_path) if a.variant == "default")


# --- measure() ---------------------------------------------------------------


def test_an_artifact_that_is_no_uki_refuses_with_hint(tmp_path: Path) -> None:
    artifact = _default(tmp_path)

    with pytest.raises(MeasurementError) as caught:
        measure(artifact, scheme="qemu")

    error = caught.value
    assert error.code == "E_MEASUREMENT"
    assert "is not a PE/EFI image" in str(error)  # the fake build's boot payload is no UKI
    assert error.hint is not None and "UKI (.efi)" in error.hint


@pytest.mark.parametrize("scheme", ["gcp", "azure", "portable"])
def test_no_scheme_measures_what_is_no_uki(tmp_path: Path, scheme: Scheme) -> None:
    gcp = next(a for a in _baked(tmp_path) if a.variant == "gcp")
    with pytest.raises(MeasurementError):
        measure(gcp, scheme=scheme)


# --- CLI ---------------------------------------------------------------------


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(argv[0])


def test_doctor_lists_the_built_in_measurement() -> None:
    out = io.StringIO()
    doctor(out, runner=_probe)  # build rows are blocked without the probed tools
    lines = out.getvalue().splitlines()

    assert f"verify:qemu      ready (built-in TDX replay {MEASURE_VERSION})" in lines


def test_doctor_for_gcp_needs_no_measurement_tool() -> None:
    out = io.StringIO()
    doctor(out, area="gcp", runner=_probe)
    assert "verify:gcp       ready (built-in replay" in out.getvalue()
