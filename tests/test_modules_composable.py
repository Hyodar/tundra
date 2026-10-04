"""Keys, disks and secrets lower to the tundra-tools builds, configs and runtime-init steps."""

import json
from pathlib import Path

import pytest

from tundravm.declarative import (
    Declaration,
    Disk,
    Fragment,
    Git,
    Init,
    Key,
    Recipe,
    RuntimeTools,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
)
from tundravm.errors import ValidationError
from tundravm.testing import CompiledTree, compile_tree

KEY_CONFIG = "mkosi.extra/etc/tdx/key-gen.yaml"
DISK_CONFIG = "mkosi.extra/etc/tdx/disk-setup.yaml"
SECRET_CONFIG = "mkosi.extra/etc/tdx/secrets.yaml"
SECRET_MANIFEST = "mkosi.extra/etc/tdx/secrets.json"
PERSISTENT_KEY = Path("/persistent/key")


def _recipe(*items: Declaration) -> Recipe:
    return Recipe("runtime", Fragment("runtime", items=items))


def _compile(tmp_path: Path, *items: Declaration) -> CompiledTree:
    return compile_tree(_recipe(*items), path=tmp_path / "tree")


def _builds(tree: CompiledTree) -> list[str]:
    """The build script's source-build lines, one per build."""
    return [line for line in tree.script("build").splitlines() if "git clone" in line]


def _conf_list(conf: str, key: str) -> list[str]:
    """The values of a multi-line ``key=`` list in an mkosi.conf."""
    lines = conf.splitlines()
    start = lines.index(f"{key}=") + 1 if f"{key}=" in lines else len(lines)
    values: list[str] = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        values.append(line.strip())
    return values


def _probes(priority: int) -> tuple[Init, Init]:
    """Inits that run just before and just after *priority*."""
    return (
        Init("probe-early", "echo probe-early", priority=priority - 1),
        Init("probe-late", "echo probe-late", priority=priority + 1),
    )


def _assert_runs_at_priority(script: str, marker: str) -> None:
    assert script.index("probe-early") < script.index(marker) < script.index("probe-late")


# ── Keys ─────────────────────────────────────────────────────────────


