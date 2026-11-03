"""Delivered secrets: :class:`Secret` requirements and the :class:`BrokerKey` that may
release them; :class:`Generated` secrets the image makes at boot instead.

A secret is a requirement of the image, never a value in it: the value arrives at run
time in ``/run/tundravm/credentials/<name>`` and a service reads it as a systemd
credential. Plan section 6.3 is the contract: the attested endpoint writes the file on a
TLS session whose key a fresh quote bound, and its config records the requirement in the
image.
"""

from __future__ import annotations

import re
from dataclasses import KW_ONLY, dataclass

from tundravm._location import Located
from tundravm._modules import instance_key
from tundravm.errors import ValidationError

from ._component import BootReady
from ._values import Credential

__all__ = [
    "CREDENTIALS_DIRECTORY",
    "CREDENTIAL_REFERENCE",
    "GENERATED_DIRECTORY",
    "RUNTIME_DIRECTORY",
    "BrokerKey",
    "Generated",
    "InstanceKey",
    "Secret",
    "credential_wait_unit",
]

RUNTIME_DIRECTORY = "/run/tundravm"
"""The root-owned tmpfs directory the runtime contracts write into (plan section 6.3)."""

CREDENTIALS_DIRECTORY = f"{RUNTIME_DIRECTORY}/credentials"
"""Where a delivered secret lands, one file per name, mode 0600, before systemd copies it
into the bound unit's own credential directory."""

GENERATED_DIRECTORY = f"{RUNTIME_DIRECTORY}/secrets"
"""Where a :class:`Generated` secret is written at boot, one file per name, mode 0600."""

CREDENTIAL_REFERENCE = re.compile(r"\$\{CREDENTIALS_DIRECTORY\}/([A-Za-z0-9_.-]+)")
"""How an argv names a credential it reads: ``secret.path``, systemd's
``${CREDENTIALS_DIRECTORY}`` plus the credential name."""

_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")


def credential_wait_unit(name: str) -> str:
    """The boot unit that finishes once the secret *name* has been delivered."""
    return f"credential-{name}.service"


@dataclass(frozen=True, slots=True)
class BrokerKey(Located):
    """A key broker that releases a value after it has verified this image's attestation.

    ``Volume(unlock=BrokerKey(url, audience=...))`` unlocks a volume with the key the
    broker releases, for unattended fleets; ``Secret(name, source=BrokerKey(...))``
    takes a secret from it instead of from an operator. The broker speaks the same
    protocol as ``tundravm secrets deliver`` (plan section 6.3) with a pull client in the
    image; that client is gate 3 (tools/). Until it lands the image records the
    broker in its runtime manifest and waits for the value exactly as it waits for an
    operator's delivery, so an operator can stand in for the broker.
    """

    url: str
    _: KW_ONLY
    audience: str

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise ValidationError(
                f"BrokerKey: url {self.url!r} must be an https:// URL.",
                hint="A broker releases keys over an authenticated channel: "
                "BrokerKey('https://kbs.example', audience='prover').",
            )
        if any(c.isspace() for c in self.url):
            raise ValidationError(
                f"BrokerKey: url {self.url!r} must be one word.",
                hint="Pass the broker's URL alone: BrokerKey('https://kbs.example', ...).",
            )
        if not isinstance(self.audience, str) or not self.audience or "\n" in self.audience:
            raise ValidationError(
                "BrokerKey: audience must name who the broker releases to.",
                hint="Pass audience='prover': the broker's policy names it.",
            )

    def manifest(self) -> dict[str, object]:
        """The broker as the runtime manifest records it."""
        return {"kind": "broker-key", "url": self.url, "audience": self.audience}


@dataclass(frozen=True, slots=True)
class Secret(Located):
    """A secret the image needs at run time and never contains: ``Secret("jwt")``.

    The value is delivered after the image has proved what it runs: by an operator
    (``tundravm secrets deliver INSTANCE jwt=@jwt.txt``) or, with ``source=``, by a key
    broker. It lands in ``/run/tundravm/credentials/<name>`` (root-owned tmpfs, mode 0600)
    and a service reads it as a systemd credential: ``Service(credentials=[jwt])`` renders
    ``LoadCredential=jwt:/run/tundravm/credentials/jwt``, systemd copies the file into the
    unit's read-only ``$CREDENTIALS_DIRECTORY`` at start, and ``jwt.path`` is where the
    program finds it (an argv word, rendered as ``${CREDENTIALS_DIRECTORY}/jwt``).

    *required* secrets hold every bound service until they arrive (``jwt.ready``); an
    optional one starts the service without it, which then reads an empty credential.
    The endpoint delivers each secret once per boot.
    """

    name: str
    _: KW_ONLY
    required: bool = True
    source: BrokerKey | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise ValidationError(
                f"Secret: {self.name!r} is not a secret name.",
                hint="Use lower-case letters, digits, '-', '_' and '.': Secret('api-token').",
            )
        if not isinstance(self.required, bool):
            raise ValidationError(
                "Secret: required= must be True or False.",
                hint="Pass required=False for a secret the service can start without.",
            )
        if self.source is not None and not isinstance(self.source, BrokerKey):
            raise ValidationError(
                f"Secret: source {self.source!r} is not a BrokerKey.",
                hint="Leave source=None for operator delivery, or pass BrokerKey(url, "
                "audience=...).",
            )

    @property
    def location(self) -> str:
        """Where the delivered value lands: ``/run/tundravm/credentials/<name>``."""
        return f"{CREDENTIALS_DIRECTORY}/{self.name}"

    @property
    def path(self) -> str:
        """Where a bound service reads it: ``${CREDENTIALS_DIRECTORY}/<name>``, an argv word."""
        return "${CREDENTIALS_DIRECTORY}/" + self.name

    @property
    def credential(self) -> Credential:
        """The ``LoadCredential=`` binding a service gets."""
        return Credential(self.name, self.location)

    @property
    def ready(self) -> BootReady:
        """ "It has been delivered": what ``requires=`` takes for a secret."""
        return BootReady(credential_wait_unit(self.name))


