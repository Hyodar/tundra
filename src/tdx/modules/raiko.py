"""Built-in Raiko service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/raiko reference layout used in nethermind-tdx images.

Build: clones and compiles the Rust binary from source with reproducibility flags.
Runtime: systemd service, user/group creation.
"""

from __future__ import annotations

from dataclasses import dataclass
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
    after: tuple[str, ...] = ("runtime-init.service", "tdxs.service")

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
            f"if [ ! -d \"$RAIKO_SRC\" ]; then "
            f"git clone --depth=1 -b {self.source_branch} "
            f"{self.source_repo} \"$RAIKO_SRC\"; "
            f"fi && "
            f"cd \"$RAIKO_SRC\" && "
            f"CARGO_HOME=$BUILDDIR/cargo-home "
            f"CARGO_PROFILE_RELEASE_LTO=thin "
            f"CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 "
            f"CARGO_PROFILE_RELEASE_PANIC=abort "
            f"CARGO_PROFILE_RELEASE_OPT_LEVEL=3 "
            f'RUSTFLAGS="-C target-cpu=generic -C link-arg=-Wl,--build-id=none" '
            f"cargo build --release -p raiko-host && "
            f"install -m 0755 target/release/raiko-host \"$DESTDIR/usr/bin/raiko\""
        )
        image.hook("build", "sh", "-c", build_cmd, shell=True)

    def _add_runtime_config(self, image: Image) -> None:
        """Add runtime config, unit file, and user/group creation."""
        # Systemd unit file
        image.file(
            "/usr/lib/systemd/system/raiko.service",
            content=self._render_service_unit(),
        )

        # Config files if provided
        if self.config_path is not None:
            image.file("/etc/raiko/config.json", src=self.config_path)
        if self.chain_spec_path is not None:
            image.file("/etc/raiko/chain-spec.json", src=self.chain_spec_path)

        # User creation (postinst phase)
        image.run(
            "mkosi-chroot", "useradd", "--system",
            "--home-dir", f"/home/{self.user}",
            "--shell", "/usr/sbin/nologin",
            "--gid", self.group,
            self.user,
            phase="postinst",
        )

    def _render_service_unit(self) -> str:
        """Render raiko.service systemd unit."""
        after_line = " ".join(self.after)
        requires_line = " ".join(self.after)
        return (
            "[Unit]\n"
            "Description=Raiko\n"
            f"After={after_line}\n"
            f"Requires={requires_line}\n"
            "\n"
            "[Service]\n"
            f"User={self.user}\n"
            f"Group={self.group}\n"
            "Restart=on-failure\n"
            "ExecStart=/usr/bin/raiko\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )
