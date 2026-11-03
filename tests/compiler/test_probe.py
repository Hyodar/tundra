"""The boot probe (plan 5.2 item 7): the checklist a development profile compiles in.

One image declaring a service that exposes a TCP and a UDP port under a preset sandbox, a
scheduled one, a packaged one, a volume, an egress filter, an ``Observability``, a built kernel and
``dev=DevAccess(...)``: its dev profile's ``/usr/lib/tundravm/probe`` is compared with
``probe-dev.sh`` beside this file (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it), and its
production profile carries none.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import pytest

import tundravm
from tundravm import Image, Kernel, Service
from tundravm._modules import probe
from tundravm.attestation import tagged
from tundravm.components import (
    DevAccess,
    Egress,
    JournalExport,
    Network,
    Observability,
    PrometheusRemoteWrite,
)
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
            Service.packaged("nginx.service"),
        ],
        observability=Observability(
            logs=JournalExport("https://logs.example:19532"),
            metrics=PrometheusRemoteWrite("https://10.0.0.9/api/v1/write", export="health"),
        ),
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


@pytest.mark.parametrize(
    ("answer", "caller", "verdict"),
    [("$nonce", "endpoint", "ok"), ("00" * 16, "endpoint", "fail"), ("$nonce", "signer", "fail")],
    ids=["answers-the-nonce", "answers-another", "quoted-for-another-caller"],
)
def test_the_endpoint_check_passes_only_on_evidence_for_its_nonce(
    tmp_path: Path, answer: str, caller: str, verdict: str
) -> None:
    """Against a stand-in ``curl`` printing the endpoint's JSON for the nonce it was asked,
    with a document tdxs quoted for *caller*."""
    user_data = base64.b64encode(tagged(caller, b"binding")).decode()
    document = json.dumps({"raw_quote": "", "user_data": user_data}, separators=(",", ":"))
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        "for arg; do case $arg in *nonce=*) nonce=${arg##*nonce=} ;; esac; done\n"
        f'printf \'{{"purpose":"tundravm-endpoint/1","nonce":"%s",'
        f'"document":"{document.encode().hex()}"}}\' "{answer}"\n'
    )
    curl.chmod(0o755)
    script = (
        probe._HEADER
        + probe._HELPERS["endpoint"].replace(f"-ge {probe.WAIT_SECONDS}", "-ge 0")
        + "check=endpoint:8745\nprobe_endpoint 8745 tundravm-endpoint.service\n"
    )
    done = subprocess.run(
        ["/bin/sh", "-c", script],
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.split()[:3] == [probe.PREFIX.strip(), "endpoint:8745", verdict]


@pytest.mark.parametrize(
    ("rootfs", "size", "call", "after"),
    [
        ("initramfs", None, "probe_rootfs initramfs", ()),
        ("initramfs", "4G", "probe_rootfs initramfs 4194304", ("systemd-remount-fs.service",)),
        ("initramfs", "50%", "probe_rootfs initramfs", ()),
        ("verity", None, "probe_rootfs verity", ()),
    ],
    ids=["initramfs", "capped", "percent", "verity"],
)
def test_the_root_check_is_what_rootfs_declared(
    rootfs: str, size: str | None, call: str, after: tuple[str, ...]
) -> None:
    """Every probe checks ``/``: the initrd's tmpfs, with the KiB cap the kernel reports
    for a sized one, or the volatile overlay on the verity root."""
    check = probe.derive((), modules=None, rootfs=rootfs, root_size=size).checks[2]
    assert (check.name, check.call, check.after) == (f"rootfs:{rootfs}", call, after)
