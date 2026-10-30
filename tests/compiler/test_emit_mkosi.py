"""The emitter's output for one recipe at a time, read back from the mkosi-native tree.

The root ``mkosi.conf`` holds what every image shares (distribution, snapshot, tools
tree, network, source date, build mounts); ``payload/mkosi.conf`` and its
``mkosi.conf.d/`` the OS; ``payload/mkosi.<phase>.d/`` its scripts; a variant that adds
content is ``payload/mkosi.profiles/<variant>/``; a cloud target is a wrapper under
``mkosi.images/<target>/``.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal, cast

import pytest

from tests.helpers import conf_list, on_platform
from tundravm.compiler.emit_mkosi import ARCH_TO_MKOSI, AZURE_CONVERTER, GCP_CONVERTER
from tundravm.declarative import (
    BootStep,
    Debian,
    Debloat,
    Declaration,
    File,
    Fragment,
    Git,
    Hardening,
    Hook,
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
from tundravm.declarative._compile import emit
from tundravm.declarative.utils import Backports, EfiStub
from tundravm.errors import ValidationError
from tundravm.models import Phase

Arch = Literal["x86_64", "aarch64"]
LINUX = "https://github.com/gregkh/linux"
UNIT_DIR = Path("payload") / "mkosi.extra" / "usr" / "lib" / "systemd" / "system"


def _recipe(
    *items: Declaration | Fragment,
    variants: Sequence[Variant] | None = None,
    arch: Arch = "x86_64",
    epoch: int | None = 0,
    mkosi: Mkosi | None = None,
) -> Recipe:
    return Recipe(
        "test",
        items=items,
        variants=tuple(variants or (Variant("default", outputs=("qemu",)),)),
        platform=Debian("bookworm", arch=arch, epoch=epoch, boot=None),
        mkosi=mkosi or Mkosi(),
    )


def _compile(recipe: Recipe, out: Path, *, variants: Sequence[str] | None = None) -> Path:
    compile(recipe, variants=variants).write(out)
    return out


def _conf(out: Path, profile: str | None = None) -> str:
    """The payload's configuration as mkosi reads it: ``mkosi.conf``, ``mkosi.conf.d/*``,
    then *profile*'s overlay ``mkosi.conf``."""
    payload = out / "payload"
    texts = [(payload / "mkosi.conf").read_text(encoding="utf-8")]
    texts += [p.read_text(encoding="utf-8") for p in sorted(payload.glob("mkosi.conf.d/*"))]
    if profile is not None:
        texts.append((payload / "mkosi.profiles" / profile / "mkosi.conf").read_text())
    return "\n".join(texts)


def _main(out: Path) -> str:
    return (out / "mkosi.conf").read_text(encoding="utf-8")


def _script(out: Path, phase: str, name: str | None = None) -> Path:
    return out / "payload" / f"mkosi.{phase}.d" / (name or f"50-{phase}.sh")


def _phase_scripts(recipe: Recipe, phase: Phase) -> list[str]:
    commands = lower(recipe).state.profiles["default"].phases.get(phase, [])
    return [command.argv[0] for command in commands]


def _kernel(version: str, config: Path | None = None, repo: str = LINUX) -> Kernel:
    return Kernel(version, Git(repo, f"v{version}"), config=config)


def test_compile_golden_output(tmp_path: Path) -> None:
    recipe = _recipe(
        Package("jq"),
        Package("curl"),
        Hook("prep", "prepare", "echo prep", env={"B": "2", "A": "1"}, cwd="/work"),
        Hook("build", "build", "echo build"),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    main = _main(output_dir)
    assert "[Distribution]\nDistribution=debian\nRelease=bookworm\nArchitecture=x86-64\n" in main
    assert "\nSourceDateEpoch=0\n" in main
    assert "\nSeed=" in main
    assert "\nWithNetwork=no\n" in main

    conf_text = _conf(output_dir)
    assert "[Output]\nFormat=uki\nOutput=payload\n" in conf_text
    assert "ManifestFormat=json" in conf_text
    assert "@Format" not in conf_text and "@ImageId" not in conf_text
    assert "CleanPackageMetadata=yes" in conf_text
    assert conf_list(conf_text, "Packages") == ["curl", "jq"]
    # mkosi finds the scripts in mkosi.<phase>.d/: no *Scripts= key is written
    assert "Scripts=" not in conf_text

    assert _script(output_dir, "prepare").read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nset -euo pipefail\n\n(cd /work && A=1 B=2 echo prep)\n"
    )
    assert _script(output_dir, "build").read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\nset -euo pipefail\n\necho build\n"
    )


