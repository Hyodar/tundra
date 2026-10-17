"""Typed systemd building blocks the unit declarations share (docs/design/systemd-units.md 2).

:class:`Hardening` is the reviewed sandbox profile of a generated unit, :class:`Dynamic` asks
for ``DynamicUser=yes``, and the relation helpers turn unit names and refs (``after=``,
``requires=`` ...) into the unit names a unit file lists. ``Service`` (and the other unit
shapes) import them from here; nothing here imports the declaration model at module level.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from tundravm._source import FrozenMap
from tundravm._units.directives import is_known
from tundravm._units.render import HARDENING_KEYS
from tundravm.errors import ValidationError
from tundravm.models import unit_name

if TYPE_CHECKING:
    from .model import Ref

__all__ = [
    "SERVICE_OWNED",
    "UNIT_REF_KINDS",
    "Dynamic",
    "Extra",
    "Hardening",
    "UnitTarget",
    "extra_entries",
    "mount_unit",
    "require_extra",
    "require_targets",
    "target_unit",
]

ProtectSystem = Literal["strict", "full", "yes", "no"]
Network = Literal["inet", "unix", "none", "any"]

_FAMILIES = {"inet": "AF_UNIX AF_INET AF_INET6", "unix": "AF_UNIX", "none": "none"}
_CAPABILITY = re.compile(r"CAP_[A-Z_]+")
_UMASK = re.compile(r"0?[0-7]{3}")
_WORD = re.compile(r"~?@?[A-Za-z0-9_:-]+")


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{type(owner).__name__}: {message}", hint=hint)


@dataclass(frozen=True, slots=True)
class Hardening:
    """The sandbox a generated unit runs in: one field per knob, three reviewed presets.

    The field defaults are :meth:`strict`; :meth:`relaxed` and :meth:`none` are the other
    presets, and every preset takes per-field overrides (``Hardening.strict(jit=True)``), so
    a relaxation is visible where it is made. ``devices`` replaces ``PrivateDevices=yes``
    with ``DevicePolicy=closed`` plus one ``DeviceAllow=<dev> rw`` each; ``capabilities``
    ``None`` leaves the bounding set alone, ``()`` empties it, names keep (and grant) those;
    ``jit=True`` drops ``MemoryDenyWriteExecute=`` (.NET, JVM, V8); ``network`` picks
    ``RestrictAddressFamilies=`` (``"any"``: no restriction); ``kernel`` covers
    ``ProtectKernel{Modules,Tunables,Logs}=``, ``ProtectControlGroups=`` and
    ``ProtectClock=``; ``sandbox`` covers ``ProtectHostname=``, ``RestrictRealtime=``,
    ``RestrictNamespaces=``, ``LockPersonality=`` and ``SystemCallArchitectures=native``.
    """

    protect_system: ProtectSystem = "strict"
    protect_home: bool = True
    private_tmp: bool = True
    private_devices: bool = True
    devices: tuple[str, ...] = ()
    no_new_privileges: bool = True
    capabilities: tuple[str, ...] | None = ()
    jit: bool = False
    network: Network = "inet"
    syscalls: tuple[str, ...] = ("@system-service",)
    deny_syscalls: tuple[str, ...] = ("@privileged", "@resources")
    kernel: bool = True
    sandbox: bool = True
    umask: str | None = "0077"

    def __post_init__(self) -> None:
        for name in ("devices", "capabilities", "syscalls", "deny_syscalls"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))
            elif value is not None and not isinstance(value, tuple):
                raise _fail(self, f"{name}= must be a list of strings, got {value!r}.")
        if self.protect_system not in ("strict", "full", "yes", "no"):
            raise _fail(self, f"invalid protect_system {self.protect_system!r}.")
        if self.network not in ("inet", "unix", "none", "any"):
            raise _fail(
                self,
                f"invalid network {self.network!r}.",
                hint="Expected 'inet', 'unix', 'none' or 'any'.",
            )
        for name in ("protect_home", "private_tmp", "private_devices", "no_new_privileges"):
            _require_flag(self, name)
        for name in ("jit", "kernel", "sandbox"):
            _require_flag(self, name)
        for device in self.devices:
            if not isinstance(device, str) or not device.startswith("/dev/"):
                raise _fail(self, f"device {device!r} must be a path under /dev/.")
        for cap in self.capabilities or ():
            if not isinstance(cap, str) or not _CAPABILITY.fullmatch(cap):
                raise _fail(self, f"invalid capability {cap!r}.", hint="Use CAP_NET_BIND_SERVICE.")
        for word in (*self.syscalls, *self.deny_syscalls):
            if not isinstance(word, str) or not _WORD.fullmatch(word):
                raise _fail(self, f"invalid system call or group {word!r}.")
        if self.umask is not None and (
            not isinstance(self.umask, str) or not _UMASK.fullmatch(self.umask)
        ):
            raise _fail(self, f"invalid umask {self.umask!r}.", hint="Use an octal string: '0077'.")

    @classmethod
    def strict(cls, **overrides: Any) -> Hardening:
        """The default, reviewed profile (the field defaults), with *overrides*."""
        return cls(**overrides)

    @classmethod
    def relaxed(cls, **overrides: Any) -> Hardening:
        """``ProtectSystem=full``, ``PrivateTmp``, ``NoNewPrivileges``, the kernel knobs;
        no syscall filter, no device, capability or address-family limits."""
        return dataclasses.replace(_RELAXED, **overrides)

    @classmethod
    def none(cls) -> Hardening:
        """No sandbox at all: renders nothing."""
        return _NONE

    def entries(self) -> tuple[tuple[str, str], ...]:
        """The ``[Service]`` directives, in the canonical hardening order (design 2.3)."""
        out: list[tuple[str, str]] = []
        add = out.append
        if self.protect_system != "no":
            add(("ProtectSystem", self.protect_system))
        for key, on in (
            ("ProtectHome", self.protect_home),
            ("PrivateTmp", self.private_tmp),
            ("NoNewPrivileges", self.no_new_privileges),
            ("ProtectKernelModules", self.kernel),
            ("ProtectKernelTunables", self.kernel),
            ("ProtectControlGroups", self.kernel),
            ("RestrictSUIDSGID", self.no_new_privileges),
            ("MemoryDenyWriteExecute", not self.jit),
        ):
            if on:
                add((key, "yes"))
        if self.devices:
            add(("DevicePolicy", "closed"))
            out.extend(("DeviceAllow", f"{device} rw") for device in self.devices)
        elif self.private_devices:
            add(("PrivateDevices", "yes"))
        if self.kernel:
            out += [("ProtectKernelLogs", "yes"), ("ProtectClock", "yes")]
        if self.sandbox:
            for key in ("ProtectHostname", "RestrictRealtime", "RestrictNamespaces"):
                add((key, "yes"))
            add(("LockPersonality", "yes"))
        if self.network != "any":
            add(("RestrictAddressFamilies", _FAMILIES[self.network]))
        if self.sandbox:
            add(("SystemCallArchitectures", "native"))
        if self.syscalls:
            add(("SystemCallFilter", " ".join(self.syscalls)))
        if self.deny_syscalls:
            add(("SystemCallFilter", "~" + " ".join(self.deny_syscalls)))
        if self.capabilities is not None:
            add(("CapabilityBoundingSet", " ".join(self.capabilities)))
            if self.capabilities:
                add(("AmbientCapabilities", " ".join(self.capabilities)))
        if self.umask is not None:
            add(("UMask", self.umask))
        return tuple(out)


def _require_flag(owner: object, name: str) -> None:
    if not isinstance(getattr(owner, name), bool):
        raise _fail(owner, f"{name} must be True or False, got {getattr(owner, name)!r}.")


_RELAXED = Hardening(
    protect_system="full",
    protect_home=False,
    private_devices=False,
    capabilities=None,
    jit=True,
    network="any",
    syscalls=(),
    deny_syscalls=(),
    sandbox=False,
    umask=None,
)
_NONE = Hardening(
    protect_system="no",
    protect_home=False,
    private_tmp=False,
    private_devices=False,
    no_new_privileges=False,
    capabilities=None,
    jit=True,
    network="any",
    syscalls=(),
    deny_syscalls=(),
    kernel=False,
    sandbox=False,
    umask=None,
)


@dataclass(frozen=True, slots=True)
class Dynamic:
    """``user=Dynamic()``: a transient user per run (``DynamicUser=yes``), for stateless
    services; ``state=``/``logs=``/``cache=`` directories still persist."""


# ── relations ────────────────────────────────────────────────────────────

UNIT_REF_KINDS: tuple[str, ...] = ("Unit", "Service", "Task", "Mount")
"""Declaration kinds a relation ref (``requires=[Service.ref("tdxs")]``) may name."""

type UnitTarget = str | Ref
"""A relation target: a unit name (``"tdxs.socket"``) or a ref to a unit declaration."""

_UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_PLAIN = re.compile(r"[A-Za-z0-9:_]")


def mount_unit(where: str) -> str:
    """The ``.mount`` unit name of mount point *where* (``systemd-escape --path``)."""
    path = re.sub("/+", "/", where).strip("/")
    if not path:
        return "-.mount"
    out = []
    for index, char in enumerate(path):
        if char == "/":
            out.append("-")
        elif _PLAIN.fullmatch(char) or (char == "." and index):
            out.append(char)
        else:
            out.extend(f"\\x{byte:02x}" for byte in char.encode())
    return "".join(out) + ".mount"


def target_unit(target: UnitTarget) -> str:
    """The unit name *target* stands for: a name as given, a ref's unit."""
    if isinstance(target, str):
        return target
    if target.kind == "Mount":
        return mount_unit(target.identity[0])
    return unit_name(target.identity[0])


