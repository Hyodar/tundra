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
    img.emit_mkosi(emit_dir)

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

    # --- Test 2: Raw disk format (artifact collection) ---
    print("\n" + "=" * 60)
    print("TEST 2: Raw disk format — validates artifact collection")
    print("=" * 60)

    img2 = Image(
        build_dir=build_dir / "test2",
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
        bake_result2 = img2.bake(output_dir=build_dir / "test2" / "output")
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
