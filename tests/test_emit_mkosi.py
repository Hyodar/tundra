from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

from tundravm.compiler import DEFAULT_TDX_INIT_SCRIPT
from tundravm.compiler.emit_mkosi import ARCH_TO_MKOSI
from tundravm.declarative import (
    Debloat,
    Declaration,
    File,
    Fragment,
    Git,
    Hook,
    Kernel,
    Mkosi,
    Package,
    Recipe,
    Service,
    Setting,
    Template,
    User,
    Variant,
    compile,
    lower,
)
from tundravm.declarative.utils import BACKPORTS_TREE, Backports, EfiStub
from tundravm.errors import ValidationError
from tundravm.models import Phase

Arch = Literal["x86_64", "aarch64"]
LINUX = "https://github.com/gregkh/linux"
UNIT_DIR = Path("default") / "mkosi.extra" / "usr" / "lib" / "systemd" / "system"


def _recipe(
    *items: Declaration | Fragment,
    variants: Sequence[Variant] | None = None,
    arch: Arch = "x86_64",
    epoch: int | None = 0,
    mkosi: Mkosi | None = None,
) -> Recipe:
    return Recipe(
        "test",
        Fragment("common", items=items),
        variants=tuple(variants or (Variant("default", target="qemu"),)),
        base="debian/bookworm",
        arch=arch,
        epoch=epoch,
        mkosi=mkosi or Mkosi(),
    )


def _compile(recipe: Recipe, out: Path, *, variants: Sequence[str] | None = None) -> Path:
    compile(recipe, variants=variants).write(out)
    return out


def _conf(out: Path, variant: str = "default") -> str:
    return (out / variant / "mkosi.conf").read_text(encoding="utf-8")


def _script(out: Path, name: str, variant: str = "default") -> Path:
    return out / variant / "scripts" / name


def _phase_scripts(recipe: Recipe, phase: Phase) -> list[str]:
    commands = lower(recipe).state.profiles["default"].phases.get(phase, [])
    return [command.argv[0] for command in commands]


def _kernel(version: str, config: Path | None = None, repo: str = LINUX) -> Kernel:
    return Kernel(version, Git(repo, f"v{version}"), config=config)


