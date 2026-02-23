"""Golden test: SDK emission vs real NethermindEth/nethermind-tdx base layer.

Configures an Image matching the nethermind-tdx base layer, compiles it, and
compares every emitted file against the reference files checked into
.golden_reference/nethermind-tdx-base/ (fetched from the upstream repo).

The tests document *exactly* where the SDK diverges from the real repo and why.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tdx import Image, Kernel
from tdx.modules.tdxs import Tdxs

from .conftest import snapshot_tree

pytestmark = pytest.mark.integration

GOLDEN_REF = Path(__file__).parent / ".golden_reference" / "nethermind-tdx-base"

# ── Constants matching upstream mkosi.conf exactly ────────────────────

UPSTREAM_RUNTIME_PACKAGES = (
    "kmod",
    "systemd",
    "systemd-boot-efi",
    "busybox",
    "util-linux",
    "procps",
    "ca-certificates",
    "openssl",
    "iproute2",
    "udhcpc",
    "e2fsprogs",
)

UPSTREAM_BUILD_PACKAGES = (
    "build-essential",
    "git",
    "curl",
    "cmake",
    "pkg-config",
    "clang",
    "cargo/sid",
    "flex",
    "bison",
    "elfutils",
    "bc",
    "perl",
    "gawk",
    "zstd",
    "libssl-dev",
    "libelf-dev",
)

UPSTREAM_KERNEL_CMDLINE = (
    "console=tty0 console=ttyS0,115200n8 "
    "mitigations=auto,nosmt "
    "spec_store_bypass_disable=on "
    "nospectre_v2"
)

UPSTREAM_SEED = "630b5f72-a36a-4e83-b23d-6ef47c82fd9c"


def _build_nethermind_base_image() -> Image:
    """Reproduce the nethermind-tdx base layer via the SDK."""
    img = Image(
        base="debian/trixie",
        reproducible=True,
        with_network=True,
        clean_package_metadata=True,
        manifest_format="json",
        init_script=Image.DEFAULT_TDX_INIT,
        seed=UPSTREAM_SEED,
        output_directory="build",
        package_cache_directory="mkosi.cache",
        sandbox_trees=(
            "mkosi.builddir/debian-backports.sources"
            ":/etc/apt/sources.list.d/debian-backports.sources",
        ),
    )
    img.kernel = Kernel.tdx_kernel("6.8", cmdline=UPSTREAM_KERNEL_CMDLINE)
    img.install(*UPSTREAM_RUNTIME_PACKAGES)
    img.build_install(*UPSTREAM_BUILD_PACKAGES)

    # Skeleton files matching upstream base/mkosi.skeleton/
    img.skeleton("/etc/resolv.conf", content=_ref("mkosi.skeleton/etc/resolv.conf"))
    img.skeleton(
        "/etc/systemd/system/network-setup.service",
        content=_ref("mkosi.skeleton/etc/systemd/system/network-setup.service"),
    )
    # minimal.target is auto-emitted by debloat

    img.debloat(enabled=True)
    Tdxs(issuer_type="dcap").apply(img)
    return img


@pytest.fixture(scope="module")
def emission_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile once and share across all tests in this module."""
    img = _build_nethermind_base_image()
    out = tmp_path_factory.mktemp("nethermind_golden")
    img.compile(out / "mkosi")
    return out / "mkosi"


def _ref(relpath: str) -> str:
    """Read a reference file from the golden snapshot."""
    return (GOLDEN_REF / relpath).read_text(encoding="utf-8")


def _extract_package_list(conf_text: str, key: str) -> set[str]:
    """Extract a multi-line package list from mkosi.conf text."""
    pattern = rf"^{re.escape(key)}=(.*?)(?=^\S|\Z)"
    match = re.search(pattern, conf_text, re.MULTILINE | re.DOTALL)
    if not match:
        return set()
    raw = match.group(1)
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _find_script(emission_dir: Path, pattern: str) -> str:
    """Find and read a script matching the glob pattern."""
    scripts_dir = emission_dir / "default" / "scripts"
    matches = sorted(scripts_dir.glob(pattern))
    assert matches, f"No script matching {pattern}"
    return matches[0].read_text()


def _extract_bash_array(text: str, name: str) -> set[str]:
    """Extract values from a bash array like: name=("a" "b" "c")."""
    pattern = rf'{re.escape(name)}=\((.*?)\)'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return set()
    raw = match.group(1)
    return {m.group(1) for m in re.finditer(r'"([^"]+)"', raw)}


def _extract_rm_rf_paths(text: str) -> set[str]:
    """Extract paths from rm -rf "$BUILDROOT/path" lines."""
    return {m.group(1) for m in re.finditer(r'rm -rf "\$BUILDROOT(/[^"]+)"', text)}


