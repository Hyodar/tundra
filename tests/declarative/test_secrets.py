"""Secrets: declaration validation, delivery targets, and values kept out of the lockfile."""

import json
from pathlib import Path

import pytest

from tundravm.declarative import (
    Fragment,
    Recipe,
    Schema,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    lock,
)
from tundravm.errors import ValidationError
from tundravm.testing import compile_tree


def _recipe(*secrets: Secret) -> Recipe:
    return Recipe("secrets", Fragment("secrets", items=(Secrets(entries=secrets),)))


def test_secret_targets_support_file_and_global_env(tmp_path: Path) -> None:
    file_target = SecretFile("/run/secrets/api-token", mode=0o400)
    env_target = SecretEnv("API_TOKEN")

    assert file_target.path == "/run/secrets/api-token"
    assert env_target.name == "API_TOKEN"
    assert env_target.service is None  # global environment

    recipe = _recipe(Secret("api_token", targets=(file_target, env_target)))
    tree = compile_tree(recipe, path=tmp_path / "tree")
    manifest = json.loads(tree.read("mkosi.extra/etc/tdx/secrets.json"))
    assert manifest["secrets"][0]["targets"] == [
        {"kind": "file", "location": "/run/secrets/api-token", "mode": "0400"},
        {"kind": "env", "location": "API_TOKEN", "scope": "global"},
    ]


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
    (secret,) = json.loads(lock_text)["recipe"]["profiles"]["default"]["secrets"]
    assert set(secret) == {"name", "required", "schema", "targets"}


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
