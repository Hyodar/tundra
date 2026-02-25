import json
from pathlib import Path
from typing import Any, cast

from tdx import Image
from tdx.backends import InProcessBackend


def test_bake_report_schema_contains_observability_fields(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend=InProcessBackend())
    image.output_targets("qemu")
    image.run("echo hello", phase="prepare")
    result = image.bake()

    report_path = result.profiles["default"].report_path
    assert report_path is not None
    report = _read_json(report_path)

    assert "artifact_digests" in report
    assert "lock_digest" in report
    assert "emitted_scripts" in report
    assert "logs" in report
    assert "backend" in report

    artifact_digests = cast(dict[str, str], report["artifact_digests"])
    assert "qemu" in artifact_digests
    assert len(artifact_digests["qemu"]) == 64

    emitted_scripts = cast(dict[str, str], report["emitted_scripts"])
    assert emitted_scripts
    assert all(len(checksum) == 64 for checksum in emitted_scripts.values())


def test_structured_logs_include_profile_phase_module_and_builder(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build", backend=InProcessBackend())
    image.output_targets("qemu")
    image.bake()

    records = image.logger.records_for_profile("default")
    assert records
    for record in records:
        assert record["profile"] == "default"
        assert "phase" in record
        assert "module" in record
        assert "builder" in record


def _read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)
