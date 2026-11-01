"""A kernel with a config of its own: gregkh/linux 6.13.12 from ``yocto.config``.

``Kernel.build(VERSION, Git(...), config=CONFIG)`` lowers to this: the config as the base
and nothing else (``Kernel.build(config=)`` adds only ``CONFIG_MODULES=n``), with the same
``BootProbe`` as :mod:`tests.fixtures.kernel.recipe`. The config is the yocto one the
nethermind-tdx images built from. CI's ``kernel`` workflow builds it as a second matrix
entry, so the drivers check runs on a config of its own as well as Debian's.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.fixtures.kernel.recipe import SEED, SNAPSHOT, BootProbe
from tundravm.declarative import Debian, Git, Kernel, Mkosi, Recipe, Variant

VERSION = "6.13.12"
CONFIG = Path(__file__).with_name("yocto.config")
_LOCALVERSION = re.search(r'^CONFIG_LOCALVERSION="(.*)"$', CONFIG.read_text(), re.M)
RELEASE = VERSION + (_LOCALVERSION.group(1) if _LOCALVERSION else "")
"""What ``uname -r`` prints: the config appends its ``CONFIG_LOCALVERSION``."""

recipe = Recipe(
    name="kernel-example",
    platform=Debian("trixie", snapshot=SNAPSHOT, kernel_package=None),
    mkosi=Mkosi(seed=SEED),
    items=(
        Kernel(
            VERSION,
            Git("https://github.com/gregkh/linux", f"v{VERSION}"),
            base=CONFIG,
        ),
        BootProbe(),
    ),
    variants=(Variant("default", outputs=("qemu", "azure", "gcp")),),
)
