"""A profile's components contribute their egress.

An image filtering egress whose ``dev`` profile adds a component sending UDP 9 and to
``logs.example:19532``: the dev overlay carries its own ``nftables.conf``, compared with
``nftables-dev.conf`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it), the
shared payload keeps the image's, and the dev probe's egress check sends to a port the dev
rules refuse.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import tundravm
from tundravm import Image, Profile, Service
from tundravm._modules import probe
from tundravm.components import Component
from tundravm.components.network import Egress, Network
from tundravm.ports import UDP
from tundravm.testing._model import CompiledTree

GOLDEN = Path(__file__).with_name("nftables-dev.conf")
RULES = "mkosi.extra/etc/nftables.conf"


class Collector(Component):
    """A development collector: discard datagrams anywhere, logs to one receiver."""

    def compose(self) -> Iterator[object]:
        yield Service("collector", ["/usr/bin/collector"])

    def outbound(self) -> Egress:
        return Egress(ports=[UDP(9)], hosts=["logs.example:19532"])


def test_a_profiles_components_join_its_own_egress(tmp_path: Path) -> None:
    image = Image(
        "egress",
        network=Network(egress=Egress()),
        profiles={"dev": Profile(add=[Collector()], production=False)},
    )
    compiled = tundravm.compile(image)
    compiled.write(tmp_path / "tree")
    tree = CompiledTree(tmp_path / "tree", compiled.profiles, "production")
    rules = tree.read(RULES, "dev")
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(rules, encoding="utf-8")
    assert rules == GOLDEN.read_text(encoding="utf-8")
    assert tree.path(RULES, "dev") != tree.path(RULES, "production")
    assert "collector" not in tree.read(RULES, "production")
    script = tree.read(f"mkosi.extra{probe.SCRIPT_PATH}", "dev")
    assert "probe_egress 192.0.2.1 7" in script
