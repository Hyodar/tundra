"""Parsed systemd units and mkosi.conf sections, for :class:`tundravm.testing.Image`."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from tundravm._units import UnitText
from tundravm._units.render import HARDENING_KEYS

Sections = Mapping[str, Mapping[str, tuple[str, ...]]]
"""``{section: {key: values}}``: every assignment of a key, in file order."""


def parse_unit(text: str) -> Sections:
    """A systemd unit's sections; an empty assignment (``After=``) resets the key's list.

    A view over the package's one lossless unit parser, :class:`tundravm._units.UnitText`.
    """
    sections: dict[str, dict[str, list[str]]] = {}
    for section in UnitText.parse(text).sections:
        current = sections.setdefault(section.name, {})
        for directive in section.directives:
            values = current.setdefault(directive.key, [])
            if directive.value:
                values.append(directive.value)
            else:
                values.clear()
    return {name: {k: tuple(v) for k, v in keys.items()} for name, keys in sections.items()}


def parse_conf(text: str) -> Sections:
    """mkosi.conf sections: indented lines continue the key above, repeated keys accumulate."""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] | None = None
    key: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if raw[:1].isspace() and current is not None and key is not None:
            current[key].append(line)
        elif line.startswith("[") and line.endswith("]"):
            current, key = sections.setdefault(line[1:-1], {}), None
        elif current is not None and "=" in line:
            name, _, value = line.partition("=")
            key = name.strip()
            values = current.setdefault(key, [])
            if value.strip():
                values.append(value.strip())
    return {name: {k: tuple(v) for k, v in keys.items()} for name, keys in sections.items()}


_DIRECTORY_KINDS = ("state", "logs", "runtime", "cache")
_HARDENING_KEYS = frozenset(HARDENING_KEYS)


class UnitView:
    """One compiled systemd unit: its text, parsed sections and whether the image enables it."""

    __slots__ = ("enabled", "name", "path", "sections", "text")

    def __init__(self, name: str, path: str, text: str, *, enabled: bool) -> None:
        self.name = name
        self.path = path
        self.text = text
        self.enabled = enabled
        self.sections = parse_unit(text)

    def __repr__(self) -> str:
        return f"UnitView({self.name!r}, enabled={self.enabled})"

    def values(self, section: str, key: str) -> tuple[str, ...]:
        """Every value of *key* in *section*, in order (empty when unset)."""
        return self.sections.get(section, {}).get(key, ())

    def get(self, section: str, key: str) -> str | None:
        """The last value of *key* in *section*, the one systemd uses; ``None`` when unset."""
        found = self.values(section, key)
        return found[-1] if found else None

    def _names(self, section: str, key: str) -> tuple[str, ...]:
        return tuple(name for value in self.values(section, key) for name in value.split())

    def requires(self, unit: str) -> bool:
        """Whether ``Requires=`` names *unit* (a unit or a target)."""
        return unit in self._names("Unit", "Requires")

    def wants(self, unit: str) -> bool:
        """Whether ``Wants=`` names *unit*."""
        return unit in self._names("Unit", "Wants")

    def after(self, unit: str) -> bool:
        """Whether ``After=`` names *unit*."""
        return unit in self._names("Unit", "After")

    @property
    def wanted_by(self) -> tuple[str, ...]:
        """The ``[Install] WantedBy=`` targets."""
        return self._names("Install", "WantedBy")

    @property
    def exec_start(self) -> str | None:
        """The ``[Service] ExecStart=`` command (the last one), ``None`` for a non-service."""
        return self.get("Service", "ExecStart")

    @property
    def user(self) -> str | None:
        """The ``[Service] User=``."""
        return self.get("Service", "User")

    @property
    def environment(self) -> dict[str, str]:
        """Every ``[Service] Environment=`` assignment, later ones winning."""
        words = (w for value in self.values("Service", "Environment") for w in shlex.split(value))
        return {key: value for key, _, value in (word.partition("=") for word in words)}

    def before(self, unit: str) -> bool:
        """Whether ``Before=`` names *unit*."""
        return unit in self._names("Unit", "Before")

    def part_of(self, unit: str) -> bool:
        """Whether ``PartOf=`` names *unit*."""
        return unit in self._names("Unit", "PartOf")

    def binds_to(self, unit: str) -> bool:
        """Whether ``BindsTo=`` names *unit*."""
        return unit in self._names("Unit", "BindsTo")

    def conflicts(self, unit: str) -> bool:
        """Whether ``Conflicts=`` names *unit*."""
        return unit in self._names("Unit", "Conflicts")

    @property
    def dynamic_user(self) -> bool:
        """Whether ``[Service] DynamicUser=yes``."""
        return self.get("Service", "DynamicUser") in ("yes", "true", "1", "on")

    @property
    def directories(self) -> dict[str, str]:
        """``{"state": ..., "logs": ..., "runtime": ..., "cache": ...}`` for the directory
        directives the unit sets (``StateDirectory=`` ...)."""
        found = {kind: self.get("Service", f"{kind.title()}Directory") for kind in _DIRECTORY_KINDS}
        return {kind: value for kind, value in found.items() if value is not None}

    @property
    def credentials(self) -> dict[str, str]:
        """``LoadCredential=`` as ``{name: path}``."""
        pairs = (value.partition(":") for value in self.values("Service", "LoadCredential"))
        return {name: path for name, _, path in pairs}

    @property
    def hardening(self) -> dict[str, tuple[str, ...]]:
        """The sandbox directives the unit sets (``ProtectSystem=`` ...), every value in order."""
        return {
            key: values
            for key, values in self.sections.get("Service", {}).items()
            if key in _HARDENING_KEYS
        }
