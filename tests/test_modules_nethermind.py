from tdx import Image
from tdx.modules import Nethermind


def test_nethermind_setup_declares_build_packages() -> None:
    image = Image(reproducible=False)
    module = Nethermind()

    module.setup(image)

    profile = image.state.profiles["default"]
    for pkg in ("dotnet-sdk-10.0", "dotnet-runtime-10.0", "build-essential", "git"):
        assert pkg in profile.build_packages


def test_nethermind_install_adds_build_hook_with_dotnet_properties() -> None:
    image = Image(reproducible=False)
    module = Nethermind()

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    # Verify source cloning
    assert "git clone" in build_script
    assert "NethermindEth/nethermind.git" in build_script
    assert "-b 1.32.3" in build_script
    # Verify .NET deterministic build properties
    assert "/p:Deterministic=true" in build_script
    assert "/p:ContinuousIntegrationBuild=true" in build_script
    assert "/p:PublishSingleFile=true" in build_script
    assert "/p:BuildTimestamp=0" in build_script
    assert "/p:Commit=0000000000000000000000000000000000000000" in build_script
    # Verify project path and runtime
    assert "src/Nethermind/Nethermind.Runner" in build_script
    assert "-r linux-x64" in build_script
    # Verify artifact installation
    assert "$DESTDIR/usr/bin/nethermind" in build_script
    assert "etc/nethermind-surge/NLog.config" in build_script
    assert "etc/nethermind-surge/plugins" in build_script


def test_nethermind_custom_version_and_repo() -> None:
    image = Image(reproducible=False)
    module = Nethermind(
        source_repo="https://github.com/custom/nethermind-fork.git",
        version="2.0.0",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/nethermind-fork.git" in build_script
    assert "-b 2.0.0" in build_script


def test_nethermind_service_unit_content() -> None:
    image = Image(reproducible=False)
    module = Nethermind()

    module.install(image)

    profile = image.state.profiles["default"]
    svc_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/nethermind-surge.service"
    ]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=nethermind-surge" in svc_content
    assert "Group=eth" in svc_content
    assert "After=runtime-init.service" in svc_content
    assert "Requires=runtime-init.service" in svc_content
    assert "Restart=on-failure" in svc_content
    assert "LimitNOFILE=1048576" in svc_content
    assert "EnvironmentFile=/etc/nethermind-surge/env" in svc_content
    assert "ExecStart=/usr/bin/nethermind" in svc_content
    assert "--config /etc/nethermind-surge/config.json" in svc_content
    assert "--datadir /home/nethermind-surge/data" in svc_content
    assert "--JsonRpc.EngineHost 0.0.0.0" in svc_content
    assert "--JsonRpc.EnginePort 8551" in svc_content


def test_nethermind_creates_system_user_in_postinst() -> None:
    image = Image(reproducible=False)
    module = Nethermind()

    module.install(image)

    profile = image.state.profiles["default"]
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 1
    argv = postinst_commands[0].argv
    assert argv[:3] == ("mkosi-chroot", "useradd", "--system")
    assert "--groups" in argv
    assert "eth" in argv
    assert "nethermind-surge" in argv


def test_nethermind_apply_combines_setup_and_install() -> None:
    image = Image(reproducible=False)
    module = Nethermind()

    module.apply(image)

    profile = image.state.profiles["default"]
    # Build packages from setup()
    assert "dotnet-sdk-10.0" in profile.build_packages
    assert "dotnet-runtime-10.0" in profile.build_packages
    # Files from install()
    assert any(
        f.path == "/usr/lib/systemd/system/nethermind-surge.service"
        for f in profile.files
    )
    # Build hook
    assert len(profile.phases.get("build", [])) == 1
    # Postinst hook (user creation)
    assert len(profile.phases.get("postinst", [])) == 1


def test_nethermind_config_files_mapping(tmp_path: object) -> None:
    from pathlib import Path

    tmp = Path(str(tmp_path))
    cfg = tmp / "nethermind.json"
    cfg.write_text('{"Network": {}}')
    nlog = tmp / "NLog.config"
    nlog.write_text("<nlog/>")

    image = Image(reproducible=False)
    module = Nethermind(
        config_files={
            str(cfg): "/etc/nethermind-surge/config.json",
            str(nlog): "/etc/nethermind-surge/NLog.config",
        },
    )

    module.install(image)

    profile = image.state.profiles["default"]
    config_paths = [f.path for f in profile.files]
    assert "/etc/nethermind-surge/config.json" in config_paths
    assert "/etc/nethermind-surge/NLog.config" in config_paths


def test_nethermind_custom_runtime() -> None:
    image = Image(reproducible=False)
    module = Nethermind(runtime="linux-arm64")

    module.install(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "-r linux-arm64" in build_script
