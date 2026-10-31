"""Measurements are real (replayed from the artifact) or explicitly ``placeholder``, never fake."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import write_recipe_file
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
from tundravm.measure import (
    MEASURE_VERSION,
    PlaceholderMeasurementWarning,
    derive_measurements,
)
from tundravm.models import ArtifactRef, ProfileBuildResult

from .test_measure import build_uki


def _baked(tmp_path: Path, *, simulated: bool = False) -> tuple[Artifact, ...]:
    """In-process artifacts of a qemu ``default`` and a ``gcp`` variant.

    Unless *simulated*, they are marked as a real backend's, so ``measure`` reads the
    artifact instead of refusing a simulated one.
    """
    recipe = Recipe(
        "measured",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu",)), Variant("gcp", outputs=("gcp",))),
    )
    artifacts = bake(
        recipe, lock=lock(recipe), backend=Backend("inprocess"), build=tmp_path / "build"
    )
    return artifacts if simulated else tuple(replace(a, simulated=False) for a in artifacts)


def _default(tmp_path: Path) -> Artifact:
    return next(a for a in _baked(tmp_path) if a.variant == "default")


# --- measure() ---------------------------------------------------------------


def test_an_artifact_that_is_no_uki_refuses_with_hint(tmp_path: Path) -> None:
    artifact = _default(tmp_path)

    with pytest.raises(MeasurementError) as caught:
        measure(artifact, scheme="qemu")

    error = caught.value
    assert error.code == "E_MEASUREMENT"
    assert "is not a PE/EFI image" in str(error)  # the in-process boot payload is no UKI
    assert error.hint is not None and "UKI (.efi)" in error.hint


@pytest.mark.parametrize("scheme", ["qemu", "gcp", "azure", "portable"])
def test_allow_placeholder_returns_flagged_values_and_warns(tmp_path: Path, scheme: Scheme) -> None:
    artifact = _default(tmp_path)

    with pytest.warns(PlaceholderMeasurementWarning, match="not real measurements") as record:
        found = measure(artifact, scheme=scheme, allow_placeholder=True)

    assert record[0].filename == __file__
    assert found.tool == "placeholder"
    assert found.scheme == scheme
    assert found.artifact_digest == artifact.sha256
    assert found.registers or found.unverifiable

    profile = ProfileBuildResult(
        profile=artifact.variant,
        artifacts={artifact.target: ArtifactRef(target=artifact.target, path=artifact.path)},
    )
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = derive_measurements(
            backend=scheme, profile="default", profile_result=profile, allow_placeholder=True
        )
    assert dict(found.registers) == {k: tuple(v) for k, v in measurements.registers.items()}
    assert dict(found.unverifiable) == measurements.unverifiable
    assert measurements.source == "placeholder"
    assert measurements.tool_version is None
    assert measurements.artifact is None


def test_variant_measure_passes_allow_placeholder(tmp_path: Path) -> None:
    gcp = next(a for a in _baked(tmp_path, simulated=True) if a.variant == "gcp")
    assert gcp.simulated
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = measure(gcp, scheme="gcp", allow_placeholder=True)
    assert measurements.tool == "placeholder"
    with pytest.raises(MeasurementError, match="simulated"):
        measure(gcp, scheme="gcp")


def test_a_changed_artifact_is_refused_before_measuring(tmp_path: Path) -> None:
    artifact = _default(tmp_path)
    artifact.path.write_bytes(build_uki())

    with pytest.raises(Exception, match="changed since the bake"):
        measure(artifact, scheme="qemu")


# --- CLI ---------------------------------------------------------------------

RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Fragment, Package, Recipe, Variant

recipe = Recipe(
    "measured",
    items=(Package("curl"), Package("linux-image-amd64")),
    variants=(Variant("default", outputs=("qemu",)),),
)
backend = InProcessBackend()
"""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from *tmp_path* so its build dir is ``tmp_path / "build"``."""
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def _manifest(recipe: Path) -> str:
    """The build directory holding ``bake-result.json``."""
    return str(recipe.parent / "build")


def _real_manifest(recipe: Path) -> str:
    """The build directory, its manifest's ``simulated`` cleared as a real backend's bake."""
    path = Path(_manifest(recipe)) / "bake-result.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["declarative"]["simulated"] = False
    for entry in payload["variants"].values():
        entry["declarative"]["simulated"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path.parent)


def _swap_artifact(build: str, artifact: Path, content: bytes) -> None:
    """Replace *artifact* with *content* and record its new sha256 in the manifest."""
    old = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.write_bytes(content)
    path = Path(build) / "bake-result.json"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace(old, hashlib.sha256(content).hexdigest()), encoding="utf-8")


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
