from collections.abc import Sequence
from dataclasses import replace
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
    Init,
    Kernel,
    Mkosi,
    Package,
    Recipe,
    Repository,
    Service,
    Setting,
    Template,
    Unit,
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
HISTORICAL = Mkosi(dialect="nethermind-v1")


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


def test_users_pull_in_the_account_tools_package(tmp_path: Path) -> None:
    """``mkosi-chroot useradd`` exits 127 unless the image installs ``passwd``."""
    with_user = _compile(_recipe(Package("systemd"), User("app")), tmp_path / "user")
    assert "    passwd\n" in _conf(with_user)

    without = _compile(_recipe(Package("systemd")), tmp_path / "none")
    assert "passwd" not in _conf(without)


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


def test_compile_not_bootable_writes_a_disk_without_uki(tmp_path: Path) -> None:
    """Bootable=no swaps the UKI for a plain disk; mkosi needs a kernel for a UKI."""
    off = Setting("Content", "Bootable", ("no",))
    conf_text = _conf(_compile(_recipe(Package("curl"), off), tmp_path / "off"))
    assert "Format=disk" in conf_text.splitlines()
    assert "Bootable=no" in conf_text.splitlines()
    assert "Format=uki" not in conf_text
    assert "Bootable=yes" not in conf_text
    with_kernel = _compile(_recipe(_kernel("6.1.2"), off), tmp_path / "kernel")
    assert "Bootable=yes" not in _conf(with_kernel)
    assert "Bootable=no" in _conf(with_kernel).splitlines()
    plain = _conf(_compile(_recipe(Package("curl")), tmp_path / "plain"))
    assert "Format=uki" in plain
    assert "Bootable" not in plain
    with pytest.raises(ValidationError, match="writes Bootable= itself"):
        compile(_recipe(Package("curl"), Setting("Content", "Bootable", ("yes",))))


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
    # The package's own stub stays: nothing is copied over linuxx64.efi.stub
    assert "/usr/lib/systemd/boot/efi" not in content


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
    """nethermind-v1: Backports() declares a sync hook that generates debian-backports.sources."""
    recipe = _recipe(
        Package("systemd"),
        Backports(mirror="https://snapshot.debian.org/archive/debian/20251113T083151Z"),
        epoch=None,
        mkosi=HISTORICAL,
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
    """nethermind-v1: Backports() lowers to a hook in the sync phase of the profile state."""
    recipe = _recipe(Backports(mirror="https://example.com/debian"), epoch=None, mkosi=HISTORICAL)

    scripts = _phase_scripts(recipe, "sync")
    assert len(scripts) >= 1
    assert "example.com/debian" in scripts[0]
    assert "debian-backports.sources" in scripts[0]


def test_compile_backports_auto_adds_sandbox_trees() -> None:
    """nethermind-v1: Backports() adds the sandbox_trees entry for the generated file."""
    image = lower(_recipe(Backports(), epoch=None, mkosi=HISTORICAL))

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert expected_entry in image.mkosi.sandbox_trees


def test_compile_backports_no_duplicate_sandbox_trees() -> None:
    """nethermind-v1: Backports() does not duplicate a sandbox_trees entry already set."""
    recipe = _recipe(
        Setting("Build", "SandboxTrees", (BACKPORTS_TREE,)),
        Backports(),
        epoch=None,
        mkosi=HISTORICAL,
    )
    image = lower(recipe)

    expected_entry = (
        "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
    )
    assert image.mkosi.sandbox_trees.count(expected_entry) == 1


def test_compile_backports_jq_fallback_when_no_mirror() -> None:
    """nethermind-v1: without a mirror the script reads $BUILDDIR/config.json via jq."""
    script = _phase_scripts(_recipe(Backports(), epoch=None, mkosi=HISTORICAL), "sync")[0]
    assert 'jq -r .Mirror "$BUILDDIR/config.json"' in script
    assert 'MIRROR="http://deb.debian.org/debian"' in script


def test_compile_backports_custom_release() -> None:
    """nethermind-v1: Backports() accepts a custom release parameter."""
    script = _phase_scripts(
        _recipe(Backports(release="trixie"), epoch=None, mkosi=HISTORICAL), "sync"
    )[0]
    assert 'RELEASE="trixie"' in script


def test_compile_backports_sandbox_trees_in_mkosi_conf(tmp_path: Path) -> None:
    """nethermind-v1: the Backports() sandbox_trees entry appears in emitted mkosi.conf."""
    recipe = _recipe(Package("systemd"), Backports(), epoch=None, mkosi=HISTORICAL)

    conf = _conf(_compile(recipe, tmp_path / "mkosi"))
    assert "SandboxTrees=" in conf
    assert "debian-backports.sources" in conf


def test_compile_backports_writes_static_sandbox_sources(tmp_path: Path) -> None:
    """current: Backports() is a compile-time mkosi.sandbox file, with no hook or builddir tree."""
    recipe = _recipe(Package("systemd"), Backports(), epoch=None)
    recipe = replace(recipe, snapshot="20251113T083151Z")

    output_dir = _compile(recipe, tmp_path / "mkosi")

    sources = output_dir / "default/mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
    stanza = (
        "Types: deb deb-src\n"
        "URIs: https://snapshot.debian.org/archive/debian/20251113T083151Z\n"
        "Suites: {}\n"
        "Components: main\n"
        "Enabled: yes\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    )
    assert sources.read_text() == stanza.format("bookworm-backports") + "\n" + stanza.format("sid")
    pins = output_dir / "default/mkosi.sandbox/etc/apt/preferences.d/debian-backports.pref"
    assert pins.read_text() == (
        "Package: *\nPin: release n=bookworm-backports\nPin-Priority: 200\n\n"
        "Package: *\nPin: release n=sid\nPin-Priority: 100\n"
    )
    assert not _script(output_dir, "01-sync.sh").exists()
    conf = _conf(output_dir)
    assert "SandboxTrees=" not in conf and "mkosi.builddir" not in conf
    assert lower(recipe).mkosi.sandbox_trees == ()


def test_compile_backports_sandbox_sources_take_the_fragments_fields() -> None:
    """current: Backports' mirror/release win; without either, deb.debian.org and the base."""
    pinned = lower(_recipe(Backports(mirror="http://m", release="trixie"), epoch=None))
    ((path, text), (pins_path, pins)) = pinned.mkosi.sandbox_files
    assert path == "/etc/apt/sources.list.d/debian-backports.sources"
    assert "URIs: http://m\nSuites: trixie-backports\n" in text
    assert "URIs: http://m\nSuites: sid\n" in text
    assert pins_path == "/etc/apt/preferences.d/debian-backports.pref"
    assert "Pin: release n=trixie-backports\n" in pins
    plain = lower(_recipe(Backports(), epoch=None))
    assert (
        "URIs: http://deb.debian.org/debian\nSuites: bookworm-backports\n"
        in (plain.mkosi.sandbox_files[0][1])
    )
    assert not _phase_scripts(_recipe(Backports(), epoch=None), "sync")
    rooted = lower(replace(_recipe(Backports(), epoch=None), mirror="https://m.example/"))
    assert "URIs: https://m.example/debian\n" in rooted.mkosi.sandbox_files[0][1]
    snap = replace(
        _recipe(Backports(), epoch=None), mirror="https://m.example", snapshot="20250101T000000Z"
    )
    assert (
        "URIs: https://m.example/archive/debian/20250101T000000Z\n"
        in (lower(snap).mkosi.sandbox_files[0][1])
    )


def test_compile_snapshot_writes_the_snapshot_line(tmp_path: Path) -> None:
    """Recipe.snapshot is mkosi's Snapshot=; the mirror stays a root."""
    recipe = replace(
        _recipe(Package("systemd")),
        mirror="https://snapshot.debian.org",
        snapshot="20251113T083151Z",
    )
    conf = _conf(_compile(recipe, tmp_path / "mkosi"))
    assert "Mirror=https://snapshot.debian.org\nSnapshot=20251113T083151Z\n" in conf


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("mirror", "https://snapshot.debian.org/archive/debian/20251113T083151Z/"),
        ("mirror", "https://deb.debian.org/debian"),
        ("tools_mirror", "https://deb.debian.org/debian/"),
    ],
)
def test_compile_rejects_an_archive_url_as_the_mirror(field: str, url: str) -> None:
    """current: mkosi appends the archive path to Mirror=, so a full archive URL is refused."""
    with pytest.raises(ValidationError, match=f"Recipe.{field} .* names an archive"):
        recipe = _recipe(Package("systemd"))
        if field == "mirror":
            lower(replace(recipe, mirror=url))
        else:
            lower(replace(recipe, tools_mirror=url))


