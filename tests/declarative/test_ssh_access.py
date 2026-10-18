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
    lower,
)
from tundravm.declarative.lifecycle import data_disk_serials
from tundravm.declarative.utils import SshAccess, TeeBase
from tundravm.errors import ValidationError

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample me@host"
TOOLS = RuntimeTools(Git("https://github.com/Hyodar/tundra-tools", "a" * 40))


def _files(*items: object) -> dict[str, str]:
    recipe = Recipe("ssh", platform=Debian("trixie"), items=(TeeBase(), *items))  # type: ignore[arg-type]
    tree = compile(recipe, variants=["default"])
    prefix = "default/mkosi.extra"
    return {
        e.path.removeprefix(prefix): (e.content or b"").decode()
        for e in tree.entries
        if e.path.startswith(prefix)
    }


def test_baked_keys_give_key_only_root_login_and_a_firewall_opening() -> None:
    files = _files(SshAccess(authorized_keys=(KEY,)))
    config = files["/etc/ssh/sshd_config.d/50-tundravm.conf"]
    assert "Port 22\n" in config and "PasswordAuthentication no\n" in config
    assert "PermitRootLogin prohibit-password\n" in config
    assert "AuthenticationMethods publickey\n" in config
    assert files["/etc/ssh/authorized_keys.d/root"] == f"{KEY}\n"
    assert 'tcp dport 22 accept comment "ssh.service"' in files["/etc/nftables.conf"]


def test_host_keys_are_made_at_boot_before_sshd() -> None:
    files = _files(SshAccess(authorized_keys=(KEY,)))
    task = files["/usr/lib/systemd/system/ssh-host-keys.service"]
    assert "ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key\n" in task
    assert "ExecStart=/usr/bin/ssh-keygen -A\n" in task and "ProtectSystem=yes\n" in task
    needs = files["/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf"]
    assert "After=ssh-host-keys.service\n" in needs and "Requires=ssh-host-keys.service\n" in needs


def test_from_secrets_waits_for_the_delivered_key() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", mount="/data", key=key, device="/dev/disk/by-id/virtio-tundra-data")
    files = _files(TOOLS, key, disk, Secrets(store=disk), SshAccess(port=2200, from_secrets=True))
    needs = files["/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf"]
    assert "runtime-init-secrets.service" in needs
    assert "Port 2200\n" in files["/etc/ssh/sshd_config.d/50-tundravm.conf"]
    assert "/etc/ssh/authorized_keys.d/root" not in files


def test_the_image_records_its_ssh_port_and_data_disk() -> None:
    key = Key("k", output="/run/k")
    disk = Disk("d", mount="/data", key=key, device="/dev/disk/by-id/virtio-tundra-data")
    items = (TeeBase(), TOOLS, key, disk, SshAccess(port=2200, authorized_keys=(KEY,)))
    img = lower(Recipe("ssh", platform=Debian("trixie"), items=items))
    assert img.ssh_ports == {"default": 2200}
    assert data_disk_serials(img, "default") == ("tundra-data",)
    assert lower(Recipe("plain", platform=Debian("trixie"), items=(TeeBase(),))).ssh_ports == {}


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
