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
from tundravm.compiler.emit_mkosi import (
    ARCH_TO_MKOSI,
    ESP_MKFS_ENVIRONMENT,
)
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


@pytest.mark.parametrize("phase", ["invalid-phase", "boot", "skeleton"])
def test_compile_rejects_a_phase_mkosi_runs_no_scripts_for(tmp_path: Path, phase: str) -> None:
    image = lower(_recipe())
    image.state.profiles["default"].phases[cast(Phase, phase)] = []

    with pytest.raises(ValidationError) as excinfo:
        emit(image, tmp_path / "mkosi")

    assert excinfo.value.code == "E_VALIDATION"


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
        "After=network-online.target tdx-evidence.service\n"
        "Requires=network-online.target tdx-evidence.service\n"
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


def test_users_pull_in_the_account_tools_package(tmp_path: Path) -> None:
    """``mkosi-chroot useradd`` exits 127 unless the image installs ``passwd``."""
    with_user = _compile(_recipe(Package("systemd"), User("app")), tmp_path / "user")
    assert "passwd" in conf_list(_conf(with_user), "Packages")

    without = _compile(_recipe(Package("systemd")), tmp_path / "none")
    assert "passwd" not in _conf(without)


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
    """When kernel has config_file, a build script copies the fetched checkout and builds it,
    the config file is copied into the payload's kernel/ directory, and the build passes
    nothing through from the host (no Environment=KERNEL_*): the QEMU disk's one environment
    variable is repart's ESP format option."""
    config_file = tmp_path / "my.config"
    config_file.write_text("CONFIG_TDX_GUEST=y\n", encoding="utf-8")

    recipe = _recipe(Package("curl"), _kernel("6.13.12", config_file), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")
    build_script = _script(output_dir, "build", "10-kernel.sh")

    assert build_script.exists()
    script_text = build_script.read_text(encoding="utf-8")

    assert 'KERNEL_VERSION="6.13.12"' in script_text
    # Unpinned: the script fails naming lock and fetch; it never clones in the sandbox
    assert "kernel 6.13.12 is not pinned: run tundravm lock, then tundravm build" in script_text
    assert "git clone" not in script_text
    assert 'cp payload/kernel/kernel.config "$KERNEL_CACHE/src/.config"' in script_text
    assert "make olddefconfig" in script_text
    assert 'kmake -j"$(nproc)" bzImage' in script_text
    assert "mkosi-chroot env KBUILD_BUILD_TIMESTAMP=1970-01-01" in script_text
    assert "KBUILD_BUILD_TIMESTAMP" in script_text
    assert "KBUILD_BUILD_USER" in script_text
    assert "KBUILD_BUILD_HOST" in script_text
    assert "${DESTDIR}/usr/lib/modules/" in script_text
    assert "vmlinuz" in script_text

    kernel_config = output_dir / "payload" / "kernel" / "kernel.config"
    assert kernel_config.exists()
    assert "CONFIG_TDX_GUEST=y" in kernel_config.read_text(encoding="utf-8")

    assert "Environment=" not in _conf(output_dir)
    main = _main(output_dir)
    environment = [line for line in main.splitlines() if line.startswith("Environment=")]
    assert environment == [ESP_MKFS_ENVIRONMENT]


def test_compile_kernel_without_config_no_build_script(tmp_path: Path) -> None:
    """When kernel does not have config_file, no kernel build script is emitted."""
    recipe = _recipe(Package("curl"), _kernel("6.8"), epoch=None)

    output_dir = _compile(recipe, tmp_path / "mkosi")

    assert not (output_dir / "payload" / "mkosi.build.d").exists()
    assert not (output_dir / "payload" / "kernel").exists()
    assert "Bootable=yes" in _conf(output_dir).splitlines()


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


@pytest.mark.parametrize(
    ("variant", "sources"),
    [
        (
            Variant(
                "extra",
                add=Fragment(
                    "extra-repo",
                    items=(Repository("extra", "https://repo.example/debian", "stable"),),
                ),
            ),
            "extra.sources",
        ),
        (Variant("bp", inherits="default", add=Backports()), "debian-backports.sources"),
    ],
    ids=["overlay-repository", "backports"],
)
def test_a_variants_sandbox_file_is_only_in_its_profile(
    tmp_path: Path, variant: Variant, sources: str
) -> None:
    """A variant's own repository (a Repository fragment, or Backports) reaches apt only
    when that profile is selected; the default build does not get the sandbox file."""
    recipe = _recipe(
        Package("systemd"),
        variants=(Variant("default", outputs=("qemu",)), variant),
        epoch=None,
    )
    out = _compile(recipe, tmp_path / "mkosi")
    path = f"mkosi.sandbox/etc/apt/sources.list.d/{sources}"
    assert (out / "mkosi.profiles" / variant.name / path).is_file()
    assert not (out / path).exists()


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


def test_a_debloat_one_profile_changes_moves_out_of_the_shared_payload(tmp_path: Path) -> None:
    """A profile that edits the Debloat leaves no debloat both share: the shared payload runs
    none and names no profile, and the default profile's runs from its own overlay, which
    ``[Match] Profiles=`` (no profile selected) includes."""
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
    for script in (out / "payload").rglob("mkosi.*.d/*.sh"):
        text = script.read_text(encoding="utf-8")
        assert "PROFILES" not in text, script
        if "mkosi.profiles/default" not in script.as_posix():
            assert "dpkg-query" not in text and "/usr/share/doc" not in text, script
    own = out / "payload/mkosi.profiles/default"
    assert "dpkg-query" in (own / "mkosi.postinst.d/50-postinst.sh").read_text(encoding="utf-8")
    finalize = (own / "mkosi.finalize.d/50-finalize.sh").read_text(encoding="utf-8")
    assert 'rm -rf "$BUILDROOT/usr/share/doc"' in finalize
    assert (own / "mkosi.skeleton/etc/systemd/system/minimal.target").is_file()
    assert (out / "payload/mkosi.conf.d/99-default.conf").read_text(encoding="utf-8") == (
        "[Match]\nProfiles=\n\n[Include]\nInclude=mkosi.profiles/default\n"
    )
    assert not (own / "mkosi.conf").exists()  # it adds no setting, nor an ImageId
    dev = out / "payload/mkosi.profiles/dev"
    assert not (dev / "mkosi.postinst.d").exists()
    assert not (dev / "mkosi.finalize.d").exists()


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
