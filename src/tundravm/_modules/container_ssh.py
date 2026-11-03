"""A container's sshd: this boot's host key and its delivered keys, made on the host.

``Container(ssh=ContainerSsh(...))`` lowers to :func:`unit`, a root oneshot the container's
sshd requires, which runs :func:`script_path`: it makes :func:`directory` with ``host/`` and
``keys/``, generates an ed25519 host key into ``host/`` once per boot, owned by the
container's user (mode 0600), and prints :data:`CONSOLE` with the container's name and the
key's fingerprint. The sshd unit binds both directories read-only at :data:`HOST_KEYS` and
:data:`AUTHORIZED_KEYS` inside the root; the attested endpoint writes a delivered key list
to :func:`authorized_keys` and binds :func:`host_keys` into every quote, labelled by the
container's name (``tools/pkg/endpoint``), so ``tundravm attest`` pins it in
``known_hosts``.
"""

from __future__ import annotations

DIRECTORY = "/run/tundravm/containers"
"""Where each container's ``host/`` and ``keys/`` live, under a directory named after it."""
CONFIG = "/etc/ssh/tundravm/sshd_config"
"""The container's sshd configuration, in its root."""
HOST_KEYS = "/etc/ssh/tundravm/host"
"""Where the container sees its host key directory (read-only)."""
AUTHORIZED_KEYS = "/etc/ssh/tundravm/keys"
"""Where the container sees its authorized keys directory (read-only)."""
KEY = "ssh_host_ed25519_key"
SHELL = "/bin/sh"
"""The login's shell in the root: sshd runs a command through it (``dash``, essential)."""
SSHD = ("/usr/sbin/sshd", "-D", "-e", "-f", CONFIG)
"""sshd in the foreground, logging to the journal, on :data:`CONFIG` alone."""
PACKAGE = "openssh-server"
"""What the root gains for sshd."""
HOST_PACKAGE = "openssh-client"
"""What the host gains for ``ssh-keygen``."""
CONSOLE = "tundravm-container-ssh: "
"""The console line's prefix; ``NAME SHA256:...`` (the host key's fingerprint) follows."""


def directory(name: str) -> str:
    """The host directory of container *name*'s SSH material."""
    return f"{DIRECTORY}/{name}"


def host_keys(name: str) -> str:
    """The glob of container *name*'s host public keys, which the endpoint binds."""
    return f"{directory(name)}/host/ssh_host_*_key.pub"


def keys_directory(name: str) -> str:
    """The host directory the endpoint writes container *name*'s delivered keys into."""
    return f"{directory(name)}/keys"


def authorized_keys(name: str) -> str:
    """Where the endpoint writes container *name*'s delivered keys."""
    return f"{keys_directory(name)}/authorized_keys"


def unit(name: str) -> str:
    """The oneshot making container *name*'s host key."""
    return f"{name}-ssh-keys.service"


def script_path(name: str) -> str:
    """The script :func:`unit` runs."""
    return f"/usr/lib/tundravm/container-ssh/{name}"


def binds(name: str, *, delivered: bool) -> tuple[str, ...]:
    """``BindReadOnlyPaths=`` of the sshd unit: the host key directory, and the keys
    directory when the keys are *delivered* (baked ones sit in the root)."""
    found = [f"{directory(name)}/host:{HOST_KEYS}"]
    if delivered:
        found.append(f"{keys_directory(name)}:{AUTHORIZED_KEYS}")
    return tuple(found)


def script(name: str, uid: int, gid: int) -> str:
    """The host's script for container *name*, whose user is *uid*:*gid*."""
    where = directory(name)
    key = f"{where}/host/{KEY}"
    return f"""\
#!/bin/sh
# tundravm Container {name}: this boot's SSH host key, owned by the container's user, and
# the directory its delivered keys land in; its sshd binds both read-only.
set -eu
mkdir -p -m 0711 /run/tundravm {DIRECTORY} {where}
mkdir -p -m 0755 {where}/host {where}/keys
if [ ! -s {key} ] || [ ! -s {key}.pub ]; then
    rm -f {key} {key}.pub
    ssh-keygen -q -t ed25519 -N '' -C {name} -f {key}
fi
chmod 0600 {key}
chown {uid}:{gid} {key}
echo "{CONSOLE}{name} $(ssh-keygen -l -f {key}.pub | cut -d ' ' -f 2)"
"""


def config(user: str, port: int) -> str:
    """The container's ``sshd_config``: its one user by key, nothing forwarded (dropbear's
    ``-s -w -g -j -k``)."""
    lines = [
        "# tundravm ContainerSsh: this container's user by key only, nothing forwarded",
        f"Port {port}",
        f"HostKey {HOST_KEYS}/{KEY}",
        f"AuthorizedKeysFile {AUTHORIZED_KEYS}/authorized_keys",
        f"AllowUsers {user}",
        "PermitRootLogin no",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitEmptyPasswords no",
        "DisableForwarding yes",
        "PermitTunnel no",
        "PermitUserEnvironment no",
        "PermitUserRC no",
        "UsePAM no",
        "PidFile none",
        "PrintMotd no",
        "StrictModes yes",
    ]
    return "\n".join(lines) + "\n"


__all__ = [
    "AUTHORIZED_KEYS",
    "CONFIG",
    "CONSOLE",
    "DIRECTORY",
    "HOST_KEYS",
    "HOST_PACKAGE",
    "KEY",
    "PACKAGE",
    "SHELL",
    "SSHD",
    "authorized_keys",
    "binds",
    "config",
    "directory",
    "host_keys",
    "keys_directory",
    "script",
    "script_path",
    "unit",
]
