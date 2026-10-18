"""Composed kernels: a base config, KernelConfig fragments, patches, modules and the check."""

from __future__ import annotations

from pathlib import Path

import pytest

from tundravm.declarative import (
    Debian,
    DebianConfig,
    Git,
    Http,
    Kernel,
    KernelConfig,
    KernelPatch,
    Mkosi,
    Recipe,
    Variant,
    compile,
    lint,
    lock,
    lower,
)
from tundravm.declarative._kernel_hardening import ATTACK_SURFACE, HARDENING
from tundravm.declarative._lowered import kernel_payload
from tundravm.declarative.utils import KernelHardening
from tundravm.errors import ValidationError

pytestmark = pytest.mark.usefixtures("isolated_cwd")

SNAPSHOT = "20251113T083151Z"
SCRIPT = "default/scripts/04-build.sh"
DEB = (
    f"https://snapshot.debian.org/archive/debian/{SNAPSHOT}/pool/main/l/linux/"
    "linux-config-6.12_6.12.48-1_amd64.deb"
)
SOURCE = Git("https://k.example/linux", "v6.12.48")


def _recipe(kernel: Kernel, *items: object, kernel_modules: tuple[str, ...] = ()) -> Recipe:
    package = "linux-image-amd64" if kernel_modules else None
    platform = Debian(
        "trixie", snapshot=SNAPSHOT, kernel_package=package, kernel_modules=kernel_modules
    )
    return Recipe(
        "k",
        platform=platform,
        items=(kernel, *items),  # type: ignore[arg-type]
        variants=(Variant("default"),),
        mkosi=Mkosi(),
    )


def _script(recipe: Recipe, out: Path, **kwargs: object) -> str:
    compile(recipe, **kwargs).write(out)  # type: ignore[arg-type]
    return (out / SCRIPT).read_text(encoding="utf-8")


@pytest.fixture
def base(tmp_path: Path) -> Path:
    path = tmp_path / "base.config"
    path.write_text("CONFIG_MODULES=y\n", encoding="utf-8")
    return path


def test_debian_config_names_the_package_and_member() -> None:
    config = DebianConfig("6.12.48-1")
    assert config.url(f"https://snapshot.debian.org/archive/debian/{SNAPSHOT}") == DEB
    assert config.member == "./usr/src/linux-config-6.12/config.amd64_none_cloud-amd64.xz"
    with pytest.raises(ValidationError, match="kernel ABI"):
        DebianConfig("6.12.48+deb13")


def test_fragments_merge_in_declaration_order(base: Path) -> None:
    own = KernelConfig({"CONFIG_XFS_FS": "y"}, name="xfs")
    kernel = Kernel("6.12.48", SOURCE, base=base, fragments=(own,))
    extra = KernelConfig({"CONFIG_IO_URING": None, "CONFIG_XFS_FS": "y"}, name="extra")
    lowered = lower(_recipe(kernel, extra)).kernel
    assert lowered is not None
    assert lowered.options == (
        ("CONFIG_MODULES", "n"),
        ("CONFIG_XFS_FS", "y"),
        ("CONFIG_IO_URING", "n"),
    )


def test_a_conflict_names_both_declarations(base: Path) -> None:
    kernel = Kernel("6.12.48", SOURCE, base=base, modules=True)
    on = KernelConfig({"CONFIG_IO_URING": "y"}, name="wants-uring")
    off = KernelConfig({"CONFIG_IO_URING": None}, name="no-uring")
    with pytest.raises(ValidationError, match="kernel-option-conflict") as raised:
        lower(_recipe(kernel, on, off))
    message = str(raised.value)
    assert "'wants-uring'" in message and "'no-uring'" in message
    with pytest.raises(ValidationError, match="Kernel\\(modules=False\\)"):
        lower(_recipe(Kernel("6.12.48", SOURCE, base=base), KernelConfig({"CONFIG_MODULES": "y"})))


def test_config_is_exclusive_with_composition(base: Path) -> None:
    with pytest.raises(ValidationError, match="whole, final config"):
        Kernel("6.12.48", SOURCE, config=base, fragments=(KernelConfig({"CONFIG_X": "y"}),))
    with pytest.raises(ValidationError, match="need a base"):
        Kernel("6.12.48", SOURCE, patches=(KernelPatch(base),))
    with pytest.raises(ValidationError, match="needs a composed kernel"):
        lower(_recipe(Kernel("6.12.48", SOURCE, config=base), KernelConfig({"CONFIG_X": "y"})))


def _resolve(source: object) -> str:
    return "d" * 64 if getattr(source, "kind", "") == "http" else "c" * 40