@dataclass(frozen=True, slots=True, init=False, repr=False)
class Generated(Credential):
    """A secret the image makes at boot instead of having it delivered: ``Generated("jwt")``.

    For a value only the image's own services share, such as the Engine API secret an
    execution client and its consensus client authenticate with, so nobody has to deliver
    it. A root oneshot (``tundravm-generated-<name>.service``, no network, no capabilities)
    writes *bytes* random bytes as one line of lower-case hex, without a newline, to
    ``/run/tundravm/secrets/<name>`` (root-owned tmpfs, mode 0600), fresh every boot and
    kept if the unit runs again. A service binds it as it binds a :class:`Secret` (in argv,
    in ``environment=`` or with ``credentials=``), renders
    ``LoadCredential=<name>:/run/tundravm/secrets/<name>`` and requires the oneshot, so
    every service bound to one name reads the same value. Nothing outside the image ever
    holds it.
    """

    bytes: int = 32

    def __init__(self, name: str, *, bytes: int = 32) -> None:
        object.__setattr__(self, "bytes", bytes)
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValidationError(
                f"Generated: {name!r} is not a secret name.",
                hint="Use lower-case letters, digits, '-', '_' and '.': Generated('jwt').",
            )
        if isinstance(bytes, bool) or not isinstance(bytes, int) or not 16 <= bytes <= 4096:
            raise ValidationError(
                f"Generated: bytes={bytes!r} is not a length in 16..4096.",
                hint="Leave the default, 32 random bytes (64 hex characters).",
            )
        Credential.__init__(self, name, f"{GENERATED_DIRECTORY}/{name}")

    def __repr__(self) -> str:
        extra = "" if self.bytes == 32 else f", bytes={self.bytes}"
        return f"Generated({self.name!r}{extra})"

    @property
    def unit(self) -> str:
        """The oneshot that writes it."""
        return f"tundravm-generated-{self.name}.service"

    @property
    def path(self) -> str:
        """Where a bound service reads it: ``${CREDENTIALS_DIRECTORY}/<name>``, an argv word."""
        return "${CREDENTIALS_DIRECTORY}/" + self.name

    @property
    def ready(self) -> BootReady:
        """ "This boot's value exists": what a bound service requires."""
        return BootReady(self.unit)


@dataclass(frozen=True, slots=True, init=False, repr=False)
class InstanceKey(Credential):
    """This boot's instance key, for a service that signs with it: ``InstanceKey()``.

    Every image generates an ECDSA key on secp256k1 (Ethereum's curve) at boot, fresh each
    boot and on tmpfs only (``/run/tundravm/instance-key.pem``, root:root 0600, SEC1 PEM);
    a service reads it as a systemd credential. ``Service(credentials=[InstanceKey()])``
    renders ``LoadCredential=instance-key:/run/tundravm/instance-key.pem`` with
    ``Requires=`` and ``After=`` on the unit that generates it, and the program reads
    ``$CREDENTIALS_DIRECTORY/instance-key``. In argv or ``environment=`` it is that path, as
    a :class:`Secret` is, and binds itself; a ``timer=`` service may take it too. The public
    key is ``/run/tundravm/instance-key.pub`` and the console line ``run status`` and
    ``attest`` print. The quote does not bind it yet.
    """

    def __init__(self) -> None:
        Credential.__init__(self, instance_key.CREDENTIAL, instance_key.KEY)

    def __repr__(self) -> str:
        return "InstanceKey()"

    @property
    def path(self) -> str:
        """Where a bound service reads it: ``${CREDENTIALS_DIRECTORY}/instance-key``."""
        return "${CREDENTIALS_DIRECTORY}/" + self.name

    @property
    def ready(self) -> BootReady:
        """ "This boot's key exists": what a bound service requires."""
        return BootReady(instance_key.UNIT)