def test_compile_historical_dialect_keeps_an_archive_mirror(tmp_path: Path) -> None:
    """nethermind-v1 writes the historical full archive URL unchanged."""
    url = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
    recipe = replace(_recipe(Package("systemd"), mkosi=Mkosi(dialect="nethermind-v1")), mirror=url)
    assert f"Mirror={url}\n" in _conf(_compile(recipe, tmp_path / "mkosi"))


def test_compile_efi_stub_current_script_installs_from_the_image_root() -> None:
    """current: the .deb goes to $BUILDROOT/ (mkosi-chroot has its own /tmp); IDs expand."""
    (script,) = [
        cmd
        for cmd in _phase_scripts(
            _recipe(EfiStub(snapshot="20251113T083151Z", version="1")), "postinst"
        )
        if "systemd-boot-efi" in cmd
    ]
    assert (
        'EFI_SNAPSHOT_URL="https://snapshot.debian.org/archive/debian/20251113T083151Z"' in script
    )
    assert 'curl -sSfL -o "$BUILDROOT/systemd-boot-efi.deb" "$DEB_URL"' in script
    assert "mkosi-chroot dpkg -i /systemd-boot-efi.deb\n" in script
    assert 'rm -f "$BUILDROOT/systemd-boot-efi.deb"' in script
    assert "/tmp/" not in script and "systemd-bootx64.efi" not in script
    assert "pick the version a suite of that snapshot ships" in script


