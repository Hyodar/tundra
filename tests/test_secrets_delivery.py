from tdx.models import SecretSchema, SecretTarget
from tdx.modules import SecretDelivery


def test_secret_declaration_supports_schema_and_multiple_targets() -> None:
    schema = SecretSchema(kind="string", min_length=8, pattern="^tok_")
    targets = (
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    )

    delivery = SecretDelivery()
    declared = delivery.secret(
        "api_token",
        required=True,
        schema=schema,
        targets=targets,
    )

    assert declared.name == "api_token"
    assert declared.required is True
    assert declared.schema == schema
    assert len(declared.targets) == 2
