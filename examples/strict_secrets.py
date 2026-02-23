"""Strict secret schema validation example.

Declares secrets with schema constraints on SecretDelivery. The Go binary
validates and materializes them at boot time.
"""

from tdx import Image, SecretSchema, SecretTarget
from tdx.modules import SecretDelivery


def configure_strict_secrets() -> None:
    img = Image()

    delivery = SecretDelivery(method="http_post")
    delivery.secret(
        "api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=10, pattern="^tok_"),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )
    delivery.apply(img)


if __name__ == "__main__":
    configure_strict_secrets()
