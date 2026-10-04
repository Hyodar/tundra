"""Secrets with schema constraints, validated and materialized at boot.

No disk is declared, so the secrets are not stored on one (``store=None``).
"""

from tundravm.declarative import Fragment, Recipe, Schema, Secret, SecretEnv, SecretFile, Secrets

token = Secret(
    "api_token",
    targets=(SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN")),
    schema=Schema(kind="string", min_length=10, pattern="^tok_"),
)

recipe = Recipe(
    name="strict-secrets",
    base="debian/bookworm",
    common=Fragment("strict-secrets", items=(Secrets(entries=(token,)),)),
)
