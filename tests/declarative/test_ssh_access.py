"""``SshAccess``: openssh-server, boot-time host keys, key-only sshd, firewall and deploy wiring."""

from __future__ import annotations

import pytest

from tundravm.declarative import (
    Debian,
    Disk,
    Git,
    Key,
    Recipe,
    RuntimeTools,
    Secrets,
    compile,
    lint,
)
from tundravm.declarative.utils import SshAccess, TeeBase
from tundravm.errors import ValidationError

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@host"
TOOLS = RuntimeTools(Git("https://github.com/Hyodar/tundra-tools", "a" * 40))


def _files(*items: object) -> dict[str, str]:
    recipe = Recipe("ssh", platform=Debian("trixie"), items=(TeeBase(), *items))  # type: ignore[arg-type]
    tree = compile(recipe, variants=["default"])
    prefix = "payload/mkosi.extra"
    return {
        e.path.removeprefix(prefix): (e.content or b"").decode()
        for e in tree.entries
        if e.path.startswith(prefix)
    }


def test_from_secrets_waits_for_the_delivered_key() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", mount="/data", key=key, device="/dev/disk/by-id/virtio-tundra-data")
    files = _files(TOOLS, key, disk, Secrets(store=disk), SshAccess(port=2200, from_secrets=True))
    needs = files["/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf"]
    assert "runtime-init-secrets.service" in needs
    assert "Port 2200\n" in files["/etc/ssh/sshd_config.d/50-tundravm.conf"]
    assert "/etc/ssh/authorized_keys.d/root" not in files


def test_a_lone_ssh_access_lints_clean_with_tee_base() -> None:
    recipe = Recipe(
        "ssh", platform=Debian("trixie"), items=(TeeBase(), SshAccess(authorized_keys=(KEY,)))
    )
    assert [d.code for d in lint(recipe) if d.severity != "info"] == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "no key to accept"),
        ({"authorized_keys": ("not a key",)}, "not one OpenSSH public key"),
        ({"authorized_keys": KEY}, "not one string"),
        ({"port": 0, "from_secrets": True}, "not a port number"),
    ],
)
def test_a_bad_ssh_access_is_refused(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _files(SshAccess(**kwargs))  # type: ignore[arg-type]
