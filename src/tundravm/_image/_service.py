"""The public :class:`Service`: an argv, what it needs, and what it offers.

Everything that is not a relationship is a grouped value (:class:`Resources`,
:class:`Shutdown`, :class:`Hardening`) or the one escape hatch (``unit=DropIn(...)``),
so a service reads as what it does rather than as a list of systemd directives.
"""

from __future__ import annotations

import difflib
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, Literal, Protocol

from tundravm._keywords import subject
from tundravm._location import Located
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative.model import Calendar, DropIn, Every, Listen, UnitOutput
from tundravm.declarative.systemd import CEILINGS, Hardening
from tundravm.errors import ValidationError

from ._change import Ref
from ._component import (
    BootReady,
    Component,
    ComponentReady,
    Endpoint,
    ServiceReady,
    ServiceRef,
    readiness_of,
)
from ._secrets import CREDENTIAL_REFERENCE, InstanceKey, Secret
from ._storage import VolumeDirectory
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

__all__ = ["Argument", "Requirement", "Sandbox", "Service", "executable_of"]

_UNIT_STEM = re.compile(r"[A-Za-z0-9:_.\\@-]+")
_DIRECTORY = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_READINESS: tuple[type, ...] = (HttpProbe, TcpProbe, Notify)
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPECTED = {
    "readiness": "a readiness",
    "resources": "a Resources",
    "shutdown": "a Shutdown",
    "timer": "an Every or a Calendar",
    "socket": "a Listen",
    "unit": "a DropIn",
}
_EXAMPLE = {
    "readiness": "TcpProbe(8080), HttpProbe(url) or Notify()",
    "resources": 'Resources(memory_max="2G")',
    "shutdown": 'Shutdown(timeout="30s")',
    "timer": 'Every("1h") or Calendar("daily")',
    "socket": "Listen(8080)",
    "unit": "DropIn(...)",
}
_DIRECTORY_ROOTS = {"state": "/var/lib", "logs": "/var/log", "runtime": "/run"}


class Built(Protocol):
    """A built binary declaration (``GoBinary``, ``CargoBinary``): it knows where it
    installs, so a service that runs it cannot drift from the build that made it."""

    @property
    def executable(self) -> str:
        """The absolute path the binary is installed at."""


type Binding = Credential | Secret
"""What ``credentials=`` takes: a delivered :class:`Secret`, or a fixed-path credential."""

type Argument = str | Endpoint | Binding | Built
"""What an argv element may be: a word, an :class:`Endpoint` rendered as its URL, a
:class:`Secret` (or :class:`Credential`) rendered as the path the service reads it at,
which binds it, or a built binary as the first element."""

type Requirement = ServiceReady | ComponentReady | BootReady | Endpoint | ServiceRef | UnitOutput
"""What ``requires=`` takes: a readiness handle, an export that carries one, or a setup
task's ``task.output(path)`` (start after it, and only if it wrote *path*)."""

type Sandbox = Hardening | Literal["static", "jit", "node", "python", "dotnet", "relaxed"]
"""What ``hardening=`` takes: a preset by name (``Hardening.preset(name)``, or
``Hardening.relaxed()`` for ``"relaxed"``), or a :class:`Hardening` with deviations."""

_REQUIREMENTS: tuple[type, ...] = (
    ServiceReady,
    ComponentReady,
    BootReady,
    Endpoint,
    ServiceRef,
    UnitOutput,
)


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    named = subject(owner) if isinstance(owner, Service) else "Service"
    return ValidationError(f"{named}: {message}", hint=hint)


def executable_of(value: object) -> str | None:
    """The installed path of a built binary declaration (``GoBinary``, ``CargoBinary``).

    ``None`` for anything else, so a caller can tell a build from a plain word.
    """
    from tundravm.declarative.model import Fragment

    path = getattr(value, "executable", None)
    if isinstance(value, Fragment) and isinstance(path, str) and path.startswith("/"):
        return path
    return None


