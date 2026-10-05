"""The ``tundravm.declarative.utils`` fragments, file by file against the surge golden tree."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest
from examples.nethermind_base import EFI_STUB_VERSION, PINNED_MIRROR

from tests.helpers import SURGE_EXAMPLE
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Group,
    Hook,
    Install,
    Key,
    Mkosi,
    Package,
    Recipe,
    Tree,
    User,
    Variant,
    compile,
)
from tundravm.declarative.utils import (
    TUNDRA_TOOLS,
    Backports,
    Composite,
    DevTools,
    EfiStub,
    Tdxs,
)
from tundravm.errors import ValidationError

GOLDEN = SURGE_EXAMPLE / "mkosi"
SNAPSHOT = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
HISTORICAL = Mkosi(dialect="nethermind-v1")


def _recipe(*items: Fragment, variants: tuple[Variant, ...] = (Variant("default"),)) -> Recipe:
    common = Fragment("module", items=items)
    return Recipe(name="module", common=common, variants=variants, mkosi=HISTORICAL)


def _text(tree: Tree, path: str) -> str:
    content = next(e.content for e in tree.entries if e.path == path)
    assert content is not None, path
    return content.decode()


def _files(tree: Tree, variant: str = "default") -> dict[str, bytes]:
    """The variant's files, keyed by their path inside the variant directory."""
    prefix = f"{variant}/"
    return {
        e.path.removeprefix(prefix): e.content
        for e in tree.entries
        if e.path.startswith(prefix) and e.content is not None
    }


# ── The utils fragments ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("fragment", "variant", "paths"),
    [
        (
            Tdxs(),
            "default",
            (
                "mkosi.extra/etc/tdxs/config.yaml",
                "mkosi.extra/usr/lib/systemd/system/tdxs.service",
                "mkosi.extra/usr/lib/systemd/system/tdxs.socket",
            ),
        ),
        (DevTools(), "devtools", ("mkosi.extra/usr/lib/systemd/system/serial-console.service",)),
        (Backports(), "default", ("scripts/01-sync.sh",)),
    ],
    ids=["tdxs", "devtools", "backports"],
)
def test_fragment_files_match_the_surge_golden_tree(
    fragment: Fragment, variant: str, paths: tuple[str, ...]
) -> None:
    files = _files(compile(_recipe(fragment)))
    base = _files(compile(_recipe()))
    assert sorted(set(files) - set(base) - {"scripts/04-build.sh"}) == sorted(paths)
    for path in paths:
        assert files[path] == (GOLDEN / variant / path).read_bytes(), path


def test_efi_stub_hook_matches_the_surge_golden_tree() -> None:
    (hook,) = EfiStub(snapshot=PINNED_MIRROR, version=EFI_STUB_VERSION).items
    assert isinstance(hook, Hook)
    postinst = (GOLDEN / "default" / "scripts" / "06-postinst.sh").read_text()
    assert hook.script.strip() + "\n" in postinst
    tree = compile(_recipe(EfiStub(snapshot=PINNED_MIRROR, version=EFI_STUB_VERSION)))
    assert hook.script.strip() + "\n" in _text(tree, "default/scripts/06-postinst.sh")


def test_tdxs_issuer_and_validator_render_the_config() -> None:
    tdxs = Tdxs(
        issuer="azure",
        validator="azure",
        expected_measurements=(("rtmr0", "ab"),),
        check_revocations=True,
        verify_imds=True,
    )
    config = _text(compile(_recipe(tdxs)), "default/mkosi.extra/etc/tdxs/config.yaml")
    assert config == (
        "transport:\n"
        "  type: socket\n"
        "  config:\n"
        "    systemd: true\n"
        "issuer:\n"
        "  type: azure\n"
        "validator:\n"
        "  type: azure\n"
        "  config:\n"
        "    expected_measurements:\n"
        '      rtmr0: "ab"\n'
        "    check_revocations: true\n"
        "    verify_imds: true\n"
    )


