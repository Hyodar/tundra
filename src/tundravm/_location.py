"""Where a value was written: the recipe line that constructed it.

:class:`Located` is the base of every declaration, :class:`Fragment`,
:class:`Variant` and variant change. Construction records the first calling
frame outside tundravm (and outside the standard library and installed
packages) as an absolute ``file:line`` in a slot that is not a dataclass
field, so it never reaches ``__eq__``, ``__hash__``, ``repr``,
``dataclasses.fields`` or any payload, digest or lockfile built from them.
:func:`display_location` renders it relative to the working directory.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from types import FrameType
from typing import Self

_PACKAGE = os.path.dirname(os.path.abspath(__file__)) + os.sep
_LIBRARIES = tuple(
    sorted(
        {
            os.path.abspath(path) + os.sep
            for key in ("stdlib", "platstdlib", "purelib", "platlib", "scripts")
            if (path := sysconfig.get_paths().get(key))
        }
    )
)
_SKIPPED = (_PACKAGE, *_LIBRARIES)
_ANCHOR: ContextVar[str | None] = ContextVar("tundravm_location_anchor", default=None)


@contextmanager
def anchored(location: str) -> Iterator[None]:
    """Locate everything constructed inside at *location*: what tundravm generates while
    resolving (a platform's packages) belongs to the line that asked for it."""
    token = _ANCHOR.set(location)
    try:
        yield
    finally:
        _ANCHOR.reset(token)


def caller_location() -> str:
    """``file:line`` of the innermost frame outside tundravm and installed code, else ``""``.

    Inside :func:`anchored`, its location instead.
    """
    if (anchor := _ANCHOR.get()) is not None:
        return anchor
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        filename = frame.f_code.co_filename
        if not filename.startswith(("<", *_SKIPPED)):
            if not os.path.isabs(filename):
                filename = os.path.abspath(filename)
                if filename.startswith(_SKIPPED):
                    frame = frame.f_back
                    continue
            return f"{filename}:{frame.f_lineno}"
        frame = frame.f_back
    return ""


class Located:
    """Base recording where each instance was constructed (:attr:`declared_at`).

    The location is not part of the value: equal declarations written on
    different lines stay equal and hash, digest and lock the same.
    """

    __slots__ = ("_declared_at",)

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        self = object.__new__(cls)
        object.__setattr__(self, "_declared_at", caller_location())
        return self

    @property
    def declared_at(self) -> str:
        """Absolute ``file:line`` of the constructing call, ``""`` when unknown."""
        return getattr(self, "_declared_at", "")

    def __copy__(self) -> Self:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        return self


def located[T](value: T, location: str) -> T:
    """*value* with :attr:`Located.declared_at` set to *location* (a copy made inside tundravm
    keeps the original's line); returns *value*."""
    if isinstance(value, Located):
        object.__setattr__(value, "_declared_at", location)
    return value


def declared_at(value: object) -> str:
    """*value*'s absolute ``file:line``, ``""`` when it is not :class:`Located` or unknown."""
    return value.declared_at if isinstance(value, Located) else ""


def display_location(location: str, root: str | None = None) -> str:
    """*location* with its file relative to *root* (default: the working directory).

    A file outside *root* stays absolute; ``""`` stays ``""``.
    """
    if not location:
        return ""
    path, sep, line = location.rpartition(":")
    if not sep or not line.isdigit():
        return location
    base = os.path.abspath(root if root is not None else os.getcwd())
    if path == base or path.startswith(base + os.sep):
        path = os.path.relpath(path, base)
    return f"{path}:{line}"


__all__ = ["Located", "anchored", "caller_location", "declared_at", "display_location", "located"]
