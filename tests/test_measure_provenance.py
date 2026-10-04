"""Measurements are real (tool-produced) or explicitly ``placeholder``, never silently fake."""

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

from tundravm.backends import inprocess
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, PLACEHOLDER_BANNER, doctor, main
from tundravm.declarative import (
    Artifact,
    Backend,
    Fragment,
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
    MEASUREMENTS_SCHEMA_VERSION,
    Measurements,
    PlaceholderMeasurementWarning,
    derive_measurements,
    rtmr,
)
from tundravm.models import ArtifactRef, ProfileBuildResult
from tundravm.recipe import load_file

RTMR0 = "0a" * 48
RTMR1 = "1b" * 48


def _no_tools(name: str) -> str | None:
    return None


def _only(tool: str) -> rtmr.ToolLocator:
    def locate(name: str) -> str | None:
        return f"/opt/bin/{tool}" if name == tool else None

    return locate


def _completed(
    argv: Sequence[str], code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), code, stdout, stderr)


def _measured_boot_writing(payload: str, version: str = "measured-boot v1.3.0") -> rtmr.ToolRunner:
    """A fake ``measured-boot`` writing *payload* to its output-file argument."""

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return _completed(argv, stdout=f"{version}\n")
        Path(argv[2]).write_text(payload, encoding="utf-8")
        return _completed(argv)

    return run


def _dstack_printing(stdout: str, code: int = 0) -> rtmr.ToolRunner:
    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            return _completed(argv, stdout="dstack-mr 0.5.1\n")
        return _completed(argv, code, stdout, "bad UKI" if code else "")

    return run


def _baked(tmp_path: Path, *, simulated: bool = False) -> tuple[Artifact, ...]:
    """In-process artifacts of a qemu ``default`` and a ``gcp`` variant.

    Unless *simulated*, they are marked as a real backend's, so ``measure`` reaches
    the tool lookup instead of refusing a simulated artifact.
    """
    recipe = Recipe(
        "measured",
        Fragment("measured", items=(Package("curl"),)),
        variants=(Variant("default", target="qemu"), Variant("gcp", target="gcp")),
    )
    artifacts = bake(
        recipe, locked=lock(recipe), backend=Backend("inprocess"), out=tmp_path / "build"
    )
    return artifacts if simulated else tuple(replace(a, simulated=False) for a in artifacts)


def _default(tmp_path: Path) -> Artifact:
    return next(a for a in _baked(tmp_path) if a.variant == "default")


@pytest.fixture
def no_tools_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tundravm.measure.rtmr.shutil.which", _no_tools)


@pytest.fixture
def uki(tmp_path: Path) -> Path:
    path = tmp_path / "linux.efi"
    path.write_bytes(b"uki")
    return path


def _derive(
    uki: Path,
    *,
    tool_locator: rtmr.ToolLocator,
    runner: rtmr.ToolRunner,
    allow_placeholder: bool = False,
) -> Measurements:
    return rtmr.derive(
        "default",
        {str(uki): "deadbeef"},
        (uki,),
        allow_placeholder=allow_placeholder,
        tool_locator=tool_locator,
        runner=runner,
    )


# --- measure() ---------------------------------------------------------------


@pytest.mark.usefixtures("no_tools_on_path")
def test_no_tool_refuses_with_hint(tmp_path: Path) -> None:
    artifact = _default(tmp_path)

    with pytest.raises(MeasurementError) as caught:
        measure(artifact, scheme="rtmr")

    error = caught.value
    assert error.code == "E_MEASUREMENT"
    assert str(error).splitlines()[0] == "No measurement tool found for rtmr measurements."
    assert error.hint is not None
    assert "measured-boot or dstack-mr" in error.hint
    assert "on PATH" in error.hint
    assert "allow_placeholder=True" in error.hint


