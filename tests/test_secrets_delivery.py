from tdx import Image
from tdx.models import SecretSchema, SecretTarget


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
