"""Tests for composable init modules: KeyGeneration, DiskEncryption, SecretDelivery."""

from tdx import Image
from tdx.modules import (
    DiskEncryption,
    KeyGeneration,
    SecretDelivery,
)

# ── KeyGeneration ────────────────────────────────────────────────────


def test_key_generation_adds_build_hook() -> None:
    image = Image(reproducible=False)
    KeyGeneration(strategy="tpm").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "NethermindEth/nethermind-tdx" in build_script
    assert "go build" in build_script
    assert "$DESTDIR/usr/bin/key-generation" in build_script


def test_key_generation_registers_init_script() -> None:
    image = Image(reproducible=False)
    KeyGeneration(strategy="tpm", output="/persistent/key").apply(image)

    profile = image.state.profiles["default"]
    assert len(profile.init_scripts) == 1
    entry = profile.init_scripts[0]
    assert "/usr/bin/key-generation --strategy tpm --output /persistent/key" in entry.script
    assert entry.priority == 10


def test_key_generation_random_strategy() -> None:
    image = Image(reproducible=False)
    KeyGeneration(strategy="random", output="/tmp/key").apply(image)

    profile = image.state.profiles["default"]
    entry = profile.init_scripts[0]
    assert "/usr/bin/key-generation --strategy random --output /tmp/key" in entry.script


def test_key_generation_custom_repo_and_branch() -> None:
    image = Image(reproducible=False)
    KeyGeneration(
        source_repo="https://github.com/custom/fork",
        source_branch="v2.0",
    ).apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/fork" in build_script
    assert "-b v2.0" in build_script


# ── DiskEncryption ───────────────────────────────────────────────────


def test_disk_encryption_adds_build_hook() -> None:
    image = Image(reproducible=False)
    DiskEncryption(device="/dev/vda3").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "$DESTDIR/usr/bin/disk-encryption" in build_script


def test_disk_encryption_registers_init_script() -> None:
    image = Image(reproducible=False)
    DiskEncryption(
        device="/dev/vdb",
        mapper_name="cryptdata",
        key_path="/persistent/key",
        mount_point="/data",
    ).apply(image)

    profile = image.state.profiles["default"]
    assert len(profile.init_scripts) == 1
    entry = profile.init_scripts[0]
    assert "/usr/bin/disk-encryption" in entry.script
    assert "--device /dev/vdb" in entry.script
    assert "--mapper cryptdata" in entry.script
    assert "--key /persistent/key" in entry.script
    assert "--mount /data" in entry.script
    assert entry.priority == 20


def test_disk_encryption_installs_cryptsetup() -> None:
    image = Image(reproducible=False)
    DiskEncryption().apply(image)

    profile = image.state.profiles["default"]
    assert "cryptsetup" in profile.packages


def test_disk_encryption_custom_repo() -> None:
    image = Image(reproducible=False)
    DiskEncryption(
        source_repo="https://github.com/custom/disk",
        source_branch="v3",
    ).apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[-1]
    assert "custom/disk" in build_script
    assert "-b v3" in build_script


# ── SecretDelivery ───────────────────────────────────────────────────


def test_secret_delivery_adds_build_hook() -> None:
    image = Image(reproducible=False)
    SecretDelivery(method="http_post").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[-1]
    assert "git clone" in build_script
    assert "$DESTDIR/usr/bin/secret-delivery" in build_script


def test_secret_delivery_registers_init_script() -> None:
    image = Image(reproducible=False)
    SecretDelivery(method="http_post", port=9090).apply(image)

    profile = image.state.profiles["default"]
    assert len(profile.init_scripts) == 1
    entry = profile.init_scripts[0]
    expected = "/usr/bin/secret-delivery --config /etc/tdx/secrets.json"
    assert expected in entry.script
    assert "--method http_post --port 9090" in entry.script
    assert entry.priority == 30


def test_secret_delivery_installs_python3() -> None:
    image = Image(reproducible=False)
    SecretDelivery().apply(image)

    profile = image.state.profiles["default"]
    assert "python3" in profile.packages


def test_secret_delivery_writes_config_from_declared_secrets() -> None:
    import json

    from tdx.models import SecretSchema, SecretTarget

    image = Image(reproducible=False)
    delivery = SecretDelivery(method="http_post", port=9090)
    delivery.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(
            SecretTarget.file("/run/secrets/jwt.hex", owner="app", mode="0440"),
            SecretTarget.env("JWT_SECRET", scope="global"),
        ),
    )
    delivery.secret(
        "api_key",
        required=False,
        targets=(SecretTarget.file("/run/secrets/api-key"),),
    )
    delivery.apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdx/secrets.json"]
    assert len(config_files) == 1
    config = json.loads(config_files[0].content)

    assert config["method"] == "http_post"
    assert config["port"] == 9090
    assert len(config["secrets"]) == 2

    # Sorted by name
    api_key = config["secrets"][0]
    assert api_key["name"] == "api_key"
    assert api_key["required"] is False
    assert api_key["targets"] == [
        {"kind": "file", "location": "/run/secrets/api-key", "mode": "0400"},
    ]

    jwt = config["secrets"][1]
    assert jwt["name"] == "jwt_secret"
    assert jwt["required"] is True
    assert jwt["schema"] == {"kind": "string", "min_length": 64, "max_length": 64}
    assert len(jwt["targets"]) == 2
    assert jwt["targets"][0] == {
        "kind": "file", "location": "/run/secrets/jwt.hex", "mode": "0440", "owner": "app",
    }
    assert jwt["targets"][1] == {"kind": "env", "location": "JWT_SECRET", "scope": "global"}


# ── Composition with Init ────────────────────────────────────────────


def test_init_generates_runtime_init_from_init_scripts() -> None:
    image = Image(reproducible=False)
    KeyGeneration(strategy="tpm").apply(image)
    DiskEncryption(device="/dev/vda3").apply(image)
    SecretDelivery(method="http_post").apply(image)
    image._apply_init()

    profile = image.state.profiles["default"]

    # All three build hooks from modules
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 3

    # Init collected init_scripts and generated runtime-init
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 1
    script = script_files[0].content
    assert "#!/bin/bash" in script
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

    # Runtime packages from modules directly on image
    assert "cryptsetup" in profile.packages
    assert "python3" in profile.packages

    # Build packages from modules directly on image
    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages


def test_init_scripts_sorted_by_priority() -> None:
    image = Image(reproducible=False)
    # Apply in reverse priority order
    SecretDelivery(method="http_post").apply(image)  # priority 30
    KeyGeneration(strategy="tpm").apply(image)  # priority 10
    DiskEncryption(device="/dev/vda3").apply(image)  # priority 20
    image._apply_init()

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    script = script_files[0].content

    # key-generation (10) should appear before disk-encryption (20)
    # which should appear before secret-delivery (30)
    key_pos = script.index("key-generation")
    disk_pos = script.index("disk-encryption")
    secret_pos = script.index("secret-delivery")
    assert key_pos < disk_pos < secret_pos


def test_init_without_init_scripts_does_not_generate_runtime_init() -> None:
    image = Image(reproducible=False)
    image._apply_init()

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 0