@pytest.mark.parametrize("scheme", ["azure", "gcp"])
def test_cloud_backends_are_placeholder_only(tmp_path: Path, scheme: Scheme) -> None:
    artifact = _default(tmp_path)

    with pytest.raises(MeasurementError) as caught:
        measure(artifact, scheme=scheme)

    error = caught.value
    assert str(error).startswith(f"No measurement tool found for {scheme} measurements.")
    assert error.hint is not None
    assert "is a placeholder; use the cloud's attestation report" in error.hint


@pytest.mark.usefixtures("no_tools_on_path")
@pytest.mark.parametrize("scheme", ["rtmr", "azure", "gcp"])
def test_allow_placeholder_returns_flagged_values_and_warns(tmp_path: Path, scheme: Scheme) -> None:
    artifact = _default(tmp_path)

    with pytest.warns(PlaceholderMeasurementWarning, match="not real measurements") as record:
        found = measure(artifact, scheme=scheme, allow_placeholder=True)

    assert record[0].filename == __file__
    assert found.tool == "placeholder"
    assert found.scheme == scheme
    assert found.artifact_digest == artifact.sha256
    assert found.values

    profile = ProfileBuildResult(
        profile=artifact.variant,
        artifacts={artifact.target: ArtifactRef(target=artifact.target, path=artifact.path)},
    )
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = derive_measurements(
            backend=scheme, profile="default", profile_result=profile, allow_placeholder=True
        )
    assert dict(found.values) == measurements.values
    assert measurements.source == "placeholder"
    assert measurements.is_placeholder
    assert measurements.tool_version is None
    assert measurements.artifact is None
    payload = measurements.to_dict()
    assert payload["schema_version"] == MEASUREMENTS_SCHEMA_VERSION == 2
    assert payload["source"] == "placeholder"
    assert payload["tool_version"] is None
    assert payload["artifact"] is None
    assert json.loads(measurements.to_json())["source"] == "placeholder"


def test_variant_measure_passes_allow_placeholder(tmp_path: Path) -> None:
    gcp = next(a for a in _baked(tmp_path, simulated=True) if a.variant == "gcp")
    assert gcp.simulated
    with pytest.warns(PlaceholderMeasurementWarning):
        measurements = measure(gcp, scheme="gcp", allow_placeholder=True)
    assert measurements.tool == "placeholder"
    with pytest.raises(MeasurementError, match="simulated"):
        measure(gcp, scheme="gcp")


# --- rtmr.derive with injected tools -----------------------------------------


def test_measured_boot_values_carry_provenance(uki: Path) -> None:
    runner = _measured_boot_writing(
        json.dumps({"rtmr": {"0": {"expected": RTMR0}, "1": {"expected": RTMR1.upper()}}})
    )

    measurements = _derive(uki, tool_locator=_only("measured-boot"), runner=runner)

    assert measurements.source == "measured-boot"
    assert measurements.tool_version == "v1.3.0"
    assert measurements.artifact == str(uki)
    assert measurements.values == {"RTMR0": RTMR0, "RTMR1": RTMR1}
    assert measurements.to_dict()["tool_version"] == "v1.3.0"


def test_dstack_mr_json_keys_are_normalized(uki: Path) -> None:
    stdout = json.dumps({"mrtd": "ff" * 48, "rtmr0": RTMR0, "RTMR1": RTMR1})

    measurements = _derive(uki, tool_locator=_only("dstack-mr"), runner=_dstack_printing(stdout))

    assert measurements.source == "dstack-mr"
    assert measurements.tool_version == "0.5.1"
    assert measurements.values == {"RTMR0": RTMR0, "RTMR1": RTMR1}


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (json.dumps({"rtmr": {"0": {"expected": "aa"}}}), "invalid RTMR0 value"),
        (json.dumps({"rtmr": {"0": {"expected": "zz" * 48}}}), "invalid RTMR0 value"),
        (json.dumps({"rtmr": {"0": {"expected": RTMR0 + "00"}}}), "invalid RTMR0 value"),
        ("not json", "is not JSON"),
        (json.dumps({"registers": {}}), 'no "rtmr" object'),
        (json.dumps({"rtmr": {}}), "no RTMR values"),
        (json.dumps({"rtmr": {"x": {"expected": RTMR0}}}), "unexpected RTMR index"),
    ],
)
def test_invalid_tool_output_is_rejected(uki: Path, payload: str, match: str) -> None:
    with pytest.raises(MeasurementError, match=match) as caught:
        _derive(
            uki,
            tool_locator=_only("measured-boot"),
            runner=_measured_boot_writing(payload),
            allow_placeholder=True,
        )
    assert caught.value.code == "E_MEASUREMENT"
    assert caught.value.context["tool"] == "measured-boot"


