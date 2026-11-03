"""The built-kernel fixture: the smallest recipe whose payload boots a kernel the build compiles.

:data:`KERNEL` is ``Kernel.build(base=DebianConfig(...))`` as an image file writes it, and the
recipe declares what it lowers to in an image with the qemu, azure and gcp targets
(:func:`~tundravm._image._lower.kernel_of`): Debian's own config for the same release (the
``cloud-amd64`` flavor, which ``tundravm lock`` pins) and Tundra's hardening fragment,
every driver built in. So the build records what ``olddefconfig``
does to Debian's ``=m`` drivers under ``CONFIG_MODULES=n``, checks every hardening option
survived, and its drivers check proves every target's are built in. It is a ``Recipe``
rather than an ``Image`` because an image also builds the evidence service and the runtime
tools from ``tools/``, which a kernel boot needs none of.

The ``default`` variant has the qemu, azure and gcp outputs. ``BootProbe`` prints the
kernel release, how many module files the root carries and which drivers and file systems
the kernel registered; CI's ``kernel`` workflow (``.github/workflows/kernel.yml``) builds
it and boots ``qemu.raw`` through ``tests/integration/test_kernel_boot.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

from tundravm import Debian, DebianConfig, Image, Kernel
from tundravm import declarative as dm
from tundravm._image._lower import kernel_of
from tundravm.declarative import (
    Declaration,
    File,
    Hardening,
    Mkosi,
    Recipe,
    Root,
    Task,
    Variant,
    component,
)
from tundravm.declarative.model import Target
from tundravm.declarative.utils import Composite

SNAPSHOT = "20261018T082309Z"
SEED = "0f6b1c2e-7a5d-4c3b-9e81-2d4a6b8c0e13"
VERSION = "6.12.113"
"""The stable release Debian trixie's ``linux`` 6.12.113-1 packages: the newest
``linux-config-6.12`` at :data:`SNAPSHOT`, in its security pool only (the main pool has
6.12.111-1)."""
RELEASE = VERSION
"""What ``uname -r`` prints: Debian's config sets no ``CONFIG_LOCALVERSION``."""

KERNEL = Kernel.build(base=DebianConfig(f"{VERSION}-1"))
"""gregkh/linux ``v6.12.113`` from Debian's config, hardened, no loadable modules."""
TARGETS: tuple[Target, ...] = ("qemu", "azure", "gcp")


def built_kernel(kernel: Kernel) -> dm.Kernel:
    """What *kernel* lowers to in an image with :data:`TARGETS`."""
    return kernel_of(
        Image("kernel", base=Debian("trixie", snapshot=SNAPSHOT), kernel=kernel, targets=TARGETS)
    )


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
    platform=dm.Debian("trixie", snapshot=SNAPSHOT, kernel_package=None),
    mkosi=Mkosi(seed=SEED),
    items=(
        built_kernel(KERNEL),
        BootProbe(),
    ),
    variants=(Variant("default", outputs=("qemu", "azure", "gcp")),),
)