def test_compile_is_deterministic(tmp_path: Path) -> None:
    recipe = _recipe(Package("curl"), Hook("hello", "prepare", "echo hello"))

    output_a = _compile(recipe, tmp_path / "mkosi-a")
    output_b = _compile(recipe, tmp_path / "mkosi-b")

    assert _snapshot_tree(output_a) == _snapshot_tree(output_b)
    assert compile(recipe).digest == compile(recipe).digest


@pytest.mark.parametrize("phase", ["invalid-phase", "boot", "skeleton"])
def test_compile_rejects_a_phase_mkosi_runs_no_scripts_for(tmp_path: Path, phase: str) -> None:
    image = lower(_recipe())
    image.state.profiles["default"].phases[cast(Phase, phase)] = []

    with pytest.raises(ValidationError) as excinfo:
        emit(image, tmp_path / "mkosi")

    assert excinfo.value.code == "E_VALIDATION"


def test_compile_generates_extra_tree(tmp_path: Path) -> None:
    recipe = _recipe(
        File("/etc/motd", "TDX VM\n"),
        Template(
            "/etc/app/config.toml",
            "network={network}\n",
            variables={"network": "mainnet"},
        ),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    extra_dir = output_dir / "payload" / "mkosi.extra"
    assert (extra_dir / "etc" / "motd").read_text(encoding="utf-8") == "TDX VM\n"
    assert (extra_dir / "etc" / "app" / "config.toml").read_text(
        encoding="utf-8"
    ) == "network=mainnet\n"
    assert (extra_dir / "etc" / "machine-id").read_text(encoding="utf-8") == "uninitialized\n"


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
            env={"B": "two words", "A": "1"},
            env_file="/etc/app.env",
            exec_start_pre=("/usr/bin/app --check",),
            after=("network-online.target",),
            requires=("network-online.target",),
            wants=("network-online.target",),
            wanted_by="multi-user.target",
            type="notify",
            restart="on-failure",
            limits={"NOFILE": 1048576, "CORE": "infinity"},
            kill_mode="mixed",
            timeout_stop="30s",
            restart_sec="5",
            hardening=Hardening.none(),
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

    postinst = _script(output_dir, "postinst")
    assert postinst.exists()
    content = postinst.read_text(encoding="utf-8")
    assert "mkosi-chroot useradd" in content
    assert "--system" in content
    assert "--home-dir" in content
    assert "/var/lib/app" in content


def test_users_pull_in_the_account_tools_package(tmp_path: Path) -> None:
    """``mkosi-chroot useradd`` exits 127 unless the image installs ``passwd``."""
    with_user = _compile(_recipe(Package("systemd"), User("app")), tmp_path / "user")
    assert "passwd" in conf_list(_conf(with_user), "Packages")

    without = _compile(_recipe(Package("systemd")), tmp_path / "none")
    assert "passwd" not in _conf(without)


def test_compile_generates_debloat_finalize(tmp_path: Path) -> None:
    recipe = _recipe(Debloat(enabled=True, extra_remove=("/usr/share/fonts",)))

    output_dir = _compile(recipe, tmp_path / "mkosi")

    content = _script(output_dir, "finalize").read_text(encoding="utf-8")
    assert "rm -rf" in content
    assert "/usr/share/doc" in content
    assert "/usr/share/fonts" in content

    # Systemd binary cleanup is in postinst (via dpkg-query), not finalize
    postinst_content = _script(output_dir, "postinst").read_text(encoding="utf-8")
    assert "mkosi-chroot dpkg-query -L systemd" in postinst_content
    assert "default.target" in postinst_content


def test_compile_architecture_field(tmp_path: Path) -> None:
    """Architecture is mapped from the Arch type to mkosi's value, in the root mkosi.conf."""
    for py_arch, mkosi_arch in ARCH_TO_MKOSI.items():
        recipe = _recipe(Package("curl"), arch=cast(Arch, py_arch))
        output_dir = _compile(recipe, tmp_path / f"mkosi-{py_arch}")
        assert f"\nArchitecture={mkosi_arch}\n" in _main(output_dir)
        assert "Architecture=" not in _conf(output_dir)


def test_compile_with_network_configurable(tmp_path: Path) -> None:
    """WithNetwork= (a main-image setting) follows the Setting, off by default."""
    for with_net, expected in [("true", "WithNetwork=yes"), ("false", "WithNetwork=no")]:
        recipe = _recipe(Package("curl"), Setting("Build", "WithNetwork", (with_net,)))
        output_dir = _compile(recipe, tmp_path / f"mkosi-net-{with_net}")
        assert f"\n{expected}\n" in _main(output_dir)
    on = _compile(_recipe(Package("curl"), mkosi=Mkosi(build_network=True)), tmp_path / "on")
    assert "\nWithNetwork=yes\n" in _main(on)
    off = _compile(_recipe(Package("curl")), tmp_path / "off")
    assert "\nWithNetwork=no\n" in _main(off)


def test_compile_not_bootable_writes_a_disk_without_uki(tmp_path: Path) -> None:
    """Bootable=no swaps the UKI for a plain disk payload, with no disk around it."""
    off = Setting("Content", "Bootable", ("no",))
    output = _compile(_recipe(Package("curl"), off), tmp_path / "off")
    conf_text = _conf(output)
    assert "Format=disk" in conf_text.splitlines()
    assert "Bootable=no" in conf_text.splitlines()
    assert "Format=uki" not in conf_text
    assert "Bootable=yes" not in conf_text
    main = _main(output)
    assert "\nFormat=none\n" in main
    assert "ExtraTrees=" not in main
    assert not (output / "mkosi.repart").exists()
    with_kernel = _compile(_recipe(_kernel("6.1.2"), off), tmp_path / "kernel")
    assert "Bootable=yes" not in _conf(with_kernel)
    assert "Bootable=no" in _conf(with_kernel).splitlines()
    plain_out = _compile(_recipe(Package("curl")), tmp_path / "plain")
    plain = _conf(plain_out)
    assert "Format=uki" in plain
    assert "Bootable" not in plain
    assert "\nFormat=disk\n" in _main(plain_out)
    assert (plain_out / "mkosi.repart" / "00-esp.conf").is_file()
    with pytest.raises(ValidationError, match="writes Bootable= itself"):
        compile(_recipe(Package("curl"), Setting("Content", "Bootable", ("yes",))))


def test_not_bootable_emits_no_cloud_wrappers(tmp_path: Path) -> None:
    off = Setting("Content", "Bootable", ("no",))
    recipe = _recipe(
        Package("curl"),
        off,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",)),
        ),
    )
    output = _compile(recipe, tmp_path / "mkosi")
    assert sorted(p.name for p in (output / "mkosi.images").iterdir()) == ["payload"]


