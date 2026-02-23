"""Strict secret schema validation example.

Declares secrets with schema constraints. The Go secret-delivery binary
validates and materializes them at boot time.
"""

from tdx import Image
from tdx.models import SecretSchema, SecretTarget
from tdx.modules import SecretDelivery


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

    SecretDelivery(
        method="http_post",
    ).apply(img)


if __name__ == "__main__":
    configure_strict_secrets()
