"""Strict secret schema validation with global env injection."""

from tdx import Image
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import Init


def configure_strict_secrets() -> None:
    img = Image()
    img.secret(
        "api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=10, pattern="^tok_"),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )

    secret_spec = img.state.profiles["default"].secrets[0]
    init = Init(secrets=(secret_spec,))
    delivery = init.secrets_delivery(
        "http_post",
        completion="all_required",
        reject_unknown=True,
    )

    validation = delivery.validate_payload({"api_token": "tok_0123456789"})
    if validation.ready:
        delivery.materialize_runtime("runtime")


if __name__ == "__main__":
    configure_strict_secrets()