def _extract_debloat_array_paths(text: str) -> set[str]:
    """Extract paths from the upstream debloat_paths bash array."""
    match = re.search(r'debloat_paths=\((.*?)\)', text, re.DOTALL)
    if not match:
        return set()
    return {m.group(1) for m in re.finditer(r'"(/[^"]+)"', match.group(1))}


# ═══════════════════════════════════════════════════════════════════════
# Tests: matches (SDK output == upstream)
# ═══════════════════════════════════════════════════════════════════════


class TestUpstreamMatches:
    """Settings where SDK output matches the upstream repo exactly."""

    def test_distribution_section(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        ref = _ref("mkosi.conf")
        for key in ("Distribution=debian", "Release=trixie", "Architecture=x86-64"):
            assert key in sdk
            assert key in ref

    def test_output_format_and_manifest(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert "Format=uki" in sdk
        assert "ManifestFormat=json" in sdk

    def test_seed_matches_upstream(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert f"Seed={UPSTREAM_SEED}" in sdk

    def test_output_directory_matches(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert "OutputDirectory=build" in sdk

    def test_source_date_epoch(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        ref = _ref("mkosi.conf")
        assert "SourceDateEpoch=0" in sdk
        assert "SourceDateEpoch=0" in ref

    def test_with_network_syntax(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        ref = _ref("mkosi.conf")
        assert "WithNetwork=true" in sdk
        assert "WithNetwork=true" in ref

    def test_clean_package_metadata_syntax(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        ref = _ref("mkosi.conf")
        assert "CleanPackageMetadata=true" in sdk
        assert "CleanPackageMetadata=true" in ref

    def test_sandbox_trees(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert "SandboxTrees=mkosi.builddir/debian-backports.sources" in sdk

    def test_package_cache_directory(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert "PackageCacheDirectory=mkosi.cache" in sdk

    def test_runtime_packages_exact_match(self, emission_dir: Path) -> None:
        sdk = _extract_package_list(
            (emission_dir / "default" / "mkosi.conf").read_text(), "Packages"
        )
        assert sdk == set(UPSTREAM_RUNTIME_PACKAGES)

    def test_upstream_build_packages_present(self, emission_dir: Path) -> None:
        """Every upstream build package is present in SDK output."""
        sdk = _extract_package_list(
            (emission_dir / "default" / "mkosi.conf").read_text(), "BuildPackages"
        )
        for pkg in UPSTREAM_BUILD_PACKAGES:
            assert pkg in sdk, f"upstream build package {pkg!r} missing from SDK"

    def test_kernel_cmdline_matches(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        assert f"KernelCommandLine={UPSTREAM_KERNEL_CMDLINE}" in sdk

    def test_init_script_byte_identical(self, emission_dir: Path) -> None:
        sdk_init = (emission_dir / "default" / "mkosi.skeleton" / "init").read_text()
        ref_init = _ref("mkosi.skeleton/init")
        assert sdk_init == ref_init

    def test_init_script_executable(self, emission_dir: Path) -> None:
        init_path = emission_dir / "default" / "mkosi.skeleton" / "init"
        assert init_path.stat().st_mode & 0o111 != 0

    def test_skeleton_resolv_conf_matches(self, emission_dir: Path) -> None:
        sdk = (emission_dir / "default" / "mkosi.skeleton" / "etc" / "resolv.conf").read_text()
        ref = _ref("mkosi.skeleton/etc/resolv.conf")
        assert sdk == ref

    def test_skeleton_minimal_target_matches(self, emission_dir: Path) -> None:
        sdk = (
            emission_dir
            / "default"
            / "mkosi.skeleton"
            / "etc"
            / "systemd"
            / "system"
            / "minimal.target"
        ).read_text()
        ref = _ref("mkosi.skeleton/etc/systemd/system/minimal.target")
        assert sdk == ref

    def test_skeleton_network_setup_service_matches(self, emission_dir: Path) -> None:
        sdk = (
            emission_dir
            / "default"
            / "mkosi.skeleton"
            / "etc"
            / "systemd"
            / "system"
            / "network-setup.service"
        ).read_text()
        ref = _ref("mkosi.skeleton/etc/systemd/system/network-setup.service")
        assert sdk == ref

    def test_debloat_systemd_bin_whitelist_matches(self, emission_dir: Path) -> None:
        sdk_bins = _extract_bash_array(
            _find_script(emission_dir, "*-postinst.sh"), "systemd_bin_whitelist"
        )
        ref_bins = _extract_bash_array(_ref("debloat-systemd.sh"), "systemd_bin_whitelist")
        assert sdk_bins == ref_bins

    def test_debloat_systemd_unit_whitelist_matches(self, emission_dir: Path) -> None:
        sdk_units = _extract_bash_array(
            _find_script(emission_dir, "*-postinst.sh"), "systemd_svc_whitelist"
        )
        ref_units = _extract_bash_array(_ref("debloat-systemd.sh"), "systemd_svc_whitelist")
        assert sdk_units == ref_units

    def test_finalize_var_cleanup_matches(self, emission_dir: Path) -> None:
        """SDK now cleans var/log and var/cache like upstream."""
        sdk = _find_script(emission_dir, "*-finalize.sh")
        assert 'find "$BUILDROOT/var/log" -type f -delete' in sdk
        assert 'find "$BUILDROOT/var/cache" -type f -delete' in sdk

    def test_finalize_debloat_paths_superset_of_upstream(self, emission_dir: Path) -> None:
        """SDK removes all upstream debloat paths (except upstream-specific ones)."""
        sdk_paths = _extract_rm_rf_paths(_find_script(emission_dir, "*-finalize.sh"))
        upstream_paths = _extract_debloat_array_paths(_ref("debloat.sh"))

        # Paths that are upstream-specific (not expected in SDK)
        upstream_specific = {"/nix", "/etc/*-"}
        expected_upstream = upstream_paths - upstream_specific

        missing = expected_upstream - sdk_paths
        assert not missing, f"SDK missing upstream debloat paths: {missing}"


# ═══════════════════════════════════════════════════════════════════════
# Tests: documented divergences (intentional SDK differences)
# ═══════════════════════════════════════════════════════════════════════


class TestDocumentedDivergences:
    """Known differences between SDK output and upstream, with justification."""

    def test_sdk_adds_imageid(self, emission_dir: Path) -> None:
        """SDK always emits ImageId (mkosi requires it); upstream omits it."""
        sdk = (emission_dir / "default" / "mkosi.conf").read_text()
        ref = _ref("mkosi.conf")
        assert "ImageId=default" in sdk
        assert "ImageId" not in ref

    def test_sdk_adds_golang_from_tdxs_module(self, emission_dir: Path) -> None:
        """Tdxs module adds golang to build packages; upstream has it in overlay."""
        sdk_bpkgs = _extract_package_list(
            (emission_dir / "default" / "mkosi.conf").read_text(), "BuildPackages"
        )
        assert "golang" in sdk_bpkgs

    def test_sdk_has_tdxs_overlay_in_extra(self, emission_dir: Path) -> None:
        """SDK includes tdxs config/units in extra tree (upstream has separate overlay)."""
        cfg = (
            emission_dir / "default" / "mkosi.extra" / "etc" / "tdxs" / "config.yaml"
        ).read_text()
        assert "type: dcap" in cfg
        assert "systemd: true" in cfg

        svc = (
            emission_dir
            / "default"
            / "mkosi.extra"
            / "usr"
            / "lib"
            / "systemd"
            / "system"
            / "tdxs.service"
        ).read_text()
        assert "User=tdxs" in svc
        assert "Group=tdx" in svc

    def test_sdk_postinst_includes_tdxs_user_group(self, emission_dir: Path) -> None:
        """SDK postinst creates tdxs user/group (upstream does this in overlay)."""
        sdk = _find_script(emission_dir, "*-postinst.sh")
        assert "groupadd --system tdx" in sdk
        assert "useradd --system" in sdk
        assert "tdxs.socket" in sdk

    def test_upstream_efi_stub_not_modeled(self) -> None:
        """Upstream pins systemd-boot-efi from snapshot.debian.org (deployment-specific)."""
        ref = _ref("efi-stub.sh")
        assert "snapshot.debian.org" in ref

    def test_upstream_add_backports_not_modeled(self) -> None:
        """Upstream generates backports sources dynamically (deployment-specific)."""
        ref = _ref("add-backports.sh")
        assert "backports" in ref

    def test_upstream_remove_image_version_not_modeled(self) -> None:
        """Upstream strips IMAGE_VERSION from os-release (can be added via hook)."""
        ref = _ref("remove-image-version.sh")
        assert "IMAGE_VERSION" in ref

    def test_upstream_debloat_has_nix_path(self) -> None:
        """Upstream removes /nix (build env leftover); SDK doesn't have Nix."""
        upstream_paths = _extract_debloat_array_paths(_ref("debloat.sh"))
        assert "/nix" in upstream_paths


# ═══════════════════════════════════════════════════════════════════════
# Tests: build script and determinism
# ═══════════════════════════════════════════════════════════════════════


class TestBuildAndDeterminism:

    def test_build_script_tdxs(self, emission_dir: Path) -> None:
        sdk = _find_script(emission_dir, "*-build.sh")
        assert "go build" in sdk
        assert "NethermindEth/tdxs" in sdk
        assert "-trimpath" in sdk

    def test_emission_deterministic(self, tmp_path: Path) -> None:
        """Two identical SDK configs produce identical output trees."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        _build_nethermind_base_image().compile(dir_a)
        _build_nethermind_base_image().compile(dir_b)
        assert snapshot_tree(dir_a) == snapshot_tree(dir_b)
