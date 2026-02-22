from pathlib import Path

from tdx import Image
from tdx.models import SecretSchema, SecretSpec, SecretTarget
from tdx.modules import Init, Tdxs


def test_tdxs_issuer_mode_configures_package_and_service() -> None:
    image = Image()
    module = Tdxs.issuer()

    module.setup(image)
    module.install(image)

    profile = image.state.profiles["default"]
    assert "tdx-attestation-issuer" in profile.packages
    assert any(service.name == "tdxs.service" for service in profile.services)
    assert any(file.path == "/etc/tdx/tdxs.json" for file in profile.files)


def test_tdxs_validator_mode_configures_validator_package() -> None:
    image = Image()
    module = Tdxs.validator()

    module.setup(image)
    module.install(image)

    profile = image.state.profiles["default"]
    assert "tdx-attestation-validator" in profile.packages


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


def test_image_use_applies_module_setup_and_install_and_infers_declared_secrets() -> None:
    image = Image()
    image.secret(
        "jwt_secret",
        required=True,
        schema=SecretSchema(kind="string", min_length=64, max_length=64),
        targets=(SecretTarget.file("/run/tdx-secrets/jwt.hex"),),
    )

    init = Init()
    delivery = init.secrets_delivery("http_post")
    image.use(init, Tdxs.issuer())

    profile = image.state.profiles["default"]
    assert "python3" in profile.packages
    assert "tdx-attestation-issuer" in profile.packages
    assert any(file.path == "/etc/tdx/init/phases.json" for file in profile.files)
    assert any(service.name == "tdxs.service" for service in profile.services)

    validation = delivery.validate_payload({"jwt_secret": "a" * 64})
    assert validation.ready is True
