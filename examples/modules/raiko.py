"""Built-in Raiko service module.

Generates build pipeline, systemd unit, and user/group matching the
NethermindEth/raiko reference layout used in nethermind-tdx images.

Build: clones and compiles the Rust binary from source with reproducibility flags.
Runtime: systemd service, user/group creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from tundravm.modules import Module, Tdxs
from tundravm.modules.resolve import resolve_after
from tundravm.source import CargoBuild, GitSource, SourceBuild

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

# Reproducibility flags for the cargo build
RAIKO_CARGO_ENV = {
    "RUSTFLAGS": (
        "-C target-cpu=generic -C link-arg=-Wl,--build-id=none "
        "-C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu"
    ),
    "CARGO_HOME": "/build/.cargo",
    "CARGO_PROFILE_RELEASE_LTO": "thin",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "1",
    "CARGO_PROFILE_RELEASE_PANIC": "abort",
    "CARGO_PROFILE_RELEASE_INCREMENTAL": "false",
    "CARGO_PROFILE_RELEASE_OPT_LEVEL": "3",
    "CARGO_TERM_COLOR": "never",
}

RAIKO_DEFAULT_REPO = "https://github.com/NethermindEth/raiko.git"
RAIKO_DEFAULT_BRANCH = "feat/tdx"


@dataclass(slots=True)
class Raiko(Module):
    """Configures the Raiko TDX prover service.

    Handles the full lifecycle:
      1. Build: declares build packages, adds build hook to clone
         and compile the raiko-host binary from source with reproducibility flags.
      2. Runtime: generates systemd service unit and creates system user.

    Requires ``Tdxs``: the default unit orders after ``tdxs.service`` and the
    service user joins the ``tdx`` group that ``Tdxs`` creates.
    """

    requires: ClassVar[tuple[type[Module], ...]] = (Tdxs,)

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
        """Declare build packages and the raiko build hook."""
        image.build_install(*RAIKO_BUILD_PACKAGES)
        self._add_build_hook(image)

    def install(self, image: Image) -> None:
        """Declare runtime config, unit files, and the service user."""
        self._add_runtime_config(image)

    def source_spec(self) -> SourceBuild:
        """The raiko source build: ``cargo build --release`` of ``workspace_package``.

        The toolchain comes from the image's own build packages (``packages=()``),
        and ``cache_key``/``mark_unpinned`` keep the unpinned hook byte-identical to
        the hand-written one this module emitted before source builds existed.
        """
        return SourceBuild(
            name="raiko",
            source=GitSource(self.source_repo, self.source_branch),
            build=CargoBuild(
                output="raiko",
                package=self.workspace_package,
                features=(self.features,) if self.features else (),
                env=RAIKO_CARGO_ENV,
                packages=(),
            ),
            install_to="/usr/bin/raiko",
            cache_key=f"raiko-{self.source_branch}",
            mark_unpinned=False,
        )

    def _add_build_hook(self, image: Image) -> None:
        """Add the build phase hook that clones and compiles raiko from source."""
        image.source_build(self.source_spec())

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
        lines.extend(
            [
                "[Service]",
                f"User={self.user}",
                f"Group={self.group}",
                "Restart=on-failure",
                "ExecStart=/usr/bin/raiko",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            ]
        )
        return "\n".join(lines)
