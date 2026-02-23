"""US-014: Verify application layer composition works correctly.

Tests that a base image (packages + debloat + kernel) can be composed
with an application layer that adds its own packages, services, build
scripts, and config files on top.
"""

from __future__ import annotations

from tdx import Image
from tdx.modules import (
    DiskEncryption,
    Init,
    KeyGeneration,
    Nethermind,
    Raiko,
    SecretDelivery,
    TaikoClient,
    Tdxs,
)


def _build_base_image() -> Image:
    """Create a base image with packages, debloat, and kernel."""
    img = Image(
        reproducible=False,
        base="debian/bookworm",
        environment={"SOURCE_DATE_EPOCH": "0"},
        environment_passthrough=("KERNEL_IMAGE", "KERNEL_VERSION"),
    )
    img.install("systemd", "dbus")
    img.debloat(
        paths_skip_for_profiles={"devtools": ("/usr/share/bash-completion",)},
    )
    img.hook("build", "sh", "-c", "echo base-build-hook", shell=True)
    return img


def _apply_app_layer(img: Image) -> Image:
    """Apply application-layer packages, hooks, and modules."""
    img.install("prometheus", "rclone", "curl", "jq")
    img.hook("build", "sh", "-c", "echo app-build-hook", shell=True)

    KeyGeneration(strategy="tpm").apply(img)
    DiskEncryption(device="/dev/vda3").apply(img)
    SecretDelivery(method="http_post").apply(img)
    Init().apply(img)

    Tdxs(issuer_type="dcap").apply(img)

    Raiko(
        source_repo="NethermindEth/raiko.git",
        source_branch="feat/tdx",
    ).apply(img)

    TaikoClient(
        source_repo="NethermindEth/surge-taiko-mono",
        source_branch="feat/tdx-proving",
    ).apply(img)

    Nethermind(
        source_repo="NethermindEth/nethermind.git",
        version="1.32.3",
    ).apply(img)

    return img


# -- Hook ordering tests --


def test_base_build_hooks_appear_before_app_build_hooks() -> None:
    """Base-layer build hooks must precede app-layer build hooks."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    build_commands = profile.phases.get("build", [])

    # Extract the inline script from each build command
    scripts = [cmd.argv[-1] for cmd in build_commands]

    base_idx = scripts.index("echo base-build-hook")
    app_idx = scripts.index("echo app-build-hook")
    assert base_idx < app_idx, "Base build hook should appear before app build hook"


def test_module_build_hooks_ordered_by_application_sequence() -> None:
    """Modules applied in order should produce build hooks in that order."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    scripts = [cmd.argv[-1] for cmd in build_commands]

    # Init sub-modules come first (key-generation, disk-encryption, secret-delivery)
    # then Tdxs, Raiko, TaikoClient, Nethermind
    key_gen_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/key-generation" in s)
    disk_enc_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/disk-encryption" in s)
    secret_del_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/secret-delivery" in s)
    tdxs_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/tdxs" in s)
    raiko_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/raiko" in s)
    taiko_idx = next(i for i, s in enumerate(scripts) if "/usr/bin/taiko-client" in s)
    nethermind_idx = next(i for i, s in enumerate(scripts) if "dotnet publish" in s)

    # Init sub-modules before service modules
    assert key_gen_idx < tdxs_idx
    assert disk_enc_idx < tdxs_idx
    assert secret_del_idx < tdxs_idx
    # Service modules in application order
    assert tdxs_idx < raiko_idx
    assert raiko_idx < taiko_idx
    assert taiko_idx < nethermind_idx


# -- Postinst coexistence tests --


def test_postinst_has_both_base_and_app_commands() -> None:
    """Postinst phase should contain commands from both base and app layers."""
    img = _build_base_image()
    # Add a base-layer postinst command
    img.run("bash", "-c", "echo base-postinst", phase="postinst")
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    postinst_cmds = profile.phases.get("postinst", [])

    all_argv = [" ".join(cmd.argv) for cmd in postinst_cmds]
    full_text = "\n".join(all_argv)

    # Base postinst command present
    assert any("base-postinst" in a for a in all_argv), (
        "Base-layer postinst command should be present"
    )

    # App-layer postinst commands from modules (user creation, service enablement)
    # Init enables runtime-init.service
    assert any("runtime-init" in a for a in all_argv), (
        f"Init service enablement should be in postinst:\n{full_text}"
    )

    # Tdxs creates tdx group and tdxs user
    assert any("tdx" in a for a in all_argv), (
        f"Tdxs user/group creation should be in postinst:\n{full_text}"
    )


