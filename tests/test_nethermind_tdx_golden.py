"""Integration test: SDK emission vs real NethermindEth/nethermind-tdx repo.

Compiles the surge_tdx_prover example (full image, all profiles), clones
the upstream repo, and performs semantic comparison of the two project trees.

The two trees have different directory layouts, so this test compares
behavioral artifacts rather than raw file paths:
- Packages declared in mkosi.conf
- Systemd unit files (by basename)
- Config files under mkosi.extra/ (by image-root-relative path)
- Skeleton files (init script, resolv.conf, etc.)
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

_IMAGE_PATH = Path(__file__).resolve().parent.parent / "examples" / "surge-tdx-prover" / "image.py"
_spec = importlib.util.spec_from_file_location("surge_tdx_prover_image", _IMAGE_PATH)
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
build_surge_tdx_prover = _mod.build_surge_tdx_prover

pytestmark = pytest.mark.integration

# Pin upstream to a known commit; override via UPSTREAM_COMMIT env var.
UPSTREAM_COMMIT = os.environ.get(
    "NETHERMIND_TDX_COMMIT", "7ac44e7d9baecf8743a20e75b30fd4ddfce3e85f"
)

REQUIRED_SECTIONS = [
    "## Packages",
    "## Systemd units",
    "## Config files",
    "## Skeleton files",
    "## Summary",
]


@pytest.fixture(scope="module")
def sdk_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Compile the full surge-tdx-prover image with all profiles."""
    img = build_surge_tdx_prover()
    # Activate all profiles so the emission includes azure, gcp, devtools
    img._active_profiles = tuple(sorted(img.state.profiles.keys()))
    out = tmp_path_factory.mktemp("sdk_emission")
    img.compile(out / "mkosi")
    return out / "mkosi"


@pytest.fixture(scope="module")
def upstream_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Clone NethermindEth/nethermind-tdx at a pinned commit."""
    if not shutil.which("git"):
        pytest.skip("git not available")
    repo_dir = tmp_path_factory.mktemp("upstream") / "nethermind-tdx"
    subprocess.run(
        [
            "git",
            "clone",
            "https://github.com/NethermindEth/nethermind-tdx.git",
            str(repo_dir),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "checkout", UPSTREAM_COMMIT],
        check=True,
        capture_output=True,
    )
    return repo_dir


# ── Extraction helpers ────────────────────────────────────────────────


def _parse_mkosi_packages(conf_path: Path) -> set[str]:
    """Extract Packages= lines from an mkosi.conf (INI-like format)."""
    packages: set[str] = set()
    text = conf_path.read_text(encoding="utf-8")
    # mkosi uses repeated keys, which configparser doesn't handle well.
    # Parse manually.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("Packages="):
            value = stripped.split("=", 1)[1].strip()
            if value:
                packages.add(value)
        elif stripped and not stripped.startswith(("#", "[")) and "=" not in stripped:
            # Continuation line inside a multi-value key (indented package name)
            packages.add(stripped)
    return packages


def _collect_upstream_packages(repo: Path) -> set[str]:
    """Collect all Packages= from upstream mkosi configs."""
    packages: set[str] = set()
    for conf in repo.rglob("*.conf"):
        if conf.name == "mkosi.conf" or conf.name.endswith(".conf"):
            packages |= _parse_mkosi_packages(conf)
    return packages


def _collect_sdk_packages(sdk: Path) -> set[str]:
    """Collect Packages= from the default profile's mkosi.conf."""
    default_conf = sdk / "default" / "mkosi.conf"
    if default_conf.exists():
        return _parse_mkosi_packages(default_conf)
    return set()


def _collect_systemd_units(root: Path) -> dict[str, Path]:
    """Collect systemd unit files by basename."""
    units: dict[str, Path] = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix in (".service", ".socket", ".target", ".timer"):
            units[f.name] = f
    return units


