"""Secret delivery module.

Builds a Go binary that handles secret delivery at runtime, registers its
invocation into the runtime-init script via ``image.add_init_script()``,
and provides Python-side validation/materialization for secret payloads.
"""

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

SECRET_DELIVERY_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
SECRET_DELIVERY_DEFAULT_BRANCH = "main"

CompletionMode = Literal["all_required", "any"]
GLOBAL_ENV_RELATIVE_PATH = Path("run/tdx-secrets/global.env")


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
class SecretDelivery:
    """Secret delivery: build-time module + runtime validation.

    Build phase: compiles a Go binary from source and registers its
    invocation in the runtime-init script.

    Runtime phase: validates incoming secret payloads against declared
    schemas and materializes them to the filesystem.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    completion: CompletionMode = "all_required"
    reject_unknown: bool = True
    expected: dict[str, SecretSpec] = field(default_factory=dict)
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH
    _received: dict[str, object] = field(default_factory=dict, init=False, repr=False)

    def apply(self, image: Image) -> None:
        """Add build hook, packages, init script, and capture declared secrets."""
        image.build_install(*SECRET_DELIVERY_BUILD_PACKAGES)
        image.install("python3")

        build_cmd = (
            f"SECRET_DEL_SRC=$BUILDDIR/secret-delivery-src && "
            f"if [ ! -d \"$SECRET_DEL_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$SECRET_DEL_SRC\"; "
            f"fi && "
            f"cd \"$SECRET_DEL_SRC/init\" && "
            f"GOCACHE=$BUILDDIR/go-cache "
            f'go build -trimpath -ldflags "-s -w -buildid=" '
            f"-o ./build/secret-delivery ./cmd/main.go && "
            f"install -m 0755 ./build/secret-delivery "
            f"\"$DESTDIR/usr/bin/secret-delivery\""
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

        image.add_init_script(
            f"/usr/bin/secret-delivery"
            f" --method {self.method}"
            f" --port {self.port}\n",
            priority=30,
        )

        # Capture image's declared secrets for runtime validation
        for profile in image._iter_active_profiles():
            for secret in profile.secrets:
                if secret.name not in self.expected:
                    self.expected[secret.name] = secret

    def validate_payload(self, payload: dict[str, object]) -> SecretDeliveryValidation:
        """Validate an incoming secret payload against declared schemas."""
        errors: list[str] = []
        expected_names = set(self.expected)
        payload_names = set(payload)

        unknown = sorted(payload_names - expected_names)
        if unknown and self.reject_unknown:
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
        """Write validated secrets to the filesystem."""
        missing_required = tuple(
            sorted(
                name
                for name, spec in self.expected.items()
                if spec.required and name not in self._received
            ),
        )
        if self.completion == "all_required" and missing_required:
            raise ValidationError(
                "Cannot materialize before all required secrets are received.",
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
        if self.completion == "all_required":
            return not missing_required
        return bool(self._received)


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
