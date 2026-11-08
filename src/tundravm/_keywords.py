"""A public value's constructor call, checked against its keywords before ``__init__`` runs.

Python's own refusal of an unknown keyword, a missing argument or a positional too many is
a ``TypeError`` naming ``__init__()``. :class:`Keywords` turns each into a
:class:`~tundravm.errors.ValidationError` that names the class and the keyword as the image
file spells them, with the closest keyword the class takes as its fix. A public method of
the class (``Service.endpoint``, ``File.copy``) refuses a keyword it does not take the same
way.
"""

from __future__ import annotations

import dataclasses
import difflib
import functools
import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self
from weakref import WeakKeyDictionary

from tundravm.errors import ValidationError

_Spec = tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]
_SPECS: WeakKeyDictionary[type, _Spec | None] = WeakKeyDictionary()


class Keywords:
    """Base of a public value whose constructor refuses a misspelt call in the image's words."""

    __slots__ = ()

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        check_call(cls, args, kwargs)
        return object.__new__(cls)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for name, member in list(vars(cls).items()):
            checked = _checked_method(cls.__name__, name, member)
            if checked is not None:
                setattr(cls, name, checked)


_CHECKED = "__tundravm_keywords__"
"""The attribute that marks a method :func:`_checked_method` already wraps: a slotted
dataclass is built again from the class's namespace, which holds the wrapper."""


def _checked_method(kind: str, name: str, member: object) -> object | None:
    """*member* refusing a keyword it does not take as :func:`check_call` refuses one, when
    it is a public method, classmethod or staticmethod that takes keywords but no ``**``."""
    binding = type(member) if isinstance(member, (classmethod, staticmethod)) else None
    function = getattr(member, "__func__", None) if binding is not None else member
    if name.startswith("_") or not inspect.isfunction(function) or hasattr(function, _CHECKED):
        return None
    parameters = list(inspect.signature(function).parameters.values())
    parameters = parameters if binding is staticmethod else parameters[1:]
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return None
    named = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    keywords = tuple(p.name for p in parameters if p.kind in named)
    if not keywords:
        return None
    method: Callable[..., object] = function
    taken = frozenset(keywords)

    @functools.wraps(method)
    def checked(*args: object, **kwargs: object) -> object:
        if not kwargs.keys() <= taken:
            given = args if binding is staticmethod else args[1:]
            if binding is None:
                owner = subject(args[0])
            elif binding is classmethod:
                owner = _PUBLIC.get(getattr(args[0], "__name__", kind), kind)
            else:
                owner = _PUBLIC.get(kind, kind)
            _refuse_keyword(f"{owner}.{name}()", f"{owner}.{name}", given, kwargs, keywords)
        return method(*args, **kwargs)

    setattr(checked, _CHECKED, keywords)
    return checked if binding is None else binding(checked)


def check_call(cls: type, args: Sequence[object], kwargs: Mapping[str, object]) -> None:
    """Refuse a call of *cls* that its ``__init__`` would refuse with a ``TypeError``.

    A call with no arguments is let through: ``copy`` and ``pickle`` make one, and a class
    that needs arguments still refuses it in ``__init__``.
    """
    if not args and not kwargs:
        return
    spec = _spec(cls)
    if spec is None:
        return
    positional, keywords, required = spec
    if all(key in keywords for key in kwargs) and len(args) <= len(positional):
        given = {*positional[: len(args)], *kwargs}
        if all(name in given for name in required):
            return
    _refuse(cls, args, kwargs, spec)


