"""Measurements are real (replayed from the artifact) or explicitly ``placeholder``, never fake."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import warnings
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.backends import inprocess
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, PLACEHOLDER_BANNER, doctor
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
    MEASURE_TOOL,
    MEASURE_VERSION,
    PlaceholderMeasurementWarning,
    derive_measurements,
)
from tundravm.models import ArtifactRef, ProfileBuildResult
from tundravm.recipe import load_file

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


def _bake(recipe: Path) -> int:
    return run_main("bake", str(recipe))[0]


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


def test_cli_measure_without_a_uki_fails_with_hint(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    code, out = run_main("measure", "--build", _manifest(recipe), "--allow-placeholder")
    assert code == EXIT_OK
    capsys.readouterr()

    code, out = run_main("measure", "--build", _real_manifest(recipe))

    err = capsys.readouterr().err
    assert code == EXIT_SDK_ERROR
    assert out == ""
    assert "error [E_MEASUREMENT]: the UKI is not a PE/EFI image" in err


def test_cli_allow_placeholder_table_and_banner(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code, out = run_main(
            "measure", "--build", _manifest(recipe), "--scheme", "qemu", "--allow-placeholder"
        )

    lines = out.splitlines()
    disk = recipe.parent / "build" / "default" / "disk.qcow2"
    assert code == EXIT_OK
    assert lines[:2] == ["measurements default (qemu)", f"source: placeholder ({disk})"]
    assert [line.split()[0] for line in lines[2:]] == ["RTMR1", "RTMR2", "MRTD", "RTMR0"]
    assert "unverifiable: QEMU" in lines[-1]
    err = capsys.readouterr().err
    assert PLACEHOLDER_BANNER in err
    assert err.startswith("PLACEHOLDER: not real measurements")
    assert not [w for w in caught if issubclass(w.category, PlaceholderMeasurementWarning)]


def test_cli_allow_placeholder_json_carries_source(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = recipe.read_text(encoding="utf-8")
    recipe.write_text(source.replace('outputs=("qemu",)', 'outputs=("gcp",)'), "utf-8")
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    code, out = run_main(
        "measure",
        "--build",
        _manifest(recipe),
        "--scheme",
        "gcp",
        "--format",
        "json",
        "--allow-placeholder",
    )

    payload = json.loads(out)
    assert code == EXIT_OK
    assert payload["tool"] == "placeholder"
    assert payload["scheme"] == "gcp"
    assert payload["variant"] == "default"
    assert len(payload["registers"]["MRTD"]) == 3
    assert "PLACEHOLDER" in capsys.readouterr().err


def test_cli_measure_of_a_uki_prints_source(
    recipe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(inprocess.TARGET_FILENAMES, "qemu", "linux.efi")
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()
    uki = recipe.parent / "build" / "default" / "linux.efi"
    build = _real_manifest(recipe)
    _swap_artifact(build, uki, build_uki())

    code, out = run_main("measure", "--build", build)

    lines = out.splitlines()
    assert code == EXIT_OK
    assert lines[:2] == [
        "measurements default (qemu)",
        f"source: {MEASURE_TOOL} {MEASURE_VERSION} ({uki})",
    ]
    assert [line.split()[0] for line in lines[2:]] == ["RTMR1", "RTMR2", "MRTD", "RTMR0"]
    assert "PLACEHOLDER" not in capsys.readouterr().err

    code, out = run_main("measure", "--build", build, "--format", "json")
    payload = json.loads(out)
    assert payload["tool"] == f"{MEASURE_TOOL} {MEASURE_VERSION}"
    assert payload["artifact"] == str(uki)
    assert payload["artifact_digest"] == hashlib.sha256(uki.read_bytes()).hexdigest()
    assert set(payload["unverifiable"]) == {"MRTD", "RTMR0"}


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(argv[0])


def test_doctor_lists_the_built_in_measurement() -> None:
    out = io.StringIO()
    code = doctor(None, out, runner=_probe)
    lines = out.getvalue().splitlines()

    assert code == EXIT_OK
    heading = lines.index("measurement tools:")
    assert lines[heading + 1] == f"  ok {MEASURE_TOOL} {MEASURE_VERSION} (built in)"


def test_doctor_with_recipe_needs_no_measurement_tool(recipe: Path) -> None:
    out = io.StringIO()
    code = doctor(load_file(recipe), out, runner=_probe)
    text = out.getvalue()

    assert code == EXIT_OK
    assert f"measurement tools:\n  ok {MEASURE_TOOL} " in text
    assert text.splitlines()[-1].startswith("lint: ")
