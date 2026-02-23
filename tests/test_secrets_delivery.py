from tdx import Image
from tdx.models import SecretSchema, SecretSpec, SecretTarget
from tdx.modules import SecretDelivery


def test_secret_declaration_supports_schema_and_multiple_targets() -> None:
    image = Image()
    schema = SecretSchema(kind="string", min_length=8, pattern="^tok_")
    targets = (
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    )
    declared = image.secret(
        "api_token",
        required=True,
        schema=schema,
        targets=targets,
    )

    secret = image.state.profiles["default"].secrets[0]
    assert declared == secret
    assert secret.name == "api_token"
    assert secret.required is True
    assert secret.schema == schema
    assert len(secret.targets) == 2


def test_http_post_reject_unknown_when_enabled() -> None:
    secret = SecretSpec(
        name="api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=4),
        targets=(SecretTarget.file("/run/secrets/api-token"),),
    )
    delivery = SecretDelivery(
        expected={"api_token": secret},
        reject_unknown=True,
    )

    result = delivery.validate_payload({"api_token": "abcd", "extra": "bad"})

    assert result.valid is False
    assert result.ready is False
    assert any("Unknown secrets received" in error for error in result.errors)


def test_completion_all_required_blocks_until_all_required_secrets_received() -> None:
    secret_a = SecretSpec(name="token_a", required=True, targets=(SecretTarget.file("/run/a"),))
    secret_b = SecretSpec(name="token_b", required=True, targets=(SecretTarget.file("/run/b"),))
    delivery = SecretDelivery(
        expected={"token_a": secret_a, "token_b": secret_b},
    )

    first = delivery.validate_payload({"token_a": "value-a"})
    second = delivery.validate_payload({"token_b": "value-b"})

    assert first.valid is True
    assert first.ready is False
    assert first.missing_required == ("token_b",)
    assert second.valid is True
    assert second.ready is True
    assert second.missing_required == ()


def test_schema_validation_blocks_ready_until_valid() -> None:
    schema = SecretSchema(kind="string", min_length=6, pattern="^tok_")
    secret = SecretSpec(
        name="token",
        required=True,
        schema=schema,
        targets=(SecretTarget.file("/run/token"),),
    )
    delivery = SecretDelivery(
        expected={"token": secret},
    )

    bad = delivery.validate_payload({"token": "bad"})
    good = delivery.validate_payload({"token": "tok_1234"})

    assert bad.valid is False
    assert bad.ready is False
    assert bad.missing_required == ("token",)
    assert good.valid is True
    assert good.ready is True
