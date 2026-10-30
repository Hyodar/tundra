"""Nethermind TDX base layer: the NethermindEth/nethermind-tdx base image as an ``Image``.

Teaches building a real base image from the slots alone:

- ``base=Debian("trixie", snapshot=, efi_stub=)``: the archive snapshot every package and
  the UKI's EFI stub come from, so two builds see the same bytes;
- ``kernel=Kernel.build(version, Git(...), config=)``: the TDX kernel compiled from
  source with the repository's config, every driver built in (``Kernel.default()`` is
  Debian's, the tested one);
- ``packages=``: the runtime set nethermind-tdx images share. Build toolchains belong to
  the builds that need them (``tundravm.components.Nethermind`` and friends), never here.

Everything else the historical base carried is implied by every image now: NTS time
replaces openntpd, systemd-networkd and the firewall replace the custom ``/init`` and its
network script, and the evidence service is ``Tdxs()``. The surge prover
(``surge-tdx-prover/image.py``) builds on Debian's tested kernel instead.

    tundravm inspect examples/nethermind_base.py
    tundravm lint examples/nethermind_base.py   # source-unpinned until locked
    tundravm compile examples/nethermind_base.py --tree build/nethermind-base/mkosi
"""

from __future__ import annotations

from tundravm import Debian, Git, Image, Kernel
from tundravm.declarative import Resources

ASSETS = Resources.from_module(__name__)

SNAPSHOT = "20251113T083151Z"
EFI_STUB_VERSION = "255.4-1"
KERNEL_VERSION = "6.13.12"
KERNEL_CONFIG = ASSETS.file("nethermind-base/kernel/kernel-yocto.config")

RUNTIME_PACKAGES = [
    "busybox",
    "util-linux",
    "procps",
    "ca-certificates",
    "openssl",
    "iproute2",
    "e2fsprogs",
]

image = Image(
    "nethermind-tdx",
    base=Debian("trixie", snapshot=SNAPSHOT, efi_stub=EFI_STUB_VERSION),
    packages=RUNTIME_PACKAGES,
    kernel=Kernel.build(
        KERNEL_VERSION,
        Git("https://github.com/gregkh/linux", f"v{KERNEL_VERSION}"),
        config=KERNEL_CONFIG,
    ),
)
