"""A kernel with a config of its own: gregkh/linux 6.13.12 from ``yocto.config``.

:data:`KERNEL` is ``Kernel.build(VERSION, Git(...), config=CONFIG)``: the config as the base,
Tundra's hardening fragment, :data:`QUOTES` and the gcp target's ACPI patch, with the same
``dev`` probe as :mod:`tests.fixtures.kernel.recipe`. The config is the yocto one the
nethermind-tdx images built from. CI's ``kernel`` workflow builds it as a second matrix
entry, so the hardening and the drivers check run on a config of their own as well as
Debian's.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.fixtures.kernel.recipe import kernel_image
from tundravm import Kernel, KernelFragment
from tundravm.sources import Git

VERSION = "6.13.12"
CONFIG = Path(__file__).with_name("yocto.config")
_LOCALVERSION = re.search(r'^CONFIG_LOCALVERSION="(.*)"$', CONFIG.read_text(), re.M)
RELEASE = VERSION + (_LOCALVERSION.group(1) if _LOCALVERSION else "")
"""What ``uname -r`` prints: the config appends its ``CONFIG_LOCALVERSION``."""

QUOTES = KernelFragment(
    {"CONFIG_CONFIGFS_FS": "y", "CONFIG_TSM_REPORTS": "y"},
    reason="the yocto config sets neither: configfs-tsm, the quote interface, needs both",
)
"""What the drivers check requires of every TDX guest and the yocto config leaves out."""

KERNEL = Kernel.build(
    VERSION,
    Git("https://github.com/gregkh/linux", f"v{VERSION}"),
    config=CONFIG,
    fragments=[QUOTES],
)

image = kernel_image("kernel-example", KERNEL)
