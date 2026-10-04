import pytest

from tundravm.errors import ValidationError
from tundravm.models import SecretSchema, SecretSpec, SecretTarget
from tundravm.modules import SecretDelivery


def test_secret_declaration_supports_schema_and_multiple_targets() -> None:
    schema = SecretSchema(kind="string", min_length=8, pattern="^tok_")
    targets = (
        SecretTarget.file("/run/secrets/api-token"),
        SecretTarget.env("API_TOKEN", scope="global"),
    )

    declared = SecretSpec("api_token", required=True, schema=schema, targets=targets)
    delivery = SecretDelivery(secrets=(declared,))

    assert delivery.secrets == (declared,)
    assert declared.name == "api_token"
    assert declared.required is True
    assert declared.schema == schema
    assert len(declared.targets) == 2


def test_secret_delivery_rejects_unnamed_or_targetless_secrets() -> None:
    target = (SecretTarget.file("/run/secrets/x"),)
    with pytest.raises(ValidationError, match="non-empty secret names"):
        SecretDelivery(secrets=(SecretSpec("", targets=target),))
    with pytest.raises(ValidationError, match="secret 'x' requires at least one delivery target"):
        SecretDelivery(secrets=(SecretSpec("x"),))
