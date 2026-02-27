"""Secret delivery module.

Declares expected runtime secrets and emits a small Python runtime helper that
accepts a single HTTP POST payload and writes secrets to file/env targets.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from tundravm.errors import ValidationError
from tundravm.models import SecretSchema, SecretSpec, SecretTarget
from tundravm.modules._tdx_init import (
    ensure_tdx_init_build,
    ensure_tdx_init_config,
    write_tdx_init_config,
)

if TYPE_CHECKING:
    from tundravm.image import Image

SECRET_DELIVERY_DEFAULT_REPO = "https://github.com/NethermindEth/nethermind-tdx"
SECRET_DELIVERY_DEFAULT_BRANCH = "main"
SECRET_DELIVERY_CONFIG_PATH = "/etc/tdx/secrets.json"
SECRET_DELIVERY_RUNTIME_PATH = "/usr/bin/secret-delivery"


@dataclass(slots=True)
class SecretDelivery:
    """Boot-time secret delivery phase.

    Declare secrets via ``.secret()``, then call ``.apply(img)`` to write
    ``/etc/tdx/secrets.json``, emit the runtime helper, and register the
    init script.
    """

    method: Literal["http_post"] = "http_post"
    port: int = 8080
    source_repo: str = SECRET_DELIVERY_DEFAULT_REPO
    source_branch: str = SECRET_DELIVERY_DEFAULT_BRANCH
    _secrets: list[SecretSpec] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def secret(
        self,
        name: str,
        *,
        required: bool = True,
        schema: SecretSchema | None = None,
        targets: tuple[SecretTarget, ...] = (),
    ) -> SecretSpec:
        """Declare an expected secret with validation schema and targets."""
        if not name:
            raise ValidationError(
                "secret() requires a non-empty secret name.",
            )
        if not targets:
            raise ValidationError(
                "secret() requires at least one delivery target.",
            )
        entry = SecretSpec(
            name=name,
            required=required,
            schema=schema,
            targets=targets,
        )
        self._secrets.append(entry)
        return entry

    def apply(self, image: Image) -> None:
        """Add required tooling, config file, helper binary, and init script."""
        ensure_tdx_init_build(
            image,
            source_repo=self.source_repo,
            source_ref=self.source_branch,
        )
        config = ensure_tdx_init_config(image)
        write_tdx_init_config(image, config)
        image.install("python3")

        self._add_config(image)
        self._add_runtime_helper(image)

        image.add_init_script(
            f"{SECRET_DELIVERY_RUNTIME_PATH}"
            f" --config {SECRET_DELIVERY_CONFIG_PATH}"
            f" --method {self.method}"
            f" --port {self.port}\n",
            priority=30,
        )

    def _add_runtime_helper(self, image: Image) -> None:
        image.file(
            SECRET_DELIVERY_RUNTIME_PATH,
            content=_render_runtime_secret_delivery_script(),
            mode="0755",
        )

    def _add_config(self, image: Image) -> None:
        """Write the JSON config file and register secrets on the image."""
        for profile in image._iter_active_profiles():
            for spec in self._secrets:
                profile.secrets.append(spec)

        secrets = {s.name: s for s in self._secrets}
        config = _render_config(
            secrets,
            method=self.method,
            port=self.port,
        )
        image.file(SECRET_DELIVERY_CONFIG_PATH, content=config)


def _render_config(
    secrets: dict[str, SecretSpec],
    *,
    method: str,
    port: int,
) -> str:
    """Serialize secret declarations to JSON for the runtime helper."""
    entries = []
    for spec in sorted(secrets.values(), key=lambda s: s.name):
        entry: dict[str, object] = {
            "name": spec.name,
            "required": spec.required,
        }
        if spec.schema is not None:
            schema: dict[str, object] = {"kind": spec.schema.kind}
            if spec.schema.min_length is not None:
                schema["min_length"] = spec.schema.min_length
            if spec.schema.max_length is not None:
                schema["max_length"] = spec.schema.max_length
            if spec.schema.pattern is not None:
                schema["pattern"] = spec.schema.pattern
            if spec.schema.enum:
                schema["enum"] = list(spec.schema.enum)
            entry["schema"] = schema

        targets = []
        for t in spec.targets:
            target: dict[str, str] = {"kind": t.kind, "location": t.location}
            if t.kind == "file":
                target["mode"] = t.mode
                if t.owner is not None:
                    target["owner"] = t.owner
            if t.kind == "env":
                target["scope"] = t.scope
            targets.append(target)
        entry["targets"] = targets
        entries.append(entry)

    payload = {
        "method": method,
        "port": port,
        "secrets": entries,
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _render_runtime_secret_delivery_script() -> str:
    return """#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any


