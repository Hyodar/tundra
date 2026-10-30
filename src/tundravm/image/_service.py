"""The public :class:`Service`: an argv, what it needs, and what it offers.

Everything that is not a relationship is a grouped value (:class:`Resources`,
:class:`Shutdown`, :class:`Hardening`) or the one escape hatch (``unit=DropIn(...)``),
so a service reads as what it does rather than as a list of systemd directives.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, Protocol

from tundravm._location import Located
from tundravm._source import FrozenMap
from tundravm.declarative.model import Calendar, DropIn, Every, Listen, Ref
from tundravm.declarative.systemd import Hardening
from tundravm.errors import ValidationError

from ._component import Component, ComponentReady, Endpoint, ServiceReady, ServiceRef
from ._values import (
    TCP,
    UDP,
    Credential,
    HttpProbe,
    Notify,
    PortValue,
    Readiness,
    Resources,
    Shutdown,
    TcpProbe,
)

__all__ = ["Argument", "Requirement", "Service", "executable_of"]

_UNIT_STEM = re.compile(r"[A-Za-z0-9:_.\\@-]+")
_DIRECTORY = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_READINESS: tuple[type, ...] = (HttpProbe, TcpProbe, Notify)


class Built(Protocol):
    """A built binary declaration (``GoBinary``, ``CargoBinary``): it knows where it
    installs, so a service that runs it cannot drift from the build that made it."""

    @property
    def executable(self) -> str:
        """The absolute path the binary is installed at."""


type Argument = str | Endpoint | Built
"""What an argv element may be: a word, an :class:`Endpoint` rendered as its URL, or
a built binary as the first element."""

type Requirement = ServiceReady | ComponentReady | Endpoint | ServiceRef
"""What ``requires=`` takes: a readiness handle, or an export that carries one."""


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"Service: {message}", hint=hint)


def executable_of(value: object) -> str | None:
    """The installed path of a built binary declaration (``GoBinary``, ``CargoBinary``).

    ``None`` for anything else, so a caller can tell a build from a plain word.
    """
    from tundravm.declarative.model import Fragment

    path = getattr(value, "executable", None)
    if isinstance(value, Fragment) and isinstance(path, str) and path.startswith("/"):
        return path
    return None


@dataclass(frozen=True, slots=True)
class Service(Located):
    """A program the image runs, as its own user, in a tested sandbox.

    *argv* is an argv, never a command line: the program's absolute path (or a built
    binary declaration such as ``GoBinary(...)``, which pulls its build and install in)
    and one element per argument, each rendered with systemd's quoting. The visible
    exception is the shell escape, ``["/bin/sh", "-eu", "-c", script]``.

    Implied, with no further words: ``Type=exec``, ``Restart=on-failure`` with
    ``RestartSec=5s``, a dedicated system user named after the service, and
    ``Hardening.preset("static")``. The escape hatch for anything systemd knows and no
    field covers is ``unit=DropIn(...)``.

    ``requires=[other.ready]`` is a real dependency: ``Requires=`` and ``After=`` on
    whatever proves the other service ready (its ``Type=notify``, or its probe unit).
    ``expose=[TCP(8080)]`` opens that port in the image's firewall and nothing else does.
    """

    name: str
    argv: Sequence[Argument]
    _: KW_ONLY
    requires: Sequence[Requirement] = ()
    readiness: Readiness | None = None
    state: str | None = None
    """A ``StateDirectory=`` under ``/var/lib``, also the default working directory."""
    credentials: Sequence[Credential] = ()
    expose: Sequence[PortValue] = ()
    resources: Resources | None = None
    shutdown: Shutdown | None = None
    hardening: Hardening = field(default_factory=lambda: Hardening.preset("static"))
    timer: Every | Calendar | None = None
    socket: Listen | None = None
    unit: DropIn | None = None
    user: str | None = None
    groups: Sequence[str] = ()
    working_directory: str | None = None
    environment: Mapping[str, str] = field(default_factory=FrozenMap)
    logs: str | None = None
    runtime: str | None = None

    def __post_init__(self) -> None:
        self._freeze()
        if not isinstance(self.name, str) or not _UNIT_STEM.fullmatch(self.name):
            raise _fail(self, f"{self.name!r} is not a service name.")
        if "." in self.name and not self.name.endswith(".service"):
            raise _fail(self, f"{self.name!r} must be a .service unit.")
        self._check_argv()
        self._check_requires()
        self._check_values()

    def _freeze(self) -> None:
        for name in ("argv", "requires", "credentials", "expose", "groups"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise _fail(self, f"{name}= must be a list, got {value!r}.")
            object.__setattr__(self, name, tuple(value))
        object.__setattr__(self, "environment", FrozenMap(dict(self.environment)))

    def _check_argv(self) -> None:
        if not self.argv:
            raise _fail(
                self,
                f"{self.name!r} has an empty argv.",
                hint="Pass the program and its arguments: ['/usr/bin/app', '--flag'].",
            )
        first = self.argv[0]
        if executable_of(first) is None:
            if not isinstance(first, str) or not first.lstrip("+-!@:").startswith("/"):
                raise _fail(
                    self,
                    f"{self.name!r} starts with {first!r}, which is not a program path.",
                    hint="systemd searches no $PATH: spell '/usr/bin/app', or pass a built "
                    "binary such as GoBinary(...).",
                )
        for argument in self.argv[1:]:
            if not isinstance(argument, (str, Endpoint)):
                raise _fail(
                    self,
                    f"{self.name!r} argv holds {argument!r}, which is not a word.",
                    hint="Pass strings, or an Endpoint a component exported.",
                )
            if isinstance(argument, str) and "\n" in argument:
                raise _fail(self, f"{self.name!r} argv holds a multi-line argument.")

    def _check_requires(self) -> None:
        for item in self.requires:
            if isinstance(item, Service):
                raise _fail(
                    self,
                    f"{self.name!r} requires a Service value; it needs its readiness.",
                    hint=f"Pass requires=[{item.name}.ready].",
                )
            if isinstance(item, Component):
                raise _fail(
                    self,
                    f"{self.name!r} requires a Component value; it needs its readiness.",
                    hint="Pass requires=[component.ready].",
                )
            if not isinstance(item, (ServiceReady, ComponentReady, Endpoint, ServiceRef)):
                raise _fail(
                    self,
                    f"{self.name!r} requires {item!r}, which is not a readiness handle.",
                    hint="Pass other.ready, component.ready or an exported Endpoint.",
                )
            if isinstance(item, Endpoint) and item.ready is None:
                raise _fail(
                    self,
                    f"{self.name!r} requires an endpoint that carries no readiness.",
                    hint="Export it as Endpoint(..., ready=service.ready).",
                )

    def _check_values(self) -> None:
        checks: tuple[tuple[str, object, type | tuple[type, ...]], ...] = (
            ("readiness", self.readiness, _READINESS),
            ("resources", self.resources, Resources),
            ("shutdown", self.shutdown, Shutdown),
            ("timer", self.timer, (Every, Calendar)),
            ("socket", self.socket, Listen),
            ("unit", self.unit, DropIn),
        )
        for name, value, kinds in checks:
            if value is not None and not isinstance(value, kinds):
                raise _fail(self, f"{name}= holds {value!r}, which is not the expected value.")
        if not isinstance(self.hardening, Hardening):
            raise _fail(
                self,
                f"hardening= holds {self.hardening!r}, not a Hardening.",
                hint="Use Hardening.preset('static') or another runtime class.",
            )
        for credential in self.credentials:
            if not isinstance(credential, Credential):
                raise _fail(self, f"credentials= holds {credential!r}, not a Credential.")
        for port in self.expose:
            if not isinstance(port, (TCP, UDP)):
                raise _fail(
                    self,
                    f"expose= holds {port!r}, not a port.",
                    hint="Pass expose=[TCP(8080)].",
                )
        for name in ("state", "logs", "runtime"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not _DIRECTORY.fullmatch(value)
            ):
                raise _fail(self, f"{name}= {value!r} must be a directory name.")
        if self.working_directory is not None and not str(self.working_directory).startswith("/"):
            raise _fail(self, f"working_directory {self.working_directory!r} must be absolute.")
        for group in self.groups:
            if not isinstance(group, str) or not group:
                raise _fail(self, f"groups= holds {group!r}, not a group name.")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\n" in value:
                raise _fail(self, f"environment[{key!r}] must be one line of text.")
        if self.timer is not None and self.socket is not None:
            raise _fail(
                self,
                f"{self.name!r} is both timer-started and socket-activated.",
                hint="Pick one: a timer runs it on a schedule, a socket on demand.",
            )

    @property
    def unit_name(self) -> str:
        """The ``.service`` unit this lowers to."""
        return self.name if self.name.endswith(".service") else f"{self.name}.service"

    # The export a caller references.
    @property
    def ready(self) -> ServiceReady:
        """ "This service is ready": what another service's ``requires=`` takes."""
        return ServiceReady(self.unit_name, self.readiness)

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.image.Profile` change targets."""
        return Ref("Service", (self.unit_name,))

    @property
    def stem(self) -> str:
        """The unit name without its suffix: the generated user's name."""
        return self.unit_name.removesuffix(".service")

    @property
    def probe_unit(self) -> str | None:
        """The bounded probe unit a dependent waits for, when readiness needs one."""
        if self.readiness is None or isinstance(self.readiness, Notify):
            return None
        return f"{self.stem}-ready.service"

    def binaries(self) -> tuple[Any, ...]:
        """The built binary declarations this service's argv pulls in."""
        return tuple(item for item in self.argv if executable_of(item) is not None)

    def command(self) -> tuple[str, ...]:
        """The argv as words: a built first element becomes its installed path, an
        :class:`Endpoint` its URL."""
        words: list[str] = []
        for item in self.argv:
            built = executable_of(item)
            if built is not None:
                words.append(built)
            elif isinstance(item, Endpoint):
                words.append(item.url)
            else:
                words.append(str(item))
        return tuple(words)

    @property
    def state_directory(self) -> str | None:
        """The working directory a ``state=`` implies."""
        if self.state is None:
            return None
        return posixpath.join("/var/lib", self.state)
