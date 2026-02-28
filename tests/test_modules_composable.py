"""Tests for composable init modules: KeyGeneration, DiskEncryption, SecretDelivery."""

from typing import Literal

import pytest

from tundravm import Image
from tundravm.errors import ValidationError
from tundravm.modules import (
    DiskEncryption,
    KeyGeneration,
    SecretDelivery,
)

# ── KeyGeneration ────────────────────────────────────────────────────


def _module_with_key(
    name: str = "key_persistent",
    *,
    strategy: Literal["tpm", "random", "pipe"] = "tpm",
    output: str | None = None,
    size: int = 64,
    pipe_path: str | None = None,
    persist_in_tpm: bool | None = None,
) -> KeyGeneration:
    module = KeyGeneration()
    module.key(
        name,
        strategy=strategy,
        output=output,
        size=size,
        pipe_path=pipe_path,
        persist_in_tpm=persist_in_tpm,
    )
    return module


def _module_with_disk(
    name: str = "disk_persistent",
    *,
    device: str | None = "/dev/vda3",
    mapper_name: str | None = None,
    key_path: str | None = "/persistent/key",
    mount_point: str = "/persistent",
    key_name: str | None = "key_persistent",
    format_policy: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail",
    dirs: tuple[str, ...] = ("ssh", "data", "logs"),
) -> DiskEncryption:
    module = DiskEncryption()
    module.disk(
        name,
        device=device,
        mapper_name=mapper_name,
        key_path=key_path,
        mount_point=mount_point,
        key_name=key_name,
        format_policy=format_policy,
        dirs=dirs,
    )
    return module


def test_key_generation_adds_build_hook() -> None:
    image = Image(reproducible=False)
    _module_with_key(strategy="tpm").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "go build" in build_script
    assert "./cmd/key-gen" in build_script
    assert "$DESTDIR/usr/bin/key-gen" in build_script


def test_key_generation_registers_init_script() -> None:
    image = Image(reproducible=False)
    _module_with_key(strategy="tpm", output="/persistent/key").apply(image)

    profile = image.state.profiles["default"]
    assert len(image.init._scripts) == 1
    entry = image.init._scripts[0]
    assert "/usr/bin/key-gen setup /etc/tdx/key-gen.yaml" in entry.script
    assert entry.priority == 10

    # output_path is in the config file, not the init script
    config_files = [f for f in profile.files if f.path == "/etc/tdx/key-gen.yaml"]
    assert len(config_files) == 1
    assert 'output_path: "/persistent/key"' in config_files[0].content
    assert "tpm: true" in config_files[0].content


def test_key_generation_allows_output_for_non_tpm_keys() -> None:
    image = Image(reproducible=False)
    _module_with_key(strategy="random", output="/tmp/key").apply(image)

    profile = image.state.profiles["default"]
    config = next(f.content for f in profile.files if f.path == "/etc/tdx/key-gen.yaml")
    assert 'output_path: "/tmp/key"' in config
    assert "tpm: false" in config


def test_key_generation_pipe_strategy_renders_pipe_config() -> None:
    image = Image(reproducible=False)
    _module_with_key(
        "rootfs_key",
        strategy="pipe",
        pipe_path="/run/tdx/passphrase",
        persist_in_tpm=True,
    ).apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdx/key-gen.yaml"]
    assert len(config_files) == 1
    content = config_files[0].content
    assert "rootfs_key:" in content
    assert 'strategy: "pipe"' in content
    assert 'pipe_path: "/run/tdx/passphrase"' in content
    assert "tpm: true" in content


def test_key_generation_custom_repo_and_branch() -> None:
    image = Image(reproducible=False)
    module = KeyGeneration(
        source_repo="https://github.com/custom/fork",
        source_branch="v2.0",
    )
    module.key("key_persistent", strategy="tpm")
    module.apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[0]
    assert "custom/fork" in build_script
    assert "-b v2.0" in build_script


def test_key_generation_supports_multiple_keys() -> None:
    image = Image(reproducible=False)
    module = KeyGeneration()
    module.key("root", strategy="tpm", output="/persistent/root.key")
    module.key(
        "data",
        strategy="pipe",
        pipe_path="/run/keys/data.pipe",
        persist_in_tpm=True,
        output="/persistent/data.key",
    )
    module.apply(image)

    profile = image.state.profiles["default"]
    config = next(f.content for f in profile.files if f.path == "/etc/tdx/key-gen.yaml")
    assert "root:" in config
    assert "data:" in config
    assert 'pipe_path: "/run/keys/data.pipe"' in config
    assert 'output_path: "/persistent/root.key"' in config
    assert 'output_path: "/persistent/data.key"' in config

    script = image.init._scripts[0].script
    assert script.count("/usr/bin/key-gen setup /etc/tdx/key-gen.yaml") == 1


