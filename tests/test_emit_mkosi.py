from pathlib import Path
from typing import cast

import pytest

from tdx import Image
from tdx.compiler.emit_mkosi import ARCH_TO_MKOSI
from tdx.errors import ValidationError
from tdx.models import Kernel, Phase


def test_compile_golden_output(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("jq", "curl")
    image.run("echo prep", phase="prepare", env={"B": "2", "A": "1"}, cwd="/work")
    image.run("echo build", phase="build")

    output_dir = image.compile(tmp_path / "mkosi")

    conf_path = output_dir / "default" / "mkosi.conf"
    prepare_script = output_dir / "default" / "scripts" / "03-prepare.sh"
    build_script = output_dir / "default" / "scripts" / "04-build.sh"

    conf_text = conf_path.read_text(encoding="utf-8")

    # Verify key sections exist in the mkosi.conf
    assert "[Distribution]" in conf_text
    assert "Distribution=debian" in conf_text
    assert "Release=bookworm" in conf_text
    assert "Architecture=x86-64" in conf_text
    assert "[Output]" in conf_text
    assert "Format=uki" in conf_text
    assert "ImageId=default" in conf_text
    assert "ManifestFormat=json" in conf_text
    # No @ prefix on Format or ImageId
    assert "@Format" not in conf_text
    assert "@ImageId" not in conf_text
    assert "[Content]" in conf_text
    assert "CleanPackageMetadata=true" in conf_text
    assert "curl" in conf_text
    assert "jq" in conf_text
    # Script references are in [Content] section (no separate [Scripts] section in mkosi v20+)
    assert "PrepareScripts=scripts/03-prepare.sh" in conf_text
    assert "BuildScripts=scripts/04-build.sh" in conf_text

    # Verify reproducibility settings
    assert "SourceDateEpoch=0" in conf_text
    assert "Seed=" in conf_text

    # Verify build settings
    assert "WithNetwork=true" in conf_text

    assert prepare_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nset -euo pipefail\n\n(cd /work && A=1 B=2 echo prep)\n"
    )
    assert build_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nset -euo pipefail\n\necho build\n"
    )


