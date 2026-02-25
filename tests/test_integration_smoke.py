"""Integration smoke tests: emit + build via local_linux backend.

These tests require mkosi >= 25 and sudo privileges.
Run with: uv run pytest tests/test_integration_smoke.py -m integration
"""

import json
from pathlib import Path

import pytest

from tundravm import Image
from tundravm.backends.local_linux import LocalLinuxBackend
from tundravm.modules import Tdxs

# ── Test 1: Directory format ────────────────────────────────────────


@pytest.mark.integration
def test_directory_format_pipeline(tmp_path: Path) -> None:
    """Emit + bake a minimal Debian image in directory format."""
    img = Image(
        build_dir=tmp_path,
        base="debian/bookworm",
        backend=LocalLinuxBackend(),
        reproducible=True,
    )
    img.install("systemd", "udev")
    img.file("/etc/tdx-test", content="integration-test\n")
    img.service("hello", exec="/bin/true", restart="no", enabled=True)
    img.user("appuser", system=True, shell="/bin/false")

    backend = LocalLinuxBackend(
        privilege="sudo",
        mkosi_args=["--format=directory", "--bootable=no"],
    )
    img.backend = backend

    emit_dir = tmp_path / "mkosi"
    img.compile(emit_dir)

    # Verify emitted service unit
    unit_path = (
        emit_dir
        / "default"
        / "mkosi.extra"
        / "usr"
        / "lib"
        / "systemd"
        / "system"
        / "hello.service"
    )
    assert unit_path.exists(), "hello.service not emitted"

    # Bake
    output_dir = tmp_path / "output"
    bake_result = img.bake(output_dir=output_dir)

    for _pname, presult in bake_result.profiles.items():
        if presult.report_path and presult.report_path.exists():
            report = json.loads(presult.report_path.read_text())
            assert "backend" in report


# ── Test 2: Tdxs module emission ────────────────────────────────────


def test_tdxs_module_emission(tmp_path: Path) -> None:
    """Emit Tdxs module and verify config, units, and build script."""
    img = Image(
        build_dir=tmp_path,
        base="debian/bookworm",
        backend=LocalLinuxBackend(),
        reproducible=True,
    )
    img.install("systemd")
    Tdxs(issuer_type="dcap").apply(img)

    emit_dir = tmp_path / "mkosi"
    img.compile(emit_dir)

    # mkosi.conf has BuildPackages
    conf_text = (emit_dir / "default" / "mkosi.conf").read_text()
    assert "BuildPackages=" in conf_text
    assert "golang" in conf_text
    assert "git" in conf_text

    # config.yaml
    config_yaml = emit_dir / "default" / "mkosi.extra" / "etc" / "tdxs" / "config.yaml"
    assert config_yaml.exists(), "config.yaml not emitted"
    config_content = config_yaml.read_text()
    assert "type: dcap" in config_content
    assert "systemd: true" in config_content

    # Service unit
    svc_unit = (
        emit_dir / "default" / "mkosi.extra" / "usr" / "lib" / "systemd" / "system" / "tdxs.service"
    )
    assert svc_unit.exists(), "tdxs.service not emitted"
    svc_text = svc_unit.read_text()
    assert "User=tdxs" in svc_text
    assert "Group=tdx" in svc_text
    assert "Type=notify" in svc_text
    assert "ExecStart=/usr/bin/tdxs" in svc_text

    # Socket unit
    sock_unit = (
        emit_dir / "default" / "mkosi.extra" / "usr" / "lib" / "systemd" / "system" / "tdxs.socket"
    )
    assert sock_unit.exists(), "tdxs.socket not emitted"
    sock_text = sock_unit.read_text()
    assert "ListenStream=/var/tdxs.sock" in sock_text
    assert "SocketGroup=tdx" in sock_text

    # Build script
    build_scripts = list((emit_dir / "default" / "scripts").glob("*build*"))
    assert build_scripts, "No build script emitted"
    build_text = build_scripts[0].read_text()
    assert "go build" in build_text
    assert "NethermindEth/tdxs" in build_text
    assert "-trimpath" in build_text

    # Postinst
    postinst = emit_dir / "default" / "scripts" / "06-postinst.sh"
    assert postinst.exists(), "postinst not emitted"
    postinst_text = postinst.read_text()
    assert "groupadd --system tdx" in postinst_text
    assert "useradd --system" in postinst_text
    assert "systemctl enable tdxs.socket" in postinst_text


# ── Test 3: Raw disk format ─────────────────────────────────────────


@pytest.mark.integration
def test_raw_disk_format(tmp_path: Path) -> None:
    """Bake a raw disk image and verify artifact collection."""
    img = Image(
        build_dir=tmp_path,
        base="debian/bookworm",
        backend=LocalLinuxBackend(),
        reproducible=True,
    )
    img.install("systemd")

    backend = LocalLinuxBackend(
        privilege="sudo",
        mkosi_args=["--format=disk", "--bootable=no"],
    )
    img.backend = backend

    output_dir = tmp_path / "output"
    bake_result = img.bake(output_dir=output_dir)

    for _pname, presult in bake_result.profiles.items():
        for _target, artifact in presult.artifacts.items():
            art_path = Path(artifact.path)
            assert art_path.exists(), f"Artifact not found: {art_path}"
            assert art_path.stat().st_size > 0, f"Empty artifact: {art_path}"
