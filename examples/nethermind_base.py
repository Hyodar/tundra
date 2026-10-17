"""Nethermind TDX base layer: the NethermindEth/nethermind-tdx base image as a platform.

Teaches building a real base image as a ``Platform`` plus one ``Composite``.
``NethermindPlatform`` is what every nethermind-tdx image builds on:

- Debian trixie from the pinned snapshot mirror, ``SOURCE_DATE_EPOCH=0``
- a UKI with its own ``/init`` (``boot="uki"``) and the EFI stub pinned from that mirror
- a TDX kernel built from source with the repository's config and a hardened command line
- reproducible output (fixed seed) and Debian backports generated at sync time

``NethermindBase`` declares what every image on it shares:

- skeleton files: the custom ``/init``, static DNS and DHCP network setup, read from
  ``nethermind-base/`` next to this module with ``Resources.from_module(__name__)``
- full systemd debloat (binary stripping, unit masking, path removal)
- the ``tdxs`` attestation service

Recipes built on them (``surge-tdx-prover/image.py``) set ``mkosi=NETHERMIND_V1`` to emit
the historical tree byte for byte. ``recipe`` below is the base layer on its own.

    tundravm inspect examples/nethermind_base.py
    tundravm lint examples/nethermind_base.py   # source-unpinned until locked
    tundravm compile examples/nethermind_base.py --tree build/nethermind-base/mkosi
"""

from __future__ import annotations

from dataclasses import dataclass

from tundravm.declarative import (
    Boot,
    Debian,
    Debloat,
    Declaration,
    File,
    Fragment,
    Git,
    Kernel,
    Mkosi,
    Package,
    Recipe,
    Resources,
    Setting,
)
from tundravm.declarative.utils import Backports, Composite, Tdxs

ASSETS = Resources.from_module(__name__)

PINNED_MIRROR = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
EFI_STUB_VERSION = "255.4-1"
KERNEL_VERSION = "6.13.12"
KERNEL_CONFIG = ASSETS.file("nethermind-base/kernel/kernel-yocto.config")
KERNEL_CMDLINE = (
    "console=tty0 console=ttyS0,115200n8 "
    "mitigations=auto,nosmt "
    "spec_store_bypass_disable=on "
    "nospectre_v2"
)
SEED = "630b5f72-a36a-4e83-b23d-6ef47c82fd9c"

NETHERMIND_V1 = Mkosi(dialect="nethermind-v1")
"""Compiler dialect that reproduces the historical nethermind-tdx tree."""

# NethermindPlatform's boot="uki" set brings kmod, systemd and systemd-boot-efi.
RUNTIME_PACKAGES = (
    "busybox",
    "util-linux",
    "procps",
    "ca-certificates",
    "openssl",
    "iproute2",
    "udhcpc",
    "e2fsprogs",
)

# Toolchains for the kernel and the services; stripped from the final image.
BUILD_PACKAGES = (
    "build-essential",
    "git",
    "curl",
    "cmake",
    "pkg-config",
    "clang",
    "cargo/sid",
    "flex",
    "bison",
    "elfutils",
    "bc",
    "perl",
    "gawk",
    "zstd",
    "libssl-dev",
    "libelf-dev",
)

# Host files ship next to this module, found wherever it is imported from.
TDX_INIT = ASSETS.text("nethermind-base/init")
RESOLV_CONF = ASSETS.text("nethermind-base/resolv.conf")  # no trailing newline
NETWORK_SETUP_SERVICE = ASSETS.text("nethermind-base/network-setup.service")


@dataclass(frozen=True, slots=True, kw_only=True)
class NethermindPlatform(Debian):
    """Debian trixie from *mirror*, with the TDX kernel, pinned EFI stub and build settings.

    *mirror* is a snapshot archive, so the EFI stub comes from the same snapshot as
    every package.
    """

    release: str = "trixie"
    mirror: str | None = PINNED_MIRROR
    tools_mirror: str | None = PINNED_MIRROR
    boot: Boot | None = "uki"
    kernel_package: str | None = None
    efi_stub: str | None = EFI_STUB_VERSION
    items: tuple[Declaration | Fragment, ...] = (
        Kernel(
            KERNEL_VERSION,
            Git("https://github.com/gregkh/linux", f"v{KERNEL_VERSION}"),
            config=KERNEL_CONFIG,
            cmdline=KERNEL_CMDLINE,
        ),
        Setting("Output", "Seed", (SEED,)),
        Setting("Output", "OutputDirectory", ("build",)),
        Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
        Setting("Build", "Environment", ("KERNEL_IMAGE", "KERNEL_VERSION")),
        Backports(),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class NethermindBase(Composite):
    """The nethermind-tdx base layer: /init, network setup, packages, debloat and tdxs."""

    def compose(self) -> Fragment:
        return Fragment(
            "nethermind-base",
            items=(
                File.text("/init", TDX_INIT, mode=0o755, stage="skeleton"),
                *(Package(name) for name in RUNTIME_PACKAGES),
                *(Package(name, role="build") for name in BUILD_PACKAGES),
                File.text("/etc/resolv.conf", RESOLV_CONF, stage="skeleton"),
                File.text(
                    "/etc/systemd/system/network-setup.service",
                    NETWORK_SETUP_SERVICE,
                    stage="skeleton",
                ),
                Debloat(),
                Tdxs(),
            ),
        )


recipe = Recipe(
    name="nethermind-tdx",
    platform=NethermindPlatform(),
    items=(NethermindBase(),),
    mkosi=NETHERMIND_V1,
)
