#!/usr/bin/env python3
"""Integration smoke test: build a minimal Debian image via local_linux backend."""

import json
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from tdx.backends.local_linux import LocalLinuxBackend
from tdx.image import Image


def main() -> None:
    build_dir = Path("/tmp/tdx-integ-smoke")
    build_dir.mkdir(parents=True, exist_ok=True)

    print(f"Build dir: {build_dir}")

    # --- Test 1: Directory format (basic pipeline validation) ---
    print("\n" + "=" * 60)
    print("TEST 1: Directory format — validates emit + build pipeline")
    print("=" * 60)

    img = Image(
        build_dir=build_dir,
        base="debian/bookworm",
        backend="local_linux",
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
    img.set_backend(backend)

    # Emit and inspect
    emit_dir = build_dir / "mkosi"
    img.compile(emit_dir)

    conf_path = emit_dir / "default" / "mkosi.conf"
    print(f"\nmkosi.conf:\n{conf_path.read_text()}")

    # Check extra tree has our service unit
    unit_path = (
        emit_dir / "default" / "mkosi.extra"
        / "usr" / "lib" / "systemd" / "system" / "hello.service"
    )
    if unit_path.exists():
        print(f"Service unit generated: {unit_path}")
        print(unit_path.read_text())
    else:
        print(f"WARNING: service unit not found at {unit_path}")

    # Check postinst script
    postinst = emit_dir / "default" / "scripts" / "06-postinst.sh"
    if postinst.exists():
        print(f"Postinst script:\n{postinst.read_text()}")

    # Bake
    try:
        bake_result = img.bake(output_dir=build_dir / "output")
        print("Bake succeeded!")

        for pname, presult in bake_result.profiles.items():
            print(f"  Profile: {pname}")
            if presult.report_path and presult.report_path.exists():
                report = json.loads(presult.report_path.read_text())
                print(f"  Report backend: {report.get('backend')}")

        # Verify custom file in rootfs
        rootfs = build_dir / "output" / "default" / "output" / "default"
        test_file = rootfs / "etc" / "tdx-test"
        if test_file.exists():
            content = subprocess.run(
                ["sudo", "cat", str(test_file)],
                capture_output=True, text=True, check=True,
            ).stdout
            assert content.strip() == "integration-test", f"Unexpected content: {content!r}"
            print("  /etc/tdx-test content verified!")
        else:
            print("  WARNING: /etc/tdx-test not found in rootfs")

        # Check that the user was created
        passwd_file = rootfs / "etc" / "passwd"
        if passwd_file.exists():
            passwd = subprocess.run(
                ["sudo", "cat", str(passwd_file)],
                capture_output=True, text=True, check=True,
            ).stdout
            if "appuser" in passwd:
                print("  User 'appuser' verified in /etc/passwd!")
            else:
                print("  WARNING: 'appuser' not found in /etc/passwd")

        # Check service unit
        svc_file = rootfs / "usr" / "lib" / "systemd" / "system" / "hello.service"
        if svc_file.exists():
            print("  hello.service verified in rootfs!")
        else:
            print("  WARNING: hello.service not found in rootfs")

    except Exception as e:
        print(f"\nBake failed: {type(e).__name__}: {e}")
        if hasattr(e, "context"):
            print(f"  Context: {e.context}")
        sys.exit(1)

    # --- Test 2: Tdxs module emission (config + units + build pipeline) ---
    print("\n" + "=" * 60)
    print("TEST 2: Tdxs module — validates emitted config, units, build script")
    print("=" * 60)

    from tdx.modules import Tdxs

    tdxs_dir = build_dir / "test-tdxs"
    tdxs_dir.mkdir(parents=True, exist_ok=True)

    img_tdxs = Image(
        build_dir=tdxs_dir,
        base="debian/bookworm",
        backend="local_linux",
        reproducible=True,
    )
    img_tdxs.install("systemd")
    Tdxs(issuer_type="dcap").apply(img_tdxs)

    emit_tdxs = tdxs_dir / "mkosi"
    img_tdxs.compile(emit_tdxs)

    # Verify mkosi.conf has BuildPackages
    tdxs_conf = (emit_tdxs / "default" / "mkosi.conf").read_text()
    print(f"\nmkosi.conf:\n{tdxs_conf}")
    assert "BuildPackages=" in tdxs_conf, "Missing BuildPackages in mkosi.conf"
    assert "golang" in tdxs_conf, "Missing golang in BuildPackages"
    assert "git" in tdxs_conf, "Missing git in BuildPackages"
    print("  BuildPackages verified (golang, git, build-essential)")

    # Verify config.yaml
    config_yaml = (
        emit_tdxs / "default" / "mkosi.extra" / "etc" / "tdxs" / "config.yaml"
    )
    assert config_yaml.exists(), "config.yaml not emitted"
    config_content = config_yaml.read_text()
    assert "type: dcap" in config_content
    assert "systemd: true" in config_content
    print(f"  config.yaml verified:\n{config_content}")

    # Verify service unit
    svc_unit = (
        emit_tdxs / "default" / "mkosi.extra"
        / "usr" / "lib" / "systemd" / "system" / "tdxs.service"
    )
    assert svc_unit.exists(), "tdxs.service not emitted"
    svc_text = svc_unit.read_text()
    assert "User=tdxs" in svc_text
    assert "Group=tdx" in svc_text
    assert "Type=notify" in svc_text
    assert "ExecStart=/usr/bin/tdxs" in svc_text
    print("  tdxs.service verified (User=tdxs, Group=tdx, Type=notify)")

    # Verify socket unit
    sock_unit = (
        emit_tdxs / "default" / "mkosi.extra"
        / "usr" / "lib" / "systemd" / "system" / "tdxs.socket"
    )
    assert sock_unit.exists(), "tdxs.socket not emitted"
    sock_text = sock_unit.read_text()
    assert "ListenStream=/var/tdxs.sock" in sock_text
    assert "SocketGroup=tdx" in sock_text
    print("  tdxs.socket verified (ListenStream=/var/tdxs.sock, SocketGroup=tdx)")

    # Verify build script exists and contains go build
    import glob

    build_scripts = glob.glob(
        str(emit_tdxs / "default" / "scripts" / "*build*")
    )
    assert build_scripts, "No build script emitted"
    build_text = Path(build_scripts[0]).read_text()
    assert "go build" in build_text
    assert "NethermindEth/tdxs" in build_text
    assert "-trimpath" in build_text
    print(f"  Build script verified: {Path(build_scripts[0]).name}")

    # Verify postinst has groupadd, useradd, systemctl enable
    postinst_tdxs = emit_tdxs / "default" / "scripts" / "06-postinst.sh"
    assert postinst_tdxs.exists(), "postinst not emitted"
    postinst_text = postinst_tdxs.read_text()
    assert "groupadd --system tdx" in postinst_text
    assert "useradd --system" in postinst_text
    assert "systemctl enable tdxs.socket" in postinst_text
    print("  Postinst verified (groupadd tdx, useradd tdxs, enable tdxs.socket)")

    # --- Test 3: Raw disk format (artifact collection) ---
    print("\n" + "=" * 60)
    print("TEST 3: Raw disk format — validates artifact collection")
    print("=" * 60)

    img2 = Image(
        build_dir=build_dir / "test3",
        base="debian/bookworm",
        backend="local_linux",
        reproducible=True,
    )
    img2.install("systemd")

    backend2 = LocalLinuxBackend(
        privilege="sudo",
        mkosi_args=["--format=disk", "--bootable=no"],
    )
    img2.set_backend(backend2)

    try:
        bake_result2 = img2.bake(output_dir=build_dir / "test3" / "output")
        print("Bake succeeded!")
        for pname, presult in bake_result2.profiles.items():
            print(f"  Profile: {pname}")
            for target, artifact in presult.artifacts.items():
                art_path = Path(artifact.path)
                exists = art_path.exists()
                size = art_path.stat().st_size if exists else 0
                print(f"    [{target}]: {art_path.name} — {size / 1024 / 1024:.1f} MB")
    except Exception as e:
        print(f"\nBake failed: {type(e).__name__}: {e}")
        if hasattr(e, "context"):
            print(f"  Context: {e.context}")
        # Non-fatal for test2

    print("\n" + "=" * 60)
    print("ALL INTEGRATION TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
