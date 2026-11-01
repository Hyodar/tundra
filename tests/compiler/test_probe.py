"""The boot probe (plan 5.2 item 7): the checklist a development profile compiles in.

One image declaring a service that exposes a TCP and a UDP port under a preset sandbox, a
scheduled one, a volume, an egress filter, a built kernel and ``dev=DevAccess(...)``: its
dev profile's ``/usr/lib/tundravm/probe`` is compared with ``probe-dev.sh`` beside this
file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it), and its production profile carries none.
"""

from __future__ import annotations

import os
from pathlib import Path

import tundravm
from tundravm import Image, Kernel, Service
from tundravm._modules import probe
from tundravm.components import DevAccess, Egress, Network
from tundravm.declarative import Git
from tundravm.ports import TCP, UDP
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import Every, Hardening
from tundravm.testing._model import CompiledTree

GOLDEN = Path(__file__).with_name("probe-dev.sh")
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIProbeProbeProbeProbeProbeProbeProbeProbeProbe dev@probe"


def test_a_dev_profile_compiles_the_checks_its_declarations_imply(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("# CONFIG_MAGIC_SYSRQ is not set\nCONFIG_IO_URING=y\n")
    data = Volume("data", mount="/persistent", unlock=OperatorKey())
    image = Image(
        "probe",
        services=[
            Service(
                "app",
                ["/usr/bin/app"],
                expose=[TCP(8080), UDP(5353)],
                hardening=Hardening.preset("jit"),
                state=data.directory("app"),
            ),
            Service("tick", ["/usr/bin/tick"], timer=Every("15m")),
        ],
        volumes=[data],
        network=Network(egress=Egress(ports=[UDP(9), TCP(443)], hosts=["192.0.2.0/24"])),
        kernel=Kernel.build(
            "6.12.48", Git("https://example.com/linux.git", "v6.12.48"), config=config
        ),
        dev=DevAccess(ssh_keys=[KEY]),
    )
    compiled = tundravm.compile(image)
    compiled.write(tmp_path / "tree")
    tree = CompiledTree(tmp_path / "tree", compiled.profiles, "production")
    script = tree.read(f"mkosi.extra{probe.SCRIPT_PATH}", "dev")
    if os.environ.get("TUNDRAVM_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(script, encoding="utf-8")
    assert script == GOLDEN.read_text(encoding="utf-8")
    assert f"After={probe.UNIT}" not in tree.unit(probe.UNIT, "dev")
    assert not tree.exists(f"mkosi.extra{probe.SCRIPT_PATH}", "production")
    assert not tree.exists(f"mkosi.extra/usr/lib/systemd/system/{probe.UNIT}", "production")