def test_compile_service_enablement_uses_mkosi_chroot(tmp_path: Path) -> None:
    """Service enablement uses mkosi-chroot systemctl enable."""
    recipe = _recipe(Service("myapp", "/usr/bin/myapp"))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    content = _script(output_dir, "postinst").read_text(encoding="utf-8")
    assert "mkosi-chroot systemctl enable myapp.service" in content


def test_compile_debloat_uses_dpkg_query(tmp_path: Path) -> None:
    """Debloat uses mkosi-chroot dpkg-query for binary and unit enumeration."""
    output_dir = _compile(_recipe(Debloat(enabled=True)), tmp_path / "mkosi")
    content = _script(output_dir, "postinst").read_text(encoding="utf-8")

    # Binary cleanup via dpkg-query
    assert "mkosi-chroot dpkg-query -L systemd | grep -E '^/usr/bin/'" in content
    # Unit masking via dpkg-query: paths and automounts too, and the triggers of masked units
    assert "grep -E '\\.(service|socket|timer|target|mount|path|automount)$'" in content
    assert "grep -E '/systemd/system/[^/]+\\.(socket|timer|path|automount)$'" in content


def test_compile_default_target(tmp_path: Path) -> None:
    """Default systemd target is set to minimal.target when debloat is enabled."""
    output_dir = _compile(_recipe(Debloat(enabled=True)), tmp_path / "mkosi")
    content = _script(output_dir, "postinst").read_text(encoding="utf-8")
    assert 'ln -sf minimal.target "$BUILDROOT/etc/systemd/system/default.target"' in content
    minimal = output_dir / "payload/mkosi.skeleton/etc/systemd/system/minimal.target"
    assert minimal.is_file()


