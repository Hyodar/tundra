"""Init module primitives including secret delivery validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tdx.errors import ValidationError
from tdx.models import SecretSchema, SecretSpec

if TYPE_CHECKING:
    from tdx.image import Image

CompletionMode = Literal["all_required", "any"]
GLOBAL_ENV_RELATIVE_PATH = Path("run/tdx-secrets/global.env")


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


@dataclass(frozen=True, slots=True)
class SecretsRuntimeArtifacts:
    file_targets: tuple[Path, ...]
    global_env_path: Path | None
    loads_before: tuple[str, ...] = ("secrets-ready.target",)


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

    def materialize_runtime(self, runtime_root: str | Path) -> SecretsRuntimeArtifacts:
        missing_required = tuple(
            sorted(
                name
                for name, spec in self.expected.items()
                if spec.required and name not in self._received
            ),
        )
        if self.config.completion == "all_required" and missing_required:
            raise ValidationError(
                (
                    "Cannot materialize secrets runtime artifacts before all required "
                    "secrets are received."
                ),
                hint="Submit payloads for all required secrets first.",
                context={"missing_required": ",".join(missing_required)},
            )

        root = Path(runtime_root)
        file_targets: list[Path] = []
        global_env: dict[str, str] = {}

        for name, spec in self.expected.items():
            if name not in self._received:
                continue
            value = _to_secret_text(self._received[name])
            for target in spec.targets:
                if target.kind == "file":
                    path = root / target.location.lstrip("/")
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(value, encoding="utf-8")
                    file_targets.append(path)
                elif target.kind == "env" and target.scope == "global":
                    global_env[target.location] = value

        global_env_path: Path | None = None
        if global_env:
            global_env_path = root / GLOBAL_ENV_RELATIVE_PATH
            global_env_path.parent.mkdir(parents=True, exist_ok=True)
            lines = [f"{name}={value}" for name, value in sorted(global_env.items())]
            global_env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return SecretsRuntimeArtifacts(
            file_targets=tuple(sorted(file_targets)),
            global_env_path=global_env_path,
        )

    def _is_ready(self, *, missing_required: tuple[str, ...], has_errors: bool) -> bool:
        if has_errors:
            return False
        if self.config.completion == "all_required":
            return not missing_required
        return bool(self._received)


@dataclass(frozen=True, slots=True)
class DiskEncryptionConfig:
    device: str = "/dev/vda3"
    mapper_name: str = "cryptroot"
    format: str = "luks2"


@dataclass(frozen=True, slots=True)
class SshKeyDeliveryConfig:
    authorized_keys_path: str = "/root/.ssh/authorized_keys"


@dataclass(slots=True)
class Init:
    _secrets: dict[str, SecretSpec] = field(default_factory=dict)
    disk_encryption: DiskEncryptionConfig | None = None
    ssh_keys: list[str] = field(default_factory=list)
    ssh_config: SshKeyDeliveryConfig = field(default_factory=SshKeyDeliveryConfig)
    _delivery: HttpPostSecretDelivery | None = None

    def __init__(self, secrets: tuple[SecretSpec, ...] = ()) -> None:
        self._secrets = {secret.name: secret for secret in secrets}
        self.disk_encryption = None
        self.ssh_keys = []
        self.ssh_config = SshKeyDeliveryConfig()
        self._delivery = None

    def add_secret(self, spec: SecretSpec) -> None:
        self._secrets[spec.name] = spec

    def enable_disk_encryption(
        self,
        *,
        device: str = "/dev/vda3",
        mapper_name: str = "cryptroot",
        format: str = "luks2",
    ) -> None:
        self.disk_encryption = DiskEncryptionConfig(
            device=device,
            mapper_name=mapper_name,
            format=format,
        )

    def add_ssh_authorized_key(self, key: str) -> None:
        if not key:
            raise ValidationError("SSH key must be non-empty.")
        self.ssh_keys.append(key)

    def attach_secret_delivery(self, delivery: HttpPostSecretDelivery) -> None:
        self._delivery = delivery

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
        delivery = HttpPostSecretDelivery(expected=dict(self._secrets), config=config)
        self._delivery = delivery
        return delivery

    def setup(self, image: Image) -> None:
        if self.disk_encryption is not None:
            image.install("cryptsetup")
        if self.ssh_keys:
            image.install("openssh-server")
        if self._delivery is not None:
            image.install("python3")

    def install(self, image: Image) -> None:
        if self.disk_encryption is not None:
            payload = json.dumps(
                {
                    "device": self.disk_encryption.device,
                    "mapper_name": self.disk_encryption.mapper_name,
                    "format": self.disk_encryption.format,
                },
                indent=2,
                sort_keys=True,
            )
            image.file("/etc/tdx/init/disk-encryption.json", content=payload + "\n")
        if self.ssh_keys:
            keys = "\n".join(self.ssh_keys) + "\n"
            image.file(self.ssh_config.authorized_keys_path, content=keys, mode="0600")
        if self._delivery is not None:
            payload = json.dumps(
                {
                    "method": "http_post",
                    "completion": self._delivery.config.completion,
                    "reject_unknown": self._delivery.config.reject_unknown,
                },
                indent=2,
                sort_keys=True,
            )
            image.file("/etc/tdx/init/secrets-delivery.json", content=payload + "\n")
            image.service("secrets-ready.target", enabled=True)

    def validate_and_materialize(
        self,
        payload: dict[str, object],
        *,
        runtime_root: str | Path,
    ) -> tuple[SecretDeliveryValidation, SecretsRuntimeArtifacts | None]:
        if self._delivery is None:
            raise ValidationError("Secret delivery is not configured for Init.")
        validation = self._delivery.validate_payload(payload)
        if not validation.ready:
            return validation, None
        return validation, self._delivery.materialize_runtime(runtime_root)


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


def _to_secret_text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)
