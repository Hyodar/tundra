"""Components: a named group of declarations written as a class, and what it exports.

A :class:`Component` subclass is a frozen keyword-only dataclass. Its fields are
configuration, :meth:`Component.compose` returns the declarations (and nested
components) it stands for, and an optional ``check(self, resolved)`` returns
diagnostics. Its **exports** are the typed values a caller references without
knowing any of that: :class:`ServiceReady` ("it is up"), :class:`Endpoint` ("it
answers here, and here is its readiness") and :class:`ServiceRef` (the service a
caller may depend on or edit from a :class:`~tundravm.Profile`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, fields, is_dataclass
from typing import TYPE_CHECKING, Any, dataclass_transform

from tundravm._location import Located
from tundravm._source import FrozenMap
from tundravm.declarative.model import Ref
from tundravm.errors import ValidationError

from ._values import Readiness

if TYPE_CHECKING:
    from ._service import Service

__all__ = [
    "BootReady",
    "Component",
    "ComponentReady",
    "Endpoint",
    "ServiceReady",
    "ServiceRef",
    "builds_with_network",
    "component_name",
    "composed",
    "export_providers",
]

_BUILT = "__tundravm_component__"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


# ── Exports ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ServiceReady:
    """ "That service is ready": ``service.ready``, what ``requires=`` takes.

    *probe* is the readiness mechanism the service declared, so a dependent unit is
    ordered after what proves readiness rather than after the exec.
    """

    unit: str
    probe: Readiness | None = field(default=None, compare=False, repr=False)

    def __str__(self) -> str:
        return self.unit


@dataclass(frozen=True, slots=True)
class BootReady:
    """ "That boot step has finished": ``volume.ready``, ``secret.ready``.

    *unit* is a oneshot the image generates (the volume's unlock-and-prepare step, the
    wait for a delivered credential); its start job finishing is what proves it, so a
    dependent unit gets ``Requires=`` and ``After=`` on it and nothing else.
    """

    unit: str

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit.endswith(".service"):
            raise ValidationError(
                f"BootReady: {self.unit!r} is not a .service unit.",
                hint="Use volume.ready or secret.ready.",
            )

    def __str__(self) -> str:
        return self.unit


type Readied = ServiceReady | BootReady
"""What proves one thing ready: a service's readiness, or a boot step finishing."""


@dataclass(frozen=True, slots=True)
class ComponentReady:
    """ "That component is up": ``component.ready``.

    It stands for :meth:`Component.readiness`: by default the readiness of every service
    the component composes that declares one; a component whose services declare none,
    and that names no other readiness, cannot be waited for.
    """

    component: Component = field(repr=False)

    @property
    def name(self) -> str:
        """The component's name."""
        return self.component.name

    def handles(self) -> tuple[Readied, ...]:
        """What proves the component ready, one handle per unit."""
        return self.component.readiness()

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where a component answers, with its readiness: an export a caller can wire.

    It renders as its :attr:`url` in an argv, and ``requires=[endpoint]`` waits for the
    service behind it.
    """

    host: str
    port: int
    path: str = "/"
    scheme: str = "http"
    ready: ServiceReady | None = field(default=None, compare=False)
    provider: str | Component | None = field(default=None, compare=False, repr=False)
    """The component that publishes it (``provider=self``): a consumer holding this
    endpoint in a field requires that component, without naming it."""

    def __post_init__(self) -> None:
        if isinstance(self.provider, Component):
            object.__setattr__(self, "provider", self.provider.name)
        schemes = ("http", "https", "ws", "wss")
        problem = None
        if not isinstance(self.host, str) or not self.host or any(c in self.host for c in "/ "):
            problem = f"host must be a host name or address, got {self.host!r}"
        elif isinstance(self.port, bool) or not isinstance(self.port, int):
            problem = f"port must be an int, got {self.port!r}"
        elif not 0 < self.port < 65536:
            problem = f"port must be in 1..65535, got {self.port}"
        elif not isinstance(self.path, str) or not self.path.startswith("/"):
            problem = f"path must start with '/', got {self.path!r}"
        elif self.scheme not in schemes:
            problem = f"scheme must be one of {', '.join(schemes)}, got {self.scheme!r}"
        if problem is not None:
            raise ValidationError(f"Endpoint: {problem}.", hint="Pass Endpoint('127.0.0.1', 80).")

    @property
    def address(self) -> str:
        """``host:port``, with an IPv6 host in brackets."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        """``scheme://host:port/path``."""
        return f"{self.scheme}://{self.address}{self.path}"

    @property
    def loopback(self) -> bool:
        """Whether only the image itself can reach it (``127.0.0.0/8``, ``::1``,
        ``localhost``)."""
        return self.host in ("localhost", "::1") or self.host.startswith("127.")

    def with_scheme(self, scheme: str) -> Endpoint:
        """The same address under *scheme* (``ws`` for a JSON-RPC server's WebSocket)."""
        return Endpoint(
            self.host, self.port, self.path, scheme, ready=self.ready, provider=self.provider
        )

    def __str__(self) -> str:
        return self.url


