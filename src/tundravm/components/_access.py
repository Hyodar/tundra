"""The ways in for people: ``SshAccess`` (root, and each non-root ``Login`` with its
``Command`` values) and ``DevAccess`` (``image.dev``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path

from tundravm._image._component import Component
from tundravm._image._values import (
    TCP,
)
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.declarative._network import MODES_CONFIG
from tundravm.declarative.utils import Command, CommandParam, Login, ParamType
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError

from ._base import _diagnostic

__all__ = [
    "Command",
    "CommandParam",
    "DevAccess",
    "Login",
    "ParamType",
    "SshAccess",
]

_SSH_KEY = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\n]*)?"
)


class SshAccess(Component):
    """Root logs in over SSH with a public key, and with nothing else.

    *keys* are public keys baked into the image; *from_delivery* accepts the key the
    attested endpoint takes once ``tundravm attest`` verified the quote binding the host
    keys. On a TDX boot the endpoint takes that key only from an owner the encrypted copy
    of an ``OperatorKey()`` volume confirmed, so a TDX image without one gets no delivered
    key at all: bake it with *keys*. *login* is an account other than root, or a list of
    them: a :class:`Login` gets in with its own keys, or the owner's (``owner=True``), into
    the program of its commands, its root commands through exact sudoers lines, and
    nothing else. An
    image whose ``SshAccess`` declares a login hands root no delivered key unless
    *from_delivery* says so, and root without a key never logs in. A key or a login is
    required. The port is opened in the firewall; host keys are generated per instance at
    boot. Exports :attr:`port`. Component name: ``ssh-access``.
    """

    keys: Sequence[str] = ()
    from_delivery: bool = False
    listen_port: int = 22
    login: Login | Sequence[Login] = ()

    def __post_init__(self) -> None:
        if isinstance(self.keys, str):
            raise ValidationError(
                "SshAccess: keys= takes a list of public keys.",
                hint="Pass keys=['ssh-ed25519 AAAA... me@host'].",
            )
        object.__setattr__(self, "keys", tuple(key.strip() for key in self.keys))
        for key in self.keys:
            if not _SSH_KEY.fullmatch(key):
                raise ValidationError(
                    f"SshAccess: {key!r} is not one OpenSSH public key line.",
                    hint="Paste the .pub file's line: 'ssh-ed25519 AAAA... comment'.",
                )
        logins = (self.login,) if isinstance(self.login, Login) else self.login
        if isinstance(logins, str) or not all(isinstance(item, Login) for item in logins):
            raise ValidationError(
                f"SshAccess: login= takes a Login or a list of them, got {self.login!r}.",
                hint='Pass login=Login("tenant", keys=[...], '
                'commands={"status": Command(runs=[...])}).',
            )
        object.__setattr__(self, "login", tuple(logins))
        if not self.keys and not self.from_delivery and not self.login:
            raise ValidationError(
                "SshAccess has no key to accept, so nobody could log in.",
                hint="Pass keys=[...], from_delivery=True, or login=Login(...).",
            )

    @property
    def port(self) -> TCP:
        """The port ``sshd`` listens on."""
        return TCP(self.listen_port)

    def compose(self) -> Iterator[object]:
        access = du.SshAccess(
            port=self.listen_port,
            authorized_keys=tuple(self.keys),
            from_secrets=self.from_delivery,
            logins=self.logins,
        )
        yield from access.items

    @property
    def logins(self) -> tuple[Login, ...]:
        """Every :class:`Login`, in the order given."""
        assert isinstance(self.login, tuple)
        return self.login

    def built_in(self, development: Sequence[str]) -> SshAccess:
        """The access in an image whose *development* profiles compile each login's
        ``dev=True`` commands into its program."""
        return replace(self, login=tuple(login.built_in(development) for login in self.logins))

    def development(self) -> SshAccess:
        """What a development build holds: each login's ``dev=True`` commands too."""
        if not any(login.developed for login in self.logins):
            return self
        return replace(self, login=tuple(login.development() for login in self.logins))

    rules = (
        Rule(
            "ssh-keys-baked",
            "warning",
            "SSH keys are baked into a cloud image, so every instance of it lets their holders "
            "in as root.",
            "Use SshAccess(from_delivery=True) so the control plane delivers the key at boot, "
            "or keep baked keys in a non-production profile with dev=DevAccess(...).",
        ),
        Rule(
            "ssh-login-shell",
            "warning",
            "A production login gets an interactive shell and no commands, so whoever "
            "holds its key runs whatever that shell allows.",
            "Pin what it runs: "
            'Login("tenant", commands={"status": Command(runs=["/usr/bin/app-status"])}).',
        ),
        Rule(
            "login-mode-undeclared",
            "error",
            "A login's command names a firewall mode the image's Firewall does not declare, so "
            "its program would never offer it.",
            'Name a mode of Firewall(modes=): Command(..., modes=["maintenance"]), or declare '
            'the mode: Firewall(modes={"production": Mode(), "maintenance": Mode(...)}).',
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``ssh-keys-baked``: keys baked into a cloud image let in whoever holds them;
        ``ssh-login-shell``: a production login with a shell and no commands;
        ``login-mode-undeclared``: a command offered in a mode the firewall lacks."""
        found = [
            dm.Diagnostic(
                "ssh-login-shell",
                "warning",
                f"login {login.name!r} gets the shell {login.shell} and no commands=: whoever "
                "holds its key runs whatever that shell allows",
                subject=login.name,
                hint=f'Login("{login.name}", commands='
                '{"status": Command(runs=["/usr/bin/app-status"])}) pins what it runs.',
            )
            for login in self.logins
            if resolved.production and login.shell is not None
        ]
        found += self._undeclared_modes(resolved)
        if self.keys and {"azure", "gcp"} & set(resolved.targets):
            found.append(
                _diagnostic(
                    "ssh-keys-baked",
                    "warning",
                    f"{len(self.keys)} SSH key(s) are baked into a cloud image: every instance "
                    "of it lets their holders in as root",
                )
            )
        return tuple(found)

    def _undeclared_modes(self, resolved: dm.Resolved) -> Iterator[dm.Diagnostic]:
        config = next(
            (i for i in resolved.items if isinstance(i, dm.File) and i.path == MODES_CONFIG),
            None,
        )
        declared: list[str] = []
        if config is not None and isinstance(config.content, str):
            declared = [mode["name"] for mode in json.loads(config.content)["modes"]]
        for login in self.logins:
            for name, command in sorted((login.commands or {}).items()):
                missing = [mode for mode in command.modes if mode not in declared]
                if missing:
                    yield dm.Diagnostic(
                        "login-mode-undeclared",
                        "error",
                        f"login {login.name!r}: command {name} is offered in "
                        f"{', '.join(missing)}, which the image's Firewall does not declare "
                        f"({', '.join(declared) or 'it has no modes'})",
                        subject=login.name,
                        hint=f'Firewall(modes={{"production": Mode(), "{missing[0]}": '
                        "Mode(...)}), or drop the mode from modes=.",
                    )


class DevAccess(Component):
    """Debug access to a development build: ``dev=DevAccess(ssh_keys=["~/.ssh/id.pub"])``.

    The image gains a non-production ``dev`` profile holding it: root SSH by public key
    only, the debugging tools and a serial console. Each entry of *ssh_keys* is a public
    key, or the path of a ``.pub`` file read when the image lowers; a private key is
    refused. *password* is a root password for the serial console, never a login over the
    network. It is refused in ``components=``: debug access is a profile, never the image.
    Component name: ``dev-access``.
    """

    ssh_keys: Sequence[str] = ()
    password: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.ssh_keys, str):
            raise ValidationError(
                "DevAccess: ssh_keys= takes a list of public keys, not one string.",
                hint="Pass ssh_keys=['~/.ssh/id_ed25519.pub'].",
            )
        object.__setattr__(self, "ssh_keys", tuple(self.ssh_keys))
        for key in self.ssh_keys:
            if not isinstance(key, str) or not key.strip():
                raise ValidationError(
                    f"DevAccess: ssh_keys= entry {key!r} is not a public key or a path.",
                    hint="Pass a public key line or the path of a .pub file.",
                )
            if "PRIVATE KEY" in key:
                raise ValidationError(
                    "DevAccess: ssh_keys= holds a private key.",
                    hint="Pass the .pub file or its contents.",
                )
        if self.password is not None and (not isinstance(self.password, str) or not self.password):
            raise ValidationError(
                "DevAccess: password= must be a non-empty string or None.",
                hint="Pass password='...' for a console login, or leave it out.",
            )
        if not self.ssh_keys and self.password is None:
            raise ValidationError(
                "DevAccess grants nothing: no SSH key and no password.",
                hint="Pass ssh_keys=['~/.ssh/id_ed25519.pub'].",
            )

    def public_keys(self) -> tuple[str, ...]:
        """The public key lines, reading every entry that names a ``.pub`` file."""
        found: list[str] = []
        for entry in self.ssh_keys:
            candidate = Path(entry).expanduser()
            if "\n" not in entry and candidate.is_file():
                text = candidate.read_text(encoding="utf-8")
                found.extend(line.strip() for line in text.splitlines() if line.strip())
            elif entry.strip().endswith(".pub") and " " not in entry.strip():
                raise ValidationError(
                    f"DevAccess: ssh_keys= names {entry}, which does not exist.",
                    hint="Pass the path of an existing .pub file (relative paths are read from "
                    "the working directory), or the key's text.",
                )
            else:
                found.extend(line.strip() for line in entry.splitlines() if line.strip())
        return tuple(dict.fromkeys(found))

    def tools(self) -> tuple[object, ...]:
        """The debugging tools and, with *password*, the console login."""
        if self.password is None:
            return ()
        return (du.DevTools(root_password=self.password),)

    def compose(self) -> Iterator[object]:
        keys = self.public_keys()
        if keys:
            yield du.SshAccess(authorized_keys=keys)
        yield from self.tools()

    rules = (
        Rule(
            "dev-access-rsa-key",
            "info",
            "A development profile accepts an RSA SSH key where every current client makes "
            "ed25519.",
            "Make an ed25519 key (ssh-keygen -t ed25519) and pass its .pub instead.",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``dev-access-rsa-key``: an RSA key where every current client makes ed25519."""
        rsa = [key for key in self.public_keys() if key.startswith("ssh-rsa ")]
        if not rsa:
            return ()
        return (
            _diagnostic(
                "dev-access-rsa-key",
                "info",
                f"profile {resolved.profile!r} accepts {len(rsa)} RSA SSH key(s): prefer "
                "ed25519 (ssh-keygen -t ed25519)",
            ),
        )
