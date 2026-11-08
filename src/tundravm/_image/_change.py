"""What a :class:`~tundravm.Profile` changes: a value the image declares, by reference.

``value.ref`` names a :class:`~tundravm.Service`, :class:`~tundravm.File`,
:class:`~tundravm.Tree` or :class:`~tundravm.Package`; ``ref.edit(...)`` takes the keyword
names of that value's own constructor, and ``ref.remove()`` drops it. An edit is applied to
the value at once, so the edited value is checked like any other; the lowering then lowers
the profile's services as edited, so the profile builds what the edited value would have
built had the image declared it so, and nothing it no longer needs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tundravm._location import Located
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.errors import ValidationError

__all__ = ["EDITABLE", "Change", "Edit", "Ref", "Remove"]

EDITABLE: Mapping[str, tuple[str, ...]] = {
    "Service": (
        "argv",
        "requires",
        "readiness",
        "state",
        "credentials",
        "expose",
        "resources",
        "shutdown",
        "hardening",
        "timer",
        "socket",
        "unit",
        "user",
        "groups",
        "quotes",
        "working_directory",
        "environment",
        "logs",
        "runtime",
    ),
    "File": ("content", "mode"),
    "Tree": ("src", "exclude"),
    "Package": (),
}
"""The constructor keywords an edit may set, per kind of value."""

_FIXED: Mapping[str, Mapping[str, str]] = {
    "Service": {
        "name": "it names the service",
    },
    "File": {
        "path": "it names the file",
        "owner": "the owner is applied at boot, from the image's own files",
    },
    "Tree": {
        "dst": "it names the tree",
        "owner": "the owner is applied at boot, from the image's own files",
    },
    "Package": {
        "name": "it names the package",
        "backports": "the archive is chosen once, for the whole image",
    },
}
"""Constructor keywords an edit cannot set, each with the reason."""


@dataclass(frozen=True, slots=True)
class Ref:
    """A value the image declares, named for a :class:`~tundravm.Profile` change.

    ``value.ref.edit(...)`` sets keywords of the value's constructor;
    ``value.ref.remove()`` drops it, and what only it needed.
    """

    value: object = field(repr=False)
    """The :class:`~tundravm.Service`, :class:`~tundravm.File`, :class:`~tundravm.Tree` or
    :class:`~tundravm.Package` named, ``None`` for a bare
    :class:`~tundravm.components.ServiceRef`."""
    target: dm.Ref = field(repr=False)
    """The declaration the value lowers to."""

    def __str__(self) -> str:
        if self.value is None:
            return f"Service({self.target.identity[0]!r})"
        key = next(
            getattr(self.value, name)
            for name in ("name", "path", "dst")
            if hasattr(self.value, name)
        )
        return f"{type(self.value).__name__}({key!r})"

    def __repr__(self) -> str:
        return f"<ref to {self}>"

    def edit(self, **fields: object) -> Edit:
        """A change that sets *fields*, keywords of the value's constructor."""
        return Edit(self, fields)

    def remove(self) -> Remove:
        """A change that drops the value."""
        return Remove(self)


@dataclass(frozen=True, slots=True)
class Edit(Located):
    """Set constructor keywords of a value the image declares: ``app.ref.edit(user="www")``.

    :attr:`edited` is the value with them set, built and checked at once.
    """

    ref: Ref
    fields: Mapping[str, object]
    edited: Any = field(init=False, repr=False, compare=False)
    """The value with :attr:`fields` set."""

    def __post_init__(self) -> None:
        if not isinstance(self.ref, Ref):
            raise ValidationError(f"Edit: {self.ref!r} is not a value's ref.", hint=_HINT)
        object.__setattr__(self, "fields", FrozenMap(dict(self.fields)))
        value = self.ref.value
        if value is None:
            raise ValidationError(
                f"The edit of {self.ref} names a service whose value it does not carry.",
                hint="Take the ref from the component: component.service('name').ref.",
            )
        kind = type(value).__name__
        accepted = EDITABLE.get(kind, ())
        if not self.fields:
            raise ValidationError(f"The edit of {self.ref} sets nothing.", hint=_listing(kind))
        for name in self.fields:
            if name in accepted:
                continue
            reason = _FIXED.get(kind, {}).get(name)
            if reason is not None:
                raise ValidationError(
                    f"An edit cannot set {kind} {name}=: {reason}.",
                    hint=f"Remove the {kind.lower()} and add one with the new {name}= in add=.",
                )
            raise ValidationError(
                f"{kind} takes no keyword {name!r} an edit can set.", hint=_listing(kind)
            )
        edited = dataclasses.replace(value, **self.fields)  # type: ignore[type-var]
        if edited == value:
            raise ValidationError(
                f"The edit of {self.ref} changes nothing.",
                hint="Drop it, or set a keyword to a value it does not have yet.",
            )
        if kind == "Service":
            _check_service_edit(self.ref, value, edited)
        object.__setattr__(self, "edited", edited)


@dataclass(frozen=True, slots=True)
class Remove(Located):
    """Drop a value the image declares: ``worker.ref.remove()``."""

    ref: Ref

    def __post_init__(self) -> None:
        if not isinstance(self.ref, Ref):
            raise ValidationError(f"Remove: {self.ref!r} is not a value's ref.", hint=_HINT)


type Change = Edit | Remove | dm.Edit | dm.Remove
"""What ``Profile(changes=)`` holds: ``value.ref.edit(...)`` or ``value.ref.remove()`` (a
:class:`~tundravm.User` or :class:`~tundravm.Group` ref makes the declaration's own)."""

_HINT = "Use value.ref.edit(...) or value.ref.remove()."


def _listing(kind: str) -> str:
    accepted = EDITABLE.get(kind, ())
    if not accepted:
        return f"A {kind} has nothing an edit can set: remove it and add another in add=."
    return f"An edit of a {kind} sets: {', '.join(accepted)}."


def _check_service_edit(ref: Ref, value: Any, edited: Any) -> None:
    """What a service edit cannot carry: a different built binary, a credential bound or
    unbound by argv or environment, another user for a directory on a volume."""
    if edited.binaries() != value.binaries():
        raise ValidationError(
            f"The edit of {ref} changes the built binary it runs, whose build the image "
            "already declares.",
            hint="Remove the service and add one that runs the new binary in add=.",
        )
    before = {binding.name: binding for binding in value._references()}
    after = {binding.name: binding for binding in edited._references()}
    if after != before:
        changed = sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        )
        raise ValidationError(
            f"The edit of {ref} binds or unbinds {', '.join(changed)} through argv= or "
            "environment=: what a profile binds, it says in credentials=.",
            hint="Set credentials=[...] in the edit, with secret.path in the word that reads it.",
        )
    state = next((item for item in value.volume_directories if item.group is None), None)
    if state is not None and (edited.user or edited.stem) != (value.user or value.stem):
        raise ValidationError(
            f"The edit of {ref} runs it as another user, but the image prepares its "
            f"directory on volume {state.volume.name!r} at boot for {value.user or value.stem!r}.",
            hint="Share the directory through a group (volume.directory(name, group=...)), or "
            "remove the service and add one with the new user= in add=.",
        )