@dataclass(frozen=True, slots=True)
class ServiceRef:
    """A service a component composed, named from outside: ``raiko.service("raiko")``.

    ``.ready`` waits for it and ``.ref`` is what a :class:`~tundravm.Profile`
    edits or removes, so a caller changes one service of a component without the
    component exposing its unit file.
    """

    unit: str
    probe: Readiness | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.unit, str) or not self.unit:
            raise ValidationError(
                "ServiceRef: a service name is required.",
                hint="Use component.service('app').",
            )
        if not self.unit.endswith(".service"):
            object.__setattr__(self, "unit", f"{self.unit}.service")

    @property
    def ready(self) -> ServiceReady:
        """The readiness handle of that service."""
        return ServiceReady(self.unit, self.probe)

    @property
    def ref(self) -> Ref:
        """The reference a profile change targets."""
        return Ref("Service", (self.unit,))


# ── The base class ──────────────────────────────────────────────────────


class _ComponentMeta(type):
    """Makes every :class:`Component` subclass a frozen, keyword-only dataclass."""

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        *,
        fragment: str | None = None,
        requires: tuple[str, ...] = (),
        build_network: bool = False,
        **kwargs: Any,
    ) -> Any:
        if namespace.pop(_BUILT, False) or not any(
            isinstance(base, _ComponentMeta) for base in bases
        ):
            return super().__new__(mcls, name, bases, namespace, **kwargs)
        inherited = any(
            "compose" in vars(klass)
            for base in bases
            for klass in base.__mro__
            if klass.__name__ != "Component"
        )
        if not inherited and "compose" not in namespace:
            raise ValidationError(
                f"Component {name} has no compose() method.",
                hint="Define compose(self) returning the declarations it stands for.",
            )
        namespace[_BUILT] = True
        namespace["__fragment_name__"] = fragment
        namespace["__fragment_requires__"] = tuple(requires)
        namespace["__build_network__"] = bool(build_network)
        for attribute in namespace.get("__annotations__", {}):
            default = namespace.get(attribute)
            if isinstance(default, list):
                namespace[attribute] = tuple(default)
            elif isinstance(default, dict):
                namespace[attribute] = FrozenMap(default)
        cls = super().__new__(mcls, name, bases, namespace, **kwargs)
        return dataclass(frozen=True, slots=True, kw_only=True)(cls)


