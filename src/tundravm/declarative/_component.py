"""Components: fragments written as plain classes, and the typed values they export.

:class:`Composite` is a :class:`Fragment` built from its own dataclass fields;
:func:`component` turns an annotated class into one. :class:`Export` values
(:class:`HttpEndpoint`) let one component hand another a typed fact (where it
listens) instead of the consumer parsing the provider's files.
"""

from __future__ import annotations

import re
import sys
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import KW_ONLY, dataclass, field, fields
from typing import Literal, Union, dataclass_transform, get_args, get_origin, overload

from tundravm._source import FrozenMap
from tundravm.errors import ValidationError

from .model import Check, Declaration, Diagnostic, Fragment, Resolved

_DERIVED = ("name", "items", "requires", "checks")
_PRIMITIVES: tuple[type, ...] = (str, int, float, bool, type(None))
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_FIELD_TYPES: dict[type, dict[str, tuple[type, ...]]] = {}


@dataclass(frozen=True, slots=True)
class Composite(Fragment):
    """A :class:`Fragment` built from its own fields.

    Write one with :func:`component`. Construction freezes list fields into
    tuples and mappings into ``FrozenMap`` copies, then calls :meth:`compose`
    once: it returns the fragment's items (or a whole :class:`Fragment`, for
    full control over ``name``, ``requires`` and ``checks``). The instance's
    ``repr`` and equality are its configuration fields.
    """

    name: str = field(init=False, repr=False, compare=False)
    items: tuple[Declaration | Fragment, ...] = field(init=False, repr=False, compare=False)
    requires: tuple[str, ...] = field(init=False, repr=False, compare=False)
    checks: tuple[Check, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        settings: _Settings | None = getattr(type(self), "__component__", None)
        for item in fields(self):
            if not item.init:
                continue
            if isinstance(value := getattr(self, item.name), list):
                object.__setattr__(self, item.name, tuple(value))
            elif isinstance(value, Mapping):
                object.__setattr__(self, item.name, FrozenMap(value))
            if settings is not None:
                _check_field(self, item.name, getattr(self, item.name))
        name = self._fragment_name(settings)
        object.__setattr__(self, "name", name)
        composed = self.compose()
        if not isinstance(composed, Fragment):
            composed = self._wrap(name, composed)
        requires = [*composed.requires, *(settings.requires if settings else ())]
        requires.extend(_providers(self))
        object.__setattr__(self, "name", composed.name)
        object.__setattr__(self, "items", composed.items)
        object.__setattr__(
            self, "requires", tuple(dict.fromkeys(r for r in requires if r != composed.name))
        )
        object.__setattr__(self, "checks", composed.checks)

    def compose(self) -> Iterable[Declaration | Fragment] | Fragment:
        """The fragment's items, or the whole :class:`Fragment` this instance stands for."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compose().")

    def _fragment_name(self, settings: _Settings | None) -> str:
        if settings is not None and settings.name is not None:
            return settings.name
        for attribute in ("id", "name"):
            if attribute in {item.name for item in fields(self) if item.init}:
                value = getattr(self, attribute)
                if not isinstance(value, str) or not value:
                    raise ValidationError(
                        f"{type(self).__name__}: {attribute} must be a non-empty string, "
                        f"got {value!r}.",
                        hint=f"The {attribute} field names the fragment; pass {attribute}='...'.",
                    )
                return value
        return _CAMEL.sub("-", type(self).__name__).lower()

    def _wrap(self, name: str, composed: object) -> Fragment:
        if isinstance(composed, (str, bytes)) or not isinstance(composed, Iterable):
            raise ValidationError(
                f"{type(self).__name__}.compose() returned {composed!r}, not items or a Fragment.",
                hint="Return (or yield) the declarations and fragments the component holds.",
            )
        check: Callable[[Resolved], Iterable[Diagnostic]] | None = getattr(self, "check", None)
        checks = () if check is None else (_collected(check),)
        return Fragment(name, items=tuple(composed), checks=checks)


@dataclass(frozen=True, slots=True)
class _Settings:
    name: str | None
    requires: tuple[str, ...]


@overload
def component[T](cls: type[T], /) -> type[T]: ...
@overload
def component[T](
    *, name: str | None = None, requires: Iterable[str] = ()
) -> Callable[[type[T]], type[T]]: ...


@dataclass_transform(kw_only_default=True, frozen_default=True)
def component[T](
    cls: type[T] | None = None, /, *, name: str | None = None, requires: Iterable[str] = ()
) -> type[T] | Callable[[type[T]], type[T]]:
    """Make the annotated class *cls* a :class:`Composite`, a frozen keyword-only dataclass.

    Its annotated attributes are its configuration fields; a ``str``, ``int``,
    ``float`` or ``bool`` field (or one of those ``| None``) refuses a value of
    another type, and a list or dict default is frozen like a passed value.
    ``compose(self)`` returns (or yields) the fragment's items. The
    fragment is named *name*, else after an ``id`` (or ``name``) field, else after
    the class (``HealthCheck`` is ``health-check``). It requires *requires* plus
    the provider of each :class:`Export` held in a field; a ``check(self, resolved)``
    method returning diagnostics becomes its lint check. Subclass ``Composite``
    to let a type checker see instances as fragments; a plain class is rebased.
    """
    settings = _Settings(name, tuple(requires))
    if isinstance(requires, str):
        raise ValidationError(
            f"component(requires={requires!r}) takes fragment names, not one string.",
            hint=f"Pass requires=({requires!r},).",
        )

    def wrap(cls: type[T]) -> type[T]:
        if not any("compose" in vars(klass) for klass in cls.__mro__ if klass is not Composite):
            raise ValidationError(
                f"@component class {cls.__name__} has no compose() method.",
                hint="Define compose(self) returning the fragment's declarations.",
            )
        if not issubclass(cls, Composite):
            namespace = {k: v for k, v in vars(cls).items() if k not in ("__dict__", "__weakref__")}
            bases = tuple(base for base in cls.__bases__ if base is not object)
            cls = type(cls.__name__, (*bases, Composite), namespace)
        for attribute in vars(cls).get("__annotations__", {}):
            default = vars(cls).get(attribute)
            if isinstance(default, list):
                setattr(cls, attribute, tuple(default))
            elif isinstance(default, dict):
                setattr(cls, attribute, FrozenMap(default))
        cls.__component__ = settings  # type: ignore[attr-defined]
        return dataclass(frozen=True, slots=True, kw_only=True)(cls)

    return wrap if cls is None else wrap(cls)


def _check_field(owner: Composite, name: str, value: object) -> None:
    allowed = _primitive_fields(type(owner)).get(name)
    if allowed is None:
        return
    if isinstance(value, allowed) and (bool in allowed or not isinstance(value, bool)):
        return
    expected = " or ".join("None" if kind is type(None) else kind.__name__ for kind in allowed)
    raise ValidationError(
        f"{type(owner).__name__}: {name} must be {expected}, got {value!r}.",
        hint=f"Pass {name}= a value of the annotated type.",
    )


def _primitive_fields(cls: type) -> dict[str, tuple[type, ...]]:
    """The fields of *cls* annotated with primitives, mapped to the types they accept."""
    if (cached := _FIELD_TYPES.get(cls)) is not None:
        return cached
    found: dict[str, tuple[type, ...]] = {}
    for klass in reversed(cls.__mro__):
        namespace = vars(sys.modules[klass.__module__]) if klass.__module__ in sys.modules else {}
        for name, annotation in vars(klass).get("__annotations__", {}).items():
            try:
                hint = (
                    eval(annotation, dict(namespace)) if isinstance(annotation, str) else annotation
                )
            except Exception:
                found.pop(name, None)
                continue
            kinds = get_args(hint) if get_origin(hint) in (Union, types.UnionType) else (hint,)
            if kinds and all(kind in _PRIMITIVES for kind in kinds):
                found[name] = (*kinds, int) if float in kinds else tuple(kinds)
            else:
                found.pop(name, None)
    _FIELD_TYPES[cls] = found
    return found


def exports(owner: Fragment) -> Iterable[tuple[str, Export]]:
    """The :class:`Export` values *owner*'s configuration fields hold, with the field names."""
    if not isinstance(owner, Composite):
        return
    for item in fields(owner):
        if not item.init:
            continue
        value = getattr(owner, item.name)
        for candidate in value if isinstance(value, tuple) else (value,):
            if isinstance(candidate, Export):
                yield item.name, candidate


def _providers(owner: Composite) -> Iterable[str]:
    for _, export in exports(owner):
        provider = export.provider
        name = provider.name if isinstance(provider, Fragment) else provider
        if name is not None:
            yield name


def _collected(check: Callable[[Resolved], Iterable[Diagnostic]]) -> Check:
    """*check* (a component's ``check`` method) as a fragment check returning a tuple."""

    def run(resolved: Resolved) -> tuple[Diagnostic, ...]:
        return tuple(check(resolved))

    return run


@dataclass(frozen=True, slots=True, kw_only=True)
class Export:
    """A typed value a component publishes for other components to consume.

    The publisher builds it in a property with ``provider=self``; a component
    holding it in a field then requires the provider's fragment, so neither
    side names the other's fragment or parses its files. ``provider=self``
    keeps the publishing instance in :attr:`origin` and stores its fragment
    name in ``provider``: resolution reports ``export-stale`` when a variant
    holds another configuration of that fragment, or edits what it composed.
    A bare name (``provider=self.name``) binds the name only. Subclass it as a
    frozen dataclass for other facts (a socket path, a database address),
    calling ``Export.__post_init__(self)`` from your own ``__post_init__``.
    """

    provider: str | Fragment | None = None
    origin: Fragment | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        provider: object = self.provider
        if isinstance(provider, Fragment):
            object.__setattr__(self, "origin", provider)
            object.__setattr__(self, "provider", provider.name)


@dataclass(frozen=True, slots=True)
class HttpEndpoint(Export):
    """Where a service answers HTTP: *host*, *port* and *path*, joined in :attr:`url`."""

    host: str
    port: int
    path: str = "/"
    _: KW_ONLY
    scheme: Literal["http", "https"] = "http"

    def __post_init__(self) -> None:
        Export.__post_init__(self)
        problem = None
        if not isinstance(self.host, str) or not self.host or any(c in self.host for c in "/ "):
            problem = f"host must be a host name or address, got {self.host!r}"
        elif isinstance(self.port, bool) or not isinstance(self.port, int):
            problem = f"port must be an int, got {self.port!r}"
        elif not 0 < self.port < 65536:
            problem = f"port must be in 1..65535, got {self.port}"
        elif not isinstance(self.path, str) or not self.path.startswith("/"):
            problem = f"path must start with '/', got {self.path!r}"
        elif self.scheme not in ("http", "https"):
            problem = f"scheme must be 'http' or 'https', got {self.scheme!r}"
        if problem is not None:
            raise ValidationError(
                f"HttpEndpoint: {problem}.",
                hint="Pass HttpEndpoint('127.0.0.1', 8080, '/path').",
            )

    @property
    def address(self) -> str:
        """``host:port``, with an IPv6 host in brackets."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def url(self) -> str:
        """``scheme://host:port/path``."""
        return f"{self.scheme}://{self.address}{self.path}"
