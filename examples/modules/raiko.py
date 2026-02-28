"""Built-in Raiko service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/raiko reference layout used in nethermind-tdx images.

Build: clones and compiles the Rust binary from source with reproducibility flags.
Runtime: systemd service, user/group creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tundravm.build_cache import Build, Cache
from tundravm.modules.resolve import resolve_after

if TYPE_CHECKING:
    from tundravm.image import Image

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
    features: str = "tdx"
    workspace_package: str = "raiko-host"
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
        clone_dir = Build.build_path("raiko")
        chroot_dir = Build.chroot_path("raiko")
        features_flag = f" --features {self.features}" if self.features else ""
        cache = Cache.declare(
            f"raiko-{self.source_branch}",
            (
                Cache.file(
                    src=Build.build_path(f"raiko/target/release/{self.workspace_package}"),
                    dest=Build.dest_path("usr/bin/raiko"),
                    name="raiko",
                ),
            ),
        )

        build_cmd = (
            f"git clone --depth=1 -b {self.source_branch} "
            f'{self.source_repo} "{clone_dir}" && '
            "mkosi-chroot bash -c '"
            "export "
            'RUSTFLAGS="-C target-cpu=generic -C link-arg=-Wl,--build-id=none '
            '-C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu" '
            "CARGO_HOME=/build/.cargo "
            "CARGO_PROFILE_RELEASE_LTO=thin "
            "CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 "
            "CARGO_PROFILE_RELEASE_PANIC=abort "
            "CARGO_PROFILE_RELEASE_INCREMENTAL=false "
            "CARGO_PROFILE_RELEASE_OPT_LEVEL=3 "
            "CARGO_TERM_COLOR=never "
            f"&& cd {chroot_dir} "
            "&& cargo fetch "
            f"&& cargo build --release --frozen{features_flag}"
            f" --package {self.workspace_package}"
            "'"
        )
        image.hook("build", cache.wrap(build_cmd))

    def _resolve_after(self, image: Image) -> tuple[str, ...]:
        return resolve_after(self.after, image)

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
            f"mkosi-chroot useradd --system --home-dir /home/{self.user} "
            f"--shell /usr/sbin/nologin --gid {self.group} {self.user}",
            phase="postinst",
        )
        image.service("raiko", enabled=True)

    def _render_service_unit(self, *, after: tuple[str, ...] | None = None) -> str:
        """Render raiko.service systemd unit."""
        effective = after if after is not None else self.after
        lines = ["[Unit]", "Description=Raiko"]
        if effective:
            lines.append(f"After={' '.join(effective)}")
            lines.append(f"Requires={' '.join(effective)}")
        lines.append("")
        lines.extend([
            "[Service]",
            f"User={self.user}",
            f"Group={self.group}",
            "Restart=on-failure",
            "ExecStart=/usr/bin/raiko",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ])
        return "\n".join(lines)