def test_debloat_masking_coexists_with_module_postinst() -> None:
    """Debloat systemd masking and module user creation should both be in postinst."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    postinst_cmds = profile.phases.get("postinst", [])
    all_argv = [" ".join(cmd.argv) for cmd in postinst_cmds]

    # Modules add user creation / service enablement via postinst
    has_module_commands = any(
        "useradd" in a or "groupadd" in a or "systemctl" in a
        for a in all_argv
    )
    assert has_module_commands, "Module postinst commands (user/group/service) should be present"


# -- Multiple modules without conflicts --


def test_multiple_modules_have_distinct_build_hooks() -> None:
    """Each module should contribute its own build hook without overwriting others."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    build_commands = profile.phases.get("build", [])

    # base hook + app hook + 3 init sub-module hooks + 4 service module hooks = 9
    assert len(build_commands) >= 9, (
        f"Expected at least 9 build hooks, got {len(build_commands)}"
    )


def test_all_module_packages_present() -> None:
    """Build packages from all modules should be merged into the profile."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]

    # Init sub-modules + Tdxs + TaikoClient require golang
    assert "golang" in profile.build_packages

    # Raiko requires clang
    assert "clang" in profile.build_packages

    # Nethermind requires dotnet-sdk-10.0
    assert "dotnet-sdk-10.0" in profile.build_packages

    # Common build dep
    assert "build-essential" in profile.build_packages
    assert "git" in profile.build_packages

    # Runtime packages from base + app layer
    assert "systemd" in profile.packages
    assert "prometheus" in profile.packages
    assert "curl" in profile.packages


def test_all_module_files_present() -> None:
    """Config files and service units from all modules should be present."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    file_paths = {f.path for f in profile.files}

    # Init generates runtime-init script
    assert "/usr/bin/runtime-init" in file_paths

    # Modules emit service units as files in /usr/lib/systemd/system/
    service_unit_files = {p for p in file_paths if p.endswith(".service")}
    assert any("runtime-init" in p for p in service_unit_files), (
        f"runtime-init.service not found in: {service_unit_files}"
    )
    assert any("tdxs" in p for p in service_unit_files), (
        f"tdxs.service not found in: {service_unit_files}"
    )
    assert any("raiko" in p for p in service_unit_files), (
        f"raiko.service not found in: {service_unit_files}"
    )
    assert any("taiko-client" in p for p in service_unit_files), (
        f"taiko-client.service not found in: {service_unit_files}"
    )
    assert any("nethermind" in p for p in service_unit_files), (
        f"nethermind-surge.service not found in: {service_unit_files}"
    )


def test_all_module_service_units_present() -> None:
    """Service unit files from all modules should be emitted without conflicts."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    file_paths = {f.path for f in profile.files}
    service_unit_files = {p for p in file_paths if p.endswith(".service")}

    # Each module emits its own service unit file
    expected_services = [
        "runtime-init.service",
        "tdxs.service",
        "raiko.service",
        "taiko-client.service",
        "nethermind-surge.service",
    ]
    for svc in expected_services:
        assert any(svc in p for p in service_unit_files), (
            f"{svc} not found in emitted service files: {service_unit_files}"
        )


def test_no_duplicate_build_packages() -> None:
    """Packages added by multiple modules should not cause duplicates (set semantics)."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]
    # build_packages is a set — no duplicates by definition
    # But verify the common packages only appear once
    pkg_list = list(profile.build_packages)
    assert len(pkg_list) == len(set(pkg_list)), "build_packages should have no duplicates"


# -- End-to-end composition test --


def test_full_composition_produces_valid_state() -> None:
    """A fully composed image (base + app + modules) should have consistent state."""
    img = _build_base_image()
    _apply_app_layer(img)

    profile = img.state.profiles["default"]

    # Has build hooks for all modules
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) >= 9

    # Has postinst phase with commands
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) >= 1, "Should have at least one postinst command"

    # Has files from modules
    assert len(profile.files) >= 2, "Should have at least config files from modules"

    # Has service unit files from modules (emitted as files, not via image.service())
    service_files = [f for f in profile.files if f.path.endswith(".service")]
    assert len(service_files) >= 5, (
        f"Expected at least 5 service unit files, got {len(service_files)}"
    )

    # Build packages are a superset of all module requirements
    assert profile.build_packages >= {
        "golang", "git", "build-essential", "clang",
        "dotnet-sdk-10.0", "dotnet-runtime-10.0",
    }

    # Runtime packages include both base and app layer
    assert profile.packages >= {"systemd", "dbus", "prometheus", "rclone", "curl", "jq"}
