"""The instance key: generated at boot in every profile, read by the services that ask.

The generator's script runs here too, with its paths moved under a temporary directory,
when the host has ``openssl``.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tundravm import Image, Profile
from tundravm import Service as ImageService
from tundravm._modules import execution as gate
from tundravm._modules import instance_key as key
from tundravm.components import DevAccess
from tundravm.testing import plan

IMAGE = Image(
    "demo",
    services=[ImageService("app", ["/usr/bin/app"])],
    profiles={"staging": Profile(production=False)},
    dev=DevAccess(password="tdx"),
)


@pytest.mark.parametrize("profile", ["production", "staging", "dev"])
def test_every_profile_generates_the_key_before_the_gate(profile: str) -> None:
    planned = plan(IMAGE, profile)
    unit = planned.unit(key.UNIT)
    assert unit.get("Unit", "Before") == gate.GATE_UNIT
    assert unit.get("Service", "ExecStart") == f"/bin/sh {key.SCRIPT_PATH}"
    assert unit.get("Service", "PrivateNetwork") == "yes"
    assert planned.file(key.SCRIPT_PATH).text == key.SCRIPT
    assert key.PACKAGE in planned.packages
    assert gate.GATE_UNIT not in (unit.get("Unit", "Requires") or "")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl on the host")
def test_the_script_makes_a_secp256k1_key_once_per_boot(tmp_path: Path) -> None:
    run = tmp_path / "run"
    script = key.SCRIPT.replace("/run/tundravm", str(run))
    said = [
        subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=True).stdout
        for _ in range(2)
    ]
    private, public = run / "instance-key.pem", run / "instance-key.pub"
    pub = public.read_text().strip()
    assert said == [f"{key.CONSOLE}{pub}\n"] * 2
    assert key.PUBLIC_KEY.fullmatch(pub)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    assert stat.S_IMODE(run.stat().st_mode) == 0o711
    assert private.read_text().startswith("-----BEGIN EC PRIVATE KEY-----\n")
    text = subprocess.run(
        ["openssl", "ec", "-in", str(private), "-noout", "-text"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "ASN1 OID: secp256k1" in text