def test_key_generation_adds_build_hook(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Key("key_persistent"))

    builds = _builds(tree)
    assert len(builds) == 1
    build_script = builds[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "go build" in build_script
    assert "./cmd/key-gen" in build_script
    assert "$DESTDIR/usr/bin/key-gen" in build_script


def test_key_generation_registers_init_script(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Key("key_persistent", output="/persistent/key"), *_probes(10))

    script = tree.runtime_init()
    assert script.count("/usr/bin/key-gen setup /etc/tdx/key-gen.yaml") == 1
    _assert_runs_at_priority(script, "key-gen setup")

    # output_path is in the config file, not the init script
    config = tree.read(KEY_CONFIG)
    assert 'output_path: "/persistent/key"' in config
    assert 'strategy: "random"' in config
    assert "tpm: true" in config
    assert "/persistent/key" not in script


def test_key_generation_allows_output_for_non_tpm_keys(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Key("key_persistent", output="/tmp/key", persist_in_tpm=False))

    config = tree.read(KEY_CONFIG)
    assert 'output_path: "/tmp/key"' in config
    assert "tpm: false" in config
    assert "tpm2-tools" not in _conf_list(tree.conf(), "Packages")


def test_key_generation_pipe_strategy_renders_pipe_config(tmp_path: Path) -> None:
    key = Key("rootfs_key", strategy="pipe", pipe="/run/tdx/passphrase", persist_in_tpm=True)
    tree = _compile(tmp_path, key)

    content = tree.read(KEY_CONFIG)
    assert "rootfs_key:" in content
    assert 'strategy: "pipe"' in content
    assert 'pipe_path: "/run/tdx/passphrase"' in content
    assert "tpm: true" in content
    assert "tpm2-tools" in _conf_list(tree.conf(), "Packages")


def test_key_generation_custom_repo_and_branch(tmp_path: Path) -> None:
    tools = RuntimeTools(Git("https://github.com/custom/fork", "v2.0"))
    tree = _compile(tmp_path, Key("key_persistent"), tools)

    (build_script,) = _builds(tree)
    assert "custom/fork" in build_script
    assert "-b v2.0" in build_script


def test_key_generation_supports_multiple_keys(tmp_path: Path) -> None:
    tree = _compile(
        tmp_path,
        Key("root", output="/persistent/root.key"),
        Key(
            "data",
            strategy="pipe",
            pipe="/run/keys/data.pipe",
            persist_in_tpm=True,
            output="/persistent/data.key",
        ),
    )

    config = tree.read(KEY_CONFIG)
    assert "root:" in config
    assert "data:" in config
    assert 'pipe_path: "/run/keys/data.pipe"' in config
    assert 'output_path: "/persistent/root.key"' in config
    assert 'output_path: "/persistent/data.key"' in config

    assert len(_builds(tree)) == 1
    script = tree.runtime_init()
    assert script.count("/usr/bin/key-gen setup /etc/tdx/key-gen.yaml") == 1


def test_key_generation_pipe_strategy_requires_pipe_path() -> None:
    with pytest.raises(ValidationError, match="the pipe strategy requires pipe="):
        Key("bad", strategy="pipe")


def test_key_generation_pipe_path_only_valid_with_pipe_strategy() -> None:
    with pytest.raises(ValidationError, match="pipe= is only valid"):
        Key("bad", pipe="/run/keys/x.pipe")


def test_key_generation_rejects_duplicate_and_shared_outputs(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"Key\(a\) is declared twice"):
        _compile(tmp_path, Key("a"), Key("a", output="/k"))
    with pytest.raises(ValidationError, match="output path must be unique"):
        _compile(tmp_path, Key("a", output="/k"), Key("b", output="/k"))
    with pytest.raises(ValidationError, match="invalid key name"):
        Key("bad name")


# ── Disks ────────────────────────────────────────────────────────────


def test_disk_encryption_adds_build_hook(tmp_path: Path) -> None:
    disk = Disk("disk_persistent", "/persistent", device="/dev/vda3", key=PERSISTENT_KEY)
    tree = _compile(tmp_path, disk)

    builds = _builds(tree)
    assert len(builds) == 1
    build_script = builds[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "./cmd/disk-setup" in build_script
    assert "$DESTDIR/usr/bin/disk-setup" in build_script


def test_disk_encryption_registers_init_script(tmp_path: Path) -> None:
    disk = Disk(
        "disk_persistent", "/data", device="/dev/vdb", key=PERSISTENT_KEY, mapper="cryptdata"
    )
    tree = _compile(tmp_path, disk, *_probes(20))

    script = tree.runtime_init()
    assert script.count("/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml") == 1
    assert "cryptsetup rename crypt_disk_disk_persistent cryptdata" in script
    _assert_runs_at_priority(script, "disk-setup setup")

    # key_path and mount_at are in the config file
    config = tree.read(DISK_CONFIG)
    assert 'encryption_key_path: "/persistent/key"' in config
    assert 'mount_at: "/data"' in config
    assert 'pattern: "/dev/vdb"' in config


def test_disk_encryption_renders_custom_disk_config(tmp_path: Path) -> None:
    key = Key("rootfs_key")
    disk = Disk(
        "scratch",
        "/mnt/scratch",
        key=key,
        format="on_initialize",
        directories=("data", "cache"),
    )
    tree = _compile(tmp_path, key, disk)

    content = tree.read(DISK_CONFIG)
    assert "scratch:" in content
    assert 'strategy: "largest"' in content
    assert 'format: "on_initialize"' in content
    assert 'encryption_key: "rootfs_key"' in content
    assert 'mount_at: "/mnt/scratch"' in content
    assert 'dirs: ["data", "cache"]' in content


def test_disk_encryption_installs_cryptsetup(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Disk("disk_persistent", "/persistent", key=PERSISTENT_KEY))

    assert "cryptsetup" in _conf_list(tree.conf(), "Packages")


def test_disk_encryption_custom_repo(tmp_path: Path) -> None:
    tools = RuntimeTools(Git("https://github.com/custom/disk", "v3"))
    tree = _compile(tmp_path, Disk("disk_persistent", "/persistent"), tools)

    (build_script,) = _builds(tree)
    assert "custom/disk" in build_script
    assert "-b v3" in build_script


def test_disk_encryption_supports_multiple_disks(tmp_path: Path) -> None:
    data_key = Key("data_key")
    logs_key = Key("logs_key", output="/persistent/logs.key")
    tree = _compile(
        tmp_path,
        data_key,
        logs_key,
        Disk("data", "/data", device="/dev/vdb", key=data_key),
        Disk(
            "logs",
            "/var/log/app",
            device="/dev/vdc",
            key=logs_key,
            mapper="cryptlogs",
            directories=("logs", "archive"),
        ),
        Disk("scratch", "/scratch", format="on_initialize", directories=("cache",)),
    )

    config = tree.read(DISK_CONFIG)
    assert "data:" in config
    assert "logs:" in config
    assert "scratch:" in config
    assert 'encryption_key: "data_key"' in config
    assert 'encryption_key_path: "/persistent/logs.key"' in config
    assert 'mount_at: "/scratch"' in config
    assert 'dirs: ["logs", "archive"]' in config
    assert 'dirs: ["cache"]' in config

    script = tree.runtime_init()
    assert script.count("/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml") == 1
    assert "cryptsetup rename crypt_disk_logs cryptlogs" in script


def test_disk_encryption_rejects_mapper_name_for_plain_disks() -> None:
    with pytest.raises(ValidationError, match="a plain disk cannot set mapper="):
        Disk("disk_persistent", "/persistent", mapper="plain")


def test_disk_encryption_rejects_conflicting_disks(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match=r"Disk\(a\) is declared twice"):
        _compile(tmp_path, Disk("a", "/a"), Disk("a", "/b"))
    with pytest.raises(ValidationError, match="unique mount point"):
        _compile(tmp_path, Disk("a", "/persistent"), Disk("b", "/persistent"))
    with pytest.raises(ValidationError, match="unique mapper name"):
        _compile(
            tmp_path,
            Disk("a", "/a", key=Path("/k/a"), mapper="m"),
            Disk("b", "/b", key=Path("/k/b"), mapper="m"),
        )


# ── Secrets ──────────────────────────────────────────────────────────


def test_secret_delivery_adds_build_hook(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Secrets())

    builds = _builds(tree)
    assert len(builds) == 1
    build_script = builds[0]
    assert "git clone" in build_script
    assert "Hyodar/tundra-tools" in build_script
    assert "mkosi-chroot bash -c" in build_script
    assert "mkdir -p ./build" in build_script
    assert "./cmd/secret-delivery" in build_script
    assert "$DESTDIR/usr/bin/secret-delivery" in build_script


def test_secret_delivery_registers_init_script(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Secrets(port=9090), *_probes(30))

    script = tree.runtime_init()
    expected = "/usr/bin/secret-delivery setup /etc/tdx/secrets.yaml"
    assert script.count(expected) == 1
    _assert_runs_at_priority(script, "secret-delivery setup")
    assert 'server_url: "0.0.0.0:9090"' in tree.read(SECRET_CONFIG)


def test_secret_delivery_does_not_add_runtime_packages(tmp_path: Path) -> None:
    tree = _compile(tmp_path, Secrets())

    assert "python3" not in _conf_list(tree.conf(), "Packages")


def test_secret_delivery_writes_config_from_declared_secrets(tmp_path: Path) -> None:
    store = Disk("data-disk", "/data")
    secrets = Secrets(
        entries=(
            Secret(
                "jwt_secret",
                targets=(
                    SecretFile("/run/secrets/jwt.hex", owner="app", mode=0o440),
                    SecretEnv("JWT_SECRET"),
                ),
                required=True,
                schema=Schema(kind="string", min_length=64, max_length=64),
            ),
            Secret("api_key", targets=(SecretFile("/run/secrets/api-key"),), required=False),
        ),
        store=store,
        host="127.0.0.1",
        port=9090,
        ssh_directory="/var/lib/app/.ssh",
        ssh_key_path="/run/keys/root.pub",
    )
    tree = _compile(tmp_path, store, secrets)

    yaml_content = tree.read(SECRET_CONFIG)
    assert 'server_url: "127.0.0.1:9090"' in yaml_content
    assert 'dir: "/var/lib/app/.ssh"' in yaml_content
    assert 'key_path: "/run/keys/root.pub"' in yaml_content
    assert 'store_at: "data-disk"' in yaml_content

    config = json.loads(tree.read(SECRET_MANIFEST))
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


# ── Composition through runtime-init ─────────────────────────────────


def _runtime_items() -> tuple[Declaration, ...]:
    key = Key("key_persistent", output="/persistent/key")
    disk = Disk("disk_persistent", "/persistent", device="/dev/vda3", key=key)
    return (key, disk, Secrets(store=disk))


def test_init_generates_runtime_init_from_init_scripts(tmp_path: Path) -> None:
    tree = _compile(tmp_path, *_runtime_items())

    assert len(_builds(tree)) == 3

    script = tree.runtime_init()
    assert "#!/bin/bash" in script
    assert "/usr/bin/key-gen setup /etc/tdx/key-gen.yaml" in script
    assert "/usr/bin/disk-setup setup /etc/tdx/disk-setup.yaml" in script
    assert "/usr/bin/secret-delivery" in script

    svc = tree.unit("runtime-init.service")
    assert "Type=oneshot" in svc
    assert "ExecStart=/usr/bin/runtime-init" in svc

    conf = tree.conf()
    packages = _conf_list(conf, "Packages")
    assert "cryptsetup" in packages
    assert "python3" not in packages

    build_packages = _conf_list(conf, "BuildPackages")
    assert "golang" in build_packages
    assert "git" in build_packages


def test_init_scripts_sorted_by_priority(tmp_path: Path) -> None:
    key, disk, secrets = _runtime_items()
    tree = _compile(tmp_path, secrets, disk, key)

    script = tree.runtime_init()
    key_pos = script.index("key-gen setup")
    disk_pos = script.index("disk-setup setup")
    secret_pos = script.index("secret-delivery")
    assert key_pos < disk_pos < secret_pos


def test_init_without_init_scripts_does_not_generate_runtime_init(tmp_path: Path) -> None:
    tree = _compile(tmp_path)

    assert not tree.exists("mkosi.extra/usr/bin/runtime-init")
    assert not tree.exists("mkosi.extra/usr/lib/systemd/system/runtime-init.service")
