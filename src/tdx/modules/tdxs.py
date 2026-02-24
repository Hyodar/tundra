"""Built-in TDX quote service (tdxs) module.

Generates build pipeline, config, systemd units, and user/group matching the
NethermindEth/tdxs reference layout used in nethermind-tdx images.

Build: clones and compiles the Go binary from source.
Runtime: config.yaml, systemd service + socket activation, user/group.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING

from tdx.build_cache import Build, Cache

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile tdxs from source
TDXS_BUILD_PACKAGES = (
    "golang",
    "git",
    "build-essential",
)

TDXS_DEFAULT_REPO = "https://github.com/NethermindEth/tdxs"
TDXS_DEFAULT_BRANCH = "master"


@dataclass(slots=True)
class Tdxs:
    """Configures the tdxs TDX quote issuer/validator service.

    Handles the full lifecycle:
      1. Build: declares build packages (Go, git), adds build hook to clone
         and compile the tdxs binary from source.
      2. Runtime: generates /etc/tdxs/config.yaml, systemd service + socket
         units, user/group creation, and socket enablement.
    """

    issuer_type: str = "dcap"
    socket_path: str = "/var/tdxs.sock"
    user: str = "tdxs"
    group: str = "tdx"
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

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles tdxs from source."""
        clone_dir = Build.build_path("tdxs")
        chroot_dir = Build.chroot_path("tdxs")
        cache = Cache.declare(
            f"tdxs-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path("tdxs/build/tdxs"),
                    dest=Build.dest_path("usr/bin/tdxs"),
                    name="tdxs",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            f"cd {chroot_dir} && "
            "make sync-constellation && "
            'go build -trimpath -ldflags "-s -w -buildid=" '
            "-o ./build/tdxs ./cmd/tdxs/main.go"
            "'"
        )
        image.hook("build", "sh", "-c", cache.wrap(build_cmd), shell=True)

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        """Build the After= list, prepending the init service if available."""
        after = list(self.after)
        if image.init is not None and image.init.has_scripts:
            init_svc = image.init.service_name
            if init_svc not in after:
                after.insert(0, init_svc)
        return tuple(after)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit files, user/group, and service enablement."""
        resolved_after = self._resolve_after(image)
        image.file("/etc/tdxs/config.yaml", content=self._render_config())

        image.file(
            "/usr/lib/systemd/system/tdxs.service",
            content=self._render_service_unit(after=resolved_after),
        )
        image.file(
            "/usr/lib/systemd/system/tdxs.socket",
            content=self._render_socket_unit(after=resolved_after),
        )

        image.run(
            "mkosi-chroot",
            "groupadd",
            "--system",
            self.group,
            phase="postinst",
        )
        image.run(
            "mkosi-chroot",
            "useradd",
            "--system",
            "--home-dir",
            f"/home/{self.user}",
            "--shell",
            "/usr/sbin/nologin",
            "--gid",
            self.group,
            self.user,
            phase="postinst",
        )
        image.run(
            "mkosi-chroot",
            "systemctl",
            "enable",
            "tdxs.socket",
            phase="postinst",
        )

    def _render_config(self) -> str:
        """Render /etc/tdxs/config.yaml content."""
        return dedent(f"""\
            transport:
              type: socket
              config:
                systemd: true

            issuer:
              type: {self.issuer_type}
        """)

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render tdxs.service systemd unit."""
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires_line = " ".join((*effective, "tdxs.socket"))
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
                --config /etc/tdxs/config.yaml
            Restart=on-failure

            [Install]
            WantedBy=default.target
        """)

    def _render_socket_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render tdxs.socket systemd unit."""
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
            SocketMode=0660
            SocketUser=root
            SocketGroup={self.group}
            Accept=false

            [Install]
            WantedBy=sockets.target
        """)
