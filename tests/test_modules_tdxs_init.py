from pathlib import Path

from tdx import Image
from tdx.models import SecretSchema, SecretSpec, SecretTarget
from tdx.modules import Init, Tdxs


def test_tdxs_generates_config_yaml_and_units() -> None:
    image = Image()
    module = Tdxs(issuer_type="dcap")

    module.apply(image)

    profile = image.state.profiles["default"]

    # Config file
    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    assert len(config_files) == 1
    config_content = config_files[0].content
    assert "transport:" in config_content
    assert "type: socket" in config_content
    assert "systemd: true" in config_content
    assert "issuer:" in config_content
    assert "type: dcap" in config_content

    # Service unit
    svc_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/tdxs.service"
    ]
    assert len(svc_files) == 1
    svc_content = svc_files[0].content
    assert "User=tdxs" in svc_content
    assert "Group=tdx" in svc_content
    assert "Type=notify" in svc_content
    assert "ExecStart=/usr/bin/tdxs" in svc_content
    assert "--config /etc/tdxs/config.yaml" in svc_content
    assert "Requires=runtime-init.service tdxs.socket" in svc_content

    # Socket unit
    sock_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/tdxs.socket"
    ]
    assert len(sock_files) == 1
    sock_content = sock_files[0].content
    assert "ListenStream=/var/tdxs.sock" in sock_content
    assert "SocketMode=0660" in sock_content
    assert "SocketGroup=tdx" in sock_content

    # Postinst hooks: groupadd, useradd, systemctl enable
    postinst_commands = profile.phases.get("postinst", [])
    assert len(postinst_commands) == 3
    assert postinst_commands[0].argv == (
        "mkosi-chroot", "groupadd", "--system", "tdx",
    )
    assert postinst_commands[1].argv[:3] == ("mkosi-chroot", "useradd", "--system")
    assert "tdxs" in postinst_commands[1].argv
    assert postinst_commands[2].argv == (
        "mkosi-chroot", "systemctl", "enable", "tdxs.socket",
    )


def test_tdxs_custom_issuer_type() -> None:
    image = Image()
    module = Tdxs(issuer_type="azure-tdx")

    module.apply(image)

    profile = image.state.profiles["default"]
    config_files = [f for f in profile.files if f.path == "/etc/tdxs/config.yaml"]
    assert "type: azure-tdx" in config_files[0].content


def test_tdxs_custom_socket_path() -> None:
    image = Image()
    module = Tdxs(socket_path="/run/tdx/quote.sock")

    module.apply(image)

    profile = image.state.profiles["default"]
    sock_files = [
        f for f in profile.files
        if f.path == "/usr/lib/systemd/system/tdxs.socket"
    ]
    assert "ListenStream=/run/tdx/quote.sock" in sock_files[0].content


def test_init_supports_disk_encryption_ssh_and_secret_delivery() -> None:
    image = Image()
    secret = SecretSpec(
        name="api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(SecretTarget.file("/run/secrets/api-token"),),
    )
    init = Init(secrets=(secret,))
    init.enable_disk_encryption(device="/dev/vdb", mapper_name="cryptdata")
    init.add_ssh_authorized_key("ssh-ed25519 AAAATEST")
    init.secrets_delivery("http_post")

    init.setup(image)
    init.install(image)

    profile = image.state.profiles["default"]
    assert "cryptsetup" in profile.packages
    assert "openssh-server" in profile.packages
    assert "python3" in profile.packages
    assert any(file.path == "/etc/tdx/init/disk-encryption.json" for file in profile.files)
    assert any(file.path == "/root/.ssh/authorized_keys" for file in profile.files)
    assert any(file.path == "/etc/tdx/init/secrets-delivery.json" for file in profile.files)
    assert any(file.path == "/etc/tdx/init/phases.json" for file in profile.files)
    assert any(service.name == "secrets-ready.target" for service in profile.services)


def test_init_secret_delivery_integration_materializes_runtime(tmp_path: Path) -> None:
    secret = SecretSpec(
        name="api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )
    init = Init(secrets=(secret,))
    init.secrets_delivery("http_post")

    validation, artifacts = init.validate_and_materialize(
        {"api_token": "tok_1234"},
        runtime_root=tmp_path,
    )

    assert validation.ready is True
    assert artifacts is not None
    assert (tmp_path / "run/secrets/api-token").exists()
    assert artifacts.global_env_path is not None
    assert artifacts.global_env_path.exists()


def test_modules_apply_directly_and_infer_declared_secrets() -> None:
    image = Image()
    image.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(SecretTarget.file("/run/tdx-secrets/jwt.hex"),),
    )

    init = Init()
    delivery = init.secrets_delivery("http_post")
    init.apply(image)
    Tdxs(issuer_type="dcap").apply(image)

    profile = image.state.profiles["default"]
    assert "python3" in profile.packages
    assert any(file.path == "/etc/tdx/init/phases.json" for file in profile.files)
    assert any(file.path == "/etc/tdxs/config.yaml" for file in profile.files)

    validation = delivery.validate_payload({"jwt_secret": "a" * 64})
    assert validation.ready is True
