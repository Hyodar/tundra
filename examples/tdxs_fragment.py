"""The TDX quote service as a fragment.

    tundravm inspect examples/tdxs_fragment.py

``Tdxs()`` declares its build packages, the source build, ``config.yaml``, the
socket-activated units and the ``tdxs`` user in the ``tdx`` group.
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import Fragment, Package, Recipe
from tundravm.declarative.utils import Tdxs

recipe = Recipe(
    name="tdxs-fragment",
    base="debian/bookworm",
    common=Fragment("tdxs-fragment", items=(Package("ca-certificates"), Tdxs())),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