def test_the_script_patches_in_order_merges_and_checks(base: Path, tmp_path: Path) -> None:
    first, second = tmp_path / "0001-a.patch", tmp_path / "b.diff"
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")
    remote = Http("https://p.example/fix.patch", sha256="e" * 64)
    kernel = Kernel(
        "6.12.48",
        SOURCE,
        base=base,
        patches=(KernelPatch(second), KernelPatch(remote), KernelPatch(first)),
        fragments=(KernelConfig({"CONFIG_CMDLINE": '"quiet"', "CONFIG_XFS_FS": None}),),
    )
    recipe = _recipe(kernel)
    out = tmp_path / "out"
    script = _script(recipe, out, lock=lock(recipe, resolver=_resolve))
    assert (out / "default/kernel/base.config").read_text() == "CONFIG_MODULES=y\n"
    assert (out / "default/kernel/fragment.config").read_text() == (
        '# CONFIG_MODULES is not set\nCONFIG_CMDLINE="quiet"\n# CONFIG_XFS_FS is not set\n'
    )
    assert (out / "default/kernel/patches/01-b.diff").read_text() == "b\n"
    assert (out / "default/kernel/patches/03-0001-a.patch").read_text() == "a\n"
    order = [script.index(name) for name in ("01-b.diff", "fix.patch does not apply", "03-0001")]
    assert order == sorted(order)
    assert "sha256sum -c --quiet -" in script
    assert (
        '    ./scripts/kconfig/merge_config.sh -m .config "$KERNEL_TREE/fragment.config"\n'
        "    make olddefconfig\n"
        "    # Kconfig drops a requested option whose dependencies are unmet: "
        "check each survived\n"
        "    kconfig_check() {\n"
        "        local final\n"
        '        final=$(sed -n "s/^$1=//p" .config)\n'
        '        if [ "${final:-n}" != "$2" ]; then\n'
        '            echo "tundravm: kernel-option-dropped: $1 requested $2, '
        'final ${final:-n}" >&2\n'
        "            return 1\n"
        "        fi\n"
        "    }\n"
        "    dropped=0\n"
        "    kconfig_check CONFIG_MODULES n || dropped=1\n"
        "    kconfig_check CONFIG_CMDLINE '\"quiet\"' || dropped=1\n"
        "    kconfig_check CONFIG_XFS_FS n || dropped=1\n"
        '    [ "$dropped" -eq 0 ] || exit 1\n'
        '    make -j"$(nproc)" bzImage ARCH=x86_64\n'
    ) in script
    assert "modules_install" not in script


def test_a_debian_base_is_locked_fetched_and_extracted(tmp_path: Path) -> None:
    kernel = Kernel("6.12.48", SOURCE, base=DebianConfig("6.12.48-1"), modules=True)
    recipe = _recipe(kernel)
    unpinned = _script(recipe, tmp_path / "unpinned")
    assert "tundravm: kernel-config is not pinned" in unpinned
    locked = lock(recipe, resolver=_resolve)
    pins = {pin.name: (pin.kind, pin.source, pin.digest) for pin in locked.lockfile.fetches}
    assert pins["kernel-config"] == ("http", DEB, "d" * 64)
    script = _script(recipe, tmp_path / "pinned", lock=locked)
    assert (
        f'dpkg-deb --fsys-tarfile "$SRCDIR/tundravm-sources/kernel-config-{"d" * 12}-'
    ) in script
    assert '| xz -dc > "$KERNEL_CACHE/src/.config"' in script
    assert 'make -j"$(nproc)" bzImage modules ARCH=x86_64' in script
    assert 'modules_install INSTALL_MOD_PATH="${DESTDIR}/usr"' in script
    payload = kernel_payload(lower(recipe).kernel)
    assert payload is not None
    assert payload["base"] == {
        "deb": DEB,
        "member": "./usr/src/linux-config-6.12/config.amd64_none_cloud-amd64.xz",
    }
    assert (payload["options"], payload["patches"], payload["modules"]) == ({}, [], True)


def test_the_cache_key_follows_fragments_and_patches(base: Path, tmp_path: Path) -> None:
    def key(*fragments: KernelConfig) -> str:
        kernel = Kernel("6.12.48", SOURCE, base=base, fragments=fragments)
        script = _script(_recipe(kernel), tmp_path / str(len(fragments)))
        return next(line for line in script.splitlines() if line.startswith("KERNEL_CACHE="))

    assert key() != key(KernelConfig({"CONFIG_XFS_FS": "y"}))


def test_kernel_hardening_declares_the_deny_list_and_hardening() -> None:
    attack, hardening = KernelHardening().items
    assert isinstance(attack, KernelConfig) and isinstance(hardening, KernelConfig)
    assert attack.name == "kernel-attack-surface"
    assert set(attack.options.values()) == {"n"}
    assert {"CONFIG_IO_URING", "CONFIG_USERFAULTFD", "CONFIG_XFRM_USER"} <= set(attack.options)
    assert len(attack.options) == len(ATTACK_SURFACE)
    assert hardening.options["CONFIG_KEXEC"] == "n"
    assert hardening.options["CONFIG_SECURITY_LOCKDOWN_LSM"] == "y"
    assert all(reason for _, _, reason in HARDENING)
    relaxed = KernelHardening(allow=("CONFIG_IO_URING",)).items[0]
    assert isinstance(relaxed, KernelConfig) and "CONFIG_IO_URING" not in relaxed.options
    with pytest.raises(ValidationError, match="does not set"):
        _ = KernelHardening(allow=("CONFIG_NOPE",)).items


def test_kernel_hardening_composes_into_a_built_kernel(base: Path) -> None:
    kernel = Kernel("6.12.48", SOURCE, base=base)
    lowered = lower(_recipe(kernel, KernelHardening())).kernel
    assert lowered is not None
    assert dict(lowered.options)["CONFIG_DEVMEM"] == "n"
    assert len(lowered.options) == 1 + len(ATTACK_SURFACE) + len(HARDENING)


def test_lint_flags_kernel_modules_a_monolithic_kernel_cannot_have(base: Path) -> None:
    kernel = Kernel("6.12.48", SOURCE, base=base)
    found = lint(_recipe(kernel, kernel_modules=("vfat",)))
    assert "kernel-modules-ignored" in {d.code for d in found}
    clean = lint(_recipe(Kernel("6.12.48", SOURCE, base=base, modules=True)))
    assert "kernel-modules-ignored" not in {d.code for d in clean}
