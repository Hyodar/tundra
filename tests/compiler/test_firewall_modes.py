"""Firewall modes: the ruleset a switchable firewall renders and what it refuses.

An image whose firewall has a ``maintenance`` mode opening SSH: its ``nftables.conf`` is
compared with ``nftables-modes.conf`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1``
rewrites it), port 22 opens in that mode's chain only, ``mode-switch`` ships with its
configuration, and the attested endpoint runs to take the switch.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

import tundravm
from tundravm import DebianConfig, Image, Kernel
from tundravm.components.access import Choice, Sudo
from tundravm.components.network import Egress, Firewall, Mode, Network
from tundravm.declarative._network import expand_firewall
from tundravm.declarative.model import EgressRule, FirewallMode, Port
from tundravm.declarative.model import Firewall as Ruleset
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.testing import plan
from tundravm.testing._model import CompiledTree

GOLDEN = Path(__file__).with_name("nftables-modes.conf")
RULES = "mkosi.extra/etc/nftables.conf"

MODES = {
    "production": Mode(),
    "maintenance": Mode(ingress=[TCP(22)], egress=Egress(hosts=["deb.debian.org:443"])),
}


def test_a_switchable_firewall_opens_ssh_in_maintenance_only(tmp_path: Path) -> None:
    image = Image(
        "modes",
        network=Network(egress=Egress(ports=[TCP(443)])),
        components=[Firewall(modes=MODES)],
    )
    compiled = tundravm.compile(image)
    compiled.write(tmp_path / "tree")
    tree = CompiledTree(tmp_path / "tree", compiled.profiles, "production")
    rules = tree.read(RULES, "production")
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(rules, encoding="utf-8")
    assert rules == GOLDEN.read_text(encoding="utf-8")
    config = json.loads(tree.read("mkosi.extra/etc/tundravm/modes.json", "production"))
    assert config["production_build"] is True
    assert config["modes"][1] == {"name": "maintenance", "mark": 2, "lasts": 1800}
    unit = "mkosi.extra/usr/lib/systemd/system/mode-switch.service"
    assert "CapabilityBoundingSet=CAP_NET_ADMIN" in tree.read(unit, "production")
    assert "tcp dport 8745 accept" in rules


@pytest.mark.parametrize(
    ("build", "problem"),
    [
        (lambda: Firewall(modes={"maintenance": Mode(ingress=[TCP(22)])}), "no production mode"),
        (lambda: Firewall(modes={"production": Mode(lasts="1h")}), "production mode closes"),
        (
            lambda: Firewall(modes={"production": Mode(), "tdx": Mode()}),
            "a word of the security mode",
        ),
        (lambda: Mode(lasts="soon"), "not a systemd time span"),
        (lambda: Mode(egress=Egress(metadata=True)), "cloud metadata server"),
        (
            lambda: Ruleset(modes=(FirewallMode("production", ingress=(Port(22),)),)),
            "production mode opens port 22",
        ),
        (
            lambda: Ruleset(
                modes=(FirewallMode("production"), FirewallMode("debug", ingress=(Port(9),)))
            ),
            "never closes",
        ),
        (
            lambda: expand_firewall(
                Ruleset(
                    modes=(
                        FirewallMode("production"),
                        FirewallMode("open", egress_rules=(EgressRule(Port(443)),), lasts=60),
                    )
                ),
                (),
            ),
            "filters nothing outbound",
        ),
    ],
)
def test_what_a_switchable_firewall_refuses(build: Callable[[], object], problem: str) -> None:
    with pytest.raises(ValidationError, match=problem):
        build()


SWITCHABLE = {"production": Mode(), "maintenance": Mode(ingress=[TCP(22)])}


@pytest.mark.parametrize(
    ("kernel", "modes", "warned"),
    [
        (Kernel.default(), SWITCHABLE, True),
        (Kernel.build(base=DebianConfig("6.12.113-1")), SWITCHABLE, True),
        (
            Kernel.build(base=DebianConfig("6.18.15-1~bpo13+1", snapshot="20261003T143013Z")),
            SWITCHABLE,
            False,
        ),
        (Kernel.default(), {}, False),
    ],
    ids=["debian-6.12", "built-6.12", "backports-6.18", "no-modes"],
)
def test_modes_on_a_kernel_without_rtmr3_extends_are_warned(
    kernel: Kernel, modes: dict[str, Mode], warned: bool
) -> None:
    image = Image("modes", kernel=kernel, components=[Firewall(modes=modes)])
    found = [d for d in plan(image).lint() if d.code == "firewall-modes-kernel"]
    assert [
        (d.severity, "Linux 6.12" in d.message, "Kernel.build(" in (d.hint or "")) for d in found
    ] == ([("warning", True, True)] if warned else [])


THREE = {"production": Mode(), "debug": Mode(ingress=[TCP(22)]), "sync": Mode(ingress=[TCP(9)])}


@pytest.mark.parametrize(
    ("modes", "entry", "command"),
    [
        (MODES, "status", Sudo("/usr/bin/mode-switch", "status")),
        (
            MODES,
            "switch",
            Sudo("/usr/bin/mode-switch", "to", Choice("mode", "production", "maintenance")),
        ),
        (THREE, "status", Sudo("/usr/bin/mode-switch", "status")),
        (
            THREE,
            "switch",
            Sudo("/usr/bin/mode-switch", "to", Choice("mode", "production", "debug", "sync")),
        ),
        ({}, "status", None),
        ({}, "switch", None),
    ],
)
def test_a_switchable_firewall_hands_out_its_login_commands(
    modes: dict[str, Mode], entry: str, command: Sudo | None
) -> None:
    firewall = Firewall(modes=modes)
    if command is None:
        with pytest.raises(ValidationError, match="no modes"):
            getattr(firewall, entry)
        return
    assert getattr(firewall, entry) == command
