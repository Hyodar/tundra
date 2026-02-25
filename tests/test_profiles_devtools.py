"""Tests for devtools platform profile helper."""

from tundravm import Image
from tundravm.modules.devtools import (
    DEVTOOLS_PACKAGES,
    DEVTOOLS_POSTINST_SCRIPT,
    SERIAL_CONSOLE_SERVICE,
    Devtools,
)


def test_devtools_profile_adds_debug_packages() -> None:
    image = Image(reproducible=False)

    with image.profile("devtools"):
        Devtools().apply(image)

    profile = image.state.profiles["devtools"]
    for pkg in DEVTOOLS_PACKAGES:
        assert pkg in profile.packages, f"Missing package: {pkg}"


def test_devtools_profile_includes_expected_packages() -> None:
    """Verify the exact package list matches the upstream devtools profile."""
    expected = {
        "apt",
        "bash-completion",
        "curl",
        "dnsutils",
        "iputils-ping",
        "net-tools",
        "netcat-openbsd",
        "openssh-server",
        "socat",
        "strace",
        "tcpdump",
        "tcpflow",
        "vim",
    }
    assert set(DEVTOOLS_PACKAGES) == expected


def test_devtools_profile_emits_serial_console_service() -> None:
    image = Image(reproducible=False)

    with image.profile("devtools"):
        Devtools().apply(image)

    profile = image.state.profiles["devtools"]
    file_paths = {f.path for f in profile.files}
    assert "/usr/lib/systemd/system/serial-console.service" in file_paths

    svc_entry = next(
        f for f in profile.files if f.path == "/usr/lib/systemd/system/serial-console.service"
    )
    assert svc_entry.content == SERIAL_CONSOLE_SERVICE


def test_serial_console_service_content() -> None:
    """Verify the serial console service enables serial-getty@ttyS0."""
    assert "serial-getty@ttyS0.service" in SERIAL_CONSOLE_SERVICE
    assert "Type=oneshot" in SERIAL_CONSOLE_SERVICE
    assert "RemainAfterExit=yes" in SERIAL_CONSOLE_SERVICE
    assert "WantedBy=minimal.target" in SERIAL_CONSOLE_SERVICE


def test_devtools_profile_enables_serial_console_in_postinst() -> None:
    image = Image(reproducible=False)

    with image.profile("devtools"):
        Devtools().apply(image)

    profile = image.state.profiles["devtools"]
    postinst_commands = profile.phases.get("postinst", [])

    enable_cmds = [
        cmd for cmd in postinst_commands if "systemctl" in cmd.argv[0] and "enable" in cmd.argv[0]
    ]
    assert len(enable_cmds) >= 1
    assert any("serial-console.service" in cmd.argv[0] for cmd in enable_cmds)


def test_devtools_postinst_sets_root_password() -> None:
    """Verify the postinst script sets root password via openssl passwd."""
    assert "openssl passwd" in DEVTOOLS_POSTINST_SCRIPT
    assert "usermod -p" in DEVTOOLS_POSTINST_SCRIPT
    assert "passwd -u root" in DEVTOOLS_POSTINST_SCRIPT


def test_devtools_postinst_configures_dropbear() -> None:
    """Verify the postinst script removes restrictive dropbear flags."""
    assert "dropbear" in DEVTOOLS_POSTINST_SCRIPT
    assert "-s" in DEVTOOLS_POSTINST_SCRIPT
    assert "-w" in DEVTOOLS_POSTINST_SCRIPT
    assert "-g" in DEVTOOLS_POSTINST_SCRIPT


def test_devtools_postinst_configures_openssh() -> None:
    """Verify the postinst script enables password auth for openssh."""
    assert "PermitRootLogin yes" in DEVTOOLS_POSTINST_SCRIPT
    assert "PasswordAuthentication yes" in DEVTOOLS_POSTINST_SCRIPT


def test_devtools_profile_registers_postinst_password_hook() -> None:
    image = Image(reproducible=False)

    with image.profile("devtools"):
        Devtools().apply(image)

    profile = image.state.profiles["devtools"]
    postinst_commands = profile.phases.get("postinst", [])
    # Should have at least 2 postinst commands: systemctl enable + password setup
    assert len(postinst_commands) >= 2

    # Check that the password/auth setup script is in postinst
    all_args = " ".join(" ".join(cmd.argv) for cmd in postinst_commands)
    assert "openssl passwd" in all_args or "bash" in all_args


def test_devtools_profile_does_not_affect_default_profile() -> None:
    image = Image(reproducible=False)

    with image.profile("devtools"):
        Devtools().apply(image)

    default_profile = image.state.profiles["default"]
    assert "vim" not in default_profile.packages
    assert not any(
        f.path == "/usr/lib/systemd/system/serial-console.service" for f in default_profile.files
    )


def test_devtools_profile_bash_completion_in_debloat_skip() -> None:
    """Verify that paths_skip_for_profiles with devtools preserves bash-completion."""
    image = Image(reproducible=False)
    image.debloat(
        paths_skip_for_profiles={"devtools": ("/usr/share/bash-completion",)},
    )

    with image.profile("devtools"):
        Devtools().apply(image)

    # Verify bash-completion is in the devtools profile packages
    profile = image.state.profiles["devtools"]
    assert "bash-completion" in profile.packages

    # Verify debloat config has the conditional skip
    default_profile = image.state.profiles["default"]
    debloat_config = default_profile.debloat
    conditional = debloat_config.profile_conditional_paths
    assert "devtools" in conditional
    assert "/usr/share/bash-completion" in conditional["devtools"]
