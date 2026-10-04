import pytest

from tundravm.declarative import Schema, Secret, SecretEnv, SecretFile, Secrets
from tundravm.errors import ValidationError


def test_secret_declaration_supports_schema_and_multiple_targets() -> None:
    schema = Schema(kind="string", min_length=8, pattern="^tok_")
    targets = (SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN"))

    declared = Secret("api_token", targets=targets, required=True, schema=schema)
    delivery = Secrets(entries=(declared,))

    assert delivery.entries == (declared,)
    assert declared.name == "api_token"
    assert declared.required is True
    assert declared.schema == schema
    assert len(declared.targets) == 2


def test_secret_delivery_rejects_unnamed_or_targetless_secrets() -> None:
    target = (SecretFile("/run/secrets/x"),)
    with pytest.raises(ValidationError, match="name must be a non-empty string"):
        Secret("", targets=target)
    with pytest.raises(ValidationError, match="secret 'x' requires at least one delivery target"):
        Secret("x", targets=())
