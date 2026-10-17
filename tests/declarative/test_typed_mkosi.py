"""Typed build mechanics: ``Mkosi`` and ``Debian`` fields write the keys ``Setting`` spelled."""

from pathlib import Path

import pytest
from examples.nethermind_base import recipe as nethermind_base

from tundravm.check import TYPED_SETTINGS
from tundravm.declarative import Debian, Mkosi, Package, Recipe, Setting, compile
from tundravm.declarative.lower import _BOOL_SETTINGS, _SINGLE_SETTINGS
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
    compile(recipe).write(tmp_path)
    return (tmp_path / "default" / "mkosi.conf").read_text(encoding="utf-8").splitlines()


def test_typed_fields_write_the_tree_the_settings_wrote() -> None:
    typed = compile(_recipe(mkosi=TYPED, clean=False))
    raw = compile(_recipe(*RAW))
    assert typed.digest == raw.digest


def test_typed_fields_write_their_mkosi_keys(tmp_path: Path) -> None:
    conf = _conf(_recipe(mkosi=TYPED, clean=False), tmp_path)
    for line in (
        f"Seed={SEED}",
        "OutputDirectory=build",
        "PackageCacheDirectory=mkosi.cache",
        "CompressOutput=zstd",
        "ManifestFormat=changelog",
        "WithNetwork=false",
        "CleanPackageMetadata=false",
    ):
        assert line in conf


def test_defaults_write_the_opinionated_keys(tmp_path: Path) -> None:
    conf = _conf(_recipe(), tmp_path)
    assert {"ManifestFormat=json", "WithNetwork=true", "CleanPackageMetadata=true"} <= set(conf)
    assert not any(line.startswith(("OutputDirectory=", "CompressOutput=")) for line in conf)


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


def test_every_compiler_mapped_setting_names_its_typed_field() -> None:
    assert set(_SINGLE_SETTINGS) | set(_BOOL_SETTINGS) == set(TYPED_SETTINGS)


def test_a_kernel_with_a_config_passes_its_variables_through_unasked(tmp_path: Path) -> None:
    # examples/nethermind_base.py dropped Setting("Build", "Environment", ("KERNEL_IMAGE",
    # "KERNEL_VERSION")): the compiler writes both for a Kernel(config=...) on its own.
    conf = _conf(nethermind_base, tmp_path)
    assert conf.count("Environment=KERNEL_IMAGE") == 1
    assert conf.count("Environment=KERNEL_VERSION") == 1
    assert f"Seed={SEED}" in conf and "OutputDirectory=build" in conf