def _spec(cls: type) -> _Spec | None:
    """``(positional, keywords, required)`` parameter names of *cls*'s ``__init__``, ``None``
    when it takes ``*args`` or ``**kwargs``."""
    if cls in _SPECS:
        return _SPECS[cls]
    try:
        init = vars(cls).get("__init__") or next(
            vars(base)["__init__"] for base in cls.__mro__ if "__init__" in vars(base)
        )
        parameters = list(inspect.signature(init).parameters.values())[1:]
    except (TypeError, ValueError):
        found: _Spec | None = None
    else:
        kinds = {parameter.kind for parameter in parameters}
        found = (
            None
            if kinds & {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
            else (
                tuple(
                    p.name
                    for p in parameters
                    if p.kind
                    in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ),
                tuple(p.name for p in parameters if p.kind != inspect.Parameter.POSITIONAL_ONLY),
                tuple(p.name for p in parameters if p.default is inspect.Parameter.empty),
            )
        )
    _SPECS[cls] = found
    return found


_EXAMPLES = {
    ("Service", "argv"): '["/usr/bin/app", "--flag"]',
    ("Git", "ref"): '"v1.2.3"',
    ("Volume", "unlock"): "OperatorKey()",
    ("GoBinary", "source"): 'Local("app")',
    ("CargoBinary", "source"): 'Local("app")',
    ("Build", "source"): 'Local("app")',
    ("File", "content"): '"text"',
    ("Credential", "source"): '"/run/app/token"',
}
"""What a missing argument looks like where ``...`` would leave a new user guessing."""


def _word(value: object) -> str:
    """*value* as the image file writes it, or ``...`` for anything but a short string."""
    if isinstance(value, str) and value and len(value) <= 60 and not {'"', "\n"} & set(value):
        return f'"{value}"'
    return "..."


def _refuse(cls: type, args: Sequence[object], kwargs: Mapping[str, object], spec: _Spec) -> None:
    positional, keywords, required = spec
    kind = cls.__name__
    named = bool(args) and positional[:1] == ("name",) and _word(args[0]) != "..."
    subject = f"{kind}({_word(args[0])})" if named else kind
    given = [_word(arg) for arg in args[: len(positional)]]
    _refuse_keyword(subject, kind, args[: len(positional)], kwargs, keywords)
    if len(args) > len(positional):
        rest = [name for name in keywords if name not in positional]
        raise ValidationError(
            f"{subject} takes {_count(len(positional))}, and the call passes {len(args)}.",
            hint=f"Name each one: {kind}({', '.join([*given, *(f'{n}=...' for n in rest[:3])])})"
            if rest
            else f"Drop the extra arguments: {kind}({', '.join(given)}).",
        )
    missing = [name for name in required if name not in {*positional[: len(args)], *kwargs}]
    filled = [f"{name}={_EXAMPLES.get((kind, name), '...')}" for name in missing]
    raise ValidationError(
        f"{subject} needs {_listing(missing)}.",
        hint=f"{kind}({', '.join([*given, *filled])})",
    )


def _refuse_keyword(
    subject: str,
    call: str,
    args: Sequence[object],
    kwargs: Mapping[str, object],
    keywords: Sequence[str],
) -> None:
    """Refuse the first of *kwargs* that is not one of *keywords*, its fix *call* with the
    closest one: ``Service("app") takes no keyword enviroment=``."""
    given = [_word(arg) for arg in args]
    for key in kwargs:
        if key not in keywords:
            close = difflib.get_close_matches(key, keywords, n=1)
            raise ValidationError(
                f"{subject} takes no keyword {key}=.",
                hint=f"{call}({', '.join([*given, f'{close[0]}=...'])})"
                if close
                else f"Spell one it takes: {_listing(keywords)}.",
            )


_PUBLIC = {
    "FileTree": "Tree",
    "GoBuild": "Go",
    "CargoBuild": "Cargo",
    "DotnetBuild": "Dotnet",
    "Unit": "Service",
    "Task": "Service",
    "Platform": "Debian",
    "Repository": "AptRepository",
    "Variant": "Profile",
    "Recipe": "Image",
}
"""The public class an image file writes for each internal one it lowers to."""
_NAMING = ("path", "url", "src", "source")
"""A first field that names its value when the class has no ``name``: ``Local("app")``."""


def subject(owner: object) -> str:
    """*owner* as the image file names it: ``Service("app")``, ``Local("app")``,
    ``Kernel``; a keyword-only ``name`` field reads ``Postgres(name="db")``."""
    kind = _PUBLIC.get(type(owner).__name__, type(owner).__name__)
    if not dataclasses.is_dataclass(owner):
        return kind
    fields = [f for f in dataclasses.fields(owner) if f.init]
    field = next((f for f in fields if f.name == "name"), None)
    if field is None and fields and fields[0].name in _NAMING and not fields[0].kw_only:
        field = fields[0]
    name = getattr(owner, field.name, None) if field is not None else None
    if kind == "Service" and isinstance(name, str):
        name = name.removesuffix(".service").removesuffix(".timer")
    if field is None or _word(name) == "...":
        return kind
    if field.kw_only:
        return f"{kind}({field.name}={_word(name)})"
    return f"{kind}({_word(name)})"


def shown(value: object) -> str:
    """*value* short enough for one line of an error: a declaration as :func:`subject`
    spells it, anything else as its ``repr``, cut at 60 characters."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        named = subject(value)
        return named if named.endswith(")") else f"{named}(...)"
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _count(number: int) -> str:
    if number == 0:
        return "keywords only"
    return f"{number} positional argument{'s' if number != 1 else ''}"


def _listing(names: Sequence[str]) -> str:
    return ", ".join(f"{name}=" for name in names) or "no keywords"


__all__ = ["Keywords", "check_call", "shown", "subject"]