# ── DiskEncryption ───────────────────────────────────────────────────


def test_disk_encryption_adds_build_hook() -> None:
    image = Image(reproducible=False)
    _module_with_disk(device="/dev/vda3").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "./cmd/disk-setup" in build_script
    assert "$DESTDIR/usr/bin/disk-setup" in build_script


def test_disk_encryption_registers_init_script() -> None:
    image = Image(reproducible=False)
    _module_with_disk(
        device="/dev/vdb",
        mapper_name="cryptdata",
        key_path="/persistent/key",
        mount_point="/data",
    ).apply(image)

    profile = image.state.profiles["default"]
    assert len(image.init._scripts) == 1
    entry = image.init._scripts[0]
    assert "/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml" in entry.script
    assert "cryptsetup rename crypt_disk_disk_persistent cryptdata" in entry.script
    assert entry.priority == 20

    # key_path and mount_point are in the config file
    config_files = [f for f in profile.files if f.path == "/etc/tdx/disk-setup.yaml"]
    assert len(config_files) == 1
    assert 'encryption_key_path: "/persistent/key"' in config_files[0].content
    assert 'mount_at: "/data"' in config_files[0].content
    assert 'pattern: "/dev/vdb"' in config_files[0].content


def test_disk_encryption_renders_custom_disk_config() -> None:
    image = Image(reproducible=False)
    _module_with_disk(
        "scratch",
        device=None,
        key_name="rootfs_key",
        format_policy="on_initialize",
        dirs=("data", "cache"),
        mount_point="/mnt/scratch",
    ).apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdx/disk-setup.yaml"]
    assert len(config_files) == 1
    content = config_files[0].content
    assert "scratch:" in content
    assert 'strategy: "largest"' in content
    assert 'format: "on_initialize"' in content
    assert 'encryption_key: "rootfs_key"' in content
    assert 'mount_at: "/mnt/scratch"' in content
    assert 'dirs: ["data", "cache"]' in content


def test_disk_encryption_installs_cryptsetup() -> None:
    image = Image(reproducible=False)
    _module_with_disk().apply(image)

    profile = image.state.profiles["default"]
    assert "cryptsetup" in profile.packages


def test_disk_encryption_custom_repo() -> None:
    image = Image(reproducible=False)
    module = DiskEncryption(
        source_repo="https://github.com/custom/disk",
        source_branch="v3",
    )
    module.disk("disk_persistent")
    module.apply(image)

    profile = image.state.profiles["default"]
    build_script = profile.phases["build"][0].argv[0]
    assert "custom/disk" in build_script
    assert "-b v3" in build_script


def test_disk_encryption_supports_multiple_disks() -> None:
    image = Image(reproducible=False)
    module = DiskEncryption()
    module.disk(
        "data",
        device="/dev/vdb",
        key_name="data_key",
        key_path="/persistent/data.key",
        mount_point="/data",
    )
    module.disk(
        "logs",
        device="/dev/vdc",
        key_name="logs_key",
        key_path="/persistent/logs.key",
        mount_point="/var/log/app",
        mapper_name="cryptlogs",
        dirs=("logs", "archive"),
    )
    module.disk(
        "scratch",
        device=None,
        key_name=None,
        key_path=None,
        mount_point="/scratch",
        format_policy="on_initialize",
        dirs=("cache",),
    )
    module.apply(image)

    profile = image.state.profiles["default"]
    config = next(f.content for f in profile.files if f.path == "/etc/tdx/disk-setup.yaml")
    assert "data:" in config
    assert "logs:" in config
    assert "scratch:" in config
    assert 'encryption_key: "data_key"' in config
    assert 'encryption_key: "logs_key"' in config
    assert 'encryption_key_path: "/persistent/data.key"' in config
    assert 'encryption_key_path: "/persistent/logs.key"' in config
    assert 'mount_at: "/scratch"' in config
    assert 'dirs: ["cache"]' in config

    script = image.init._scripts[0].script
    assert script.count("/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml") == 1
    assert "cryptsetup rename crypt_disk_logs cryptlogs" in script


