from tundravm import Image
from tundravm.modules import Tdxs


def test_tdxs_setup_declares_build_packages() -> None:
    image = Image()
    module = Tdxs()

    module.setup(image)

    profile = image.state.profiles["default"]
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages
    assert "build-essential" in profile.build_packages


def test_tdxs_install_adds_build_hook() -> None:
    image = Image()
    module = Tdxs()

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "go build" in build_script
    assert "./cmd/tdxs" in build_script
    assert "$DESTDIR/usr/bin/tdxs" in build_script
    assert "-trimpath" in build_script
    assert "-buildid=" in build_script
    assert "sync-constellation" not in build_script


def test_tdxs_custom_source_repo_and_branch() -> None:
    image = Image()
    module = Tdxs(
        source_repo="https://github.com/custom/tdxs-fork",
        source_branch="v2.0",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[0]
    assert "custom/tdxs-fork" in build_script
    assert "-b v2.0" in build_script


def test_tdxs_generates_config_yaml_and_units() -> None:
    image = Image()
    module = Tdxs()

    module.apply(image)

    profile = image.state.profiles["default"]
    assert "golang" in profile.build_packages

    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    assert len(config_files) == 1
    config_content = config_files[0].content
    assert "transport:" in config_content
    assert "type: socket" in config_content
    assert "systemd: true" in config_content
    assert "issuer:" in config_content
    assert "type: tdx" in config_content

    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=tdxs" in svc_content
    assert "Group=tdx" in svc_content
    assert "Type=notify" in svc_content
    assert "ExecStart=/usr/bin/tdxs" in svc_content
    assert "--log-level info" in svc_content
    assert "Requires=tdxs.socket" in svc_content

    sock_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.socket"]
    assert len(sock_files) == 1
    sock_content = sock_files[0].content
    assert "ListenStream=/var/tdxs.sock" in sock_content
    assert "SocketMode=0660" in sock_content
    assert "SocketUser=root" in sock_content
    assert "SocketGroup=tdx" in sock_content

    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 2
    assert postinst_commands[0].argv[0] == "mkosi-chroot groupadd --system tdx"
    assert "mkosi-chroot useradd --system" in postinst_commands[1].argv[0]
    assert "tdxs" in postinst_commands[1].argv[0]

    service_names = {s.name for s in profile.services}
    assert "tdxs.service" in service_names
    assert "tdxs.socket" in service_names


def test_tdxs_resolves_init_dependency_when_init_scripts_present() -> None:
    from tundravm.modules import KeyGeneration

    image = Image()
    KeyGeneration(strategy="tpm").apply(image)
    Tdxs().apply(image)

    profile = image.state.profiles["default"]
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    svc_content = svc_files[0].content
    assert "After=runtime-init.service" in svc_content
    assert "Requires=runtime-init.service tdxs.socket" in svc_content


def test_tdxs_no_init_dependency_when_no_init_scripts() -> None:
    image = Image()
    Tdxs().apply(image)

    profile = image.state.profiles["default"]
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    svc_content = svc_files[0].content
    assert "runtime-init" not in svc_content


def test_tdxs_compatibility_aliases_are_canonicalized() -> None:
    image = Image()
    module = Tdxs(issuer_type="azure-tdx", validator_type="gcp-tdx")

    module.apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    content = config_files[0].content
    assert "issuer:" in content
    assert "type: azure" in content
    assert "validator:" in content
    assert "type: gcp" in content


def test_tdxs_validator_config_supports_expected_measurements() -> None:
    image = Image()
    module = Tdxs(
        issuer_type=None,
        validator_type="tdx",
        expected_measurements={
            "mrtd": "abc123",
            "rtmr0": "def456",
        },
        check_revocations=True,
        get_collateral=True,
    )

    module.apply(image)

    profile = image.state.profiles["default"]
    config = next(f.content for f in profile.files if f.path == "/etc/tdxs/config.yaml")
    assert "issuer:" not in config
    assert "validator:" in config
    assert "type: tdx" in config
    assert "expected_measurements:" in config
    assert 'mrtd: "abc123"' in config
    assert 'rtmr0: "def456"' in config
    assert "check_revocations: true" in config
    assert "get_collateral: true" in config


def test_tdxs_custom_socket_and_service_names() -> None:
    image = Image()
    module = Tdxs(
        socket_path="/run/tdx/quote.sock",
        socket_mode="0600",
        socket_user="tdxs",
        service_name="quote-issuer.service",
        socket_name="quote-issuer.socket",
        log_level="debug",
    )

    module.apply(image)

    profile = image.state.profiles["default"]
    sock_files = [
        f for f in profile.files if f.path == "/usr/lib/systemd/system/quote-issuer.socket"
    ]
    assert len(sock_files) == 1
    assert "ListenStream=/run/tdx/quote.sock" in sock_files[0].content
    assert "SocketMode=0600" in sock_files[0].content
    assert "SocketUser=tdxs" in sock_files[0].content

    svc_files = [
        f for f in profile.files if f.path == "/usr/lib/systemd/system/quote-issuer.service"
    ]
    assert len(svc_files) == 1
    assert "Requires=quote-issuer.socket" in svc_files[0].content
    assert "--log-level debug" in svc_files[0].content

    service_names = {s.name for s in profile.services}
    assert "quote-issuer.service" in service_names
    assert "quote-issuer.socket" in service_names


def test_image_build_install_adds_build_packages() -> None:
    image = Image()
    image.build_install("golang", "git")

    profile = image.state.profiles["default"]
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages


def test_image_build_source_adds_build_sources() -> None:
    image = Image()
    image.build_source("../services/tdxs", "tdxs")

    profile = image.state.profiles["default"]
    assert ("../services/tdxs", "tdxs") in profile.build_sources
