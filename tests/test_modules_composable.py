"""Tests for composable init modules: KeyGeneration, DiskEncryption, SecretDelivery."""

from tdx import Image
from tdx.modules import (
    DiskEncryption,
    Init,
    KeyGeneration,
    SecretDelivery,
)

# ── KeyGeneration ────────────────────────────────────────────────────


def test_key_generation_adds_build_hook() -> None:
    init = Init()
    KeyGeneration(strategy="tpm").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "NethermindEth/nethermind-tdx" in build_script
    assert "go build" in build_script
    assert "$DESTDIR/usr/bin/key-generation" in build_script


def test_key_generation_invokes_binary_in_runtime_init() -> None:
    init = Init()
    KeyGeneration(strategy="tpm", output="/persistent/key").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 1
    script = script_files[0].content
    assert "#!/bin/bash" in script
    assert "set -euo pipefail" in script
    assert "/usr/bin/key-generation --strategy tpm --output /persistent/key" in script


def test_key_generation_random_strategy() -> None:
    init = Init()
    KeyGeneration(strategy="random", output="/tmp/key").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    script = script_files[0].content
    assert "/usr/bin/key-generation --strategy random --output /tmp/key" in script


def test_key_generation_custom_repo_and_branch() -> None:
    init = Init()
    KeyGeneration(
        source_repo="https://github.com/custom/fork",
        source_branch="v2.0",
    ).apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/fork" in build_script
    assert "-b v2.0" in build_script


# ── DiskEncryption ───────────────────────────────────────────────────


def test_disk_encryption_adds_build_hook() -> None:
    init = Init()
    DiskEncryption(device="/dev/vda3").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "$DESTDIR/usr/bin/disk-encryption" in build_script


def test_disk_encryption_invokes_binary_in_runtime_init() -> None:
    init = Init()
    DiskEncryption(
        device="/dev/vdb",
        mapper_name="cryptdata",
        key_path="/persistent/key",
        mount_point="/data",
    ).apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    script = script_files[0].content
    assert "/usr/bin/disk-encryption" in script
    assert "--device /dev/vdb" in script
    assert "--mapper cryptdata" in script
    assert "--key /persistent/key" in script
    assert "--mount /data" in script


def test_disk_encryption_installs_cryptsetup() -> None:
    init = Init()
    DiskEncryption().apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    assert "cryptsetup" in profile.packages


def test_disk_encryption_custom_repo() -> None:
    init = Init()
    DiskEncryption(
        source_repo="https://github.com/custom/disk",
        source_branch="v3",
    ).apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/disk" in build_script
    assert "-b v3" in build_script


# ── SecretDelivery ───────────────────────────────────────────────────


def test_secret_delivery_adds_build_hook() -> None:
    init = Init()
    SecretDelivery(method="http_post").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "$DESTDIR/usr/bin/secret-delivery" in build_script


def test_secret_delivery_invokes_binary_in_runtime_init() -> None:
    init = Init()
    SecretDelivery(method="http_post", port=9090).apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    script = script_files[0].content
    assert "/usr/bin/secret-delivery --method http_post --port 9090" in script


def test_secret_delivery_installs_python3() -> None:
    init = Init()
    SecretDelivery().apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    assert "python3" in profile.packages


# ── Composition ──────────────────────────────────────────────────────


def test_multiple_modules_compose_into_single_runtime_init() -> None:
    init = Init()
    KeyGeneration(strategy="tpm").apply(init)
    DiskEncryption(device="/dev/vda3").apply(init)
    SecretDelivery(method="http_post").apply(init)

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]

    # All three build hooks
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 3

    # Single runtime-init script with all three binary calls
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 1
    script = script_files[0].content
    assert "/usr/bin/key-generation" in script
    assert "/usr/bin/disk-encryption" in script
    assert "/usr/bin/secret-delivery" in script

    # Service unit
    svc_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/runtime-init.service"
    ]
    assert len(svc_files) == 1
    svc = svc_files[0].content
    assert "Type=oneshot" in svc
    assert "ExecStart=/usr/bin/runtime-init" in svc
    assert "RemainAfterExit=yes" in svc

    # Runtime packages from sub-modules
    assert "cryptsetup" in profile.packages
    assert "python3" in profile.packages

    # Build packages from sub-modules
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages


def test_init_without_bash_blocks_does_not_generate_runtime_init() -> None:
    init = Init()

    image = Image(reproducible=False)
    init.apply(image)

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 0
