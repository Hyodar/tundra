from examples.modules import TaikoClient

from tdx import Image


def test_taiko_client_setup_declares_build_packages() -> None:
    image = Image(reproducible=False)
    module = TaikoClient()

    module.setup(image)

    profile = image.state.profiles["default"]
    for pkg in ("golang", "git", "build-essential"):
        assert pkg in profile.build_packages


def test_taiko_client_install_adds_build_hook_with_cgo_flags() -> None:
    image = Image(reproducible=False)
    module = TaikoClient()

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    # Verify source cloning
    assert "git clone" in build_script
    assert "NethermindEth/surge-taiko-mono" in build_script
    assert "-b feat/tdx-proving" in build_script
    # Verify CGO flags
    assert 'CGO_CFLAGS="-O -D__BLST_PORTABLE__"' in build_script
    assert 'CGO_CFLAGS_ALLOW="-O -D__BLST_PORTABLE__"' in build_script
    # Verify Go build flags
    assert "-trimpath" in build_script
    assert '-ldflags "-s -w -buildid="' in build_script
    # Verify build path and output
    assert "packages/taiko-client" in build_script
    assert "$DESTDIR/usr/bin/taiko-client" in build_script


def test_taiko_client_custom_source_and_build_path() -> None:
    image = Image(reproducible=False)
    module = TaikoClient(
        source_repo="https://github.com/custom/taiko-fork",
        source_branch="main",
        build_path="cmd/client",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/taiko-fork" in build_script
    assert "-b main" in build_script
    assert "cmd/client" in build_script


def test_taiko_client_service_unit_content() -> None:
    image = Image(reproducible=False)
    module = TaikoClient()

    module.install(image)

    profile = image.state.profiles["default"]
    svc_files = [
        f for f in profile.files if f.path == "/usr/lib/systemd/system/taiko-client.service"
    ]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=taiko-client" in svc_content
    assert "Group=eth" in svc_content
    # No init scripts registered, so no runtime-init.service dependency
    assert "runtime-init" not in svc_content
    assert "Restart=on-failure" in svc_content
    assert "ExecStart=/usr/bin/taiko-client" in svc_content


def test_taiko_client_creates_system_user_in_postinst() -> None:
    image = Image(reproducible=False)
    module = TaikoClient()

    module.install(image)

    profile = image.state.profiles["default"]
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 1
    argv = postinst_commands[0].argv
    assert argv[:3] == ("mkosi-chroot", "useradd", "--system")
    assert "--groups" in argv
    assert "eth" in argv
    assert "taiko-client" in argv


def test_taiko_client_apply_combines_setup_and_install() -> None:
    image = Image(reproducible=False)
    module = TaikoClient()

    module.apply(image)

    profile = image.state.profiles["default"]
    # Build packages from setup()
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages
    # Files from install()
    assert any(f.path == "/usr/lib/systemd/system/taiko-client.service" for f in profile.files)
    # Build hook
    assert len(profile.phases.get("build", [])) == 1
    # Postinst hook (user creation)
    assert len(profile.phases.get("postinst", [])) == 1