def _read_config(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _normalize_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if "secrets" in payload and isinstance(payload["secrets"], dict):
        return dict(payload["secrets"])
    return dict(payload)


def _validate_secret(name: str, value: Any, schema: dict[str, Any] | None) -> str:
    if schema is None:
        if isinstance(value, str):
            return value
        return json.dumps(value, separators=(",", ":"), sort_keys=True)

    kind = schema.get("kind", "string")
    if kind == "json":
        if isinstance(value, str):
            json.loads(value)
            rendered = value
        else:
            rendered = json.dumps(value, separators=(",", ":"), sort_keys=True)
        return rendered

    if not isinstance(value, str):
        raise ValueError(f"secret {name} must be a string")

    min_length = schema.get("min_length")
    if min_length is not None and len(value) < int(min_length):
        raise ValueError(f"secret {name} shorter than min_length")
    max_length = schema.get("max_length")
    if max_length is not None and len(value) > int(max_length):
        raise ValueError(f"secret {name} longer than max_length")
    pattern = schema.get("pattern")
    if pattern and re.search(str(pattern), value) is None:
        raise ValueError(f"secret {name} does not match required pattern")
    enum = schema.get("enum")
    if enum is not None and value not in list(enum):
        raise ValueError(f"secret {name} is not one of allowed values")
    return value


def _write_file_target(target: dict[str, Any], rendered: str) -> None:
    path = Path(str(target["location"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")

    mode_value = str(target.get("mode", "0400"))
    os.chmod(path, int(mode_value, 8))

    owner = target.get("owner")
    if owner:
        import grp
        import pwd

        owner_text = str(owner)
        if ":" in owner_text:
            user_name, group_name = owner_text.split(":", 1)
        else:
            user_name, group_name = owner_text, owner_text
        uid = pwd.getpwnam(user_name).pw_uid
        gid = grp.getgrnam(group_name).gr_gid
        os.chown(path, uid, gid)


def _env_line(name: str, value: str) -> str:
    escaped = value.replace("\\\\", "\\\\\\\\").replace("\\"", "\\\\\\"").replace("\\n", "\\\\n")
    return f'{name}="{escaped}"'


def _write_env_targets(env_lines: dict[str, list[str]]) -> None:
    for scope, lines in env_lines.items():
        if not lines:
            continue
        if scope == "global":
            path = Path("/etc/environment.d/99-tdx-secrets.conf")
        else:
            path = Path("/run/tdx-secrets.env")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\\n".join(lines) + "\\n"
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)


def _apply_payload(config: dict[str, Any], payload: dict[str, Any]) -> None:
    env_targets: dict[str, list[str]] = {"global": [], "service": []}
    for entry in config.get("secrets", []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        if not name:
            continue
        required = bool(entry.get("required", True))
        if required and name not in payload:
            raise ValueError(f"missing required secret: {name}")
        if name not in payload:
            continue

        rendered = _validate_secret(name, payload[name], entry.get("schema"))
        targets = entry.get("targets", [])
        if not isinstance(targets, list):
            continue
        for target in targets:
            if not isinstance(target, dict):
                continue
            kind = target.get("kind")
            if kind == "file":
                _write_file_target(target, rendered)
                continue
            if kind == "env":
                env_name = str(target.get("location", ""))
                if not env_name:
                    continue
                scope = str(target.get("scope", "service"))
                if scope not in env_targets:
                    env_targets[scope] = []
                env_targets[scope].append(_env_line(env_name, rendered))
    _write_env_targets(env_targets)


def _handler_factory(config: dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        _config = config

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                normalized = _normalize_payload(payload)
                _apply_payload(self._config, normalized)
            except Exception as exc:  # noqa: BLE001
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(f"error: {exc}\\n".encode("utf-8"))
                self.server.delivery_error = str(exc)  # type: ignore[attr-defined]
                self.server.delivery_done = True  # type: ignore[attr-defined]
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\\n")
            self.server.delivery_done = True  # type: ignore[attr-defined]

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--method", default="http_post")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    config = _read_config(args.config)
    method = str(config.get("method", args.method))
    if method != args.method:
        raise SystemExit("config method does not match --method")
    if args.method != "http_post":
        raise SystemExit("only http_post is supported")

    handler = _handler_factory(config)
    server = HTTPServer(("0.0.0.0", args.port), handler)
    server.delivery_done = False  # type: ignore[attr-defined]
    server.delivery_error = None  # type: ignore[attr-defined]

    deadline = time.time() + max(args.timeout, 1)
    while time.time() < deadline:
        if getattr(server, "delivery_done", False):
            break
        server.timeout = max(deadline - time.time(), 0.1)
        server.handle_request()

    if not getattr(server, "delivery_done", False):
        print("error: timed out waiting for secrets", file=sys.stderr)
        return 1
    delivery_error = getattr(server, "delivery_error", None)
    if delivery_error:
        print(f"error: {delivery_error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""
