"""Strict secret schema validation example.

Declares secrets with schema constraints on SecretDelivery. The Go binary
validates and materializes them at boot time.
"""

from tundravm import Image, SecretSchema, SecretTarget
from tundravm.modules import SecretDelivery, SecretSpec


def build() -> Image:
    img = Image()

    # No DiskEncryption here, so secrets are not stored on a disk (store_at=None).
    token = SecretSpec(
        "api_token",
        required=True,
        schema=SecretSchema(kind="string", min_length=10, pattern="^tok_"),
        targets=(
            SecretTarget.file("/run/secrets/api-token"),
            SecretTarget.env("API_TOKEN", scope="global"),
        ),
    )
    img.apply(SecretDelivery(secrets=(token,), method="http_post", store_at=None))
    return img


if __name__ == "__main__":
    print(build().summary())
