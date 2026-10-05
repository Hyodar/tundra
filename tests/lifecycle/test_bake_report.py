import json
from pathlib import Path
from typing import Any, cast

from tundravm.declarative import Backend, Fragment, Hook, Package, Recipe, bake, lock
from tundravm.models import BakeResult


def _bake(tmp_path: Path, *items: Hook) -> Path:
    """Bake a qemu recipe in-process; return the default variant's ``report.json``."""
    recipe = Recipe("report", Fragment("report", items=(Package("linux-image-amd64"), *items)))
    out = tmp_path / "build"
    bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), out=out)
    report_path = BakeResult.load(out).profiles["default"].report_path
    assert report_path is not None
    return report_path


def test_bake_report_schema_contains_observability_fields(tmp_path: Path) -> None:
    report = _read_json(_bake(tmp_path, Hook("hello", "prepare", "echo hello")))

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
    records = cast(list[dict[str, Any]], _read_json(_bake(tmp_path))["logs"])

    assert records
    for record in records:
        assert record["profile"] == "default"
        assert "phase" in record
        assert "module" in record
        assert "builder" in record


def _read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)
