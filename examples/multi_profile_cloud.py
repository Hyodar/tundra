"""One image, three targets: each standalone variant carries its own guest agent.

tundravm inspect examples/multi_profile_cloud.py
tundravm bake examples/multi_profile_cloud.py
"""

from tundravm.backends import LimaMkosiBackend
from tundravm.declarative import Fragment, Package, Recipe, Variant


def _agent(variant: str, package: str) -> Fragment:
    return Fragment(variant, items=(Package(package),))


recipe = Recipe(
    name="multi-profile-cloud",
    base="debian/bookworm",
    common=Fragment("common"),
    variants=(
        Variant("qemu", parent=None, target="qemu", add=_agent("qemu", "qemu-guest-agent")),
        Variant("azure", parent=None, target="azure", add=_agent("azure", "waagent")),
        Variant("gcp", parent=None, target="gcp", add=_agent("gcp", "google-guest-agent")),
    ),
)

backend = LimaMkosiBackend(cpus=6, memory="12GiB", disk="100GiB")
