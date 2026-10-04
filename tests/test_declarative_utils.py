"""The ``tundravm.declarative.utils`` fragments and the surge recipe lower to the fluent trees."""

from __future__ import annotations

import importlib.util
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from tundravm._image import Image
from tundravm._modules import DevTools as FluentDevTools
from tundravm._modules import Tdxs as FluentTdxs
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Group,
    Hook,
    Install,
    Mkosi,
    Package,
    Recipe,
    User,
    Variant,
    lower,
)
from tundravm.declarative.lower import groupadd_line, useradd_line
from tundravm.declarative.utils import (
    TUNDRA_TOOLS,
    Backports,
    Composite,
    DevTools,
    EfiStub,
    Tdxs,
)
from tundravm.diff import diff_trees
from tundravm.errors import ValidationError
from tundravm.recipe import load_image

ROOT = Path(__file__).resolve().parent.parent
SURGE = ROOT / "examples" / "surge-tdx-prover"
SNAPSHOT = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
HISTORICAL = Mkosi(dialect="nethermind-v1")


def _surge_fluent() -> ModuleType:
    """``tests/fixtures/surge_fluent.py``: the fluent parity oracle."""
    path = Path(__file__).parent / "fixtures" / "surge_fluent.py"
    spec = importlib.util.spec_from_file_location("surge_fluent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def blank_image(**options: object) -> Image:
    return cast(Image, _surge_fluent().blank_image(**options))


def _recipe(*items: Fragment, variants: tuple[Variant, ...] = (Variant("default"),)) -> Recipe:
    common = Fragment("module", items=items)
    return Recipe(name="module", common=common, variants=variants, mkosi=HISTORICAL)


def _fluent(configure: Callable[[Image], object]) -> Image:
    img = blank_image(base="debian/trixie", reproducible=True)
    configure(img)
    return img


def _assert_same_tree(fluent: Image, recipe: Recipe, tmp_path: Path) -> None:
    fluent.compile(tmp_path / "fluent")
    lower(recipe).compile(tmp_path / "declarative")
    diff = diff_trees(tmp_path / "fluent", tmp_path / "declarative")
    assert diff.is_clean, diff.render("text")


@pytest.mark.parametrize(
    ("configure", "fragment"),
    [
        (lambda img: FluentTdxs().apply(img), Tdxs()),
        (
            lambda img: FluentTdxs(
                issuer_type="azure",
                validator_type="azure",
                expected_measurements={"rtmr0": "ab"},
                check_revocations=True,
                verify_imds=True,
            ).apply(img),
            Tdxs(
                issuer="azure",
                validator="azure",
                expected_measurements=(("rtmr0", "ab"),),
                check_revocations=True,
                verify_imds=True,
            ),
        ),
        (lambda img: FluentDevTools().apply(img), DevTools()),
        (
            lambda img: img.efi_stub(snapshot_url=SNAPSHOT, package_version="255.4-1"),
            EfiStub(snapshot=SNAPSHOT, version="255.4-1"),
        ),
        (lambda img: img.backports(), Backports()),
        (
            lambda img: img.backports(mirror="http://m", release="trixie"),
            Backports(mirror="http://m", release="trixie"),
        ),
    ],  # fmt: skip
    ids=["tdxs", "tdxs-validator", "devtools", "efi-stub", "backports", "backports-pinned"],
)
def test_fragment_lowers_like_the_fluent_module(
    configure: Callable[[Image], object], fragment: Fragment, tmp_path: Path
) -> None:
    _assert_same_tree(_fluent(configure), _recipe(fragment), tmp_path)


def test_tdxs_after_init_matches_module_rendering(tmp_path: Path) -> None:
    from tundravm.declarative import Key

    def configure(img: Image) -> None:
        img.runtime_init("/usr/bin/key-gen setup /etc/tdx/key-gen.yaml\n", priority=10)
        FluentTdxs(after=("runtime-init.service",)).apply(img)

    img = lower(_recipe(Tdxs(after_init=True), Fragment("keys", items=(Key("k"),))))
    units = {
        f.path: f.content
        for f in img.state.profiles["default"].files
        if f.path.startswith("/usr/lib/systemd/system/tdxs")
    }
    expected = {
        f.path: f.content
        for f in _fluent(configure).state.profiles["default"].files
        if f.path.startswith("/usr/lib/systemd/system/tdxs")
    }
    assert units == expected


def test_devtools_root_password() -> None:
    hooks = [item for item in DevTools(root_password="s3cret").items if isinstance(item, Hook)]
    assert 'openssl passwd -6 "s3cret"' in hooks[-1].script
    with pytest.raises(ValidationError):
        DevTools(root_password='a"b')


def test_utils_are_fragments_configured_by_their_fields() -> None:
    efi = EfiStub(snapshot=SNAPSHOT, version="255.4-1")
    assert isinstance(efi, Fragment)
    assert (efi.name, efi.requires, efi.checks) == ("efi-stub", (), ())
    assert [type(item) for item in efi.items] == [Hook]
    assert repr(efi) == f"EfiStub(snapshot={SNAPSHOT!r}, version='255.4-1')"
    assert str(inspect.signature(EfiStub)) == "(*, snapshot: 'str', version: 'str') -> None"
    assert repr(DevTools()) == "DevTools(root_password='tdx')"
    assert repr(Backports(release="trixie")) == "Backports(mirror=None, release='trixie')"
    assert Tdxs().source == TUNDRA_TOOLS
    assert Tdxs().name == "tdxs" and Tdxs(after_init=True) != Tdxs()
    assert Tdxs() == Tdxs() and hash(Tdxs()) == hash(Tdxs())
    assert {DevTools().name, Backports().name} == {"devtools", "backports"}


def test_utils_freeze_lists_and_reject_bad_values() -> None:
    tdxs = Tdxs(expected_measurements=[("rtmr0", "ab")])  # type: ignore[arg-type]
    assert tdxs.expected_measurements == (("rtmr0", "ab"),)
    with pytest.raises(ValidationError, match="EfiStub"):
        EfiStub(snapshot="", version="1")
    with pytest.raises(TypeError):
        EfiStub(SNAPSHOT, "1")  # type: ignore[misc]


def test_composite_subclass_works_as_a_fragment() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class Tools(Composite):
        packages: tuple[str, ...] = ("strace",)

        def compose(self) -> Fragment:
            return Fragment("tools", items=tuple(Package(p) for p in self.packages))

    tools = Tools(packages=("strace", "gdb"))
    assert repr(tools).endswith(".Tools(packages=('strace', 'gdb'))")
    recipe = Recipe(name="m", common=Fragment("m", items=(tools, tools)))
    assert {"strace", "gdb"} <= lower(recipe).state.effective_profile("default").packages
    variant = Recipe(name="m", common=Fragment("m"), variants=(Variant("v", add=Tools()),))
    lower(variant)


def test_historical_dialect_spells_accounts_as_postinst_lines() -> None:
    assert groupadd_line(Group("tdx")) == "mkosi-chroot groupadd --system tdx"
    assert groupadd_line(Group("x", system=False, gid=7)) == "mkosi-chroot groupadd --gid 7 x"
    user = User("svc", home="/home/svc", uid=9, primary_group="tdx", groups=("eth", "x"))
    assert useradd_line(user) == (
        "mkosi-chroot useradd --system --home-dir /home/svc --shell /usr/sbin/nologin "
        "--uid 9 --gid tdx --groups eth,x svc"
    )


def test_current_dialect_uses_the_account_prelude(tmp_path: Path) -> None:
    recipe = Recipe(name="m", common=Fragment("m", items=(Tdxs(),)))
    lower(recipe).compile(tmp_path)
    postinst = (tmp_path / "default" / "scripts" / "06-postinst.sh").read_text()
    lines = postinst.splitlines()
    assert lines[3] == "mkosi-chroot groupadd --system tdx"
    assert lines[4].startswith("mkosi-chroot useradd --system --home-dir /home/tdxs --create-home")


def test_historical_dialect_lowers_account_replacement_standalone() -> None:
    recipe = _recipe(
        Fragment("users", items=(User("svc"),)),
        variants=(
            Variant("default"),
            Variant("other", parent="default", replace=(User("svc", uid=5),)),
        ),
    )
    img = lower(recipe)
    assert img.state.profiles["other"].extends is None
    commands = [" ".join(c.argv) for c in img.state.effective_profile("other").phases["postinst"]]
    assert any("--uid 5 svc" in command for command in commands)


def test_build_cache_key_and_unpinned_marker() -> None:
    build = Build(
        "tool",
        Git("https://example.com/tool.git", "feat/x"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
        cache_key="tool-feat/x",
    )
    historical = lower(_recipe(Fragment("b", items=(build,)))).source_builds()["tool"]
    assert historical.cache_key == "tool-feat/x"
    assert not historical.render().startswith("# unpinned")
    assert '"$BUILDDIR/tool-feat_x"' in historical.render()
    current = Recipe(name="m", common=Fragment("b", items=(build,)))
    assert lower(current).source_builds()["tool"].render().startswith("# unpinned: feat/x\n")
    with pytest.raises(ValidationError, match="cache_key"):
        Build("t", build.source, script="make", install=build.install, cache_key="a b")


# ── The surge recipe ────────────────────────────────────────────────


def _fluent_surge() -> Image:
    spec = importlib.util.spec_from_file_location(
        "surge_fluent", ROOT / "tests" / "fixtures" / "surge_fluent.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    img = module.build()
    assert isinstance(img, Image)
    return img


def _declarative_surge() -> Image:
    return load_image(SURGE / "image.py", extra_paths=[ROOT])


def test_surge_recipe_matches_the_committed_tree(tmp_path: Path) -> None:
    img = _declarative_surge()
    img.compile(tmp_path, profiles=sorted(img.state.profiles))
    diff = diff_trees(SURGE / "mkosi", tmp_path)
    assert diff.is_clean, diff.render("text")


def test_surge_recipe_matches_the_fluent_recipe_for_every_variant(tmp_path: Path) -> None:
    fluent, declarative = _fluent_surge(), _declarative_surge()
    assert sorted(fluent.state.profiles) == sorted(declarative.state.profiles)
    names = sorted(fluent.state.profiles)
    fluent.compile(tmp_path / "fluent", profiles=names)
    declarative.compile(tmp_path / "declarative", profiles=names)
    diff = diff_trees(tmp_path / "fluent", tmp_path / "declarative")
    assert diff.is_clean, diff.render("text")
