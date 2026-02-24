from tdx import Image
from tdx.modules import Tdxs


def test_tdxs_setup_declares_build_packages() -> None:
    image = Image()
    module = Tdxs(issuer_type="dcap")

    module.setup(image)

    profile = image.state.profiles["default"]
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages
    assert "build-essential" in profile.build_packages


def test_tdxs_install_adds_build_hook() -> None:
    image = Image()
    module = Tdxs(issuer_type="dcap")

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    # The build hook is a shell script
    build_script = build_commands[0].argv[-1]
    assert build_commands[0].shell is True
    assert "git clone" in build_script
    assert "NethermindEth/tdxs" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "go build" in build_script
    assert "$DESTDIR/usr/bin/tdxs" in build_script
    assert "-trimpath" in build_script
    assert "-buildid=" in build_script


def test_tdxs_custom_source_repo_and_branch() -> None:
    image = Image()
    module = Tdxs(
        issuer_type="dcap",
        source_repo="https://github.com/custom/tdxs-fork",
        source_branch="v2.0",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    build_script = build_commands[0].argv[-1]
    assert "custom/tdxs-fork" in build_script
    assert "-b v2.0" in build_script


def test_tdxs_generates_config_yaml_and_units() -> None:
    image = Image()
    module = Tdxs(issuer_type="dcap")

    module.apply(image)

    profile = image.state.profiles["default"]

    # Build packages
    assert "golang" in profile.build_packages

    # Config file
    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    assert len(config_files) == 1
    config_content = config_files[0].content
    assert "transport:" in config_content
    assert "type: socket" in config_content
    assert "systemd: true" in config_content
    assert "issuer:" in config_content
    assert "type: dcap" in config_content

    # Service unit
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=tdxs" in svc_content
    assert "Group=tdx" in svc_content
    assert "Type=notify" in svc_content
    assert "ExecStart=/usr/bin/tdxs" in svc_content
    # No init scripts registered, so no runtime-init.service dependency
    assert "Requires=tdxs.socket" in svc_content

    # Socket unit
    sock_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.socket"]
    assert len(sock_files) == 1
    sock_content = sock_files[0].content
    assert "ListenStream=/var/tdxs.sock" in sock_content
    assert "SocketMode=0660" in sock_content
    assert "SocketGroup=tdx" in sock_content

    # Postinst hooks: groupadd, useradd, systemctl enable
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 3
    assert postinst_commands[0].argv == (
        "mkosi-chroot",
        "groupadd",
        "--system",
        "tdx",
    )
    assert postinst_commands[1].argv[:3] == ("mkosi-chroot", "useradd", "--system")
    assert "tdxs" in postinst_commands[1].argv
    assert postinst_commands[2].argv == (
        "mkosi-chroot",
        "systemctl",
        "enable",
        "tdxs.socket",
    )


def test_tdxs_resolves_init_dependency_when_init_scripts_present() -> None:
    """Tdxs service should depend on runtime-init when init scripts exist."""
    from tdx.modules import KeyGeneration

    image = Image()
    KeyGeneration(strategy="tpm").apply(image)
    Tdxs(issuer_type="dcap").apply(image)

    profile = image.state.profiles["default"]
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    svc_content = svc_files[0].content
    assert "After=runtime-init.service" in svc_content
    assert "Requires=runtime-init.service tdxs.socket" in svc_content


def test_tdxs_no_init_dependency_when_no_init_scripts() -> None:
    """Tdxs service should not reference runtime-init when no init scripts exist."""
    image = Image()
    Tdxs(issuer_type="dcap").apply(image)

    profile = image.state.profiles["default"]
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.service"]
    svc_content = svc_files[0].content
    assert "runtime-init" not in svc_content


def test_tdxs_custom_issuer_type() -> None:
    image = Image()
    module = Tdxs(issuer_type="azure-tdx")

    module.apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    assert "type: azure-tdx" in config_files[0].content


def test_tdxs_custom_socket_path() -> None:
    image = Image()
    module = Tdxs(socket_path="/run/tdx/quote.sock")

    module.apply(image)

    profile = image.state.profiles["default"]
    sock_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/tdxs.socket"]
    assert "ListenStream=/run/tdx/quote.sock" in sock_files[0].content


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