def test_compile_skeleton_file_keeps_its_mode(tmp_path: Path) -> None:
    """A skeleton file (a custom /init) lands in payload/mkosi.skeleton with its mode."""
    recipe = _recipe(File("/init", "#!/bin/sh\nexec /sbin/init\n", mode=0o755, stage="skeleton"))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    init_path = output_dir / "payload" / "mkosi.skeleton" / "init"
    assert init_path.read_text(encoding="utf-8") == "#!/bin/sh\nexec /sbin/init\n"
    assert init_path.stat().st_mode & 0o777 == 0o755


def test_compile_version_script(tmp_path: Path) -> None:
    """mkosi.version is emitted at the emission root when enabled."""
    recipe = _recipe(Package("curl"), mkosi=Mkosi(version_script=True))

    output_dir = _compile(recipe, tmp_path / "mkosi")
    version_path = output_dir / "mkosi.version"
    assert version_path.exists()
    content = version_path.read_text(encoding="utf-8")
    assert "git rev-parse --short=6 HEAD" in content
    assert version_path.stat().st_mode & 0o755 == 0o755


@pytest.mark.parametrize(
    ("target", "name", "converter"),
    [("gcp", "tar", GCP_CONVERTER), ("azure", "vhd", AZURE_CONVERTER)],
)
def test_compile_cloud_wrapper(tmp_path: Path, target: str, name: str, converter: str) -> None:
    """A cloud target is a wrapper subimage: the payload in a disk, converted at postoutput."""
    recipe = _recipe(
        Package("curl"),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(target, outputs=(cast(Literal["azure", "gcp"], target),)),
        ),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    wrapper = output_dir / "mkosi.images" / target
    script = wrapper / "mkosi.postoutput.d" / name
    assert script.read_text(encoding="utf-8") == converter
    assert script.stat().st_mode & 0o755 == 0o755
    conf = (wrapper / "mkosi.conf").read_text(encoding="utf-8")
    assert "Dependencies=payload\n" in conf
    assert f"Format=disk\nOutput={target}\n" in conf
    assert "ExtraTrees=%O/payload.efi:/efi/EFI/BOOT/BOOTX64.EFI\n" in conf
    assert (wrapper / "mkosi.repart" / "00-esp.conf").is_file()
    # A variant that only adds outputs gets no profile overlay
    assert not (output_dir / "payload" / "mkosi.profiles").exists()


def test_compile_overlay_variant_is_a_profile(tmp_path: Path) -> None:
    """A variant adding content is payload/mkosi.profiles/<name>/ with only its additions."""
    recipe = _recipe(
        Package("curl"),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("prod", add=Fragment("prod", items=(Package("nginx"),))),
        ),
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")

    profile = output_dir / "payload" / "mkosi.profiles" / "prod" / "mkosi.conf"
    assert profile.read_text(encoding="utf-8") == (
        "[Output]\nImageId=%i-prod\n\n[Content]\nPackages=\n    nginx\n"
    )
    assert "nginx" not in _conf(output_dir)
    assert conf_list(_conf(output_dir, "prod"), "Packages") == ["curl", "nginx"]