def test_compile_repository_reaches_the_build_and_the_image(tmp_path: Path) -> None:
    """current: a Repository goes to mkosi.sandbox (with its declared keyring) and the image."""
    keyring = "/etc/apt/keyrings/extra.asc"
    recipe = _recipe(
        Repository("extra", "https://repo.example/debian", "stable", keyring=keyring, priority=10),
        Repository("tools", "https://tools.example/apt", "stable", in_image=False),
        File(keyring, "KEY\n"),
    )
    out = _compile(recipe, tmp_path / "mkosi") / "default"
    sandbox = out / "mkosi.sandbox/etc/apt"
    skeleton = out / "mkosi.skeleton/etc/apt"
    assert (sandbox / "sources.list.d/extra.sources").read_text() == (
        "Types: deb\nURIs: https://repo.example/debian\nSuites: stable\nComponents: main\n"
        f"Enabled: yes\nSigned-By: {keyring}\n"
    )
    assert "Pin-Priority: 10\n" in (sandbox / "preferences.d/extra.pref").read_text()
    assert (out / "mkosi.sandbox" / keyring.lstrip("/")).read_text() == "KEY\n"
    assert (
        (skeleton / "sources.list.d/extra.sources").read_text().startswith("Types: deb deb-src\n")
    )
    assert (sandbox / "sources.list.d/tools.sources").is_file()
    assert not (skeleton / "sources.list.d/tools.sources").exists()


def test_compile_historical_repository_stays_in_the_skeleton(tmp_path: Path) -> None:
    """nethermind-v1: repositories are image-only skeleton files, as before."""
    recipe = _recipe(
        Repository("extra", "https://repo.example/debian", "stable"),
        mkosi=Mkosi(dialect="nethermind-v1"),
    )
    out = _compile(recipe, tmp_path / "mkosi") / "default"
    assert (out / "mkosi.skeleton/etc/apt/sources.list.d/extra.sources").is_file()
    assert not (out / "mkosi.sandbox").exists()


