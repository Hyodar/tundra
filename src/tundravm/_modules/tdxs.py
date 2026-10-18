"""Renders the tdxs config and unit files for the ``declarative.utils.Tdxs`` fragment."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from tundravm._units import v1
from tundravm.errors import ValidationError

if TYPE_CHECKING:
    from tundravm.declarative.model import Ready, Service

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
TSM_REPORT_DIRECTORY = "/sys/kernel/config/tsm/report"
"""configfs-tsm's report directory, where go-tdx-guest asks the kernel for a quote."""
TDX_GUEST_DEVICE = "/dev/tdx_guest"
"""The TDX guest device go-tdx-guest falls back to without configfs-tsm."""


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
        return v1.tdxs_service(
            socket=self.socket_name,
            user=self.user,
            group=self.group,
            config=self.config_path,
            log_level=self.log_level,
        )

    def render_socket_unit(self) -> str:
        return v1.tdxs_socket(
            path=self.socket_path, mode=self.socket_mode, user=self.socket_user, group=self.group
        )

    def service(self, needs: Sequence[Ready]) -> Service:
        """The typed socket-activated ``tdxs`` service (current dialect), waiting for *needs*.

        Least privilege from tundra-tools' source (unverified on TDX hardware):

        - Quotes go through go-tdx-guest v0.3.1: the configfs-tsm provider (``mkdir``,
          ``inblob`` write, ``outblob`` read under ``/sys/kernel/config/tsm/report``) when
          that directory exists, else ``/dev/tdx_guest``. ``ProtectKernelTunables=`` keeps
          ``/sys`` read-only, so the report directory is an optional ``ReadWritePaths=``
          entry (``-``: ignored on a guest without configfs-tsm) instead of ``kernel=False``;
          the device is the one ``DeviceAllow=`` (``DevicePolicy=closed``).
        - configfs-tsm entries are root-owned (``inblob`` is mode 0200), so the ``tdxs``
          user can only use them with ``CAP_DAC_OVERRIDE`` or as uid 0: the hardware run
          decides that grant; without it, issuing works only through the device fallback.
        - The socket comes from systemd and readiness goes to ``$NOTIFY_SOCKET``, both
          ``AF_UNIX``: ``RestrictAddressFamilies=AF_UNIX`` unless the issuer is ``azure`` or
          ``gcp`` (IMDS/metadata over HTTP) or a validator option fetches collateral,
          revocations or tokens over HTTPS.
        - No ``WorkingDirectory=/home/<user>``: ``ProtectHome=yes`` hides it, and tdxs
          reads its config by absolute path.
        """
        from tundravm.declarative.model import Listen, Service
        from tundravm.declarative.systemd import Hardening

        issuer = self._canonical_role_type(self.issuer_type)
        fetches = (
            self.check_revocations
            or self.get_collateral
            or self.verify_imds
            or self.verify_identity_token
        )
        online = fetches or issuer in ("azure", "gcp")
        return Service(
            self.service_name,
            ("/usr/bin/tdxs", "--config", self.config_path, "--log-level", self.log_level),
            description="TDXS",
            user=self.user,
            group=self.group,
            type="notify",
            socket=Listen(self.socket_path, mode=int(self.socket_mode, 8), group=self.group),
            write_paths=(f"-{TSM_REPORT_DIRECTORY}",),
            hardening=Hardening.strict(
                devices=(TDX_GUEST_DEVICE,), network="inet" if online else "unix"
            ),
            needs=needs,
        )
