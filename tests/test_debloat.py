import json
from pathlib import Path
from typing import Any, cast

from tundravm.declarative import (
    Backend,
    Debloat,
    Fragment,
    Package,
    Recipe,
    Variant,
    bake,
    lock,
    lower,
)
from tundravm.models import BakeResult, DebloatConfig


def _recipe(*variants: Variant) -> Recipe:
    return Recipe(
        "debloat",
        Fragment("common", items=(Package("curl"),)),
        variants=(Variant("default", target="qemu"), *variants),
    )


def _prod(*, target: bool = False) -> Variant:
    return Variant(
        "prod",
        add=Fragment("prod", items=(Debloat(enabled=False),)),
        target="qemu" if target else None,
    )


def test_debloat_default_is_enabled_and_deterministic() -> None:
    image = lower(_recipe())
    first = image.explain_debloat()
    second = lower(_recipe()).explain_debloat()

    assert first["enabled"] is True
    assert first == second
    assert first["paths_remove"]


def test_debloat_variant_override_is_supported() -> None:
    image = lower(_recipe(_prod()))

    assert image.explain_debloat(profile="default")["enabled"] is True
    assert image.explain_debloat(profile="prod")["enabled"] is False


def test_bake_report_contains_debloat_section(tmp_path: Path) -> None:
    recipe = _recipe(_prod(target=True))
    out = tmp_path / "build"

    bake(recipe, locked=lock(recipe), backend=Backend("inprocess"), out=out)

    result = BakeResult.load(out)
    default_report = _read_report(result.profiles["default"].report_path)
    prod_report = _read_report(result.profiles["prod"].report_path)

    assert default_report["debloat"]["enabled"] is True
    assert prod_report["debloat"]["enabled"] is False


def test_debloat_paths_skip_for_profiles_excludes_from_effective() -> None:
    """Paths in paths_skip_for_profiles are excluded from effective_paths_remove."""
    config = DebloatConfig(
        paths_skip_for_profiles=(("devtools", ("/usr/share/bash-completion",)),),
    )
    # /usr/share/bash-completion is in DEFAULT_DEBLOAT_PATHS_REMOVE
    assert "/usr/share/bash-completion" not in config.effective_paths_remove
    # But it appears in profile_conditional_paths
    assert "devtools" in config.profile_conditional_paths
    assert "/usr/share/bash-completion" in config.profile_conditional_paths["devtools"]


def test_debloat_keep_paths_by_variant_lowers_to_profile_conditional_paths() -> None:
    """Debloat(keep_paths_by_variant=...) keeps the path out of the unconditional removal."""
    recipe = Recipe(
        "debloat",
        Fragment(
            "common",
            items=(
                Debloat(keep_paths_by_variant=(("devtools", ("/usr/share/bash-completion",)),)),
            ),
        ),
        variants=(Variant("default", target="qemu"), Variant("devtools")),
    )
    config = lower(recipe, variants=("default",)).state.ensure_profile("default").debloat
    assert "/usr/share/bash-completion" not in config.effective_paths_remove
    conditional = config.profile_conditional_paths
    assert "devtools" in conditional
    assert "/usr/share/bash-completion" in conditional["devtools"]


def _read_report(path: Path | None) -> dict[str, Any]:
    assert path is not None
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)
