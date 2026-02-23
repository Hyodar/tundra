"""Tests for Azure platform profile helper."""

from tdx import Image
from tdx.profiles.azure import (
    AZURE_PROVISIONING_SCRIPT,
    AZURE_PROVISIONING_SERVICE,
    apply_azure_profile,
)


def test_azure_profile_adds_dmidecode_package() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    assert "dmidecode" in profile.packages


def test_azure_profile_emits_provisioning_script() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    file_paths = {f.path for f in profile.files}
    assert "/usr/bin/azure-complete-provisioning" in file_paths

    script_entry = next(
        f for f in profile.files if f.path == "/usr/bin/azure-complete-provisioning"
    )
    assert script_entry.mode == "0755"
    assert script_entry.content == AZURE_PROVISIONING_SCRIPT


def test_azure_provisioning_script_content() -> None:
    """Verify the provisioning script checks dmidecode and contacts wireserver."""
    assert "dmidecode -s system-manufacturer" in AZURE_PROVISIONING_SCRIPT
    assert "Microsoft Corporation" in AZURE_PROVISIONING_SCRIPT
    assert "168.63.129.16" in AZURE_PROVISIONING_SCRIPT
    assert "Health" in AZURE_PROVISIONING_SCRIPT
    assert "Ready" in AZURE_PROVISIONING_SCRIPT
    assert "MAX_RETRIES=5" in AZURE_PROVISIONING_SCRIPT
    assert "goalstate" in AZURE_PROVISIONING_SCRIPT


def test_azure_profile_emits_service_unit() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    file_paths = {f.path for f in profile.files}
    assert (
        "/usr/lib/systemd/system/azure-complete-provisioning.service" in file_paths
    )

    svc_entry = next(
        f
        for f in profile.files
        if f.path == "/usr/lib/systemd/system/azure-complete-provisioning.service"
    )
    assert svc_entry.content == AZURE_PROVISIONING_SERVICE


def test_azure_service_unit_content() -> None:
    """Verify the service unit has correct Type, After, Requires, RemainAfterExit."""
    assert "Type=oneshot" in AZURE_PROVISIONING_SERVICE
    assert "After=network.target network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "Requires=network-setup.service" in AZURE_PROVISIONING_SERVICE
    assert "RemainAfterExit=yes" in AZURE_PROVISIONING_SERVICE
    assert "ExecStart=/usr/bin/azure-complete-provisioning" in AZURE_PROVISIONING_SERVICE


def test_azure_profile_enables_service_in_postinst() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) >= 1

    # Check that systemctl enable is called
    enable_cmds = [
        cmd
        for cmd in postinst_commands
        if "systemctl" in cmd.argv and "enable" in cmd.argv
    ]
    assert len(enable_cmds) >= 1
    assert any(
        "azure-complete-provisioning.service" in cmd.argv for cmd in enable_cmds
    )


def test_azure_profile_symlinks_to_minimal_target() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    postinst_commands = profile.phases.get("postinst", [])

    # Check symlink into minimal.target.wants
    link_cmds = [cmd for cmd in postinst_commands if "ln" in cmd.argv]
    assert len(link_cmds) >= 1
    link_cmd = link_cmds[0]
    assert "minimal.target.wants/azure-complete-provisioning.service" in " ".join(
        link_cmd.argv
    )


def test_azure_profile_sets_output_target() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    profile = image.state.profiles["azure"]
    assert "azure" in profile.output_targets


def test_azure_profile_does_not_affect_default_profile() -> None:
    image = Image(reproducible=False)

    with image.profile("azure"):
        apply_azure_profile(image)

    default_profile = image.state.profiles["default"]
    assert "dmidecode" not in default_profile.packages
    assert not any(
        f.path == "/usr/bin/azure-complete-provisioning"
        for f in default_profile.files
    )