def test_compile_environment_key_value(tmp_path: Path) -> None:
    """Environment=KEY=VALUE pairs are emitted in the payload's [Build] section."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("MY_VAR=hello", "OTHER=world")),
        epoch=None,
    )

    conf_text = _conf(_compile(recipe, tmp_path / "mkosi"))

    assert "[Build]\nEnvironment=MY_VAR=hello\nEnvironment=OTHER=world\n" in conf_text


@pytest.mark.parametrize(
    "values", [("KERNEL_IMAGE", "KERNEL_VERSION"), ("SOURCE_DATE_EPOCH=0", "KERNEL_IMAGE")]
)
def test_compile_refuses_environment_passthrough(values: tuple[str, ...]) -> None:
    """Environment=KEY (the host's value) never enters the build."""
    recipe = _recipe(Package("curl"), Setting("Build", "Environment", values), epoch=None)
    with pytest.raises(ValidationError, match="mkosi-environment-passthrough"):
        compile(recipe)


def test_compile_user_epoch_variable_does_not_change_the_source_date(tmp_path: Path) -> None:
    """A user SOURCE_DATE_EPOCH reaches the payload's scripts; SourceDateEpoch= is the
    platform's."""
    recipe = _recipe(
        Package("curl"),
        Setting("Build", "Environment", ("SOURCE_DATE_EPOCH=1234",)),
        epoch=0,
    )

    out = _compile(recipe, tmp_path / "mkosi")

    assert "Environment=SOURCE_DATE_EPOCH=1234" in _conf(out)
    assert "\nSourceDateEpoch=0\n" in _main(out)


@pytest.mark.parametrize(("epoch", "written"), [(1700000000, "1700000000"), (0, "0"), (None, "0")])
def test_compile_epoch_sets_source_date_epoch_consistently(
    tmp_path: Path, epoch: int | None, written: str
) -> None:
    """SourceDateEpoch= is always written, once, in the root mkosi.conf (mkosi exports it to
    every script): the platform's epoch, 0 without one."""
    out = _compile(_recipe(Package("curl"), epoch=epoch), tmp_path / "mkosi")

    found = [
        line
        for path in sorted(out.rglob("*.conf"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if "SOURCE_DATE_EPOCH" in line or line.startswith("SourceDateEpoch=")
    ]
    assert found == [f"SourceDateEpoch={written}"]
    assert f"\nSourceDateEpoch={written}\n" in _main(out)


def test_compile_kernel_with_config_emits_build_script(tmp_path: Path) -> None:
    """When kernel has config_file, a build script copies the fetched checkout and builds it."""
    config_file = tmp_path / "kernel-yocto.config"
    config_file.write_text("# CONFIG_LOCALVERSION is not set\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    build_script = _script(output_dir, "build", "10-kernel.sh")

    assert build_script.exists()
    script_text = build_script.read_text(encoding="utf-8")

    assert 'KERNEL_VERSION="6.13.12"' in script_text
    # Unpinned: the script fails naming lock and fetch; it never clones in the sandbox
    assert "kernel 6.13.12 is not pinned: run tundravm lock, then tundravm fetch" in script_text
    assert "git clone" not in script_text
    assert 'cp payload/kernel/kernel.config "$KERNEL_CACHE/src/.config"' in script_text
    assert "make olddefconfig" in script_text
    assert "make -j" in script_text
    assert "bzImage ARCH=x86_64" in script_text
    assert "KBUILD_BUILD_TIMESTAMP" in script_text
    assert "KBUILD_BUILD_USER" in script_text
    assert "KBUILD_BUILD_HOST" in script_text
    assert "${DESTDIR}/usr/lib/modules/" in script_text
    assert "vmlinuz" in script_text


def test_compile_kernel_config_file_copied(tmp_path: Path) -> None:
    """Kernel config file is copied into the payload's kernel/ directory."""
    config_file = tmp_path / "my.config"
    config_file.write_text("CONFIG_TDX_GUEST=y\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    kernel_config = output_dir / "payload" / "kernel" / "kernel.config"

    assert kernel_config.exists()
    assert "CONFIG_TDX_GUEST=y" in kernel_config.read_text(encoding="utf-8")


def test_compile_kernel_passes_no_host_environment(tmp_path: Path) -> None:
    """The kernel build passes nothing through from the host (no Environment=KERNEL_*)."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# kernel config\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    out = _compile(recipe, tmp_path / "mkosi")
    assert "Environment=" not in _conf(out)
    assert "Environment=" not in _main(out)


def test_compile_kernel_without_config_no_build_script(tmp_path: Path) -> None:
    """When kernel does not have config_file, no kernel build script is emitted."""
    recipe = _recipe(Package("curl"), _kernel("6.8"), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")

    assert not (output_dir / "payload" / "mkosi.build.d").exists()
    assert not (output_dir / "payload" / "kernel").exists()
    assert "Bootable=yes" in _conf(output_dir).splitlines()


def test_compile_kernel_build_script_with_user_hooks(tmp_path: Path) -> None:
    """The kernel build and the user's build hooks are two scripts, the kernel's first."""
    config_file = tmp_path / "my.config"
    config_file.write_text("# config\n", encoding="utf-8")

    recipe = _recipe(
        Package("curl"),
        _kernel("6.13.12", config_file),
        Hook("custom", "build", "echo custom-build-step"),
        epoch=None,
    )

    output_dir = _compile(recipe, tmp_path / "mkosi")
    scripts = sorted(p.name for p in (output_dir / "payload" / "mkosi.build.d").iterdir())
    assert scripts == ["10-kernel.sh", "50-build.sh"]
    assert "make olddefconfig" in _script(output_dir, "build", "10-kernel.sh").read_text()
    assert "custom-build-step" in _script(output_dir, "build").read_text()


def test_compile_kernel_custom_source_repo() -> None:
    """The kernel source (locked, fetched on the host) is the custom repository."""
    recipe = _recipe(
        Package("curl"),
        _kernel("6.13.12", Path("unused.config"), repo="https://github.com/custom/linux"),
        epoch=None,
    )

    source = lower(recipe).kernel_source("default")
    assert source is not None
    assert source.name == "kernel"
    assert source.source.url == "https://github.com/custom/linux"


def test_compile_efi_stub_postinst_hook(tmp_path: Path) -> None:
    """EfiStub() declares a postinst hook that downloads and installs the pinned EFI stub."""
    stub = EfiStub(
        snapshot="https://snapshot.debian.org/archive/debian/20251113T083151Z",
        version="255.4-1",
    )
    recipe = _recipe(Package("systemd"), stub)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    content = _script(output_dir, "postinst").read_text(encoding="utf-8")

    assert stub.render_script() in content
    assert "https://snapshot.debian.org/archive/debian/20251113T083151Z" in content
    assert "255.4-1" in content
    assert "systemd-boot-efi" in content
    assert "dpkg -i" in content
    # The package's own stub stays: nothing is copied over linuxx64.efi.stub
    assert "/usr/lib/systemd/boot/efi" not in content


def test_compile_efi_stub_registered_in_postinst_phase() -> None:
    """EfiStub() lowers to a hook in the postinst phase running its render_script()."""
    stub = EfiStub(snapshot="https://snapshot.example.com", version="255.4-1")

    scripts = _phase_scripts(_recipe(stub), "postinst")
    assert scripts[-1] == stub.render_script()
    assert "snapshot.example.com" in scripts[-1]
    assert "255.4-1" in scripts[-1]


def test_compile_strip_image_version_finalize_hook(tmp_path: Path) -> None:
    """A reproducible recipe strips IMAGE_VERSION in a finalize hook."""
    output_dir = _compile(_recipe(Package("systemd")), tmp_path / "mkosi")
    content = _script(output_dir, "finalize").read_text(encoding="utf-8")

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


def test_backports_composes_no_declarations() -> None:
    """Backports() is a marker: lowering writes its sandbox files, it declares nothing."""
    assert Backports().items == ()
    assert Backports(include_sid=False, release="trixie").items == ()


def test_compile_backports_writes_static_sandbox_sources(tmp_path: Path) -> None:
    """Backports() is a compile-time root mkosi.sandbox file, with no hook or SandboxTrees."""
    recipe = _recipe(Package("systemd"), Backports(), epoch=None)
    recipe = on_platform(recipe, snapshot="20251113T083151Z")

    output_dir = _compile(recipe, tmp_path / "mkosi")

    sources = output_dir / "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
    stanza = (
        "Types: deb deb-src\n"
        "URIs: https://snapshot.debian.org/archive/debian/20251113T083151Z\n"
        "Suites: {}\n"
        "Components: main\n"
        "Enabled: yes\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    )
    assert sources.read_text() == stanza.format("bookworm-backports") + "\n" + stanza.format("sid")
    pins = output_dir / "mkosi.sandbox/etc/apt/preferences.d/debian-backports.pref"
    assert pins.read_text() == (
        "Package: *\nPin: release n=bookworm-backports\nPin-Priority: 200\n\n"
        "Package: *\nPin: release n=sid\nPin-Priority: 100\n"
    )
    assert not (output_dir / "payload" / "mkosi.sync.d").exists()
    assert not (output_dir / "payload" / "mkosi.sandbox").exists()
    for text in (_conf(output_dir), _main(output_dir)):
        assert "SandboxTrees=" not in text and "mkosi.builddir" not in text
    assert lower(recipe).mkosi.sandbox_trees == ()


def test_compile_backports_sandbox_sources_take_the_fragments_fields() -> None:
    """Backports' mirror/release win; without either, deb.debian.org and the base."""
    pinned = lower(_recipe(Backports(archive_url="http://m", release="trixie"), epoch=None))
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
    no_sid = lower(_recipe(Backports(include_sid=False), epoch=None))
    assert "Suites: sid" not in no_sid.mkosi.sandbox_files[0][1]
    rooted = lower(on_platform(_recipe(Backports(), epoch=None), mirror="https://m.example/"))
    assert "URIs: https://m.example/debian\n" in rooted.mkosi.sandbox_files[0][1]
    snap = on_platform(
        _recipe(Backports(), epoch=None), mirror="https://m.example", snapshot="20250101T000000Z"
    )
    assert (
        "URIs: https://m.example/archive/debian/20250101T000000Z\n"
        in (lower(snap).mkosi.sandbox_files[0][1])
    )


def test_compile_snapshot_writes_the_snapshot_lines(tmp_path: Path) -> None:
    """Debian(snapshot=) is mkosi's Snapshot= and the tools tree's ToolsTreeSnapshot=; the
    mirror stays a root."""
    recipe = on_platform(
        _recipe(Package("systemd")),
        mirror="https://snapshot.debian.org",
        snapshot="20251113T083151Z",
    )
    main = _main(_compile(recipe, tmp_path / "mkosi"))
    assert "Mirror=https://snapshot.debian.org\nSnapshot=20251113T083151Z\n" in main
    assert "\nToolsTreeSnapshot=20251113T083151Z\n" in main


@pytest.mark.parametrize(
    ("field", "url"),
    [
        ("mirror", "https://snapshot.debian.org/archive/debian/20251113T083151Z/"),
        ("mirror", "https://deb.debian.org/debian"),
        ("tools_mirror", "https://deb.debian.org/debian/"),
    ],
)
def test_compile_rejects_an_archive_url_as_the_mirror(field: str, url: str) -> None:
    """mkosi appends the archive path to Mirror=, so a full archive URL is refused."""
    with pytest.raises(ValidationError, match=f"Debian\\({field}=...\\) .* names an archive"):
        recipe = _recipe(Package("systemd"))
        if field == "mirror":
            lower(on_platform(recipe, mirror=url))
        else:
            lower(on_platform(recipe, tools_mirror=url))


def test_compile_efi_stub_script_installs_from_the_image_root() -> None:
    """The .deb goes to $BUILDROOT/ (mkosi-chroot has its own /tmp); IDs expand."""
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
    """A Repository goes to the root mkosi.sandbox (with its declared keyring) and, unless
    ``in_image=False``, to the payload's skeleton."""
    keyring = "/etc/apt/keyrings/extra.asc"
    recipe = _recipe(
        Repository("extra", "https://repo.example/debian", "stable", keyring=keyring, priority=10),
        Repository("tools", "https://tools.example/apt", "stable", in_image=False),
        File(keyring, "KEY\n"),
    )
    out = _compile(recipe, tmp_path / "mkosi")
    sandbox = out / "mkosi.sandbox/etc/apt"
    skeleton = out / "payload/mkosi.skeleton/etc/apt"
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


def test_compile_overlay_repository_is_only_in_its_profiles_sandbox(tmp_path: Path) -> None:
    """A variant's own repository reaches apt only when that profile is selected."""
    recipe = _recipe(
        Package("systemd"),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "extra",
                add=Fragment(
                    "extra-repo",
                    items=(Repository("extra", "https://repo.example/debian", "stable"),),
                ),
            ),
        ),
        epoch=None,
    )
    out = _compile(recipe, tmp_path / "mkosi")
    path = "mkosi.sandbox/etc/apt/sources.list.d/extra.sources"
    assert (out / "mkosi.profiles" / "extra" / path).is_file()
    assert not (out / path).exists()


def test_compile_backports_only_in_the_variant_that_adds_it(tmp_path: Path) -> None:
    """A variant adding Backports gets the sandbox file; the default build does not."""
    recipe = _recipe(
        Package("systemd"),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("bp", inherits="default", add=Backports()),
        ),
        epoch=None,
    )
    output_dir = _compile(recipe, tmp_path / "mkosi")
    path = "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
    assert (output_dir / "mkosi.profiles" / "bp" / path).is_file()
    assert not (output_dir / path).exists()


def _devtools_debloat_recipe() -> Recipe:
    return _recipe(
        Debloat(
            enabled=True,
            keep_paths_by_variant={"devtools": ("/usr/share/bash-completion",)},
        ),
        variants=(Variant("default", outputs=("qemu",)), Variant("devtools")),
        epoch=None,
    )


def test_compile_debloat_profile_conditional_paths(tmp_path: Path) -> None:
    """Variant-conditional debloat emits a word-matching $PROFILES guard in finalize."""
    output_dir = _compile(_devtools_debloat_recipe(), tmp_path / "mkosi", variants=("default",))
    content = _script(output_dir, "finalize").read_text(encoding="utf-8")

    unconditional_section = content.split("# Debloat: profile-conditional")[0]
    assert "/usr/share/bash-completion" not in unconditional_section
    assert 'if [[ " ${PROFILES:-} " != *" devtools "* ]]; then' in content
    assert "/usr/share/bash-completion" in content


def test_compile_debloat_profile_conditional_unconditional_coexist(
    tmp_path: Path,
) -> None:
    """Unconditional paths are still removed alongside conditional ones."""
    output_dir = _compile(_devtools_debloat_recipe(), tmp_path / "mkosi", variants=("default",))
    content = _script(output_dir, "finalize").read_text(encoding="utf-8")

    assert 'rm -rf "$BUILDROOT/usr/share/doc"' in content
    assert 'rm -rf "$BUILDROOT/usr/share/bash-completion"' in content


def test_compile_overlay_debloat_makes_the_payloads_stand_aside(tmp_path: Path) -> None:
    """A profile with its own Debloat runs it instead of the payload's: the payload's
    debloat lines are guarded off for that profile."""
    debloat = Debloat(enabled=True)
    recipe = _recipe(
        Package("curl"),
        debloat,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("dev", changes=(debloat.ref.edit(enabled=False),)),
        ),
        epoch=None,
    )
    out = _compile(recipe, tmp_path / "mkosi")
    guard = 'if [[ " ${PROFILES:-} " != *" dev "* ]]; then'
    postinst = _script(out, "postinst").read_text(encoding="utf-8")
    finalize = _script(out, "finalize").read_text(encoding="utf-8")
    for text in (postinst, finalize):
        assert guard in text
    head, _, guarded = postinst.partition(guard)
    assert "dpkg-query" not in head and "dpkg-query" in guarded
    assert 'rm -rf "$BUILDROOT/usr/share/doc"' in finalize.partition(guard)[2]
    dev = out / "payload/mkosi.profiles/dev"
    assert not (dev / "mkosi.postinst.d").exists()
    assert not (dev / "mkosi.finalize.d").exists()


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return snapshot


# ── runtime-init dependencies ───────────────────────────────────────────


def _runtime_init_unit(out: Path, layer: str = "payload", unit: str = "runtime-init-hello") -> str:
    """The unit that orders runtime-init after the network: the ``hello`` step's by default."""
    path = out / layer / f"mkosi.extra/usr/lib/systemd/system/{unit}.service"
    return path.read_text(encoding="utf-8")


HELLO = BootStep("hello", "echo hi")


def test_runtime_init_waits_for_network_online_without_network_setup(tmp_path: Path) -> None:
    out = _compile(_recipe(HELLO), tmp_path / "mkosi")
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
    out = _compile(_recipe(HELLO, declared), tmp_path / "mkosi")
    unit = _runtime_init_unit(out)
    assert "After=network.target network-setup.service\nRequires=network-setup.service\n" in unit
    assert "network-online" not in unit


def test_runtime_init_follows_a_variants_own_network_setup(tmp_path: Path) -> None:
    recipe = _recipe(
        HELLO,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "net",
                inherits="default",
                add=Fragment("net", items=(Unit("network-setup.service", enabled=True),)),
            ),
        ),
    )
    out = _compile(recipe, tmp_path / "mkosi")
    assert "Wants=network-online.target" in _runtime_init_unit(out)
    overlay = _runtime_init_unit(out, "payload/mkosi.profiles/net")
    assert "Requires=network-setup.service" in overlay
