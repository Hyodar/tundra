"""The built-kernel fixture: the smallest recipe whose payload boots a kernel the build compiles.

``Kernel.build(version, source, config=)`` lowers to a composed kernel with every driver
built in: this recipe declares that kernel directly, from Debian's own config for the same
release (``DebianConfig``, the ``cloud-amd64`` flavor, which ``tundravm lock`` pins) and
:data:`FRAGMENT`, so the build records what ``olddefconfig`` does to Debian's ``=m`` drivers
under ``CONFIG_MODULES=n`` and its drivers check proves every target's are built in. It is a
``Recipe`` rather than an ``Image`` because an image builds the evidence service from
tundra-tools, which CI cannot read.

The ``default`` variant has the qemu, azure and gcp outputs. ``BootProbe`` prints the
kernel release, how many module files the root carries and which drivers and file systems
the kernel registered; CI's ``kernel`` workflow (``.github/workflows/kernel.yml``) builds
it and boots ``qemu.raw`` through ``tests/integration/test_kernel_boot.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm.declarative import (
    Debian,
    DebianConfig,
    Declaration,
    File,
    Git,
    Hardening,
    Kernel,
    KernelConfig,
    Mkosi,
    Recipe,
    Root,
    Task,
    Variant,
    component,
)
from tundravm.declarative.utils import Composite

SNAPSHOT = "20261018T082309Z"
SEED = "0f6b1c2e-7a5d-4c3b-9e81-2d4a6b8c0e13"
VERSION = "6.12.111"
"""The stable release Debian trixie's ``linux`` 6.12.111-1 packages: the newest whose
``linux-config-6.12`` :data:`SNAPSHOT`'s pool holds (6.12.113-1 is in the security pool
only, which ``DebianConfig`` does not read)."""

FRAGMENT = KernelConfig(
    {
        "CONFIG_DEBUG_INFO_NONE": "y",
        "CONFIG_DEBUG_INFO_DWARF_TOOLCHAIN_DEFAULT": None,
        "CONFIG_DEBUG_INFO_BTF": None,
    },
    name="kernel-proof",
    reason="no debug info: with every driver built in, DWARF and BTF cost an hour and gigabytes",
)
"""What the fixture changes in Debian's config: debug info only. The drivers are Debian's."""

DRIVERS = (
    "/sys/bus/virtio/drivers/virtio_net",
    "/sys/bus/virtio/drivers/virtio_blk",
    "/sys/bus/virtio/drivers/virtio_scsi",
    "/sys/bus/pci/drivers/nvme",
    "/sys/bus/pci/drivers/gve",
    "/sys/bus/pci/drivers/mana",
)
"""Drivers a built-in kernel registers on any machine (a Hyper-V VMBus driver registers only
on Hyper-V)."""
FILESYSTEMS = ("ext4", "erofs", "overlay")

PROBE = "/usr/lib/tundravm/boot-probe"
PROBE_SCRIPT = f"""\
#!/bin/sh
say() {{ echo "boot-probe: $*"; }}
say "release $(uname -r)"
say "module-files $(find /usr/lib/modules -name '*.ko*' 2>/dev/null | wc -l)"
drivers=""
for driver in {" ".join(DRIVERS)}; do
    [ -d "$driver" ] && drivers="$drivers ${{driver##*/}}"
done
say "drivers$drivers"
filesystems=""
for fs in {" ".join(FILESYSTEMS)}; do
    grep -qw "$fs" /proc/filesystems && filesystems="$filesystems $fs"
done
say "filesystems$filesystems"
say done
"""


@component
class BootProbe(Composite):
    """A boot task printing what the built kernel registered."""

    def compose(self) -> Iterator[Declaration]:
        yield File(PROBE, PROBE_SCRIPT, mode=0o755)
        yield Task(
            "boot-probe",
            ("/bin/sh", PROBE),
            description="Print what the built kernel registered",
            user=Root(),
            hardening=Hardening.none(),
        )


recipe = Recipe(
    name="kernel",
    platform=Debian("trixie", snapshot=SNAPSHOT, kernel_package=None),
    mkosi=Mkosi(seed=SEED),
    items=(
        Kernel(
            VERSION,
            Git("https://github.com/gregkh/linux", f"v{VERSION}"),
            base=DebianConfig(f"{VERSION}-1"),
            fragments=(FRAGMENT,),
        ),
        BootProbe(),
    ),
    variants=(Variant("default", outputs=("qemu", "azure", "gcp")),),
)
