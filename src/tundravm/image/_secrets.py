"""Delivered secrets: :class:`Secret` requirements and the :class:`BrokerKey` that may
release them.

A secret is a requirement of the image, never a value in it: the value arrives at run
time in ``/run/tundravm/credentials/<name>`` and a service reads it as a systemd
credential. Plan section 6.3 is the contract; the attested channel that writes the file
is gate 3 (tundra-tools), and today's ``secret-delivery`` manifest is what records the
requirement in the image.
"""

from __future__ import annotations

import re
from dataclasses import KW_ONLY, dataclass
from typing import Literal

from tundravm._location import Located
from tundravm.errors import ValidationError

from ._component import BootReady
from ._values import Credential

__all__ = [
    "CREDENTIALS_DIRECTORY",
    "CREDENTIAL_REFERENCE",
    "RUNTIME_DIRECTORY",
    "BrokerKey",
    "Secret",
    "credential_wait_unit",
]

RUNTIME_DIRECTORY = "/run/tundravm"
"""The root-owned tmpfs directory the runtime contracts write into (plan section 6.3)."""

CREDENTIALS_DIRECTORY = f"{RUNTIME_DIRECTORY}/credentials"
"""Where a delivered secret lands, one file per name, mode 0600, before systemd copies it
into the bound unit's own credential directory."""

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
    image; that client is gate 3 (tundra-tools). Until it lands the image records the
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
    *rotation* is what a re-delivery does: ``"restart"`` restarts the bound units (the
    copy systemd made is per start, so this is the only way a new value reaches them),
    ``"none"`` leaves them running on the old value until they restart for another
    reason. A ``reload`` rotation would need a directory binding instead of a credential
    and is not offered.
    """

    name: str
    _: KW_ONLY
    required: bool = True
    rotation: Literal["restart", "none"] = "restart"
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
        if self.rotation not in ("restart", "none"):
            raise ValidationError(
                f"Secret: rotation {self.rotation!r} is not 'restart' or 'none'.",
                hint="A credential is copied at start, so a new value reaches a unit only "
                "when it restarts: rotation='restart'.",
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
