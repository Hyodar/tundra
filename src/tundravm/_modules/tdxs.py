"""Renders the tdxs config and unit files for the ``declarative.utils.Tdxs`` fragment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tundravm.errors import ValidationError

TDXS_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDXS_DEFAULT_CONFIG_PATH = "/etc/tdxs/config.yaml"
TDXS_ROLE_TYPE_ALIASES = {
    "dcap": "tdx",
    "azure-tdx": "azure",
    "gcp-tdx": "gcp",
}
TDXS_VALID_TYPES = {"azure", "gcp", "simulator", "tdx"}


@dataclass(slots=True)
class Tdxs:
    """The ``tundra-tools`` TDX attestation service's config and unit files."""

    issuer_type: (
        Literal["tdx", "azure", "gcp", "simulator", "dcap", "azure-tdx", "gcp-tdx"] | None
    ) = "tdx"
    validator_type: (
        Literal["tdx", "azure", "gcp", "simulator", "dcap", "azure-tdx", "gcp-tdx"] | None
    ) = None
    socket_path: str = "/var/tdxs.sock"
    socket_mode: str = "0660"
    socket_user: str = "root"
    user: str = "tdxs"
    group: str = "tdx"
    service_name: str = "tdxs.service"
    socket_name: str = "tdxs.socket"
    config_path: str = TDXS_DEFAULT_CONFIG_PATH
    log_level: Literal["debug", "info", "warn", "error"] = "info"
    check_revocations: bool = False
    get_collateral: bool = False
    verify_imds: bool = False
    verify_identity_token: bool = False
    expected_measurements: dict[str, str] | None = None

    def _canonical_role_type(self, value: str | None) -> str | None:
        if value is None:
            return None
        canonical = TDXS_ROLE_TYPE_ALIASES.get(value, value)
        if canonical not in TDXS_VALID_TYPES:
            choices = ", ".join(sorted(TDXS_VALID_TYPES | set(TDXS_ROLE_TYPE_ALIASES)))
            raise ValidationError(
                f"Unsupported tdxs type {value!r}.",
                hint=f"Expected one of: {choices}",
            )
        return canonical

    def render_config(self) -> str:
        lines = [
            "transport:",
            "  type: socket",
            "  config:",
            "    systemd: true",
        ]
        issuer_type = self._canonical_role_type(self.issuer_type)
        validator_type = self._canonical_role_type(self.validator_type)
        if issuer_type is None and validator_type is None:
            raise ValidationError(
                "Tdxs requires at least one of issuer_type or validator_type.",
                hint="Set issuer_type and/or validator_type when constructing Tdxs.",
            )
        if issuer_type is not None:
            lines.extend(("issuer:", f"  type: {issuer_type}"))
        if validator_type is not None:
            lines.extend(("validator:", f"  type: {validator_type}"))
            config_lines = self._validator_config_lines(validator_type)
            if config_lines:
                lines.append("  config:")
                lines.extend(f"    {line}" for line in config_lines)
        return "\n".join(lines) + "\n"

    def _validator_config_lines(self, validator_type: str) -> list[str]:
        lines: list[str] = []
        if self.expected_measurements:
            lines.append("expected_measurements:")
            for key, value in sorted(self.expected_measurements.items()):
                lines.append(f'  {key}: "{value}"')
        if self.check_revocations:
            lines.append("check_revocations: true")
        if self.get_collateral:
            lines.append("get_collateral: true")
        if validator_type == "azure" and self.verify_imds:
            lines.append("verify_imds: true")
        if validator_type == "gcp" and self.verify_identity_token:
            lines.append("verify_identity_token: true")
        return lines

    def render_service_unit(self) -> str:
        lines = ["[Unit]", "Description=TDXS", f"Requires={self.socket_name}"]
        lines.append("")
        lines.extend(
            [
                "[Service]",
                f"User={self.user}",
                f"Group={self.group}",
                f"WorkingDirectory=/home/{self.user}",
                "Type=notify",
                "ExecStart=/usr/bin/tdxs \\",
                f"    --config {self.config_path} \\",
                f"    --log-level {self.log_level}",
                "Restart=on-failure",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        return "\n".join(lines)

    def render_socket_unit(self) -> str:
        lines = ["[Unit]", "Description=TDXS Socket", ""]
        lines.extend(
            [
                "[Socket]",
                f"ListenStream={self.socket_path}",
                f"SocketMode={self.socket_mode}",
                f"SocketUser={self.socket_user}",
                f"SocketGroup={self.group}",
                "Accept=false",
                "",
                "[Install]",
                "WantedBy=sockets.target",
                "",
            ]
        )
        return "\n".join(lines)
