"""The example's kernel: ``examples/nethermind_base.py``'s ``Kernel.build`` as a ``Recipe``.

The example's ``Image`` also builds the evidence service and the runtime tools from
``tools/``, which a kernel boot needs none of, so this recipe declares the kernel it lowers
to: gregkh/linux at the example's ``KERNEL_VERSION`` with its config as the base and nothing
else (``Kernel.build(config=)`` adds only ``CONFIG_MODULES=n``), and the same ``BootProbe`` as
:mod:`tests.fixtures.kernel.recipe`. CI's ``kernel`` workflow builds it as a second matrix
entry, so the drivers check runs on the example's config as well as Debian's.
"""

from __future__ import annotations

import re

from examples.nethermind_base import KERNEL_CONFIG, KERNEL_VERSION

from tests.fixtures.kernel.recipe import SEED, SNAPSHOT, BootProbe
from tundravm.declarative import Debian, Git, Kernel, Mkosi, Recipe, Variant

VERSION = KERNEL_VERSION
_LOCALVERSION = re.search(r'^CONFIG_LOCALVERSION="(.*)"$', KERNEL_CONFIG.read_text(), re.M)
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
            base=KERNEL_CONFIG,
        ),
        BootProbe(),
    ),
    variants=(Variant("default", outputs=("qemu", "azure", "gcp")),),
)
