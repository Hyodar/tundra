"""The built-kernel fixture: the smallest image whose payload boots a kernel the build compiles.

:data:`KERNEL` is ``Kernel.build(base=DebianConfig(...))``: Debian's own config for the same
release (the ``cloud-amd64`` flavor, which ``tundravm lock`` pins), Tundra's hardening
fragment and the gcp target's ACPI patch, every driver built in. So the build records what
``olddefconfig`` does to Debian's ``=m`` drivers under ``CONFIG_MODULES=n``, checks every
hardening option survived, and its drivers check proves every target's are built in.

The image targets qemu, azure and gcp. Its ``dev`` profile adds :data:`PROBE`, a service
printing the kernel release, how many module files the root carries and which drivers and
file systems the kernel registered; CI's ``kernel`` workflow
(``.github/workflows/kernel.yml``) builds that profile and boots ``qemu.raw`` through
``tests/integration/test_kernel_boot.py``.
"""

from __future__ import annotations

from tundravm import Debian, DebianConfig, File, Image, Kernel, Profile, Service
from tundravm.systemd import DropIn, Hardening

SNAPSHOT = "20261018T082309Z"
VERSION = "6.12.113"
"""The stable release Debian trixie's ``linux`` 6.12.113-1 packages: the newest
``linux-config-6.12`` at :data:`SNAPSHOT`, in its security pool only (the main pool has
6.12.111-1)."""
RELEASE = VERSION
"""What ``uname -r`` prints: Debian's config sets no ``CONFIG_LOCALVERSION``."""
KERNEL = Kernel.build(base=DebianConfig(f"{VERSION}-1"))
"""gregkh/linux ``v6.12.113`` from Debian's config, hardened, no loadable modules."""

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

DEV = Profile(
    add=[
        File.text(PROBE, PROBE_SCRIPT, mode=0o755),
        Service(
            "boot-probe",
            ["/bin/sh", PROBE],
            user="root",
            hardening=Hardening.relaxed(kernel=False),
            unit=DropIn(extra={"Service": {"StandardOutput": "journal+console"}}),
        ),
    ],
    production=False,
)
"""The probe, a root service under the relaxed sandbox with the kernel knobs off, so it sees
``/usr/lib/modules``; its lines reach the console. A production boot starts no service
without TDX, so the workflow boots this profile."""


def kernel_image(name: str, kernel: Kernel) -> Image:
    """The fixture image *name* booting *kernel*, with the qemu, azure and gcp targets."""
    return Image(
        name,
        base=Debian("trixie", snapshot=SNAPSHOT),
        kernel=kernel,
        targets=("qemu", "azure", "gcp"),
        profiles={"dev": DEV},
    )


image = kernel_image("kernel", KERNEL)
