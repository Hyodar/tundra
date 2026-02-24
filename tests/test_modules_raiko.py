from examples.modules import Raiko

from tdx import Image


def test_raiko_setup_declares_build_packages() -> None:
    image = Image(reproducible=False)
    module = Raiko()

    module.setup(image)

    profile = image.state.profiles["default"]
    for pkg in ("build-essential", "pkg-config", "git", "clang", "libssl-dev", "libelf-dev"):
        assert pkg in profile.build_packages


def test_raiko_install_adds_build_hook_with_correct_flags() -> None:
    image = Image(reproducible=False)
    module = Raiko()

    module.install(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[0]
    # Verify source cloning (host-side)
    assert "git clone" in build_script
    assert "NethermindEth/raiko.git" in build_script
    assert "-b feat/tdx" in build_script
    # Verify build runs inside mkosi-chroot
    assert "mkosi-chroot bash -c" in build_script
    # Verify Rust reproducibility flags
    assert "CARGO_PROFILE_RELEASE_LTO=thin" in build_script
    assert "CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1" in build_script
    assert "CARGO_PROFILE_RELEASE_PANIC=abort" in build_script
    assert "CARGO_PROFILE_RELEASE_INCREMENTAL=false" in build_script
    assert "CARGO_PROFILE_RELEASE_OPT_LEVEL=3" in build_script
    assert "CARGO_TERM_COLOR=never" in build_script
    assert "CARGO_HOME=/build/.cargo" in build_script
    assert "-C target-cpu=generic" in build_script
    assert "-C link-arg=-Wl,--build-id=none" in build_script
    assert "-C symbol-mangling-version=v0" in build_script
    assert "-L /usr/lib/x86_64-linux-gnu" in build_script
    # Verify cargo fetch + frozen build
    assert "cargo fetch" in build_script
    assert "cargo build --release --frozen" in build_script
    assert "--features tdx" in build_script
    assert "--package raiko-host" in build_script
    assert "$DESTDIR/usr/bin/raiko" in build_script


def test_raiko_custom_source_repo_and_branch() -> None:
    image = Image(reproducible=False)
    module = Raiko(
        source_repo="https://github.com/custom/raiko-fork.git",
        source_branch="main",
    )

    module.install(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[0]
    assert "custom/raiko-fork.git" in build_script
    assert "-b main" in build_script


def test_raiko_service_unit_content() -> None:
    image = Image(reproducible=False)
    module = Raiko()

    module.install(image)

    profile = image.state.profiles["default"]
    svc_files = [f for f in profile.files if f.path == "/usr/lib/systemd/system/raiko.service"]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=raiko" in svc_content
    assert "Group=tdx" in svc_content
    # No init scripts registered, so no runtime-init.service; just tdxs.service
    assert "After=tdxs.service" in svc_content
    assert "Requires=tdxs.service" in svc_content
    assert "Restart=on-failure" in svc_content
    assert "ExecStart=/usr/bin/raiko" in svc_content


def test_raiko_creates_system_user_in_postinst() -> None:
    image = Image(reproducible=False)
    module = Raiko()

    module.install(image)

    profile = image.state.profiles["default"]
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 1
    cmd = postinst_commands[0].argv[0]
    assert "mkosi-chroot useradd --system" in cmd
    assert "--gid" in cmd
    assert "tdx" in cmd
    assert "raiko" in cmd


def test_raiko_apply_combines_setup_and_install() -> None:
    image = Image(reproducible=False)
    module = Raiko()

    module.apply(image)

    profile = image.state.profiles["default"]
    # Build packages from setup()
    assert "build-essential" in profile.build_packages
    assert "clang" in profile.build_packages
    # Files from install()
    assert any(f.path == "/usr/lib/systemd/system/raiko.service" for f in profile.files)
    # Build hook
    assert len(profile.phases.get("build", [])) == 1
    # Postinst hook (user creation)
    assert len(profile.phases.get("postinst", [])) == 1