def test_backports_pins_mirror_and_release() -> None:
    default = _text(compile(_recipe(Backports())), "default/scripts/01-sync.sh")
    assert 'jq -r .Mirror "$BUILDDIR/config.json"' in default
    pinned = _text(
        compile(_recipe(Backports(archive_url="http://m", release="trixie"))),
        "default/scripts/01-sync.sh",
    )
    assert 'MIRROR="http://m"\nRELEASE="trixie"\n' in pinned
    assert "jq" not in pinned
    assert pinned.split("cat > ", 1)[1] == default.split("cat > ", 1)[1]


def test_tdxs_after_init_orders_after_runtime_init() -> None:
    tree = compile(_recipe(Tdxs(after_init=True), Fragment("keys", items=(Key("k"),))))
    units = "default/mkosi.extra/usr/lib/systemd/system"
    service = _text(tree, f"{units}/tdxs.service")
    socket = _text(tree, f"{units}/tdxs.socket")
    assert "After=runtime-init.service\nRequires=runtime-init.service tdxs.socket\n" in service
    assert "After=runtime-init.service\nRequires=runtime-init.service\n" in socket
    plain = compile(_recipe(Tdxs(), Fragment("keys", items=(Key("k"),))))
    assert "runtime-init" not in _text(plain, f"{units}/tdxs.service")


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
    assert repr(Backports(release="trixie")) == "Backports(archive_url=None, release='trixie')"
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
    conf = _text(compile(recipe), "default/mkosi.conf").splitlines()
    assert {"    strace", "    gdb"} <= set(conf)
    variant = Recipe(name="m", common=Fragment("m"), variants=(Variant("v", add=Tools()),))
    assert "    strace" in _text(compile(variant), "v/mkosi.conf").splitlines()


def test_historical_dialect_spells_accounts_as_postinst_lines() -> None:
    accounts = Fragment(
        "accounts",
        items=(
            Group("tdx"),
            Group("x", system=False, gid=7),
            Group("eth"),
            User("svc", home="/home/svc", uid=9, primary_group="tdx", groups=("eth", "x")),
        ),
    )
    postinst = _text(compile(_recipe(accounts)), "default/scripts/06-postinst.sh")
    assert postinst.splitlines()[3:7] == [
        "mkosi-chroot groupadd --system tdx",
        "mkosi-chroot groupadd --gid 7 x",
        "mkosi-chroot groupadd --system eth",
        "mkosi-chroot useradd --system --home-dir /home/svc --shell /usr/sbin/nologin "
        "--uid 9 --gid tdx --groups eth,x svc",
    ]


def test_current_dialect_uses_the_account_prelude() -> None:
    recipe = Recipe(name="m", common=Fragment("m", items=(Tdxs(),)))
    lines = _text(compile(recipe), "default/scripts/06-postinst.sh").splitlines()
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
    tree = compile(recipe)
    useradds = [
        line
        for line in _text(tree, "other/scripts/06-postinst.sh").splitlines()
        if "useradd" in line
    ]
    assert useradds == ["mkosi-chroot useradd --system --shell /usr/sbin/nologin --uid 5 svc"]
    assert "--uid" not in _text(tree, "default/scripts/06-postinst.sh")


def test_build_cache_key_and_unpinned_marker() -> None:
    build = Build(
        "tool",
        Git("https://example.com/tool.git", "feat/x"),
        script="make",
        install=(Install("tool", "/usr/bin/tool"),),
        cache_key="tool-feat/x",
    )
    historical = _text(
        compile(_recipe(Fragment("b", items=(build,)))), "default/scripts/04-build.sh"
    )
    assert "# unpinned" not in historical
    assert '"$BUILDDIR/tool-feat_x"' in historical
    current = Recipe(name="m", common=Fragment("b", items=(build,)))
    assert "\n# unpinned: feat/x\n" in _text(compile(current), "default/scripts/04-build.sh")
    with pytest.raises(ValidationError, match="cache_key"):
        Build("t", build.source, script="make", install=build.install, cache_key="a b")