def test_compile_backports_only_in_the_variant_that_adds_it(tmp_path: Path) -> None:
    """current: a variant adding Backports gets the sandbox file; the default does not."""
    recipe = _recipe(
        Package("systemd"),
        variants=(
            Variant("default", target="qemu"),
            Variant("bp", parent="default", add=Backports()),
        ),
        epoch=None,
    )
    output_dir = _compile(recipe, tmp_path / "mkosi")
    path = "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
    assert (output_dir / "bp" / path).is_file()
    assert not (output_dir / "default" / path).exists()


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


# ── current-dialect output names and runtime-init dependencies ──────────


@pytest.mark.parametrize(("target", "script"), [("azure", "azure"), ("gcp", "gcp")])
def test_cloud_postoutput_reads_the_output_with_or_without_a_version(
    tmp_path: Path, target: Literal["azure", "gcp"], script: str
) -> None:
    """current: ${IMAGE_ID}.efi without ImageVersion=; nethermind-v1 keeps ${IMAGE_ID}_${...}."""
    variants = (Variant("default", target="qemu"), Variant(target, parent="default", target=target))
    current = _compile(_recipe(variants=variants), tmp_path / "current")
    text = _script(current, f"{script}-postoutput.sh", target).read_text()
    assert '"${OUTPUTDIR}/${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}.efi"' in text
    assert "${IMAGE_ID}_${IMAGE_VERSION}" not in text
    old = _compile(_recipe(variants=variants, mkosi=HISTORICAL), tmp_path / "old")
    old_text = _script(old, f"{script}-postoutput.sh", target).read_text()
    assert '"${OUTPUTDIR}/${IMAGE_ID}_${IMAGE_VERSION}.efi"' in old_text


@pytest.mark.parametrize("version", [None, "1.2"])
def test_current_postoutput_output_name_runs_under_set_u(version: str | None) -> None:
    import subprocess

    from tundravm.compiler.emit_mkosi import OUTPUT_NAME

    env = {"IMAGE_ID": "node", "PATH": "/usr/bin:/bin"}
    if version is not None:
        env["IMAGE_VERSION"] = version
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; echo "{OUTPUT_NAME}.efi"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ("node.efi" if version is None else "node_1.2.efi")


def _runtime_init_unit(out: Path, variant: str = "default") -> str:
    path = out / variant / "mkosi.extra/usr/lib/systemd/system/runtime-init.service"
    return path.read_text(encoding="utf-8")


def test_runtime_init_waits_for_network_online_without_network_setup(tmp_path: Path) -> None:
    out = _compile(_recipe(Init("hello", "echo hi")), tmp_path / "mkosi")
    unit = _runtime_init_unit(out)
    assert "After=network-online.target\nWants=network-online.target\n" in unit
    assert "network-setup" not in unit


@pytest.mark.parametrize(
    "declared",
    [
        Unit("network-setup.service", enabled=True),
        File("/etc/systemd/system/network-setup.service", "[Service]\n", stage="skeleton"),
        Service("network-setup", "/usr/bin/net-up"),
    ],
    ids=["unit", "file", "service"],
)
def test_runtime_init_requires_a_declared_network_setup(
    tmp_path: Path, declared: Declaration
) -> None:
    out = _compile(_recipe(Init("hello", "echo hi"), declared), tmp_path / "mkosi")
    unit = _runtime_init_unit(out)
    assert "After=network.target network-setup.service\nRequires=network-setup.service\n" in unit
    assert "network-online" not in unit


def test_runtime_init_follows_a_variants_own_network_setup(tmp_path: Path) -> None:
    recipe = _recipe(
        Init("hello", "echo hi"),
        variants=(
            Variant("default", target="qemu"),
            Variant(
                "net",
                parent="default",
                add=Fragment("net", items=(Unit("network-setup.service", enabled=True),)),
            ),
        ),
    )
    out = _compile(recipe, tmp_path / "mkosi")
    assert "Wants=network-online.target" in _runtime_init_unit(out)
    assert "Requires=network-setup.service" in _runtime_init_unit(out, "net")


def test_runtime_init_keeps_network_setup_under_nethermind_v1(tmp_path: Path) -> None:
    out = _compile(_recipe(Init("hello", "echo hi"), mkosi=HISTORICAL), tmp_path / "mkosi")
    unit = _runtime_init_unit(out)
    assert "After=network.target network-setup.service\nRequires=network-setup.service\n" in unit
