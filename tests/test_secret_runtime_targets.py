from pathlib import Path

import pytest

from tdx import Image
from tdx.errors import ValidationError
from tdx.models import SecretSchema, SecretSpec, SecretTarget
from tdx.modules import GLOBAL_ENV_RELATIVE_PATH, SecretDelivery


def test_secret_target_helpers_support_file_and_global_env() -> None:
    file_target = SecretTarget.file("/run/secrets/api-token", mode="0400")
    env_target = SecretTarget.env("API_TOKEN", scope="global")

    assert file_target.kind == "file"
    assert file_target.location == "/run/secrets/api-token"
    assert env_target.kind == "env"
    assert env_target.scope == "global"


def test_runtime_materialization_writes_file_and_global_env(tmp_path: Path) -> None:
    secret = SecretSpec(
        name="api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )
    delivery = SecretDelivery(expected={"api_token": secret})
    validation = delivery.validate_payload({"api_token": "tok_1234"})
    runtime = delivery.materialize_runtime(tmp_path)

    assert validation.ready is True
    file_path = tmp_path / "run/secrets/api-token"
    assert file_path.exists()
    assert file_path.read_text(encoding="utf-8") == "tok_1234"
    assert runtime.global_env_path == tmp_path / GLOBAL_ENV_RELATIVE_PATH
    assert runtime.global_env_path is not None
    assert runtime.global_env_path.read_text(encoding="utf-8") == "API_TOKEN=tok_1234\n"
    assert runtime.loads_before == ("secrets-ready.target",)


def test_materialization_requires_all_required_when_completion_all_required(tmp_path: Path) -> None:
    secret = SecretSpec(
        name="required_token",
        required=True,
        targets=(SecretTarget.file("/run/secrets/required-token"),),
    )
    delivery = SecretDelivery(
        expected={"required_token": secret},
        completion="all_required",
    )

    with pytest.raises(ValidationError):
        delivery.materialize_runtime(tmp_path)


def test_secret_values_are_not_persisted_in_lockfile(tmp_path: Path) -> None:
    image = Image(build_dir=tmp_path / "build")
    secret = image.secret(
        "api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )

    delivery = SecretDelivery(expected={"api_token": secret})
    delivery.validate_payload({"api_token": "super-secret-value"})
    delivery.materialize_runtime(tmp_path / "runtime")

    lock_path = image.lock()
    lock_text = lock_path.read_text(encoding="utf-8")
    assert "super-secret-value" not in lock_text
