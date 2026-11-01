"""The instance key: generated at boot in every profile, read by the services that ask.

The generator's script runs here too, with its paths moved under a temporary directory,
when the host has ``openssl``.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tundravm import File, Image, Profile, Tree
from tundravm import Service as ImageService
from tundravm._modules import execution as gate
from tundravm._modules import instance_key as key
from tundravm.components import DevAccess
from tundravm.errors import ValidationError
from tundravm.secrets import Credential, InstanceKey
from tundravm.systemd import DropIn, Every
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


# ── delivery: InstanceKey() and nothing else ─────────────────────────────

KEY = InstanceKey()
BOUND = f"{key.CREDENTIAL}:{key.KEY}"


def test_instance_key_is_one_value_and_a_credential_naming_its_path_is_refused() -> None:
    assert KEY == InstanceKey() and repr(KEY) == "InstanceKey()"
    assert (KEY.name, KEY.source, KEY.path) == (
        key.CREDENTIAL,
        key.KEY,
        "${CREDENTIALS_DIRECTORY}/instance-key",
    )
    with pytest.raises(ValidationError, match=r"Pass InstanceKey\(\)"):
        Credential("signer", key.KEY)


@pytest.mark.parametrize(
    ("service", "exec_start", "environment"),
    [
        (
            ImageService("signer", ["/usr/bin/signer", "--key", KEY]),
            "/usr/bin/signer --key ${CREDENTIALS_DIRECTORY}/instance-key",
            None,
        ),
        (
            ImageService("signer", ["/usr/bin/signer"], environment={"KEY_FILE": KEY}),
            "/usr/bin/signer",
            "KEY_FILE=%d/instance-key",
        ),
        (ImageService("signer", ["/usr/bin/signer"], credentials=[KEY]), "/usr/bin/signer", None),
        (
            ImageService("signer", ["/usr/bin/signer", KEY], timer=Every("1h")),
            "/usr/bin/signer ${CREDENTIALS_DIRECTORY}/instance-key",
            None,
        ),
    ],
    ids=["argv", "environment", "credentials", "timer"],
)
def test_a_bound_service_loads_the_key_after_its_generation(
    service: ImageService, exec_start: str, environment: str | None
) -> None:
    unit = plan(Image("demo", services=[service]), "production").service("signer")
    assert unit.get("Service", "LoadCredential") == BOUND
    assert unit.get("Service", "ExecStart") == exec_start
    assert unit.get("Service", "Environment") == environment
    for directive in ("Requires", "After"):
        assert key.UNIT in (unit.get("Unit", directive) or "").split(), directive


def _drop_in(tmp: Path) -> dict[str, Any]:
    dropin = DropIn("30-key", extra={"Service": {"ReadOnlyPaths": key.KEY}})
    return {"services": [ImageService("app", ["/usr/bin/app"], unit=dropin)]}


@pytest.mark.parametrize(
    ("make", "subject"),
    [
        (
            lambda tmp: {"files": [File.text("/etc/app.conf", f"key = {key.KEY}\n")]},
            "/etc/app.conf",
        ),
        (lambda tmp: {"files": [Tree(tmp, "/etc/app")]}, "/etc/app/app.conf"),
        (_drop_in, "/usr/lib/systemd/system/app.service.d/30-key.conf"),
        (
            lambda tmp: {"services": [ImageService("app", ["/usr/bin/app", "--key", key.KEY])]},
            "app.service",
        ),
    ],
    ids=["file", "tree", "drop-in", "argv-word"],
)
def test_anything_else_naming_the_key_is_refused(
    tmp_path: Path, make: Callable[[Path], dict[str, Any]], subject: str
) -> None:
    (tmp_path / "app.conf").write_text(f"key = {key.KEY}\n")
    found = [
        d.subject
        for d in plan(Image("demo", **make(tmp_path)), "production").lint()
        if d.code == "instance-key-exposed"
    ]
    assert found == [subject]
    bound = Image("demo", services=[ImageService("app", ["/usr/bin/app", KEY])])
    assert not [d for d in plan(bound, "production").lint() if d.code == "instance-key-exposed"]
