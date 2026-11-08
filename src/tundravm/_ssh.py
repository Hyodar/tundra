"""``tundravm ssh``: the operator's ``ssh`` to an instance, from what tundravm already knows.

The address and the port come from the instance's receipt (a local instance forwards each
guest port from ``127.0.0.1``), and the host key is checked strictly against the
``known_hosts`` a passing ``attest`` wrote from the keys its quote binds (a local boot's
probe pins the same keys). The account comes from the build's tree: each ``Login``'s sshd
block on the host's sshd, and each container's own sshd (``ContainerSsh``) with its one
user and its guest port; ``root`` is a development build's alone. The key is the one
``attest --identity`` recorded for the instance, else ssh's own defaults. :func:`argv` is
what ``ssh`` runs; nothing passes through a shell.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import manifest as mf
from ._modules import container_ssh
from .builds import Build
from .declarative.utils import SSH_LOGIN_CONFIG
from .errors import StateError, ValidationError
from .instances import SSH_PORT, Instance, known_hosts

ROOT = "root"
"""The account a development build lets in by ``DevAccess(ssh_keys=)``."""
KEEPALIVE = ("-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2")
"""A session whose guest stops answering (a ``toggle`` that closes its port) ends in 30s."""


@dataclass(frozen=True, slots=True)
class Account:
    """One account an image lets in over SSH: its *name*, the guest *port* of the sshd
    that takes it and, for a ``ContainerSsh`` user, the *container* it runs in."""

    name: str
    port: int = SSH_PORT
    container: str | None = None

    def describe(self) -> str:
        return self.name if self.container is None else f"{self.name} (container, :{self.port})"


def accounts(build: Build) -> tuple[Account, ...]:
    """The build's accounts: its profile's ``Login`` names, sorted, then each container's
    sshd user, as the tree's sshd configurations name them."""
    tree = build.path / mf.TREE
    login = PurePosixPath(SSH_LOGIN_CONFIG)
    prefix, suffix = login.name.split("{name}")
    names = set()
    for path in tree.glob(f"**/mkosi.extra{login.parent}/{prefix}*{suffix}"):
        parts = path.relative_to(tree).parts
        if "mkosi.profiles" in parts and parts[parts.index("mkosi.profiles") + 1] != build.profile:
            continue
        names.add(path.name.removeprefix(prefix).removesuffix(suffix))
    found = [Account(name) for name in sorted(names)]
    for container in sorted(build.manifest.get("containers") or {}):
        configs = sorted(
            tree.glob(f"**/mkosi.images/{container}/mkosi.extra{container_ssh.CONFIG}")
        )
        if configs:
            settings = dict(
                line.split(None, 1) for line in configs[0].read_text().splitlines() if " " in line
            )
            found.append(Account(settings["AllowUsers"], int(settings["Port"]), container))
    return tuple(found)


def choose(instance: Instance, found: Sequence[Account], login: str | None) -> Account:
    """The account ``tundravm ssh`` logs in as: *login*, else the image's one ``Login``;
    ``root`` only on a development build."""
    if login == ROOT:
        kind = instance.data["build"]["kind"]
        if kind != "development":
            raise ValidationError(
                f"{instance.id} runs a {kind} build: root logs in only on a development build.",
                hint=f"Log in as the image's Login (`tundravm ssh {instance.id}`), or boot the "
                "dev profile, DevAccess(ssh_keys=[...]), with `tundravm dev`.",
            )
        return Account(ROOT)
    logins = [account for account in found if account.container is None]
    if login is None and len(logins) == 1:
        return logins[0]
    match = next((account for account in found if account.name == login), None)
    if match is not None:
        return match
    listed = ", ".join(account.describe() for account in found)
    if login is not None:
        what = f"no login {login!r}"
    elif logins:
        what = f"{len(logins)} logins"
    else:
        what = "no Login"
    raise ValidationError(
        f"{instance.id}'s image has {what}" + (f"; it lets in {listed}." if found else "."),
        hint=f"Name one: `tundravm ssh {instance.id} --login {found[0].name}`."
        if found
        else "Give the image SshAccess(login=Login(...)) and build it again.",
    )


def host_name(instance: Instance, port: int) -> str:
    """How ``known_hosts`` names *instance* on host *port*, as ``attest`` wrote it."""
    return instance.host if port == SSH_PORT else f"[{instance.host}]:{port}"


def require_known(instance: Instance, port: int) -> Path:
    """The instance's ``known_hosts``, refused unless it holds a key for host *port*."""
    hosts = known_hosts(instance)
    name = host_name(instance, port)
    lines = hosts.read_text(encoding="utf-8").splitlines() if hosts.is_file() else []
    if not any(name in line.split(None, 1)[0].split(",") for line in lines if line.strip()):
        what = "no known_hosts yet" if not lines else f"no host key for {name} in {hosts}"
        raise StateError(
            f"{instance.id} has {what}: ssh would have to trust a key on first use.",
            hint=f"Attest it first: `tundravm attest {instance.id} --policy POLICY` pins the host "
            "keys its quote binds.",
            context={"known_hosts": str(hosts)},
        )
    return hosts


def recorded_identity(instance: Instance) -> Path | None:
    """The key the last passing ``attest --identity`` recorded for *instance*."""
    operator = instance.data.get("operator")
    identity = operator.get("identity") if isinstance(operator, dict) else None
    return Path(identity) if isinstance(identity, str) else None


def argv(
    instance: Instance,
    account: Account,
    command: Sequence[str],
    *,
    identity: Path | None,
    tty: bool,
    batch: bool,
) -> list[str]:
    """``ssh`` to *account* on *instance* running *command*: strict host key checking
    against the instance's ``known_hosts``, *identity* alone when there is one, the
    keepalive always, ``-t`` when *tty* and ``BatchMode`` (no prompt) when *batch*."""
    port = instance.port(account.port) or account.port
    hosts = require_known(instance, port)
    found = ["ssh"]
    if tty:
        found.append("-t")
    if identity is not None:
        found += ["-i", str(identity), "-o", "IdentitiesOnly=yes"]
    if port != SSH_PORT:
        found += ["-p", str(port)]
    found += ["-o", f"UserKnownHostsFile={hosts}", "-o", "StrictHostKeyChecking=yes", *KEEPALIVE]
    if batch:
        found += ["-o", "BatchMode=yes"]
    return [*found, "--", f"{account.name}@{instance.host}", *command]


__all__ = [
    "KEEPALIVE",
    "ROOT",
    "Account",
    "accounts",
    "argv",
    "choose",
    "host_name",
    "recorded_identity",
]