def _listed(name: str, value: object) -> str:
    """*value* as the list the keyword *name* takes: a command line split into its words."""
    if isinstance(value, str):
        words = shlex.split(value) if name == "argv" else [value]
        return "[" + ", ".join(f'"{word}"' for word in words) + "]"
    if isinstance(value, (TCP, UDP)):
        return f"[{value!r}]"
    if name == "expose" and isinstance(value, int):
        return f"[TCP({value})]"
    return "[...]"


def _sandbox(value: object) -> Hardening:
    """``hardening=`` as the :class:`Hardening` it names."""
    if isinstance(value, Hardening):
        return value
    names = ", ".join(repr(name) for name in CEILINGS)
    if value == "relaxed":
        return Hardening.relaxed()
    if isinstance(value, str) and value in CEILINGS:
        return Hardening.preset(value)  # type: ignore[arg-type]
    close = difflib.get_close_matches(value, list(CEILINGS), n=1) if isinstance(value, str) else []
    raise _fail(
        value,
        f"hardening={value!r} is not a sandbox.",
        hint=f'Service(..., hardening="{close[0]}")'
        if close
        else f"Name a preset ({names}), or pass Hardening.preset(name, ...) with deviations.",
    )


@dataclass(frozen=True, slots=True)
class Service(Located):
    """A program the image runs, as its own user, in a tested sandbox.

    *argv* is an argv, never a command line: the program's absolute path (or a built
    binary declaration such as ``GoBinary(...)``, which pulls its build and install in)
    and one element per argument, each rendered with systemd's quoting. The visible
    exception is the shell escape, ``["/bin/sh", "-eu", "-c", script]``.

    Implied, with no further words: ``Type=exec``, ``Restart=on-failure`` with
    ``RestartSec=5s``, a dedicated system user named after the service, and the
    ``"static"`` sandbox; ``hardening="python"`` names another preset. The escape hatch
    for anything systemd knows and no field covers is ``unit=DropIn(...)``.

    ``requires=[other.ready]`` is a real dependency: ``Requires=`` and ``After=`` on
    whatever proves the other service ready (its ``Type=notify``, or its probe unit).
    ``requires=[setup.output("/run/app/config")]`` is the fail-safe idiom: ``After=`` and
    ``Wants=`` on the setup task, ``ConditionPathExists=`` on what it writes, so a failed
    setup skips this service instead of failing it.
    ``expose=[TCP(8080)]`` opens that port in the image's firewall and nothing else does.
    ``quotes=True`` says it asks the evidence service for TDX quotes: it joins the ``tdx``
    group and requires :data:`~tundravm.components.EVIDENCE`.

    ``state=`` is a ``StateDirectory=`` name under ``/var/lib`` (volatile, like the rest
    of the root) or a directory on a persistent volume (``data.directory("app")``), which
    the service then waits for. A :class:`Secret` in argv (``[app, "--jwt", jwt]``) or in
    ``environment=`` (``{"JWT_FILE": jwt}``) is the path the service reads it at, and
    binds it: the service waits for a required one. ``credentials=[jwt]`` binds one the
    program finds otherwise (a word that embeds ``jwt.path``, a config file); an argv
    word naming a credential the service does not bind is refused. ``InstanceKey()`` is
    this boot's secp256k1 key, bound the same ways; the service then requires the unit
    that generates it.

    ``timer=Every("1h")`` (or ``Calendar(...)``) runs it as a scheduled oneshot with every
    other keyword kept; what only a long-lived service has (``socket=``, ``readiness=``,
    ``expose=``, ``credentials=`` beyond ``InstanceKey()``, a volume ``state=``) is
    refused.

    :meth:`packaged` runs the unit a Debian package ships, under ``hardening=``.
    """

    name: str
    argv: Sequence[Argument]
    _: KW_ONLY
    requires: Sequence[Requirement] = ()
    readiness: Readiness | None = None
    state: str | VolumeDirectory | None = None
    """A ``StateDirectory=`` under ``/var/lib``, or a directory on a :class:`Volume`; also
    the default working directory."""
    credentials: Sequence[Binding] = ()
    expose: Sequence[PortValue] = ()
    resources: Resources | None = None
    shutdown: Shutdown | None = None
    hardening: Sandbox = "static"
    timer: Every | Calendar | None = None
    socket: Listen | None = None
    unit: DropIn | None = None
    user: str | None = None
    groups: Sequence[str] = ()
    quotes: bool = False
    """It asks the evidence service for quotes: ``groups=["tdx"]`` and
    ``requires=[EVIDENCE]``, said once."""
    working_directory: str | None = None
    environment: Mapping[str, str | Binding] = field(default_factory=FrozenMap)
    """Each value one line of text, or a :class:`Secret` (or :class:`Credential`), which
    binds it and renders as the path the service reads it at."""
    logs: str | None = None
    runtime: str | None = None
    package: str | None = None
    """The Debian package whose own unit this is (:meth:`packaged`): its ``ExecStart=``,
    ``User=`` and ``Type=`` run, and *argv* is empty."""

    def __post_init__(self) -> None:
        self._freeze()
        object.__setattr__(self, "hardening", _sandbox(self.hardening))
        if not isinstance(self.name, str) or not _UNIT_STEM.fullmatch(self.name):
            raise _fail(
                self,
                f"{self.name!r} is not a service name.",
                hint='Use letters, digits, "-", "_" and "@": Service("app", [...]).',
            )
        if "." in self.name and not self.name.endswith(".service"):
            raise _fail(
                self,
                f"{self.name!r} names a unit that is not a .service.",
                hint=f'Service("{self.name.partition(".")[0]}", [...]): the .service is implied.',
            )
        if self.package is None:
            self._check_argv()
        else:
            self._check_packaged()
        self._check_requires()
        self._check_values()
        self._check_bindings()

    def _freeze(self) -> None:
        for name in ("argv", "requires", "credentials", "expose", "groups"):
            value = getattr(self, name)
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise _fail(
                    self,
                    f"{name}= must be a list, got {value!r}.",
                    hint=f"{name}={_listed(name, value)}",
                )
            object.__setattr__(self, name, tuple(value))
        object.__setattr__(self, "environment", FrozenMap(dict(self.environment)))

    def _check_argv(self) -> None:
        if not self.argv:
            raise _fail(
                self,
                "argv= is empty, so nothing runs.",
                hint=f'Service("{self.name}", ["/usr/bin/app", "--flag"]), or a built binary: '
                f'Service("{self.name}", [GoBinary("app", Local("app"))]).',
            )
        first = self.argv[0]
        if executable_of(first) is None:
            if not isinstance(first, str) or not first.lstrip("+-!@:").startswith("/"):
                raise _fail(
                    self,
                    f"argv= starts with {first!r}, which is not a program path.",
                    hint=f'Service("{self.name}", ["/usr/bin/{first}", ...]): systemd searches '
                    "no $PATH; or pass a built binary such as GoBinary(...)."
                    if isinstance(first, str) and first and "/" not in first
                    else "Spell the absolute path, ['/usr/bin/app', ...], or pass a built "
                    "binary such as GoBinary(...).",
                )
        for argument in self.argv[1:]:
            if not isinstance(argument, (str, Endpoint, Secret, Credential)):
                raise _fail(
                    self,
                    f"argv= holds {argument!r}, which is not a word.",
                    hint="Pass strings, an Endpoint a component exported, or a Secret.",
                )
            if isinstance(argument, str) and "\n" in argument:
                raise _fail(
                    self,
                    "argv= holds a multi-line argument.",
                    hint="Ship the text as File.text(path, text) and pass its path in argv.",
                )

    @classmethod
    def packaged(
        cls,
        name: str,
        *,
        package: str | None = None,
        hardening: Sandbox = "static",
        requires: Sequence[Requirement] = (),
        readiness: TcpProbe | HttpProbe | None = None,
        expose: Sequence[PortValue] = (),
        unit: DropIn | None = None,
    ) -> Service:
        """The unit *name* the Debian *package* ships (default: the unit's stem), enabled
        and run under ``hardening=``, which replaces the package's own sandbox.

        The package is installed for you. Its unit keeps its ``ExecStart=``, ``User=``,
        directories and ``Type=``; ``unit=DropIn(...)`` changes anything else systemd knows.
        """
        stem = name.removesuffix(".service") if isinstance(name, str) else name
        return cls(
            stem,
            (),
            package=stem if package is None else package,
            hardening=hardening,
            requires=requires,
            readiness=readiness,
            expose=expose,
            unit=unit,
        )

    def _check_packaged(self) -> None:
        """A package's unit decides what its own file says; only the sandbox, the order,
        the ports and a drop-in are the image's."""
        if not isinstance(self.package, str) or not _PACKAGE.fullmatch(self.package):
            raise _fail(
                self,
                f"package= {self.package!r} is not a package name.",
                hint='Name the Debian package that ships the unit: package="openssh-server".',
            )
        if self.argv:
            raise _fail(
                self,
                f"is the unit of package {self.package!r}, which runs its own ExecStart=, but "
                "argv= is given too.",
                hint="Drop argv, or write Service(name, argv) without package=.",
            )
        given = {
            "user": self.user,
            "groups": self.groups,
            "quotes": self.quotes,
            "state": self.state,
            "credentials": self.credentials,
            "environment": self.environment,
            "working_directory": self.working_directory,
            "logs": self.logs,
            "runtime": self.runtime,
            "timer": self.timer,
            "socket": self.socket,
            "resources": self.resources,
            "shutdown": self.shutdown,
        }
        for keyword, value in given.items():
            if value:
                raise _fail(
                    self,
                    f"is the unit of package {self.package!r}, so {keyword}= does not apply: the "
                    "package's unit file decides it.",
                    hint="Say it in unit=DropIn(...), or write Service(name, argv) of your own.",
                )
        if isinstance(self.readiness, Notify):
            raise _fail(
                self,
                f"is the unit of package {self.package!r}, whose own Type= decides when it is "
                "ready, so readiness=Notify() does not apply.",
                hint="Use readiness=TcpProbe(port) or HttpProbe(url): a probe is its own unit.",
            )
        if self.hardening == Hardening.none():
            raise _fail(
                self,
                "a packaged unit runs under a sandbox or not at all, so hardening=Hardening.none() "
                "does not apply.",
                hint='hardening="relaxed" is the loosest sandbox a packaged unit takes.',
            )
        if self.unit is not None and self.unit.hardening is not None:
            raise _fail(
                self,
                "sets its sandbox in unit=DropIn(hardening=...).",
                hint="Pass it as hardening=: it replaces the package's sandbox.",
            )

    def _check_requires(self) -> None:
        for item in self.requires:
            if isinstance(item, Service):
                raise _fail(
                    self,
                    f"requires= holds the Service {item.name!r}; it waits for its readiness.",
                    hint=f"Pass requires=[{item.name}.ready].",
                )
            if isinstance(item, Component):
                raise _fail(
                    self,
                    "requires= holds a Component; it waits for its readiness.",
                    hint="Pass requires=[component.ready].",
                )
            if not isinstance(item, _REQUIREMENTS):
                raise _fail(
                    self,
                    f"requires= holds {item!r}, which is not a readiness handle.",
                    hint="Pass other.ready, component.ready or an exported Endpoint.",
                )
            if isinstance(item, Endpoint) and item.ready is None:
                raise _fail(
                    self,
                    "requires= holds an Endpoint that carries no readiness.",
                    hint="Export it as Endpoint(..., ready=service.ready).",
                )
            self._check_readiness(item)

    def _check_readiness(self, item: Requirement) -> None:
        """A dependency must prove it is ready; ``After=`` alone is never what was meant."""
        if isinstance(item, (BootReady, UnitOutput)):
            return
        if isinstance(item, ComponentReady):
            if not item.handles():
                raise _fail(
                    self,
                    f"requires= waits for component {item.name!r}, which declares no readiness.",
                    hint="Give one of its services readiness=HttpProbe(...), TcpProbe(...) "
                    "or Notify().",
                )
            return
        handle = item if isinstance(item, ServiceReady) else item.ready
        if handle is not None and handle.probe is None:
            raise _fail(
                self,
                f"requires= waits for {handle.unit!r}, which declares no readiness.",
                hint="Give it readiness=HttpProbe(...), TcpProbe(...) or Notify(); After= "
                "alone only waits for the exec, not for the program to serve.",
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
                raise _fail(
                    self,
                    f"{name}= holds {value!r}, not {_EXPECTED[name]}.",
                    hint=f"{name}={_EXAMPLE[name]}",
                )
        names: set[str] = set()
        for credential in self.credentials:
            if not isinstance(credential, (Credential, Secret)):
                raise _fail(
                    self,
                    f"credentials= holds {credential!r}, not a Secret or a Credential.",
                    hint="Pass the Secret the image declares: credentials=[jwt].",
                )
            if credential.name in names:
                raise _fail(
                    self,
                    f"credentials= binds {credential.name!r} twice.",
                    hint="List each Secret once in credentials=.",
                )
            names.add(credential.name)
        for port in self.expose:
            if not isinstance(port, (TCP, UDP)):
                raise _fail(
                    self,
                    f"expose= holds {port!r}, not a port.",
                    hint="Pass expose=[TCP(8080)].",
                )
        if len(set(self.expose)) != len(self.expose):
            twice = next(port for port in self.expose if self.expose.count(port) > 1)
            raise _fail(
                self,
                f"expose= lists {twice!r} twice.",
                hint=f"List each port once: expose=[{twice!r}].",
            )
        if self.state is not None and not isinstance(self.state, (str, VolumeDirectory)):
            raise _fail(
                self,
                f"state= holds {self.state!r}.",
                hint="Pass a directory name under /var/lib, or volume.directory('name').",
            )
        for name in ("state", "logs", "runtime"):
            value = getattr(self, name)
            if isinstance(value, VolumeDirectory):
                continue
            if value is not None and (
                not isinstance(value, str) or not _DIRECTORY.fullmatch(value)
            ):
                raise _fail(
                    self,
                    f"{name}= {value!r} must be a directory name.",
                    hint=f'{name}="{self.stem}": systemd makes it under '
                    f"{_DIRECTORY_ROOTS[name]}, owned by the service's user.",
                )
        if self.working_directory is not None and not str(self.working_directory).startswith("/"):
            raise _fail(
                self,
                f"working_directory= {self.working_directory!r} is not an absolute path.",
                hint=f'working_directory="/{str(self.working_directory).lstrip("./")}"',
            )
        for group in self.groups:
            if not isinstance(group, str) or not group:
                raise _fail(
                    self,
                    f"groups= holds {group!r}, not a group name.",
                    hint='groups=["tdx"]: each a group the image declares or Debian ships.',
                )
        if not isinstance(self.quotes, bool):
            raise _fail(
                self, f"quotes= holds {self.quotes!r}.", hint="Pass quotes=True or drop it."
            )
        for key, value in self.environment.items():
            if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
                raise _fail(
                    self,
                    f"environment= names {key!r}, which is not a variable name.",
                    hint='Use letters, digits and "_", not starting with a digit: '
                    'environment={"APP_PORT": "8080"}.',
                )
            if isinstance(value, (Secret, Credential)):
                continue
            if not isinstance(key, str) or not isinstance(value, str) or "\n" in value:
                raise _fail(
                    self,
                    f"environment[{key!r}] must be one line of text.",
                    hint="Pass a string, or a Secret to name the path it is read at.",
                )
        if self.timer is not None:
            self._check_timer()

    def _check_bindings(self) -> None:
        """One credential per name, and every argv word that names one names a bound one."""
        found: dict[str, Binding] = {}
        for binding in (*self.credentials, *self._references()):
            known = found.setdefault(binding.name, binding)
            if known != binding:
                raise _fail(
                    self,
                    f"binds two different credentials named {binding.name!r}.",
                    hint="Declare the Secret once and pass that value everywhere it is read.",
                )
        for argument in self.argv:
            if not isinstance(argument, str):
                continue
            for name in CREDENTIAL_REFERENCE.findall(argument):
                if name not in found:
                    raise _fail(
                        self,
                        f"argv= reads credential {name!r}, which the service does not bind.",
                        hint="Pass the Secret itself as the argument (argv=[..., secret]), or "
                        "credentials=[secret] beside a word that embeds secret.path.",
                    )

    def _references(self) -> tuple[Binding, ...]:
        """The credentials argv and ``environment=`` name, in order."""
        values = (*self.argv[1:], *self.environment.values())
        return tuple(item for item in values if isinstance(item, (Secret, Credential)))

    def _check_timer(self) -> None:
        """A timer starts a run that exits when done: what only a long-lived service has is
        refused rather than dropped."""
        refused = (
            (
                "socket",
                self.socket is not None,
                "a timer runs it on a schedule, a socket on demand",
            ),
            ("readiness", self.readiness is not None, "nothing waits for a scheduled run"),
            ("expose", bool(self.expose), "no port stays open between scheduled runs"),
            (
                "credentials",
                any(not isinstance(c, InstanceKey) for c in self.credentials),
                "secret delivery orders long-lived services only",
            ),
            (
                "state",
                isinstance(self.state, VolumeDirectory),
                "volume unlock orders long-lived services only; use a /var/lib state",
            ),
        )
        for name, given, why in refused:
            if given:
                raise _fail(
                    self,
                    f"runs on a timer, so {name}= does not apply: {why}.",
                    hint=f"Drop {name}=, or drop timer= to run it as a long-lived service.",
                )
        referenced = tuple(r for r in self._references() if not isinstance(r, InstanceKey))
        if referenced:
            raise _fail(
                self,
                "runs on a timer, so it cannot read credential "
                f"{referenced[0].name!r}: secret delivery orders long-lived services only.",
                hint="Drop it from argv= and environment=, or drop timer= to run it as a "
                "long-lived service.",
            )

    @property
    def unit_name(self) -> str:
        """The ``.service`` unit this lowers to."""
        return self.name if self.name.endswith(".service") else f"{self.name}.service"

    # The export a caller references.
    @property
    def ready(self) -> ServiceReady:
        """ "This service is ready": what another service's ``requires=`` takes."""
        return readiness_of(self)

    def endpoint(self, port: int, *, path: str = "/", scheme: str = "http") -> Endpoint:
        """Where this service answers on the loopback, with its readiness: what an ingress
        fronts (``upstream=app.endpoint(8080)``) or a dependent requires. It opens no port
        and changes no listen address."""
        return Endpoint("127.0.0.1", port, path, scheme, ready=self.ready)

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        kind = "Task" if self.timer is not None else "Service" if self.package is None else "Unit"
        return Ref(self, dm.Ref(kind, (self.unit_name,)))

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
        :class:`Endpoint` its URL, a credential ``${CREDENTIALS_DIRECTORY}/<name>``."""
        words: list[str] = []
        for item in self.argv:
            built = executable_of(item)
            if built is not None:
                words.append(built)
            elif isinstance(item, Endpoint):
                words.append(item.url)
            elif isinstance(item, (Secret, Credential)):
                words.append("${CREDENTIALS_DIRECTORY}/" + item.name)
            else:
                words.append(str(item))
        return tuple(words)

    def variables(self) -> dict[str, str]:
        """``environment=`` as text: a credential becomes ``%d/<name>``, systemd's
        specifier for the directory the service reads it in."""
        return {
            key: f"%d/{value.name}" if isinstance(value, (Secret, Credential)) else value
            for key, value in self.environment.items()
        }

    @property
    def bindings(self) -> tuple[Binding, ...]:
        """Every credential this service binds: ``credentials=``, then each one its argv or
        ``environment=`` names."""
        found = {binding.name: binding for binding in self.credentials}
        for binding in self._references():
            found.setdefault(binding.name, binding)
        return tuple(found.values())

    @property
    def state_directory(self) -> str | None:
        """The working directory a ``state=`` implies."""
        if self.state is None:
            return None
        if isinstance(self.state, VolumeDirectory):
            return self.state.path
        return posixpath.join("/var/lib", self.state)

    @property
    def secrets(self) -> tuple[Secret, ...]:
        """The delivered secrets this service binds."""
        return tuple(item for item in self.bindings if isinstance(item, Secret))