def _collect_extra_files(root: Path) -> dict[str, Path]:
    """Collect files under mkosi.extra/ keyed by image-root-relative path.

    e.g. mkosi.extra/etc/foo.conf → /etc/foo.conf
    """
    files: dict[str, Path] = {}
    for extra_dir in root.rglob("mkosi.extra"):
        if not extra_dir.is_dir():
            continue
        for f in sorted(extra_dir.rglob("*")):
            if f.is_file():
                rel = "/" + f.relative_to(extra_dir).as_posix()
                files[rel] = f
    return files


def _collect_skeleton_files(root: Path) -> dict[str, Path]:
    """Collect files under mkosi.skeleton/ keyed by image-root-relative path."""
    files: dict[str, Path] = {}
    for skel_dir in root.rglob("mkosi.skeleton"):
        if not skel_dir.is_dir():
            continue
        for f in sorted(skel_dir.rglob("*")):
            if f.is_file():
                rel = "/" + f.relative_to(skel_dir).as_posix()
                files[rel] = f
    return files


# ── Comparison ────────────────────────────────────────────────────────


def _compare_sets(
    upstream: set[str], sdk: set[str], label: str
) -> tuple[list[str], list[str], list[str]]:
    """Return (only_upstream, only_sdk, shared) for a named set."""
    only_up = sorted(upstream - sdk)
    only_sdk_items = sorted(sdk - upstream)
    shared = sorted(upstream & sdk)
    return only_up, only_sdk_items, shared


