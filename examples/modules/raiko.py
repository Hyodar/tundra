"""Built-in Raiko service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/raiko reference layout used in nethermind-tdx images.

Build: clones and compiles the Rust binary from source with reproducibility flags.
Runtime: systemd service, user/group creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tdx.image import Image

# Build packages required to compile raiko from source
RAIKO_BUILD_PACKAGES = (
    "build-essential",
    "pkg-config",
    "git",
    "clang",
    "libssl-dev",
    "libelf-dev",
)

RAIKO_DEFAULT_REPO = "https://github.com/NethermindEth/raiko.git"
RAIKO_DEFAULT_BRANCH = "feat/tdx"


@dataclass(slots=True)
class Raiko:
    """Configures the Raiko TDX prover service.

    Handles the full lifecycle:
      1. Build: declares build packages, adds build hook to clone
         and compile the raiko-host binary from source with reproducibility flags.
      2. Runtime: generates systemd service unit and creates system user.
    """

    source_repo: str = RAIKO_DEFAULT_REPO
    source_branch: str = RAIKO_DEFAULT_BRANCH
    config_path: str | None = None
    chain_spec_path: str | None = None
    user: str = "raiko"
    group: str = "tdx"
    after: tuple[str, ...] = ("tdxs.service",)

    def setup(self, image: Image) -> None:
        """Declare build-time package dependencies for compiling raiko."""
        image.build_install(*RAIKO_BUILD_PACKAGES)

    def install(self, image: Image) -> None:
        """Apply raiko build hook and runtime configuration to the image."""
        self._add_build_hook(image)
        self._add_runtime_config(image)

    def apply(self, image: Image) -> None:
        """Convenience: call setup() then install()."""
        self.setup(image)
        self.install(image)

    def _add_build_hook(self, image: Image) -> None:
        """Add build phase hook that clones and compiles raiko from source."""
        build_cmd = (
            f"RAIKO_SRC=$BUILDDIR/raiko-src && "
            f'if [ ! -d "$RAIKO_SRC" ]; then '
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "$RAIKO_SRC"; '
            f"fi && "
            f'cd "$RAIKO_SRC" && '
            f"CARGO_HOME=$BUILDDIR/cargo-home "
            f"CARGO_PROFILE_RELEASE_LTO=thin "
            f"CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 "
            f"CARGO_PROFILE_RELEASE_PANIC=abort "
            f"CARGO_PROFILE_RELEASE_OPT_LEVEL=3 "
            f'RUSTFLAGS="-C target-cpu=generic -C link-arg=-Wl,--build-id=none" '
            f"cargo build --release -p raiko-host && "
            f'install -m 0755 target/release/raiko-host "$DESTDIR/usr/bin/raiko"'
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        """Build the After= list, prepending the init service if available."""
        after = list(self.after)
        if image.init is not None and image.init.has_scripts:
            init_svc = image.init.service_name
            if init_svc not in after:
                after.insert(0, init_svc)
        return tuple(after)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user/group creation."""
        resolved_after = self._resolve_after(image)
        image.file(
            "/usr/lib/systemd/system/raiko.service",
            content=self._render_service_unit(after=resolved_after),
        )

        if self.config_path is not None:
            image.file("/etc/raiko/config.json", src=self.config_path)
        if self.chain_spec_path is not None:
            image.file("/etc/raiko/chain-spec.json", src=self.chain_spec_path)

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

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render raiko.service systemd unit."""
        effective = after if after is not None else self.after
        after_line = " ".join(effective)
        requires_line = " ".join(effective)
        return dedent(f"""\
            [Unit]
            Description=Raiko
            After={after_line}
            Requires={requires_line}

            [Service]
            User={self.user}
            Group={self.group}
            Restart=on-failure
            ExecStart=/usr/bin/raiko

            [Install]
            WantedBy=default.target
        """)
