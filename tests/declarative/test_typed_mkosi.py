"""Typed build mechanics: ``Mkosi`` and ``Debian`` fields write the keys ``Setting`` spelled."""

from pathlib import Path

import pytest

from tundravm.declarative import Debian, Mkosi, Package, Recipe, Setting, compile
from tundravm.errors import ValidationError

SEED = "630b5f72-a36a-4e83-b23d-6ef47c82fd9c"
TYPED = Mkosi(
    seed=SEED,
    output_dir="build",
    package_cache="mkosi.cache",
    compress="zstd",
    manifest="changelog",
    build_network=False,
)
RAW = (
    Setting("Output", "Seed", (SEED,)),
    Setting("Output", "OutputDirectory", ("build",)),
    Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
    Setting("Output", "CompressOutput", ("zstd",)),
    Setting("Output", "ManifestFormat", ("changelog",)),
    Setting("Build", "WithNetwork", ("no",)),
    Setting("Content", "CleanPackageMetadata", ("no",)),
)

DEFAULT_MKOSI = Mkosi()


def _recipe(*items: object, mkosi: Mkosi = DEFAULT_MKOSI, clean: bool = True) -> Recipe:
    platform = Debian("trixie", clean_package_metadata=clean)
    return Recipe("typed", items=(Package("jq"), *items), platform=platform, mkosi=mkosi)  # type: ignore[arg-type]


def _conf(recipe: Recipe, tmp_path: Path) -> list[str]:
    """The root ``mkosi.conf`` and the payload's, as lines."""
    compile(recipe).write(tmp_path)
    return [
        line
        for path in ("mkosi.conf", "payload/mkosi.conf")
        for line in (tmp_path / path).read_text(encoding="utf-8").splitlines()
    ]


def test_typed_fields_write_the_tree_the_settings_wrote() -> None:
    typed = compile(_recipe(mkosi=TYPED, clean=False))
    raw = compile(_recipe(*RAW))
    assert typed.digest == raw.digest


def test_a_setting_overrides_the_typed_field(tmp_path: Path) -> None:
    recipe = _recipe(Setting("Output", "ManifestFormat", ("json",)), mkosi=TYPED)
    assert "ManifestFormat=json" in _conf(recipe, tmp_path)


@pytest.mark.parametrize(
    "fields",
    [{"seed": "not-a-uuid"}, {"compress": "lzma"}, {"manifest": "yaml"}, {"build_network": 1}],
)
def test_mkosi_rejects_invalid_values(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Mkosi(**fields)  # type: ignore[arg-type]