def require_targets(owner: object, *names: str) -> None:
    """Normalize the relation fields *names* of *owner* to tuples of names and unit refs."""
    from .model import Ref

    for name in names:
        values = getattr(owner, name)
        if isinstance(values, (str, Ref)) or not isinstance(values, (tuple, list)):
            raise _fail(
                owner,
                f"{name}= must be a list of unit names or refs, got {values!r}.",
                hint=f'Write {name}=["{values}"].' if isinstance(values, str) else None,
            )
        for value in values:
            if isinstance(value, Ref):
                if value.kind not in UNIT_REF_KINDS:
                    raise _fail(owner, f"{name}= ref {value} does not name a unit.")
            elif not isinstance(value, str) or not _UNIT_NAME.fullmatch(value):
                raise _fail(owner, f"invalid unit name {value!r} in {name}=.")
        object.__setattr__(owner, name, tuple(dict.fromkeys(values)))


# ── the escape hatch ─────────────────────────────────────────────────────

type Extra = Mapping[str, Mapping[str, str | tuple[str, ...]]]
"""``extra={"Service": {"OOMScoreAdjust": "-500"}}``: section -> directive -> value(s)."""


def require_extra(
    owner: object,
    sections: Sequence[str],
    owned: Mapping[str, str],
    *,
    name: str = "extra",
) -> None:
    """Validate and freeze *owner*'s ``extra=`` mapping.

    Sections must be in *sections*, keys must be systemd 257 directives of their section
    and not one a typed field renders (*owned* maps ``"Section.Key"`` to that field), and
    values are one line each; a sequence value renders one line per item.
    """
    value = getattr(owner, name)
    if not isinstance(value, Mapping):
        raise _fail(owner, f"{name}= must be a mapping of sections, got {value!r}.")
    frozen: dict[str, FrozenMap[str | tuple[str, ...]]] = {}
    for section, keys in value.items():
        if section not in sections:
            raise _fail(
                owner,
                f"{name}= section {section!r} is not valid here.",
                hint=f"Expected one of: {', '.join(sections)}.",
            )
        if not isinstance(keys, Mapping) or not keys:
            raise _fail(owner, f"{name}[{section!r}] must be a non-empty mapping of directives.")
        entries: dict[str, str | tuple[str, ...]] = {}
        for key, item in keys.items():
            field = owned.get(f"{section}.{key}") or (
                "limits" if section == "Service" and key.startswith("Limit") else None
            )
            if field is not None:
                raise _fail(
                    owner,
                    f"{name}= sets {section}.{key}, which the typed field {field}= renders.",
                    hint=f"Use {field}= instead of {name}=.",
                )
            if not isinstance(key, str) or not is_known(section, key):
                raise _fail(owner, f"{name}= directive {section}.{key} is unknown to systemd.")
            items = (item,) if isinstance(item, str) else item
            if not isinstance(items, (tuple, list)) or not items:
                raise _fail(owner, f"{name}= value of {section}.{key} must be a string or list.")
            for line in items:
                if not isinstance(line, str) or "\n" in line:
                    raise _fail(owner, f"{name}= value of {section}.{key} must be one line.")
            entries[key] = item if isinstance(item, str) else tuple(items)
        frozen[section] = FrozenMap(entries)
    object.__setattr__(owner, name, FrozenMap(frozen))