def _compare_file_dicts(
    upstream: dict[str, Path], sdk: dict[str, Path]
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Compare two filename→Path dicts. Returns (only_up, only_sdk, matches, diffs)."""
    only_up = sorted(set(upstream) - set(sdk))
    only_sdk_items = sorted(set(sdk) - set(upstream))
    matches: list[str] = []
    diffs: list[str] = []
    for key in sorted(set(upstream) & set(sdk)):
        if upstream[key].read_bytes() == sdk[key].read_bytes():
            matches.append(key)
        else:
            diffs.append(key)
    return only_up, only_sdk_items, matches, diffs


# ── Test ──────────────────────────────────────────────────────────────


def test_compare_with_upstream(
    sdk_output: Path,
    upstream_repo: Path,
    tmp_path: Path,
) -> None:
    """Semantically compare SDK output against upstream NethermindEth/nethermind-tdx."""

    report_parts: list[str] = []

    # 1. Packages
    upstream_pkgs = _collect_upstream_packages(upstream_repo)
    sdk_pkgs = _collect_sdk_packages(sdk_output)
    pkg_only_up, pkg_only_sdk, pkg_shared = _compare_sets(upstream_pkgs, sdk_pkgs, "packages")

    report_parts.append("## Packages")
    up_pkgs = ", ".join(pkg_only_up) or "(none)"
    sdk_pkgs_str = ", ".join(pkg_only_sdk) or "(none)"
    report_parts.append(f"- upstream-only ({len(pkg_only_up)}): {up_pkgs}")
    report_parts.append(f"- sdk-only ({len(pkg_only_sdk)}): {sdk_pkgs_str}")
    report_parts.append(f"- shared: {len(pkg_shared)}")

    # 2. Systemd units (by basename)
    upstream_units = _collect_systemd_units(upstream_repo)
    sdk_units = _collect_systemd_units(sdk_output)
    unit_only_up, unit_only_sdk, unit_matches, unit_diffs = _compare_file_dicts(
        upstream_units, sdk_units
    )

    report_parts.append("\n## Systemd units")
    report_parts.append(f"- upstream-only: {', '.join(unit_only_up) or '(none)'}")
    report_parts.append(f"- sdk-only: {', '.join(unit_only_sdk) or '(none)'}")
    report_parts.append(f"- matching: {len(unit_matches)}")
    report_parts.append(f"- differing: {', '.join(unit_diffs) or '(none)'}")

    # 3. Config files in mkosi.extra/
    upstream_extra = _collect_extra_files(upstream_repo)
    sdk_extra = _collect_extra_files(sdk_output)
    extra_only_up, extra_only_sdk, extra_matches, extra_diffs = _compare_file_dicts(
        upstream_extra, sdk_extra
    )

    report_parts.append("\n## Config files")
    up_extra = ", ".join(extra_only_up[:20]) or "(none)"
    sdk_extra_str = ", ".join(extra_only_sdk[:20]) or "(none)"
    report_parts.append(f"- upstream-only ({len(extra_only_up)}): {up_extra}")
    report_parts.append(f"- sdk-only ({len(extra_only_sdk)}): {sdk_extra_str}")
    report_parts.append(f"- matching: {len(extra_matches)}")
    report_parts.append(f"- differing: {', '.join(extra_diffs) or '(none)'}")

    # 4. Skeleton files
    upstream_skel = _collect_skeleton_files(upstream_repo)
    sdk_skel = _collect_skeleton_files(sdk_output)
    skel_only_up, skel_only_sdk, skel_matches, skel_diffs = _compare_file_dicts(
        upstream_skel, sdk_skel
    )

    report_parts.append("\n## Skeleton files")
    report_parts.append(f"- upstream-only: {', '.join(skel_only_up) or '(none)'}")
    report_parts.append(f"- sdk-only: {', '.join(skel_only_sdk) or '(none)'}")
    report_parts.append(f"- matching: {len(skel_matches)}")
    report_parts.append(f"- differing: {', '.join(skel_diffs) or '(none)'}")

    # 5. Summary
    total_checked = (
        len(pkg_shared)
        + len(unit_matches) + len(unit_diffs)
        + len(extra_matches) + len(extra_diffs)
        + len(skel_matches) + len(skel_diffs)
    )
    total_matches = len(pkg_shared) + len(unit_matches) + len(extra_matches) + len(skel_matches)
    total_diffs = len(unit_diffs) + len(extra_diffs) + len(skel_diffs)

    report_parts.append("\n## Summary")
    report_parts.append(f"- total artifacts checked: {total_checked}")
    report_parts.append(f"- matches: {total_matches}")
    report_parts.append(f"- differences: {total_diffs}")

    report = "\n".join(report_parts) + "\n"
    report_file = tmp_path / "comparison_report.md"
    report_file.write_text(report, encoding="utf-8")
    print(report)

    # Structural assertions
    for section in REQUIRED_SECTIONS:
        assert section in report, f"Report missing required section: {section}"

    # Core semantic assertions — these services must be present in both
    required_units = {
        "runtime-init.service",
        "nethermind-surge.service",
        "raiko.service",
        "taiko-client.service",
        "tdxs.service",
        "tdxs.socket",
    }
    missing_upstream_units = required_units - set(upstream_units)
    missing_sdk_units = required_units - set(sdk_units)
    assert not missing_upstream_units, f"Upstream missing units: {missing_upstream_units}"
    assert not missing_sdk_units, f"SDK missing units: {missing_sdk_units}"

    # Skeleton: both must have /init and /etc/resolv.conf
    for skel_path in ("/init", "/etc/resolv.conf"):
        assert skel_path in upstream_skel, f"Upstream missing skeleton: {skel_path}"
        assert skel_path in sdk_skel, f"SDK missing skeleton: {skel_path}"

    # Key config files must exist in SDK extra
    required_sdk_configs = {
        "/etc/tdx/key-gen.yaml",
        "/etc/tdx/disk-setup.yaml",
        "/etc/tdx/secrets.yaml",
        "/etc/tdxs/config.yaml",
        "/etc/default/dropbear",
        "/etc/udev/rules.d/99-tdx-symlink.rules",
        "/usr/bin/runtime-init",
    }
    missing_sdk_configs = required_sdk_configs - set(sdk_extra)
    assert not missing_sdk_configs, f"SDK missing config files: {missing_sdk_configs}"

    # Upstream tdx-init config must exist
    assert "/etc/tdx-init/config.yaml" in upstream_extra, "Upstream missing tdx-init config"

    # Package overlap: the SDK should cover the vast majority of upstream packages
    if upstream_pkgs:
        coverage = len(pkg_shared) / len(upstream_pkgs)
        assert coverage >= 0.5, (
            f"SDK covers only {coverage:.0%} of upstream packages. "
            f"Missing: {', '.join(pkg_only_up[:15])}"
        )
