from tdx import Image
from tdx.modules import TdxInit


def test_tdx_init_setup_declares_build_packages() -> None:
    image = Image(reproducible=False)
    module = TdxInit()

    module.setup(image)

    profile = image.state.profiles["default"]
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages
    assert "build-essential" in profile.build_packages


def test_tdx_init_install_adds_build_hook() -> None:
    image = Image(reproducible=False)
    module = TdxInit()

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_argv = build_commands[0].argv
    assert "sh" in build_argv
    assert "-c" in build_argv
    build_script = build_argv[-1]
    assert "git clone" in build_script
    assert "NethermindEth/nethermind-tdx" in build_script
    assert "cd" in build_script
    assert "init" in build_script
    assert "go build" in build_script
    assert "-trimpath" in build_script
    assert "-buildid=" in build_script
    assert "./cmd/main.go" in build_script
    assert "$DESTDIR/usr/bin/tdx-init" in build_script


def test_tdx_init_custom_source_repo_and_branch() -> None:
    image = Image(reproducible=False)
    module = TdxInit(
        source_repo="https://github.com/custom/tdx-fork",
        source_branch="v2.0",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    build_script = build_commands[0].argv[-1]
    assert "custom/tdx-fork" in build_script
    assert "-b v2.0" in build_script


def test_tdx_init_generates_config_yaml() -> None:
    image = Image(reproducible=False)
    module = TdxInit(
        ssh_strategy="webserver",
        key_strategy="tpm",
        disk_strategy="luks",
        mount_point="/persistent",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdx-init/config.yaml"]
    assert len(config_files) == 1
    config_content = config_files[0].content
    assert "ssh:" in config_content
    assert "strategy: webserver" in config_content
    assert "key:" in config_content
    assert "strategy: tpm" in config_content
    assert "disk:" in config_content
    assert "strategy: luks" in config_content
    assert "mount_point: /persistent" in config_content


def test_tdx_init_custom_config_values() -> None:
    image = Image(reproducible=False)
    module = TdxInit(
        ssh_strategy="direct",
        key_strategy="remote",
        disk_strategy="plain",
        mount_point="/data",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdx-init/config.yaml"]
    config_content = config_files[0].content
    assert "strategy: direct" in config_content
    assert "strategy: remote" in config_content
    assert "strategy: plain" in config_content
    assert "mount_point: /data" in config_content


def test_tdx_init_runtime_init_script_content() -> None:
    image = Image(reproducible=False)
    module = TdxInit(
        runtime_users=("raiko", "nethermind-surge"),
        runtime_directories=("/persistent/data", "/persistent/logs"),
        runtime_devices=("/dev/tpm0", "/dev/tdx_guest"),
    )

    module.install(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 1
    script_content = script_files[0].content
    assert script_files[0].mode == "0755"

    # Shebang and set flags
    assert script_content.startswith("#!/bin/bash")
    assert "set -euo pipefail" in script_content

    # Group/user existence checks
    assert 'getent group "raiko"' in script_content
    assert 'getent passwd "raiko"' in script_content
    assert 'getent group "nethermind-surge"' in script_content
    assert 'getent passwd "nethermind-surge"' in script_content

    # Mount check
    assert 'mountpoint -q "/persistent"' in script_content

    # JWT generation
    assert "openssl rand -hex 32" in script_content
    assert "/persistent/jwt" in script_content
    assert "jwt.hex" in script_content

    # Directory creation
    assert 'mkdir -p "/persistent/data"' in script_content
    assert 'mkdir -p "/persistent/logs"' in script_content

    # Device checks
    assert '"/dev/tpm0"' in script_content
    assert '"/dev/tdx_guest"' in script_content


def test_tdx_init_service_unit() -> None:
    image = Image(reproducible=False)
    module = TdxInit()

    module.install(image)

    profile = image.state.profiles["default"]
    svc_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/runtime-init.service"
    ]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "Type=oneshot" in svc_content
    assert "After=network.target network-setup.service" in svc_content
    assert "ExecStart=/usr/bin/tdx-init setup /etc/tdx-init/config.yaml" in svc_content
    assert "ExecStartPost=/usr/bin/runtime-init" in svc_content
    assert "RemainAfterExit=yes" in svc_content


def test_tdx_init_enables_service_in_postinst() -> None:
    image = Image(reproducible=False)
    module = TdxInit()

    module.install(image)

    profile = image.state.profiles["default"]
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 1
    assert postinst_commands[0].argv == (
        "mkosi-chroot", "systemctl", "enable", "runtime-init.service",
    )


def test_tdx_init_apply_combines_setup_and_install() -> None:
    image = Image(reproducible=False)
    module = TdxInit()

    module.apply(image)

    profile = image.state.profiles["default"]
    # Build packages from setup()
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages
    # Files from install()
    assert any(f.path == "/etc/tdx-init/config.yaml" for f in profile.files)
    assert any(f.path == "/usr/bin/runtime-init" for f in profile.files)
    assert any(
        f.path == "/usr/lib/systemd/system/runtime-init.service"
        for f in profile.files
    )
    # Build hook
    assert len(profile.phases.get("build", [])) == 1
    # Postinst hook (enable service)
    assert len(profile.phases.get("postinst", [])) == 1
