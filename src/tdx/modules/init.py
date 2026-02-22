"""Init module primitives including secret delivery validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Literal

from tdx.errors import ValidationError
from tdx.models import SecretSchema, SecretSpec

CompletionMode = Literal["all_required", "any"]


@dataclass(frozen=True, slots=True)
class HttpPostDeliveryConfig:
    completion: CompletionMode = "all_required"
    reject_unknown: bool = True


@dataclass(frozen=True, slots=True)
class SecretDeliveryValidation:
    valid: bool
    ready: bool
    errors: tuple[str, ...]
    missing_required: tuple[str, ...]
    received: tuple[str, ...]


@dataclass(slots=True)
class HttpPostSecretDelivery:
    expected: dict[str, SecretSpec]
    config: HttpPostDeliveryConfig
    _received: dict[str, object] = field(default_factory=dict)

    def validate_payload(self, payload: dict[str, object]) -> SecretDeliveryValidation:
        errors: list[str] = []
        expected_names = set(self.expected)
        payload_names = set(payload)

        unknown = sorted(payload_names - expected_names)
        if unknown and self.config.reject_unknown:
            errors.append(f"Unknown secrets received: {', '.join(unknown)}")

        for name, value in payload.items():
            secret = self.expected.get(name)
            if secret is None:
                continue
            schema_error = _validate_schema(secret.schema, value)
            if schema_error is not None:
                errors.append(f"{name}: {schema_error}")
                continue
            self._received[name] = value

        missing_required = tuple(
            sorted(
                name
                for name, spec in self.expected.items()
                if spec.required and name not in self._received
            ),
        )
        ready = self._is_ready(missing_required=missing_required, has_errors=bool(errors))
        return SecretDeliveryValidation(
            valid=not errors,
            ready=ready,
            errors=tuple(errors),
            missing_required=missing_required,
            received=tuple(sorted(self._received)),
        )

    def _is_ready(self, *, missing_required: tuple[str, ...], has_errors: bool) -> bool:
        if has_errors:
            return False
        if self.config.completion == "all_required":
            return not missing_required
        return bool(self._received)


@dataclass(slots=True)
class Init:
    _secrets: dict[str, SecretSpec] = field(default_factory=dict)

    def __init__(self, secrets: tuple[SecretSpec, ...] = ()) -> None:
        self._secrets = {secret.name: secret for secret in secrets}

    def add_secret(self, spec: SecretSpec) -> None:
        self._secrets[spec.name] = spec

    def secrets_delivery(
        self,
        method: Literal["http_post"] = "http_post",
        *,
        completion: CompletionMode = "all_required",
        reject_unknown: bool = True,
    ) -> HttpPostSecretDelivery:
        if method != "http_post":
            raise ValidationError("Unsupported secret delivery method.", context={"method": method})
        config = HttpPostDeliveryConfig(completion=completion, reject_unknown=reject_unknown)
        return HttpPostSecretDelivery(expected=dict(self._secrets), config=config)


def _validate_schema(schema: SecretSchema | None, value: object) -> str | None:
    if schema is None:
        return None
    if schema.kind == "string":
        if not isinstance(value, str):
            return "expected string value"
        if schema.min_length is not None and len(value) < schema.min_length:
            return f"minimum length is {schema.min_length}"
        if schema.max_length is not None and len(value) > schema.max_length:
            return f"maximum length is {schema.max_length}"
        if schema.pattern is not None and re.search(schema.pattern, value) is None:
            return f"value does not match pattern {schema.pattern}"
        if schema.enum and value not in schema.enum:
            return "value is not in allowed enum set"
        return None

    if schema.kind == "json":
        if isinstance(value, (dict, list)):
            return None
        if isinstance(value, str):
            try:
                json.loads(value)
            except json.JSONDecodeError:
                return "expected JSON object/list or valid JSON string"
            return None
        return "expected JSON object/list or valid JSON string"

    return f"unsupported schema kind: {schema.kind}"
