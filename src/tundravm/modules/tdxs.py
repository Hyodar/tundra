"""Built-in TDX attestation service (tdxs) module."""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING, Literal

from tundravm.build_cache import Build, Cache

if TYPE_CHECKING:
    from tundravm.image import Image

TDXS_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDXS_DEFAULT_REPO = "https://github.com/Hyodar/tundra-tools.git"
TDXS_DEFAULT_BRANCH = "master"
TDXS_DEFAULT_CONFIG_PATH = "/etc/tdxs/config.yaml"
TDXS_ROLE_TYPE_ALIASES = {
    "dcap": "tdx",
    "azure-tdx": "azure",
    "gcp-tdx": "gcp",
}
TDXS_VALID_TYPES = {"azure", "gcp", "simulator", "tdx"}


@dataclass(slots=True)
class Tdxs:
    """Configure the ``tundra-tools`` TDX attestation service."""

    issuer_type: (
        Literal["tdx", "azure", "gcp", "simulator", "dcap", "azure-tdx", "gcp-tdx"]
        | None
    ) = "tdx"
    validator_type: (
        Literal["tdx", "azure", "gcp", "simulator", "dcap", "azure-tdx", "gcp-tdx"]
        | None
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
    after: tuple[str, ...] = ()
    source_repo: str = TDXS_DEFAULT_REPO
    source_branch: str = TDXS_DEFAULT_BRANCH

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling tdxs."""
        image.build_install(*TDXS_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply tdxs build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _cache_key(self) -> str:
        repo_hash = hashlib.sha256(self.source_repo.encode("utf-8")).hexdigest()[:12]
        return f"tdxs-{repo_hash}-{self.source_branch}"

    def _canonical_role_type(self, value: str | None) -> str | None:
        if value is None:
            return None
        canonical = TDXS_ROLE_TYPE_ALIASES.get(value, value)
        if canonical not in TDXS_VALID_TYPES:
            choices = ", ".join(sorted(TDXS_VALID_TYPES | set(TDXS_ROLE_TYPE_ALIASES)))
            raise ValueError(f"Unsupported tdxs type {value!r}; expected one of: {choices}")
        return canonical

    def _add_build_hook(self, image: Image) -> None:
        clone_dir = Build.build_path("tdxs")
        chroot_dir = Build.chroot_path("tdxs")
        cache = Cache.declare(
            self._cache_key(),
            (
                Cache.file(
                    src=Build.build_path("tdxs/build/tdxs"),
                    dest=Build.dest_path("usr/bin/tdxs"),
                    name="tdxs",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {shlex.quote(self.source_branch)} "
            f'{shlex.quote(self.source_repo)} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            "mkdir -p ./build && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/tdxs ./cmd/tdxs"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        after = list(self.after)
        if image.init is not None and image.init.has_scripts:
            init_svc = image.init.service_name
            if init_svc not in after:
                after.insert(0, init_svc)
        return tuple(after)

    def _add_runtime_config(self, image: Image) -> None:
        resolved_after = self._resolve_after(image)
        image.file(self.config_path, content=self._render_config())

        service_path = f"/usr/lib/systemd/system/{self.service_name}"
        socket_path = f"/usr/lib/systemd/system/{self.socket_name}"
        image.file(service_path, content=self._render_service_unit(after=resolved_after))
        image.file(socket_path, content=self._render_socket_unit(after=resolved_after))

        image.run(
            f"mkosi-chroot groupadd --system {self.group}",
            phase="postinst",
        )
        image.run(
            f"mkosi-chroot useradd --system --home-dir /home/{self.user} "
            f"--shell /usr/sbin/nologin --gid {self.group} {self.user}",
            phase="postinst",
        )
        image.service(self.service_name, enabled=True)
        image.service(self.socket_name, enabled=True)

    def _render_config(self) -> str:
        lines = [
            "transport:",
            "  type: socket",
            "  config:",
            "    systemd: true",
        ]
        issuer_type = self._canonical_role_type(self.issuer_type)
        validator_type = self._canonical_role_type(self.validator_type)
        if issuer_type is None and validator_type is None:
            raise ValueError("Tdxs requires at least one of issuer_type or validator_type.")
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
                lines.append(f"  {key}: \"{value}\"")
        if self.check_revocations:
            lines.append("check_revocations: true")
        if self.get_collateral:
            lines.append("get_collateral: true")
        if validator_type == "azure" and self.verify_imds:
            lines.append("verify_imds: true")
        if validator_type == "gcp" and self.verify_identity_token:
            lines.append("verify_identity_token: true")
        return lines

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires = [*effective, self.socket_name]
        requires_line = " ".join(requires)
        return dedent(f"""\
            [Unit]
            Description=TDXS
            After={after_line}
            Requires={requires_line}

            [Service]
            User={self.user}
            Group={self.group}
            WorkingDirectory=/home/{self.user}
            Type=notify
            ExecStart=/usr/bin/tdxs \\
                --config {self.config_path} \\
                --log-level {self.log_level}
            Restart=on-failure

            [Install]
            WantedBy=default.target
        """)

    def _render_socket_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires_line = " ".join(effective)
        return dedent(f"""\
            [Unit]
            Description=TDXS Socket
            After={after_line}
            Requires={requires_line}

            [Socket]
            ListenStream={self.socket_path}
            SocketMode={self.socket_mode}
            SocketUser={self.socket_user}
            SocketGroup={self.group}
            Accept=false

            [Install]
            WantedBy=sockets.target
        """)