def extra_entries(extra: Extra, section: str) -> list[tuple[str, str]]:
    """The ``(key, value)`` lines *extra* adds to *section*, sorted by key."""
    lines: list[tuple[str, str]] = []
    for key, value in sorted(extra.get(section, {}).items()):
        lines += [(key, value)] if isinstance(value, str) else [(key, v) for v in value]
    return lines


SERVICE_OWNED: Mapping[str, str] = FrozenMap(
    {
        **{f"Service.{key}": "hardening" for key in HARDENING_KEYS},
        "Unit.Description": "description",
        "Unit.After": "after",
        "Unit.Requires": "requires",
        "Unit.Wants": "wants",
        "Unit.Before": "before",
        "Unit.PartOf": "part_of",
        "Unit.BindsTo": "binds_to",
        "Unit.Conflicts": "conflicts",
        "Service.Type": "type",
        "Service.ExecStartPre": "exec_start_pre",
        "Service.ExecStart": "exec_start",
        "Service.ExecStartPost": "exec_start_post",
        "Service.ExecReload": "exec_reload",
        "Service.User": "user",
        "Service.Group": "group",
        "Service.DynamicUser": "user",
        "Service.SupplementaryGroups": "groups",
        "Service.WorkingDirectory": "working_dir",
        "Service.EnvironmentFile": "env_file",
        "Service.Environment": "env",
        "Service.LoadCredential": "credentials",
        "Service.StateDirectory": "state",
        "Service.LogsDirectory": "logs",
        "Service.RuntimeDirectory": "runtime",
        "Service.CacheDirectory": "cache",
        "Service.ReadWritePaths": "write_paths",
        "Service.Restart": "restart",
        "Service.RestartSec": "restart_sec",
        "Service.KillMode": "kill_mode",
        "Service.TimeoutStartSec": "timeout_start",
        "Service.TimeoutStopSec": "timeout_stop",
        "Install.WantedBy": "wanted_by",
    }
)
"""``"Section.Key"`` -> the typed ``Service`` field that renders it (``extra=`` may not)."""