def test_compile_is_deterministic(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("curl")
    image.run("echo hello", phase="prepare")

    output_a = image.compile(tmp_path / "mkosi-a")
    output_b = image.compile(tmp_path / "mkosi-b")

    assert _snapshot_tree(output_a) == _snapshot_tree(output_b)


def test_compile_rejects_invalid_phase(tmp_path: Path) -> None:
    image = Image()
    image.state.profiles["default"].phases[cast(Phase, "invalid-phase")] = []

    with pytest.raises(ValidationError) as excinfo:
        image.compile(tmp_path / "mkosi")

    assert excinfo.value.code == "E_VALIDATION"


def test_compile_generates_extra_tree(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.file("/etc/motd", content="TDX VM\n")
    image.template(
        "/etc/app/config.toml",
        template="network={network}\n",
        vars={"network": "mainnet"},
    )

    output_dir = image.compile(tmp_path / "mkosi")

    extra_dir = output_dir / "default" / "mkosi.extra"
    assert (extra_dir / "etc" / "motd").read_text(encoding="utf-8") == "TDX VM\n"
    assert (extra_dir / "etc" / "app" / "config.toml").read_text(
        encoding="utf-8"
    ) == "network=mainnet\n"


def test_compile_generates_service_units(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.service(
        "app",
        exec=["/usr/bin/app", "--config", "/etc/app.toml"],
        user="app",
        after=["network-online.target"],
        restart="always",
        security_profile="strict",
    )

    output_dir = image.compile(tmp_path / "mkosi")

    unit_path = (
        output_dir
        / "default"
        / "mkosi.extra"
        / "usr"
        / "lib"
        / "systemd"
        / "system"
        / "app.service"
    )
    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/app --config /etc/app.toml" in content
    assert "User=app" in content
    assert "After=network-online.target" in content
    assert "Restart=always" in content
    assert "ProtectSystem=strict" in content
    assert "WantedBy=minimal.target" in content


def test_compile_generates_postinst_with_users(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.user("app", system=True, home="/var/lib/app", uid=1000, groups=["tdx"])

    output_dir = image.compile(tmp_path / "mkosi")

    # Check that postinst script exists and has user creation via mkosi-chroot
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    assert postinst.exists()
    content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot useradd" in content
    assert "--system" in content
    assert "--home-dir" in content
    assert "/var/lib/app" in content


def test_compile_generates_debloat_finalize(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.debloat(enabled=True, paths_remove_extra=["/usr/share/fonts"])

    output_dir = image.compile(tmp_path / "mkosi")

    # Finalize has path removal
    finalize = output_dir / "default" / "scripts" / "07-finalize.sh"
    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")
    assert "rm -rf" in content
    assert "/usr/share/doc" in content
    assert "/usr/share/fonts" in content

    # Systemd binary cleanup is now in postinst (via dpkg-query), not finalize
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    assert postinst.exists()
    postinst_content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot dpkg-query -L systemd" in postinst_content
    assert "default.target" in postinst_content


def test_compile_architecture_field(tmp_path: Path) -> None:
    """Architecture is mapped correctly from Arch type to mkosi value."""
    for py_arch, mkosi_arch in ARCH_TO_MKOSI.items():
        image = Image(base="debian/bookworm", arch=py_arch)  # type: ignore[arg-type]
        image.install("curl")
        output_dir = image.compile(tmp_path / f"mkosi-{py_arch}")
        conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")
        assert f"Architecture={mkosi_arch}" in conf_text


def test_compile_with_network_configurable(tmp_path: Path) -> None:
    """WithNetwork can be set to True or False."""
    for with_net, expected in [(True, "WithNetwork=true"), (False, "WithNetwork=false")]:
        image = Image(base="debian/bookworm", with_network=with_net)
        image.install("curl")
        output_dir = image.compile(tmp_path / f"mkosi-net-{with_net}")
        conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")
        assert expected in conf_text


def test_compile_no_at_prefix(tmp_path: Path) -> None:
    """Format and ImageId do not have @ prefix in mkosi v26 output."""
    image = Image(base="debian/bookworm")
    image.install("curl")
    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")
    assert "@Format=" not in conf_text
    assert "@ImageId=" not in conf_text
    assert "Format=uki" in conf_text
    assert "ImageId=default" in conf_text


def test_compile_service_enablement_uses_mkosi_chroot(tmp_path: Path) -> None:
    """Service enablement uses mkosi-chroot systemctl enable."""
    image = Image(base="debian/bookworm")
    image.service("myapp", exec="/usr/bin/myapp", enabled=True)

    output_dir = image.compile(tmp_path / "mkosi")
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot systemctl enable myapp.service" in content


def test_compile_debloat_uses_dpkg_query(tmp_path: Path) -> None:
    """Debloat uses mkosi-chroot dpkg-query for binary and unit enumeration."""
    image = Image(base="debian/bookworm")
    image.debloat(enabled=True)

    output_dir = image.compile(tmp_path / "mkosi")
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    content = postinst.read_text(encoding="utf-8")

    # Binary cleanup via dpkg-query
    assert "mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/'" in content
    # Unit masking via dpkg-query
    assert (
        "mkosi-chroot dpkg-query -L systemd | "
        "grep -E '\\.service$|\\.socket$|\\.timer$|\\.target$|\\.mount$'"
    ) in content


def test_compile_default_target(tmp_path: Path) -> None:
    """Default systemd target is set to minimal.target when debloat is enabled."""
    image = Image(base="debian/bookworm")
    image.debloat(enabled=True)

    output_dir = image.compile(tmp_path / "mkosi")
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"
    content = postinst.read_text(encoding="utf-8")
    assert 'ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"' in content


def test_compile_skeleton_init_script(tmp_path: Path) -> None:
    """Custom init script is written to mkosi.skeleton/init when configured."""
    image = Image(base="debian/bookworm", init_script=Image.DEFAULT_TDX_INIT)
    image.install("systemd")

    output_dir = image.compile(tmp_path / "mkosi")
    init_path = output_dir / "default" / "mkosi.skeleton" / "init"
    assert init_path.exists()
    content = init_path.read_text(encoding="utf-8")
    assert "mount -t proc none /proc" in content
    assert "unshare --mount" in content
    assert "minimal.target" in content
    # Executable
    assert init_path.stat().st_mode & 0o755 == 0o755


def test_compile_version_script(tmp_path: Path) -> None:
    """mkosi.version is emitted at the emission root when enabled."""
    image = Image(base="debian/bookworm", generate_version_script=True)
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    version_path = output_dir / "mkosi.version"
    assert version_path.exists()
    content = version_path.read_text(encoding="utf-8")
    assert "git rev-parse --short=6 HEAD" in content
    assert version_path.stat().st_mode & 0o755 == 0o755


def test_compile_gcp_postoutput(tmp_path: Path) -> None:
    """GCP postoutput script is emitted when profile has gcp output target."""
    image = Image(base="debian/bookworm")
    image.install("curl")
    image.output_targets("gcp")

    output_dir = image.compile(tmp_path / "mkosi")
    gcp_script = output_dir / "default" / "scripts" / "gcp-postoutput.sh"
    assert gcp_script.exists()
    content = gcp_script.read_text(encoding="utf-8")
    assert "sgdisk" in content
    assert "tar.gz" in content
    assert gcp_script.stat().st_mode & 0o755 == 0o755


def test_compile_azure_postoutput(tmp_path: Path) -> None:
    """Azure postoutput script is emitted when profile has azure output target."""
    image = Image(base="debian/bookworm")
    image.install("curl")
    image.output_targets("azure")

    output_dir = image.compile(tmp_path / "mkosi")
    azure_script = output_dir / "default" / "scripts" / "azure-postoutput.sh"
    assert azure_script.exists()
    content = azure_script.read_text(encoding="utf-8")
    assert "qemu-img convert" in content
    assert ".vhd" in content


def test_compile_native_profiles_mode(tmp_path: Path) -> None:
    """Native profiles mode creates root mkosi.conf + mkosi.profiles/<name>/."""
    image = Image(base="debian/bookworm", emit_mode="native_profiles")
    image.install("curl")
    with image.profile("prod"):
        image.install("nginx")

    # Must emit with all profiles active
    with image.all_profiles():
        output_dir = image.compile(tmp_path / "mkosi")

    # Root mkosi.conf
    assert (output_dir / "mkosi.conf").exists()
    # Root skeleton/extra
    assert (output_dir / "mkosi.skeleton").is_dir()
    # Profile-specific override
    assert (output_dir / "mkosi.profiles" / "default" / "mkosi.conf").exists()
    assert (output_dir / "mkosi.profiles" / "prod" / "mkosi.conf").exists()


def test_compile_environment_key_value(tmp_path: Path) -> None:
    """Environment=KEY=VALUE pairs are emitted in [Build] section."""
    image = Image(
        base="debian/bookworm",
        environment={"MY_VAR": "hello", "OTHER": "world"},
        reproducible=False,
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    assert "Environment=MY_VAR=hello" in conf_text
    assert "Environment=OTHER=world" in conf_text


def test_compile_environment_passthrough(tmp_path: Path) -> None:
    """Environment=KEY (passthrough without value) is emitted in [Build] section."""
    image = Image(
        base="debian/bookworm",
        environment_passthrough=("KERNEL_IMAGE", "KERNEL_VERSION"),
        reproducible=False,
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    assert "Environment=KERNEL_IMAGE\n" in conf_text
    assert "Environment=KERNEL_VERSION\n" in conf_text


def test_compile_environment_both_forms(tmp_path: Path) -> None:
    """Both key=value and passthrough forms coexist in [Build] section."""
    image = Image(
        base="debian/bookworm",
        environment={"SOURCE_DATE_EPOCH": "0"},
        environment_passthrough=("KERNEL_IMAGE",),
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    assert "Environment=SOURCE_DATE_EPOCH=0" in conf_text
    assert "Environment=KERNEL_IMAGE\n" in conf_text


def test_compile_reproducible_auto_adds_source_date_epoch(tmp_path: Path) -> None:
    """When reproducible=True, SOURCE_DATE_EPOCH=0 is auto-added to environment."""
    image = Image(
        base="debian/bookworm",
        reproducible=True,
        environment={"MY_VAR": "test"},
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    # Both the user env and the auto-added SOURCE_DATE_EPOCH
    assert "Environment=SOURCE_DATE_EPOCH=0" in conf_text
    assert "Environment=MY_VAR=test" in conf_text


def test_compile_reproducible_no_override_user_epoch(tmp_path: Path) -> None:
    """User-provided SOURCE_DATE_EPOCH is not overridden by reproducible=True."""
    image = Image(
        base="debian/bookworm",
        reproducible=True,
        environment={"SOURCE_DATE_EPOCH": "1234"},
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    assert "Environment=SOURCE_DATE_EPOCH=1234" in conf_text
    assert "Environment=SOURCE_DATE_EPOCH=0" not in conf_text


def test_compile_kernel_with_config_emits_build_script(tmp_path: Path) -> None:
    """When kernel has config_file, a real build script is emitted."""
    # Create a fake kernel config file
    config_file = tmp_path / "kernel-yocto.config"
    config_file.write_text("# CONFIG_LOCALVERSION is not set\n", encoding="utf-8")

    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel("6.13.12", config_file=str(config_file))
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    build_script = output_dir / "default" / "scripts" / "04-build.sh"

    assert build_script.exists()
    script_text = build_script.read_text(encoding="utf-8")

    # Verify key build steps
    assert "git clone --depth 1 --branch" in script_text
    assert 'KERNEL_VERSION="6.13.12"' in script_text
    assert "v${KERNEL_VERSION}" in script_text
    assert "https://github.com/gregkh/linux" in script_text
    assert "make olddefconfig" in script_text
    assert "make -j" in script_text
    assert "bzImage ARCH=x86_64" in script_text
    assert "KBUILD_BUILD_TIMESTAMP" in script_text
    assert "KBUILD_BUILD_USER" in script_text
    assert "KBUILD_BUILD_HOST" in script_text
    assert "${DESTDIR}/usr/lib/modules/" in script_text
    assert "vmlinuz" in script_text
    assert "kernel/kernel.config" in script_text


def test_compile_kernel_config_file_copied(tmp_path: Path) -> None:
    """Kernel config file is copied into the output tree."""
    config_file = tmp_path / "my.config"
    config_file.write_text("CONFIG_TDX_GUEST=y\n", encoding="utf-8")

    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel("6.13.12", config_file=str(config_file))
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    kernel_config = output_dir / "default" / "kernel" / "kernel.config"

    assert kernel_config.exists()
    assert "CONFIG_TDX_GUEST=y" in kernel_config.read_text(encoding="utf-8")


def test_compile_kernel_config_auto_adds_env_passthrough(tmp_path: Path) -> None:
    """When kernel has config_file, KERNEL_IMAGE and KERNEL_VERSION are auto-added."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# kernel config\n", encoding="utf-8")

    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel("6.13.12", config_file=str(config_file))
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")

    assert "Environment=KERNEL_IMAGE\n" in conf_text
    assert "Environment=KERNEL_VERSION\n" in conf_text


def test_compile_kernel_without_config_no_build_script(tmp_path: Path) -> None:
    """When kernel does not have config_file, no kernel build script is emitted."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel("6.8")
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")

    # No build script should exist (no build hooks registered)
    build_script = output_dir / "default" / "scripts" / "04-build.sh"
    assert not build_script.exists()

    # No kernel dir should be created
    kernel_dir = output_dir / "default" / "kernel"
    assert not kernel_dir.exists()

    # mkosi.conf should still have comment-only kernel config
    conf_text = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")
    assert "# KernelVersion=6.8" in conf_text


def test_compile_kernel_build_script_with_user_hooks(tmp_path: Path) -> None:
    """Kernel build script is combined with user-defined build hooks."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# config\n", encoding="utf-8")

    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel("6.13.12", config_file=str(config_file))
    image.install("curl")
    image.run("echo custom-build-step", phase="build")

    output_dir = image.compile(tmp_path / "mkosi")
    build_script = output_dir / "default" / "scripts" / "04-build.sh"

    assert build_script.exists()
    script_text = build_script.read_text(encoding="utf-8")

    # Both kernel build and user hook should be present
    assert "git clone" in script_text
    assert "custom-build-step" in script_text


def test_compile_kernel_custom_source_repo(tmp_path: Path) -> None:
    """Kernel build script uses the custom source repo."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# config\n", encoding="utf-8")

    image = Image(base="debian/bookworm", reproducible=False)
    image.kernel = Kernel.tdx_kernel(
        "6.13.12",
        config_file=str(config_file),
        source_repo="https://github.com/custom/linux",
    )
    image.install("curl")

    output_dir = image.compile(tmp_path / "mkosi")
    build_script = output_dir / "default" / "scripts" / "04-build.sh"

    script_text = build_script.read_text(encoding="utf-8")
    assert "https://github.com/custom/linux" in script_text


def test_compile_efi_stub_postinst_hook(tmp_path: Path) -> None:
    """efi_stub() registers a postinst hook that downloads and installs pinned EFI stub."""
    image = Image(base="debian/bookworm")
    image.install("systemd")
    image.efi_stub(
        snapshot_url="https://snapshot.debian.org/archive/debian/20251113T083151Z",
        package_version="255.4-1",
    )

    output_dir = image.compile(tmp_path / "mkosi")
    postinst = output_dir / "default" / "scripts" / "06-postinst.sh"

    assert postinst.exists()
    content = postinst.read_text(encoding="utf-8")

    # Verify script contains the snapshot URL and package version
    assert "https://snapshot.debian.org/archive/debian/20251113T083151Z" in content
    assert "255.4-1" in content
    # Verify it downloads and installs the .deb
    assert "systemd-boot-efi" in content
    assert "dpkg -i" in content
    # Verify EFI file copy from /usr/lib/systemd/boot/efi
    assert "/usr/lib/systemd/boot/efi" in content


def test_compile_efi_stub_registered_in_postinst_phase() -> None:
    """efi_stub() registers the hook in the postinst phase of the profile state."""
    image = Image(base="debian/bookworm")
    image.efi_stub(
        snapshot_url="https://snapshot.example.com",
        package_version="255.4-1",
    )

    profile = image.state.profiles["default"]
    assert "postinst" in profile.phases
    commands = profile.phases["postinst"]
    assert len(commands) >= 1
    # The hook should contain the EFI stub script as a shell command
    efi_command = commands[-1]
    script = efi_command.argv[0]
    assert "snapshot.example.com" in script
    assert "255.4-1" in script


def test_compile_strip_image_version_finalize_hook(tmp_path: Path) -> None:
    """strip_image_version() registers a finalize hook that strips IMAGE_VERSION."""
    image = Image(base="debian/bookworm")
    image.install("systemd")
    # reproducible=True by default, so strip_image_version is auto-called

    output_dir = image.compile(tmp_path / "mkosi")
    finalize = output_dir / "default" / "scripts" / "07-finalize.sh"

    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")

    # Verify script strips IMAGE_VERSION from os-release
    assert "IMAGE_VERSION" in content
    assert "$BUILDROOT/usr/lib/os-release" in content
    assert "sed -i" in content


def test_compile_strip_image_version_registered_in_finalize_phase() -> None:
    """strip_image_version() registers the hook in the finalize phase."""
    image = Image(base="debian/bookworm")
    # reproducible=True by default, so strip_image_version is auto-called

    profile = image.state.profiles["default"]
    assert "finalize" in profile.phases
    commands = profile.phases["finalize"]
    assert len(commands) >= 1
    strip_command = commands[-1]
    assert "IMAGE_VERSION" in strip_command.argv[0]


def test_compile_strip_image_version_auto_called_when_reproducible() -> None:
    """When reproducible=True (default), strip_image_version is auto-called."""
    image = Image(base="debian/bookworm", reproducible=True)

    profile = image.state.profiles["default"]
    assert "finalize" in profile.phases
    assert any("IMAGE_VERSION" in cmd.argv[0] for cmd in profile.phases["finalize"])


def test_compile_strip_image_version_not_called_when_not_reproducible() -> None:
    """When reproducible=False, strip_image_version is NOT auto-called."""
    image = Image(base="debian/bookworm", reproducible=False)

    profile = image.state.profiles["default"]
    finalize_cmds = profile.phases.get("finalize", [])
    assert not any("IMAGE_VERSION" in cmd.argv[0] for cmd in finalize_cmds)


def test_compile_strip_image_version_can_be_disabled() -> None:
    """strip_image_version(enabled=False) removes the auto-registered hook."""
    image = Image(base="debian/bookworm", reproducible=True)
    # Auto-called in __post_init__, now disable it
    image.strip_image_version(enabled=False)

    profile = image.state.profiles["default"]
    finalize_cmds = profile.phases.get("finalize", [])
    assert not any("IMAGE_VERSION" in cmd.argv[0] for cmd in finalize_cmds)


def test_compile_backports_sync_hook(tmp_path: Path) -> None:
    """backports() registers a sync hook that generates debian-backports.sources."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.install("systemd")
    image.backports(mirror="https://snapshot.debian.org/archive/debian/20251113T083151Z")

    output_dir = image.compile(tmp_path / "mkosi")
    sync_script = output_dir / "default" / "scripts" / "01-sync.sh"

    assert sync_script.exists()
    content = sync_script.read_text(encoding="utf-8")
    assert "debian-backports.sources" in content
    assert "snapshot.debian.org" in content
    assert "${RELEASE}-backports" in content
    assert "sid" in content
    assert "debian-archive-keyring.gpg" in content


def test_compile_backports_registered_in_sync_phase() -> None:
    """backports() registers the hook in the sync phase of the profile state."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.backports(mirror="https://example.com/debian")

    profile = image.state.profiles["default"]
    assert "sync" in profile.phases
    commands = profile.phases["sync"]
    assert len(commands) >= 1

    sync_command = commands[0]
    script = sync_command.argv[0]
    assert "example.com/debian" in script
    assert "debian-backports.sources" in script


def test_compile_backports_auto_adds_sandbox_trees() -> None:
    """backports() auto-adds sandbox_trees entry for the generated file."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.backports()

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert expected_entry in image.sandbox_trees


def test_compile_backports_no_duplicate_sandbox_trees() -> None:
    """backports() does not duplicate sandbox_trees entry if already present."""
    image = Image(
        base="debian/bookworm",
        reproducible=False,
        sandbox_trees=(
            "mkosi.builddir/debian-backports.sources"
            ":/etc/apt/sources.list.d/debian-backports.sources",
        ),
    )
    image.backports()

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert image.sandbox_trees.count(expected_entry) == 1


def test_compile_backports_jq_fallback_when_no_mirror() -> None:
    """When mirror is not provided, script reads from /work/config.json via jq."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.backports()

    profile = image.state.profiles["default"]
    sync_command = profile.phases["sync"][0]
    script = sync_command.argv[0]
    assert "jq -r .Mirror /work/config.json" in script
    assert 'MIRROR="http://deb.debian.org/debian"' in script


def test_compile_backports_custom_release() -> None:
    """backports() accepts a custom release parameter."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.backports(release="trixie")

    profile = image.state.profiles["default"]
    sync_command = profile.phases["sync"][0]
    script = sync_command.argv[0]
    assert 'RELEASE="trixie"' in script


def test_compile_backports_sandbox_trees_in_mkosi_conf(tmp_path: Path) -> None:
    """backports() sandbox_trees entry appears in emitted mkosi.conf."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.install("systemd")
    image.backports()

    output_dir = image.compile(tmp_path / "mkosi")
    conf = (output_dir / "default" / "mkosi.conf").read_text(encoding="utf-8")
    assert "SandboxTrees=" in conf
    assert "debian-backports.sources" in conf


def test_compile_debloat_profile_conditional_paths(tmp_path: Path) -> None:
    """Profile-conditional debloat emits if-guard in finalize script."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.debloat(
        enabled=True,
        paths_skip_for_profiles={
            "devtools": ("/usr/share/bash-completion",),
        },
    )

    output_dir = image.compile(tmp_path / "mkosi")
    finalize = output_dir / "default" / "scripts" / "07-finalize.sh"
    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")

    # /usr/share/bash-completion should NOT appear in unconditional rm -rf section
    unconditional_section = content.split("# Debloat: profile-conditional")[0]
    assert "/usr/share/bash-completion" not in unconditional_section

    # It should appear in the conditional section with profile guard
    assert "${PROFILES:-}" in content
    assert '"devtools"' in content
    assert "/usr/share/bash-completion" in content
    assert 'if [[ ! "${PROFILES:-}" == *"devtools"* ]]; then' in content


def test_compile_debloat_profile_conditional_unconditional_coexist(
    tmp_path: Path,
) -> None:
    """Unconditional paths are still removed alongside conditional ones."""
    image = Image(base="debian/bookworm", reproducible=False)
    image.debloat(
        enabled=True,
        paths_skip_for_profiles={
            "devtools": ("/usr/share/bash-completion",),
        },
    )

    output_dir = image.compile(tmp_path / "mkosi")
    finalize = output_dir / "default" / "scripts" / "07-finalize.sh"
    content = finalize.read_text(encoding="utf-8")

    # Unconditional paths still present (e.g. /usr/share/doc)
    assert 'rm -rf "$BUILDROOT/usr/share/doc"' in content
    # Conditional path is guarded
    assert 'rm -rf "$BUILDROOT/usr/share/bash-completion"' in content


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return snapshot