def test_compile_golden_output(tmp_path: Path) -> None:
    recipe = _recipe(
        Package("jq"),
        Package("curl"),
        Hook("prep", "prepare", "echo prep", env=(("B", "2"), ("A", "1")), cwd="/work"),
        Hook("build", "build", "echo build"),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    conf_text = _conf(output_dir)
    prepare_script = _script(output_dir, "03-prepare.sh")
    build_script = _script(output_dir, "04-build.sh")

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
    recipe = _recipe(Package("curl"), Hook("hello", "prepare", "echo hello"))

    output_a = _compile(recipe, tmp_path / "mkosi-a")
    output_b = _compile(recipe, tmp_path / "mkosi-b")

    assert _snapshot_tree(output_a) == _snapshot_tree(output_b)
    assert compile(recipe).digest == compile(recipe).digest


def test_compile_rejects_invalid_phase(tmp_path: Path) -> None:
    image = lower(_recipe())
    image.state.profiles["default"].phases[cast(Phase, "invalid-phase")] = []

    with pytest.raises(ValidationError) as excinfo:
        image.compile(tmp_path / "mkosi")

    assert excinfo.value.code == "E_VALIDATION"


def test_compile_generates_extra_tree(tmp_path: Path) -> None:
    recipe = _recipe(
        File("/etc/motd", "TDX VM\n"),
        Template(
            "/etc/app/config.toml",
            "network={network}\n",
            variables=(("network", "mainnet"),),
        ),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    extra_dir = output_dir / "default" / "mkosi.extra"
    assert (extra_dir / "etc" / "motd").read_text(encoding="utf-8") == "TDX VM\n"
    assert (extra_dir / "etc" / "app" / "config.toml").read_text(
        encoding="utf-8"
    ) == "network=mainnet\n"


def test_compile_generates_service_units(tmp_path: Path) -> None:
    recipe = _recipe(
        Service(
            "app",
            ("/usr/bin/app", "--config", "/etc/app.toml"),
            user="app",
            after=("network-online.target",),
            restart="always",
            security="strict",
        )
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    unit_path = output_dir / UNIT_DIR / "app.service"
    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/app --config /etc/app.toml" in content
    assert "User=app" in content
    assert "After=network-online.target" in content
    assert "Restart=always" in content
    assert "ProtectSystem=strict" in content
    assert "WantedBy=minimal.target" in content


def test_compile_generates_service_unit_all_sections(tmp_path: Path) -> None:
    recipe = _recipe(
        Service(
            "app",
            ("/usr/bin/app",),
            description="App daemon",
            user="app",
            group="app",
            working_dir="/var/lib/app",
            env=(("B", "two words"), ("A", "1")),
            env_file="/etc/app.env",
            exec_start_pre=("/usr/bin/app --check",),
            after=("network-online.target",),
            requires=("network-online.target",),
            wants=("network-online.target",),
            wanted_by="multi-user.target",
            type="notify",
            restart="on-failure",
            limits=(("NOFILE", 1048576), ("CORE", "infinity")),
            kill_mode="mixed",
            timeout_stop="30s",
        )
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    content = (output_dir / UNIT_DIR / "app.service").read_text(encoding="utf-8")
    assert content == (
        "[Unit]\n"
        "Description=App daemon\n"
        "After=network-online.target\n"
        "Requires=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=notify\n"
        "ExecStartPre=/usr/bin/app --check\n"
        "ExecStart=/usr/bin/app\n"
        "User=app\n"
        "Group=app\n"
        "WorkingDirectory=/var/lib/app\n"
        "EnvironmentFile=/etc/app.env\n"
        "Environment=A=1\n"
        'Environment="B=two words"\n'
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "LimitCORE=infinity\n"
        "LimitNOFILE=1048576\n"
        "KillMode=mixed\n"
        "TimeoutStopSec=30s\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def test_compile_generates_postinst_with_users(tmp_path: Path) -> None:
    recipe = _recipe(User("app", system=True, home="/var/lib/app", uid=1000, groups=("tdx",)))

    output_dir = _compile(recipe, tmp_path / "mkosi")

    # Check that postinst script exists and has user creation via mkosi-chroot
    postinst = _script(output_dir, "06-postinst.sh")
    assert postinst.exists()
    content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot useradd" in content
    assert "--system" in content
    assert "--home-dir" in content
    assert "/var/lib/app" in content


def test_compile_generates_debloat_finalize(tmp_path: Path) -> None:
    recipe = _recipe(Debloat(enabled=True, extra_remove=("/usr/share/fonts",)))

    output_dir = _compile(recipe, tmp_path / "mkosi")

    # Finalize has path removal
    finalize = _script(output_dir, "07-finalize.sh")
    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")
    assert "rm -rf" in content
    assert "/usr/share/doc" in content
    assert "/usr/share/fonts" in content

    # Systemd binary cleanup is now in postinst (via dpkg-query), not finalize
    postinst = _script(output_dir, "06-postinst.sh")
    assert postinst.exists()
    postinst_content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot dpkg-query -L systemd" in postinst_content
    assert "default.target" in postinst_content


def test_compile_architecture_field(tmp_path: Path) -> None:
    """Architecture is mapped correctly from Arch type to mkosi value."""
    for py_arch, mkosi_arch in ARCH_TO_MKOSI.items():
        recipe = _recipe(Package("curl"), arch=cast(Arch, py_arch))
        output_dir = _compile(recipe, tmp_path / f"mkosi-{py_arch}")
        assert f"Architecture={mkosi_arch}" in _conf(output_dir)


def test_compile_with_network_configurable(tmp_path: Path) -> None:
    """WithNetwork can be set to True or False."""
    for with_net, expected in [("true", "WithNetwork=true"), ("false", "WithNetwork=false")]:
        recipe = _recipe(Package("curl"), Setting("Build", "WithNetwork", (with_net,)))
        output_dir = _compile(recipe, tmp_path / f"mkosi-net-{with_net}")
        assert expected in _conf(output_dir)


def test_compile_no_at_prefix(tmp_path: Path) -> None:
    """Format and ImageId do not have @ prefix in mkosi v26 output."""
    output_dir = _compile(_recipe(Package("curl")), tmp_path / "mkosi")
    conf_text = _conf(output_dir)
    assert "@Format=" not in conf_text
    assert "@ImageId=" not in conf_text
    assert "Format=uki" in conf_text
    assert "ImageId=default" in conf_text


def test_compile_service_enablement_uses_mkosi_chroot(tmp_path: Path) -> None:
    """Service enablement uses mkosi-chroot systemctl enable."""
    recipe = _recipe(Service("myapp", "/usr/bin/myapp"))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    content = _script(output_dir, "06-postinst.sh").read_text(encoding="utf-8")
    assert "mkosi-chroot systemctl enable myapp.service" in content


def test_compile_debloat_uses_dpkg_query(tmp_path: Path) -> None:
    """Debloat uses mkosi-chroot dpkg-query for binary and unit enumeration."""
    output_dir = _compile(_recipe(Debloat(enabled=True)), tmp_path / "mkosi")
    content = _script(output_dir, "06-postinst.sh").read_text(encoding="utf-8")

    # Binary cleanup via dpkg-query
    assert "mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/'" in content
    # Unit masking via dpkg-query
    assert (
        "mkosi-chroot dpkg-query -L systemd | "
        "grep -E '\\.service$|\\.socket$|\\.timer$|\\.target$|\\.mount$'"
    ) in content


def test_compile_default_target(tmp_path: Path) -> None:
    """Default systemd target is set to minimal.target when debloat is enabled."""
    output_dir = _compile(_recipe(Debloat(enabled=True)), tmp_path / "mkosi")
    content = _script(output_dir, "06-postinst.sh").read_text(encoding="utf-8")
    assert 'ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"' in content


def test_compile_skeleton_init_script(tmp_path: Path) -> None:
    """Custom init script is written to mkosi.skeleton/init when configured."""
    recipe = _recipe(Package("systemd"), mkosi=Mkosi(init_script=DEFAULT_TDX_INIT_SCRIPT))

    output_dir = _compile(recipe, tmp_path / "mkosi")
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
    recipe = _recipe(Package("curl"), mkosi=Mkosi(version_script=True))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    version_path = output_dir / "mkosi.version"
    assert version_path.exists()
    content = version_path.read_text(encoding="utf-8")
    assert "git rev-parse --short=6 HEAD" in content
    assert version_path.stat().st_mode & 0o755 == 0o755


def test_compile_gcp_postoutput(tmp_path: Path) -> None:
    """GCP postoutput script is emitted when the variant targets gcp."""
    recipe = _recipe(Package("curl"), variants=(Variant("default", target="gcp"),))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    gcp_script = _script(output_dir, "gcp-postoutput.sh")
    assert gcp_script.exists()
    content = gcp_script.read_text(encoding="utf-8")
    assert "sgdisk" in content
    assert "tar.gz" in content
    assert gcp_script.stat().st_mode & 0o755 == 0o755


def test_compile_azure_postoutput(tmp_path: Path) -> None:
    """Azure postoutput script is emitted when the variant targets azure."""
    recipe = _recipe(Package("curl"), variants=(Variant("default", target="azure"),))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    azure_script = _script(output_dir, "azure-postoutput.sh")
    assert azure_script.exists()
    content = azure_script.read_text(encoding="utf-8")
    assert "qemu-img convert" in content
    assert ".vhd" in content


def test_compile_native_profiles_mode(tmp_path: Path) -> None:
    """Native layout creates root mkosi.conf + mkosi.profiles/<name>/."""
    recipe = _recipe(
        Package("curl"),
        variants=(
            Variant("default", target="qemu"),
            Variant("prod", add=Fragment("prod", items=(Package("nginx"),))),
        ),
        mkosi=Mkosi(layout="native"),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    # Root mkosi.conf
    assert (output_dir / "mkosi.conf").exists()
    # Root skeleton/extra
    assert (output_dir / "mkosi.skeleton").is_dir()
    # Profile-specific override
    assert (output_dir / "mkosi.profiles" / "default" / "mkosi.conf").exists()
    assert (output_dir / "mkosi.profiles" / "prod" / "mkosi.conf").exists()


def test_compile_environment_key_value(tmp_path: Path) -> None:
    """Environment=KEY=VALUE pairs are emitted in [Build] section."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("MY_VAR=hello", "OTHER=world")),
        epoch=None,
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "Environment=MY_VAR=hello" in conf_text
    assert "Environment=OTHER=world" in conf_text


def test_compile_environment_passthrough(tmp_path: Path) -> None:
    """Environment=KEY (passthrough without value) is emitted in [Build] section."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("KERNEL_IMAGE", "KERNEL_VERSION")),
        epoch=None,
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "Environment=KERNEL_IMAGE\n" in conf_text
    assert "Environment=KERNEL_VERSION\n" in conf_text


def test_compile_environment_both_forms(tmp_path: Path) -> None:
    """Both key=value and passthrough forms coexist in [Build] section."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("SOURCE_DATE_EPOCH=0", "KERNEL_IMAGE")),
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "Environment=SOURCE_DATE_EPOCH=0" in conf_text
    assert "Environment=KERNEL_IMAGE\n" in conf_text


def test_compile_reproducible_auto_adds_source_date_epoch(tmp_path: Path) -> None:
    """With an epoch, SOURCE_DATE_EPOCH=0 is auto-added to environment."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("MY_VAR=test",)),
        epoch=0,
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    # Both the user env and the auto-added SOURCE_DATE_EPOCH
    assert "Environment=SOURCE_DATE_EPOCH=0" in conf_text
    assert "Environment=MY_VAR=test" in conf_text


def test_compile_reproducible_no_override_user_epoch(tmp_path: Path) -> None:
    """A user-provided SOURCE_DATE_EPOCH is not overridden by the default epoch."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("SOURCE_DATE_EPOCH=1234",)),
        epoch=0,
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "Environment=SOURCE_DATE_EPOCH=1234" in conf_text
    assert "Environment=SOURCE_DATE_EPOCH=0" not in conf_text
    assert "SourceDateEpoch=1234\n" in conf_text


@pytest.mark.parametrize(
    ("epoch", "lines"),
    [
        (1700000000, ["Environment=SOURCE_DATE_EPOCH=1700000000", "SourceDateEpoch=1700000000"]),
        (0, ["Environment=SOURCE_DATE_EPOCH=0", "SourceDateEpoch=0"]),
        (None, []),
    ],
)
def test_compile_epoch_sets_source_date_epoch_consistently(
    tmp_path: Path, epoch: int | None, lines: list[str]
) -> None:
    conf_text = _conf(_compile(_recipe(Package("curl"), epoch=epoch), tmp_path / "mkosi"))

    found = [
        line
        for line in conf_text.splitlines()
        if "SOURCE_DATE_EPOCH" in line or line.startswith("SourceDateEpoch=")
    ]
    assert found == lines


def test_compile_kernel_with_config_emits_build_script(tmp_path: Path) -> None:
    """When kernel has config_file, a real build script is emitted."""
    # Create a fake kernel config file
    config_file = tmp_path / "kernel-yocto.config"
    config_file.write_text("# CONFIG_LOCALVERSION is not set\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    build_script = _script(output_dir, "04-build.sh")

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

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    kernel_config = output_dir / "default" / "kernel" / "kernel.config"

    assert kernel_config.exists()
    assert "CONFIG_TDX_GUEST=y" in kernel_config.read_text(encoding="utf-8")


def test_compile_kernel_config_auto_adds_env_passthrough(tmp_path: Path) -> None:
    """When kernel has config_file, KERNEL_IMAGE and KERNEL_VERSION are auto-added."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# kernel config\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "Environment=KERNEL_IMAGE\n" in conf_text
    assert "Environment=KERNEL_VERSION\n" in conf_text


def test_compile_kernel_without_config_no_build_script(tmp_path: Path) -> None:
    """When kernel does not have config_file, no kernel build script is emitted."""
    recipe = _recipe(Package("curl"), _kernel("6.8"), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")

    # No build script should exist (no build hooks registered)
    assert not _script(output_dir, "04-build.sh").exists()

    # No kernel dir should be created
    assert not (output_dir / "default" / "kernel").exists()

    # mkosi.conf should still have comment-only kernel config
    assert "# KernelVersion=6.8" in _conf(output_dir)


def test_compile_kernel_build_script_with_user_hooks(tmp_path: Path) -> None:
    """Kernel build script is combined with user-defined build hooks."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# config\n", encoding="utf-8")

    recipe = _recipe(
        Package("curl"),
        _kernel("6.13.12", config_file),
        Hook("custom", "build", "echo custom-build-step"),
        epoch=None,
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    build_script = _script(output_dir, "04-build.sh")

    assert build_script.exists()
    script_text = build_script.read_text(encoding="utf-8")

    # Both kernel build and user hook should be present
    assert "git clone" in script_text
    assert "custom-build-step" in script_text


def test_compile_kernel_custom_source_repo(tmp_path: Path) -> None:
    """Kernel build script uses the custom source repo."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# config\n", encoding="utf-8")

    recipe = _recipe(
        Package("curl"),
        _kernel("6.13.12", config_file, repo="https://github.com/custom/linux"),
        epoch=None,
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    script_text = _script(output_dir, "04-build.sh").read_text(encoding="utf-8")
    assert "https://github.com/custom/linux" in script_text


def test_compile_efi_stub_postinst_hook(tmp_path: Path) -> None:
    """EfiStub() declares a postinst hook that downloads and installs pinned EFI stub."""
    recipe = _recipe(
        Package("systemd"),
        EfiStub(
            snapshot="https://snapshot.debian.org/archive/debian/20251113T083151Z",
            version="255.4-1",
        ),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    postinst = _script(output_dir, "06-postinst.sh")

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
    """EfiStub() lowers to a hook in the postinst phase of the profile state."""
    recipe = _recipe(EfiStub(snapshot="https://snapshot.example.com", version="255.4-1"))

    scripts = _phase_scripts(recipe, "postinst")
    assert len(scripts) >= 1
    # The hook should contain the EFI stub script as a shell command
    script = scripts[-1]
    assert "snapshot.example.com" in script
    assert "255.4-1" in script


def test_compile_strip_image_version_finalize_hook(tmp_path: Path) -> None:
    """A reproducible recipe strips IMAGE_VERSION in a finalize hook."""
    # epoch=0 by default, so the os-release strip is on
    output_dir = _compile(_recipe(Package("systemd")), tmp_path / "mkosi")
    finalize = _script(output_dir, "07-finalize.sh")

    assert finalize.exists()
    content = finalize.read_text(encoding="utf-8")

    # Verify script strips IMAGE_VERSION from os-release
    assert "IMAGE_VERSION" in content
    assert "$BUILDROOT/usr/lib/os-release" in content
    assert "sed -i" in content


def test_compile_strip_image_version_registered_in_finalize_phase() -> None:
    """The os-release strip lowers to a hook in the finalize phase."""
    scripts = _phase_scripts(_recipe(), "finalize")
    assert len(scripts) >= 1
    assert "IMAGE_VERSION" in scripts[-1]


def test_compile_strip_image_version_auto_called_when_reproducible() -> None:
    """With an epoch the os-release strip is on by default."""
    scripts = _phase_scripts(_recipe(epoch=0), "finalize")
    assert any("IMAGE_VERSION" in script for script in scripts)


def test_compile_strip_image_version_not_called_when_not_reproducible() -> None:
    """With epoch=None the os-release strip is off by default."""
    scripts = _phase_scripts(_recipe(epoch=None), "finalize")
    assert not any("IMAGE_VERSION" in script for script in scripts)


def test_compile_strip_image_version_can_be_disabled() -> None:
    """Mkosi(strip_os_release=False) turns the strip off despite the epoch."""
    recipe = _recipe(epoch=0, mkosi=Mkosi(strip_os_release=False))
    scripts = _phase_scripts(recipe, "finalize")
    assert not any("IMAGE_VERSION" in script for script in scripts)


def test_compile_backports_sync_hook(tmp_path: Path) -> None:
    """Backports() declares a sync hook that generates debian-backports.sources."""
    recipe = _recipe(
        Package("systemd"),
        Backports(mirror="https://snapshot.debian.org/archive/debian/20251113T083151Z"),
        epoch=None,
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    sync_script = _script(output_dir, "01-sync.sh")

    assert sync_script.exists()
    content = sync_script.read_text(encoding="utf-8")
    assert "debian-backports.sources" in content
    assert "snapshot.debian.org" in content
    assert "${RELEASE}-backports" in content
    assert "sid" in content
    assert "debian-archive-keyring.gpg" in content


def test_compile_backports_registered_in_sync_phase() -> None:
    """Backports() lowers to a hook in the sync phase of the profile state."""
    recipe = _recipe(Backports(mirror="https://example.com/debian"), epoch=None)

    scripts = _phase_scripts(recipe, "sync")
    assert len(scripts) >= 1
    assert "example.com/debian" in scripts[0]
    assert "debian-backports.sources" in scripts[0]


def test_compile_backports_auto_adds_sandbox_trees() -> None:
    """Backports() adds the sandbox_trees entry for the generated file."""
    image = lower(_recipe(Backports(), epoch=None))

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert expected_entry in image.mkosi.sandbox_trees


def test_compile_backports_no_duplicate_sandbox_trees() -> None:
    """Backports() does not duplicate a sandbox_trees entry the recipe already sets."""
    recipe = _recipe(
        Setting("Build", "SandboxTrees", (BACKPORTS_TREE,)),
        Backports(),
        epoch=None,
    )
    image = lower(recipe)

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert image.mkosi.sandbox_trees.count(expected_entry) == 1


def test_compile_backports_jq_fallback_when_no_mirror() -> None:
    """When mirror is not provided, script reads from $BUILDDIR/config.json via jq."""
    script = _phase_scripts(_recipe(Backports(), epoch=None), "sync")[0]
    assert 'jq -r .Mirror "$BUILDDIR/config.json"' in script
    assert 'MIRROR="http://deb.debian.org/debian"' in script


def test_compile_backports_custom_release() -> None:
    """Backports() accepts a custom release parameter."""
    script = _phase_scripts(_recipe(Backports(release="trixie"), epoch=None), "sync")[0]
    assert 'RELEASE="trixie"' in script


def test_compile_backports_sandbox_trees_in_mkosi_conf(tmp_path: Path) -> None:
    """Backports() sandbox_trees entry appears in emitted mkosi.conf."""
    recipe = _recipe(Package("systemd"), Backports(), epoch=None)

    conf = _conf(_compile(recipe, tmp_path / "mkosi"))
    assert "SandboxTrees=" in conf
    assert "debian-backports.sources" in conf


def _devtools_debloat_recipe() -> Recipe:
    return _recipe(
        Debloat(
            enabled=True,
            keep_paths_by_variant=(("devtools", ("/usr/share/bash-completion",)),),
        ),
        variants=(Variant("default", target="qemu"), Variant("devtools")),
        epoch=None,
    )


def test_compile_debloat_profile_conditional_paths(tmp_path: Path) -> None:
    """Variant-conditional debloat emits an if-guard in the finalize script."""
    output_dir = _compile(_devtools_debloat_recipe(), tmp_path / "mkosi", variants=("default",))
    finalize = _script(output_dir, "07-finalize.sh")
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
    output_dir = _compile(_devtools_debloat_recipe(), tmp_path / "mkosi", variants=("default",))
    content = _script(output_dir, "07-finalize.sh").read_text(encoding="utf-8")

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
