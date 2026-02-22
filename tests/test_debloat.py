import json
from pathlib import Path
from typing import Any, cast

from tdx import Image


def test_debloat_default_is_enabled_and_deterministic() -> None:
    image = Image()
    first = image.explain_debloat()
    second = image.explain_debloat()

    assert first["enabled"] is True
    assert first == second
    assert first["remove"]
    assert first["mask"]


def test_debloat_profile_override_is_supported() -> None:
    image = Image()
    with image.profile("prod"):
        image.debloat(enabled=False)

    assert image.explain_debloat(profile="default")["enabled"] is True
    assert image.explain_debloat(profile="prod")["enabled"] is False


def test_bake_report_contains_debloat_section(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build")
    image.output_targets("qemu")
    with image.profile("prod"):
        image.output_targets("qemu")
        image.debloat(enabled=False)

    with image.all_profiles():
        result = image.bake()

    default_report = _read_report(result.profiles["default"].report_path)
    prod_report = _read_report(result.profiles["prod"].report_path)

    assert default_report["debloat"]["enabled"] is True
    assert prod_report["debloat"]["enabled"] is False


def _read_report(path: Path | None) -> dict[str, Any]:
    assert path is not None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)