def test_invalid_value_error_explains_sha384(uki: Path) -> None:
    with pytest.raises(MeasurementError) as caught:
        _derive(uki, tool_locator=_only("dstack-mr"), runner=_dstack_printing('{"RTMR0": "ab"}'))
    assert caught.value.hint is not None
    assert "96 hex characters" in caught.value.hint
    assert caught.value.context["value"] == "ab"


def test_tool_failure_refuses_and_lists_failures(uki: Path) -> None:
    runner = _dstack_printing("", code=3)

    with pytest.raises(MeasurementError, match="dstack-mr could not measure") as caught:
        _derive(uki, tool_locator=_only("dstack-mr"), runner=runner)

    assert "exit 3: bad UKI" in caught.value.context["failures"]

    with pytest.warns(PlaceholderMeasurementWarning):
        fallback = _derive(
            uki, tool_locator=_only("dstack-mr"), runner=runner, allow_placeholder=True
        )
    assert fallback.source == "placeholder"


def test_tool_that_cannot_start_is_a_failure_not_a_crash(uki: Path) -> None:
    def broken(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise PermissionError(13, "Permission denied", argv[0])

    with pytest.raises(MeasurementError, match="measured-boot could not measure") as caught:
        _derive(uki, tool_locator=_only("measured-boot"), runner=broken)
    assert "Permission denied" in caught.value.context["failures"]


def test_tool_without_measurable_artifact_refuses(tmp_path: Path) -> None:
    disk = tmp_path / "disk.qcow2"
    disk.write_bytes(b"qcow2")

    def never(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"unexpected run {argv}")

    with pytest.raises(MeasurementError, match="measured-boot could not measure") as caught:
        rtmr.derive(
            "default", {str(disk): "d"}, (disk,), tool_locator=_only("measured-boot"), runner=never
        )
    assert caught.value.hint is not None
    assert ".efi" in caught.value.hint


def test_requirements_are_optional_tools() -> None:
    tools = {req.tool: req for req in rtmr.requirements()}
    assert set(tools) == {"measured-boot", "dstack-mr"}
    assert all(req.optional for req in tools.values())
    assert tools["dstack-mr"].probe == ("dstack-mr", "--version")


# --- CLI ---------------------------------------------------------------------

RECIPE = """
from tundravm.backends.inprocess import InProcessBackend
from tundravm.declarative import Fragment, Package, Recipe, Variant

recipe = Recipe(
    "measured",
    Fragment("measured", items=(Package("curl"),)),
    variants=(Variant("default", target="qemu"),),
)
backend = InProcessBackend()
"""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from its directory: the CLI's ``build/`` is ``tmp_path/build``."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "recipe.py"
    path.write_text(RECIPE, encoding="utf-8")
    return path


def _bake(recipe: Path) -> int:
    return _run("bake", str(recipe))[0]


def _run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def _manifest(recipe: Path) -> str:
    return str(recipe.parent / "build" / "bake-result.json")


def _real_manifest(recipe: Path) -> str:
    """The manifest with ``simulated`` cleared, as a real backend's bake records it."""
    path = Path(_manifest(recipe))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["declarative"]["simulated"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


@pytest.mark.usefixtures("no_tools_on_path")
def test_cli_measure_without_tool_fails_with_hint(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    code, out = _run("measure", _manifest(recipe), "--scheme", "rtmr", "--allow-placeholder")
    assert code == EXIT_OK
    capsys.readouterr()

    code, out = _run("measure", _real_manifest(recipe), "--scheme", "rtmr")

    err = capsys.readouterr().err
    assert code == EXIT_SDK_ERROR
    assert out == ""
    assert "error [E_MEASUREMENT]: No measurement tool found for rtmr measurements." in err
    assert "--allow-placeholder" in err


@pytest.mark.usefixtures("no_tools_on_path")
def test_cli_allow_placeholder_table_and_banner(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        code, out = _run("measure", _manifest(recipe), "--scheme", "rtmr", "--allow-placeholder")

    lines = out.splitlines()
    disk = recipe.parent / "build" / "default" / "disk.qcow2"
    assert code == EXIT_OK
    assert lines[:2] == ["measurements default (rtmr)", f"source: placeholder ({disk})"]
    assert [line.split()[0] for line in lines[2:]] == ["RTMR0", "RTMR1", "RTMR2"]
    err = capsys.readouterr().err
    assert PLACEHOLDER_BANNER in err
    assert err.startswith("PLACEHOLDER: not real measurements")
    assert not [w for w in caught if issubclass(w.category, PlaceholderMeasurementWarning)]


@pytest.mark.usefixtures("no_tools_on_path")
def test_cli_allow_placeholder_json_carries_source(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = recipe.read_text(encoding="utf-8")
    recipe.write_text(source.replace('target="qemu"', 'target="gcp"'), "utf-8")
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()

    code, out = _run(
        "measure", _manifest(recipe), "--scheme", "gcp", "--json", "--allow-placeholder"
    )

    payload = json.loads(out)
    assert code == EXIT_OK
    assert payload["tool"] == "placeholder"
    assert payload["scheme"] == "gcp"
    assert payload["variant"] == "default"
    assert "PLACEHOLDER" in capsys.readouterr().err


def test_cli_measure_with_tool_prints_source(
    recipe: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(inprocess.TARGET_FILENAMES, "qemu", "linux.efi")
    assert _bake(recipe) == EXIT_OK
    capsys.readouterr()
    monkeypatch.setattr("tundravm.measure.rtmr.shutil.which", _only("measured-boot"))
    monkeypatch.setattr(
        "tundravm.measure.rtmr.run_tool",
        _measured_boot_writing(json.dumps({"rtmr": {"0": {"expected": RTMR0}}})),
    )

    code, out = _run("measure", _real_manifest(recipe), "--scheme", "rtmr")

    uki = recipe.parent / "build" / "default" / "linux.efi"
    assert code == EXIT_OK
    assert out.splitlines() == [
        "measurements default (rtmr)",
        f"source: measured-boot v1.3.0 ({uki})",
        f"  RTMR0  {RTMR0}",
    ]
    assert "PLACEHOLDER" not in capsys.readouterr().err

    code, out = _run("measure", _real_manifest(recipe), "--scheme", "rtmr", "--json")
    payload = json.loads(out)
    assert payload["tool"] == "measured-boot v1.3.0"
    assert payload["artifact"] == str(uki)
    assert payload["artifact_digest"] == hashlib.sha256(uki.read_bytes()).hexdigest()


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    if argv[0] == "measured-boot":
        return _completed(argv, stdout="measured-boot v1.3.0\n")
    raise FileNotFoundError(argv[0])


def test_doctor_lists_measurement_tools() -> None:
    out = io.StringIO()
    code = doctor(None, out, runner=_probe)
    lines = out.getvalue().splitlines()

    assert code == EXIT_OK
    heading = lines.index("measurement tools:")
    assert lines[heading + 1] == "  ok measured-boot measured-boot v1.3.0"
    assert lines[heading + 2].startswith("  missing (optional) dstack-mr — ")
    assert "measured-boot or dstack-mr" in lines[heading + 2]


def test_doctor_with_recipe_ignores_missing_measurement_tools(recipe: Path) -> None:
    def nothing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    out = io.StringIO()
    code = doctor(load_file(recipe), out, runner=nothing)
    text = out.getvalue()

    assert code == EXIT_OK
    assert "measurement tools:\n  missing (optional) measured-boot — " in text
    assert text.splitlines()[-1].startswith("lint: ")