def test_disk_encryption_rejects_mapper_name_for_plain_disks() -> None:
    with pytest.raises(ValidationError, match="Plain disks cannot request custom mapper names"):
        _module_with_disk(key_name=None, key_path=None, mapper_name="plain").apply(
            Image(reproducible=False)
        )


# ── SecretDelivery ───────────────────────────────────────────────────


def test_secret_delivery_adds_build_hook() -> None:
    image = Image(reproducible=False)
    SecretDelivery(method="http_post").apply(image)

    profile = image.state.profiles["default"]
    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 1
    build_script = build_commands[0].argv[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "./cmd/secret-delivery" in build_script
    assert "$DESTDIR/usr/bin/secret-delivery" in build_script


def test_secret_delivery_registers_init_script() -> None:
    image = Image(reproducible=False)
    SecretDelivery(method="http_post", port=9090).apply(image)

    assert len(image.init._scripts) == 1
    entry = image.init._scripts[0]
    expected = "/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml"
    assert expected in entry.script
    assert entry.priority == 30


def test_secret_delivery_does_not_add_runtime_packages() -> None:
    image = Image(reproducible=False)
    SecretDelivery().apply(image)

    profile = image.state.profiles["default"]
    assert "python3" not in profile.packages


def test_secret_delivery_writes_config_from_declared_secrets() -> None:
    import json

    from tundravm.models import SecretSchema, SecretTarget

    image = Image(reproducible=False)
    delivery = SecretDelivery(
        method="http_post",
        host="127.0.0.1",
        port=9090,
        ssh_dir="/var/lib/app/.ssh",
        key_path="/run/keys/root.pub",
        store_at="data-disk",
    )
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
    manifest_files = [f for f in profile.files if f.path == "/etc/tdx/secrets.json"]
    assert len(manifest_files) == 1
    config = json.loads(manifest_files[0].content)

    yaml_files = [f for f in profile.files if f.path == "/etc/tdx/secrets.yaml"]
    assert len(yaml_files) == 1
    yaml_content = yaml_files[0].content
    assert 'server_url: "127.0.0.1:9090"' in yaml_content
    assert 'dir: "/var/lib/app/.ssh"' in yaml_content
    assert 'key_path: "/run/keys/root.pub"' in yaml_content
    assert 'store_at: "data-disk"' in yaml_content

    assert config["method"] == "http_post"
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 9090
    assert len(config["secrets"]) == 2

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
        "kind": "file",
        "location": "/run/secrets/jwt.hex",
        "mode": "0440",
        "owner": "app",
    }
    assert jwt["targets"][1] == {"kind": "env", "location": "JWT_SECRET", "scope": "global"}


# ── Composition with Init ────────────────────────────────────────────


def test_init_generates_runtime_init_from_init_scripts() -> None:
    image = Image(reproducible=False)
    _module_with_key(strategy="tpm").apply(image)
    _module_with_disk(device="/dev/vda3").apply(image)
    SecretDelivery(method="http_post").apply(image)
    image._apply_init()

    profile = image.state.profiles["default"]

    build_commands = profile.phases.get("build", [])
    assert len(build_commands) == 3

    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 1
    script = script_files[0].content
    assert "#!/bin/bash" in script
    assert "/usr/bin/key-gen setup /etc/tdx/key-gen.yaml" in script
    assert "/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml" in script
    assert "/usr/bin/secret-delivery" in script

    svc_files = [
        f for f in profile.files if f.path == "/usr/lib/systemd/system/runtime-init.service"
    ]
    assert len(svc_files) == 1
    svc = svc_files[0].content
    assert "Type=oneshot" in svc
    assert "ExecStart=/usr/bin/runtime-init" in svc

    assert "cryptsetup" in profile.packages
    assert "python3" not in profile.packages

    assert "golang" in profile.build_packages
    assert "git" in profile.build_packages


def test_init_scripts_sorted_by_priority() -> None:
    image = Image(reproducible=False)
    SecretDelivery(method="http_post").apply(image)
    _module_with_key(strategy="tpm").apply(image)
    _module_with_disk(device="/dev/vda3").apply(image)
    image._apply_init()

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    script = script_files[0].content

    key_pos = script.index("key-gen setup")
    disk_pos = script.index("disk-setup setup")
    secret_pos = script.index("secret-delivery")
    assert key_pos < disk_pos < secret_pos


def test_init_without_init_scripts_does_not_generate_runtime_init() -> None:
    image = Image(reproducible=False)
    image._apply_init()

    profile = image.state.profiles["default"]
    script_files = [f for f in profile.files if f.path == "/usr/bin/runtime-init"]
    assert len(script_files) == 0
