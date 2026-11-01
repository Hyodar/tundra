"""Secrets: declaration validation, delivery targets, and values kept out of the lockfile."""

import json

import pytest

from tundravm.declarative import (
    ExternalDelivery,
    Recipe,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    compile,
    lock,
    lower,
)
from tundravm.errors import ValidationError


def _recipe(*secrets: Secret) -> Recipe:
    return Recipe("secrets", items=(Secrets(entries=secrets),))


def test_secret_values_are_not_persisted_in_lockfile() -> None:
    token = Secret(
        "api_token",
        targets=(SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN")),
        required=True,
        schema=Schema(kind="string", min_length=4),
    )

    lock_text = lock(_recipe(token), resolver=lambda source: "0" * 40).text()
    # Schema metadata is in the lockfile via profile.secrets, but no values
    assert "api_token" in lock_text
    (secret,) = json.loads(lock_text)["image"]["profiles"]["default"]["secrets"]
    assert set(secret) == {"name", "required", "schema", "targets"}


def test_external_delivery_validates_its_name_and_kinds() -> None:
    assert ExternalDelivery("vault-agent").supports == ("file", "env")
    listed = ExternalDelivery("vault-agent", ["file"])  # type: ignore[arg-type]
    assert Secrets(delivery=listed).delivery == ExternalDelivery("vault-agent", ("file",))
    with pytest.raises(ValidationError, match="built-in provider's name"):
        ExternalDelivery("tundravm")
    with pytest.raises(ValidationError, match="supports must name 'file' and/or 'env'"):
        ExternalDelivery("vault-agent", ("ssh-key",))  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="supports must name"):
        ExternalDelivery("vault-agent", ())
    with pytest.raises(ValidationError, match="names a kind twice"):
        ExternalDelivery("vault-agent", ("file", "file"))
    with pytest.raises(ValidationError, match="delivery must be an ExternalDelivery"):
        Secrets(delivery="vault-agent")  # type: ignore[arg-type]


def test_external_delivery_changes_neither_tree_nor_digest() -> None:
    token = Secret("token", (SecretFile("/run/token"),))

    def recipe(secrets: Secrets) -> Recipe:
        return Recipe("r", items=(secrets,))

    builtin = recipe(Secrets(entries=(token,)))
    external = recipe(Secrets(entries=(token,), delivery=ExternalDelivery("vault-agent")))
    assert lower(builtin).digest() == lower(external).digest()
    assert compile(builtin).digest == compile(external).digest
