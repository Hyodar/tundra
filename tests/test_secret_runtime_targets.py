from pathlib import Path

from tundravm import Image
from tundravm.models import SecretSchema, SecretTarget
from tundravm.modules import SecretDelivery


def test_secret_target_helpers_support_file_and_global_env() -> None:
    file_target = SecretTarget.file("/run/secrets/api-token", mode="0400")
    env_target = SecretTarget.env("API_TOKEN", scope="global")

    assert file_target.kind == "file"
    assert file_target.location == "/run/secrets/api-token"
    assert env_target.kind == "env"
    assert env_target.scope == "global"


def test_secret_values_are_not_persisted_in_lockfile(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build")

    delivery = SecretDelivery()
    delivery.secret(
        "api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )
    delivery.apply(image)

    lock_path = image.lock()
    lock_text = lock_path.read_text(encoding="utf-8")
    # Schema metadata is in the lockfile via profile.secrets, but no values
    assert "api_token" in lock_text
