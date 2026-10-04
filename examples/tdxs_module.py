"""The TDX quote service as a fragment.

    tundravm inspect examples/tdxs_module.py

``tdxs()`` declares its build packages, the source build, ``config.yaml``, the
socket-activated units and the ``tdxs`` user in the ``tdx`` group.
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import Fragment, Package, Recipe, tdxs

recipe = Recipe(
    name="tdxs-module",
    base="debian/bookworm",
    common=Fragment("tdxs-module", items=(Package("ca-certificates"), tdxs())),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
