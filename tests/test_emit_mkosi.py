from pathlib import Path
from typing import cast

import pytest

from tdx import Image
from tdx.compiler.emit_mkosi import ARCH_TO_MKOSI
from tdx.errors import ValidationError
from tdx.models import Phase


def test_compile_golden_output(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("jq", "curl")
    image.run("echo", "prep", phase="prepare", env={"B": "2", "A": "1"}, cwd="/work")
    image.run("echo", "build", phase="build")

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
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        "(cd /work && A=1 B=2 echo prep)\n"
    )
    assert build_script.read_text(encoding="utf-8") == (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        "echo build\n"
    )


def test_compile_is_deterministic(tmp_path: Path) -> None:
    image = Image(base="debian/bookworm")
    image.install("curl")
    image.run("echo", "hello", phase="prepare")

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
        output_dir / "default" / "mkosi.extra"
        / "usr" / "lib" / "systemd" / "system" / "app.service"
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


def _snapshot_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snapshot[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
    return snapshot