@dataclass_transform(kw_only_default=True, frozen_default=True)
class Component(Located, metaclass=_ComponentMeta):
    """A named group of declarations, written as a class, with typed exports.

    Subclass it and annotate the configuration fields; the subclass becomes a frozen,
    keyword-only dataclass, so two components with the same configuration are equal and
    its ``repr`` is what it was called with. :meth:`compose` returns the declarations and
    nested components it stands for; they are resolved before validation, so a component
    cannot be smuggled into ``services=``. An optional ``check(self, resolved)`` returning
    diagnostics becomes the component's lint rule.

    Its name, which ``show`` and every diagnostic about it print, comes from the class
    (``TaikoClient`` is ``taiko-client``), from an ``id`` or ``name`` field when it has
    one, or from ``class X(Component, fragment="x")``. ``class X(Component, build_network=True)``
    says its builds fetch dependencies while the image is built (``cargo fetch``,
    ``dotnet restore``), so an image holding it builds with the network on, which the
    tree records (``WithNetwork=yes``).

    Exports are properties returning :class:`Endpoint`, :class:`ServiceRef` or
    :class:`ServiceReady`, so a caller wires ``requires=[db.ready]`` or
    ``[app.executable, "--db", db.endpoint]`` without naming a unit, a port or a path
    inside the component::

        class Postgres(Component):
            port: int = 5432

            @property
            def endpoint(self) -> Endpoint:
                return Endpoint("127.0.0.1", self.port, ready=self.ready)

            def compose(self) -> Iterator[Service]:
                yield Service("postgres", [...], readiness=TcpProbe(self.port))
    """

    __fragment_name__: str | None = None
    __fragment_requires__: tuple[str, ...] = ()
    __build_network__: bool = False

    def compose(self) -> Iterable[object]:
        """The declarations and nested components this component stands for."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compose().")

    @property
    def name(self) -> str:
        """The component's name, as ``show`` and its diagnostics print it."""
        return component_name(self)

    @property
    def ready(self) -> ComponentReady:
        """ "Every service this component declares readiness for is ready"."""
        return ComponentReady(self)

    def readiness(self) -> tuple[Readied, ...]:
        """What :attr:`ready` waits for: the readiness of each service it composes that
        declares one. A component whose readiness is not a service of its own (one that
        wraps a packaged or internal unit) overrides this."""
        return tuple(service.ready for service in self.services() if service.readiness is not None)

    def service(self, name: str) -> ServiceRef:
        """The service called *name*, for ``requires=`` and for a profile's ``changes=``.

        The component must compose it; naming one it does not is refused.
        """
        services = self.services()
        unit = name if name.endswith(".service") else f"{name}.service"
        for item in services:
            if item.unit_name == unit:
                return ServiceRef(unit, item.readiness)
        known = ", ".join(sorted(item.unit_name for item in services)) or "none"
        raise ValidationError(
            f"{type(self).__name__} composes no service {name!r}.",
            hint=f"It composes: {known}.",
        )

    def services(self) -> tuple[Service, ...]:
        """Every service this component composes, nested components included."""
        from ._service import Service as ServiceValue

        found: list[Service] = []
        for item in composed(self):
            if isinstance(item, ServiceValue):
                found.append(item)
            elif isinstance(item, Component):
                found.extend(item.services())
        return tuple(found)


def composed(owner: Component) -> tuple[object, ...]:
    """What ``owner.compose()`` returned, as a tuple; a non-iterable is refused."""
    items = owner.compose()
    if isinstance(items, (str, bytes)) or not isinstance(items, Iterable):
        raise ValidationError(
            f"{type(owner).__name__}.compose() returned {items!r}, not declarations.",
            hint="Return (or yield) the declarations and components it holds.",
        )
    return tuple(items)


def component_name(owner: Component) -> str:
    """The fragment name of *owner*: ``fragment=``, else its ``id``/``name`` field, else
    the class name in kebab case."""
    declared = type(owner).__fragment_name__
    if declared is not None:
        return declared
    if is_dataclass(owner):
        names = {item.name for item in fields(owner) if item.init}
        for attribute in ("id", "name"):
            if attribute in names:
                value = getattr(owner, attribute)
                if not isinstance(value, str) or not value:
                    raise ValidationError(
                        f"{type(owner).__name__}: {attribute} must be a non-empty string, "
                        f"got {value!r}.",
                        hint=f"Pass {attribute}='...'; it names the component.",
                    )
                return value
    return _CAMEL_BOUNDARY.sub("-", type(owner).__name__).lower()


def builds_with_network(owner: Component) -> bool:
    """Whether *owner*, or a component it composes, builds with the network on."""
    if type(owner).__build_network__:
        return True
    return any(builds_with_network(item) for item in composed(owner) if isinstance(item, Component))


def export_providers(owner: Component) -> Iterator[str]:
    """The components whose exports *owner* holds in a field: what its fragment requires."""
    if not is_dataclass(owner):
        return
    yield from type(owner).__fragment_requires__
    for item in fields(owner):
        if not item.init:
            continue
        value = getattr(owner, item.name)
        candidates = value if isinstance(value, tuple) else (value,)
        for candidate in candidates:
            if isinstance(candidate, (ComponentReady, Endpoint, ServiceRef)):
                provider = _provider_of(candidate)
                if provider is not None:
                    yield provider


def _provider_of(export: ComponentReady | Endpoint | ServiceRef) -> str | None:
    """The fragment that publishes *export*, when the export names one."""
    if isinstance(export, ComponentReady):
        return export.name
    if isinstance(export, Endpoint) and isinstance(export.provider, str):
        return export.provider
    return None
