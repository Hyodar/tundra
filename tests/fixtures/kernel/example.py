"""A kernel with a config of its own: gregkh/linux 6.13.12 from ``yocto.config``.

:data:`KERNEL` is ``Kernel.build(VERSION, Git(...), config=CONFIG)``, lowered as in
:mod:`tests.fixtures.kernel.recipe`: the config as the base, Tundra's hardening fragment
and the gcp target's ACPI patch, with the same ``BootProbe``. The config is the yocto one
the nethermind-tdx images built from. CI's ``kernel`` workflow builds it as a second matrix
entry, so the hardening and the drivers check run on a config of their own as well as
Debian's.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.fixtures.kernel.recipe import SEED, SNAPSHOT, BootProbe, built_kernel
from tundravm import Kernel
from tundravm.declarative import Debian, Mkosi, Recipe, Variant
from tundravm.sources import Git

VERSION = "6.13.12"
CONFIG = Path(__file__).with_name("yocto.config")
_LOCALVERSION = re.search(r'^CONFIG_LOCALVERSION="(.*)"$', CONFIG.read_text(), re.M)
RELEASE = VERSION + (_LOCALVERSION.group(1) if _LOCALVERSION else "")
"""What ``uname -r`` prints: the config appends its ``CONFIG_LOCALVERSION``."""

KERNEL = Kernel.build(VERSION, Git("https://github.com/gregkh/linux", f"v{VERSION}"), config=CONFIG)

recipe = Recipe(
    name="kernel-example",
    platform=Debian("trixie", snapshot=SNAPSHOT, kernel_package=None),
    mkosi=Mkosi(seed=SEED),
    items=(built_kernel(KERNEL), BootProbe()),
    variants=(Variant("default", outputs=("qemu", "azure", "gcp")),),
)
