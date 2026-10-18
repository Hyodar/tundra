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
    "Condition",
    "Dynamic",
    "Extra",
    "Hardening",
    "UnitTarget",
    "condition_entries",
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


_UNSET: Any = type("_Unset", (), {"__slots__": (), "__repr__": lambda self: "<strict>"})()
"""A :class:`Hardening` field's default: the :data:`_STRICT` value, not passed."""
_STRICT: dict[str, object] = {
    "protect_system": "strict",
    "protect_home": True,
    "private_tmp": True,
    "private_devices": True,
    "devices": (),
    "no_new_privileges": True,
    "capabilities": (),
    "jit": False,
    "network": "inet",
    "syscalls": ("@system-service",),
    "deny_syscalls": ("@privileged", "@resources"),
    "kernel": True,
    "sandbox": True,
    "umask": "0077",
}
"""The strict profile: every :class:`Hardening` field's default."""


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
    A unit renders :meth:`entries`; a ``DropIn`` renders :meth:`patch_entries`, only the
    fields passed (:attr:`explicit`; a preset passes them all).
    """

    protect_system: ProtectSystem = _UNSET
    protect_home: bool = _UNSET
    private_tmp: bool = _UNSET
    private_devices: bool = _UNSET
    devices: tuple[str, ...] = _UNSET
    no_new_privileges: bool = _UNSET
    capabilities: tuple[str, ...] | None = _UNSET
    jit: bool = _UNSET
    network: Network = _UNSET
    syscalls: tuple[str, ...] = _UNSET
    deny_syscalls: tuple[str, ...] = _UNSET
    kernel: bool = _UNSET
    sandbox: bool = _UNSET
    umask: str | None = _UNSET
    explicit: frozenset[str] = dataclasses.field(
        default=frozenset(), init=False, repr=False, compare=False
    )
    """The fields the caller passed (every field for a preset): what a drop-in patches."""

    def __post_init__(self) -> None:
        given = frozenset(name for name in _STRICT if getattr(self, name) is not _UNSET)
        for name in _STRICT.keys() - given:
            object.__setattr__(self, name, _STRICT[name])
        object.__setattr__(self, "explicit", given)
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
        return cls(**{**_STRICT, **overrides})

    @classmethod
    def relaxed(cls, **overrides: Any) -> Hardening:
        """``ProtectSystem=full``, ``PrivateTmp``, ``NoNewPrivileges``, the kernel knobs;
        no syscall filter, no device, capability or address-family limits."""
        return dataclasses.replace(_RELAXED, **overrides)

    @classmethod
    def none(cls) -> Hardening:
        """No sandbox at all: renders nothing in a unit (a drop-in turns every knob off)."""
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
        for entry in self._syscall_entries():
            add(entry)
        if self.capabilities is not None:
            add(("CapabilityBoundingSet", " ".join(self.capabilities)))
            if self.capabilities:
                add(("AmbientCapabilities", " ".join(self.capabilities)))
        if self.umask is not None:
            add(("UMask", self.umask))
        return tuple(out)

    def _syscall_entries(self) -> list[tuple[str, str]]:
        """The ``SystemCallFilter=`` lines, then the error number and the setuid re-allow.

        A denied call fails with ``EPERM`` instead of killing the process, so a program
        that probes an optional call logs an error rather than dying of ``SIGSYS``.
        ``@privileged`` holds the ``setuid`` family, which many programs call to drop to
        their own ids (busybox does at every start); without ``CAP_SETUID``/``CAP_SETGID``
        in the bounding set those calls cannot change identity, so they are allowed back.
        """
        out: list[tuple[str, str]] = []
        if self.syscalls:
            out.append(("SystemCallFilter", " ".join(self.syscalls)))
        if self.deny_syscalls:
            out.append(("SystemCallFilter", "~" + " ".join(self.deny_syscalls)))
            caps = self.capabilities
            if (
                "@privileged" in self.deny_syscalls
                and self.syscalls
                and caps is not None
                and not {"CAP_SETUID", "CAP_SETGID"} & set(caps)
            ):
                out.append(("SystemCallFilter", "@setuid"))
            out.append(("SystemCallErrorNumber", "EPERM"))
        return out

    def patch_entries(self) -> tuple[tuple[str, str], ...]:
        """The directives a drop-in writes to make the :attr:`explicit` fields hold over
        whatever the unit it extends sets: switches render ``yes`` or ``no``, and each list
        starts with its empty reset (``SystemCallFilter=``, ``DeviceAllow=``...), so the
        patch replaces it instead of adding to it. Unset fields render nothing."""
        on = {True: "yes", False: "no"}
        given = self.explicit
        out: list[tuple[str, str]] = []
        if "protect_system" in given:
            out.append(("ProtectSystem", self.protect_system))
        switches = (
            ("protect_home", ("ProtectHome",), self.protect_home),
            ("private_tmp", ("PrivateTmp",), self.private_tmp),
            ("no_new_privileges", ("NoNewPrivileges", "RestrictSUIDSGID"), self.no_new_privileges),
            ("jit", ("MemoryDenyWriteExecute",), not self.jit),
            ("kernel", _KERNEL_KEYS, self.kernel),
            ("sandbox", _SANDBOX_KEYS, self.sandbox),
        )
        for name, keys, value in switches:
            if name in given:
                out += [(key, on[value]) for key in keys]
        if given & {"devices", "private_devices"}:
            out.append(("PrivateDevices", on[self.private_devices and not self.devices]))
            out.append(("DevicePolicy", "closed" if self.devices else "auto"))
            out.append(("DeviceAllow", ""))
            out += [("DeviceAllow", f"{device} rw") for device in self.devices]
        if "sandbox" in given:
            out.append(("SystemCallArchitectures", ""))
            if self.sandbox:
                out.append(("SystemCallArchitectures", "native"))
        if "network" in given:
            out.append(("RestrictAddressFamilies", ""))
            if self.network != "any":
                out.append(("RestrictAddressFamilies", _FAMILIES[self.network]))
        if given & {"syscalls", "deny_syscalls"}:
            out.append(("SystemCallFilter", ""))
            out.extend(self._syscall_entries())
        if "capabilities" in given:
            out.append(("CapabilityBoundingSet", ""))  # the empty set
            if self.capabilities is None:
                out.append(("CapabilityBoundingSet", "~"))  # every capability
            elif self.capabilities:
                out.append(("CapabilityBoundingSet", " ".join(self.capabilities)))
            out.append(("AmbientCapabilities", ""))
            if self.capabilities:
                out.append(("AmbientCapabilities", " ".join(self.capabilities)))
        if "umask" in given:
            out.append(("UMask", self.umask or "0022"))  # None: systemd's default
        return tuple(out)


_KERNEL_KEYS = (
    "ProtectKernelModules",
    "ProtectKernelTunables",
    "ProtectControlGroups",
    "ProtectKernelLogs",
    "ProtectClock",
)
_SANDBOX_KEYS = ("ProtectHostname", "RestrictRealtime", "RestrictNamespaces", "LockPersonality")


def _require_flag(owner: object, name: str) -> None:
    if not isinstance(getattr(owner, name), bool):
        raise _fail(owner, f"{name} must be True or False, got {getattr(owner, name)!r}.")


_RELAXED = Hardening.strict(
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
_NONE = Hardening.strict(
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


@dataclass(frozen=True, slots=True)
class Root:
    """``user=Root()``: root plumbing that renders no ``User=`` line (systemd's default
    user), as runtime-init steps and platform glue do; linted ``service-runs-as-root``.
    ``user="root"`` spells ``User=root``."""


# ── conditions ───────────────────────────────────────────────────────────

_VIRTUALIZATION = frozenset(
    "vm container private-users qemu kvm amazon zvm vmware microsoft oracle powervm xen"
    " bochs uml parallels bhyve qnx apple sre google acrn openvz lxc lxc-libvirt"
    " systemd-nspawn docker podman rkt wsl proot pouch".split()
)
"""The ``ConditionVirtualization=`` words systemd 257 accepts besides yes/no (vm and
container classes and ``systemd-detect-virt`` ids); ``microsoft`` is Hyper-V/Azure and
``google`` is Compute Engine."""


@dataclass(frozen=True, slots=True)
class Condition:
    """One ``Condition*=`` line of a generated unit's ``[Unit]`` section: systemd skips the
    unit (no failure) when it does not hold. ``conditions=(Condition.virtualization("microsoft"),)``
    keeps cloud glue inert off its cloud, so one UKI serves every output. Build one with a
    constructor; *negate* prefixes the value with ``!``."""

    key: str
    value: str

    @classmethod
    def virtualization(cls, kind: str, *, negate: bool = False) -> Condition:
        """``ConditionVirtualization=``: ``microsoft`` (Azure), ``google`` (GCP), ``kvm``,
        ``vm`` ..."""
        if kind not in _VIRTUALIZATION:
            raise ValidationError(
                f"Condition.virtualization: unknown kind {kind!r}.",
                hint=f"Use one of: {', '.join(sorted(_VIRTUALIZATION))}.",
            )
        return cls("ConditionVirtualization", _negated(kind, negate))

    @classmethod
    def path_exists(cls, path: str, *, negate: bool = False) -> Condition:
        """``ConditionPathExists=``: the unit runs only when *path* exists."""
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise ValidationError(
                f"Condition.path_exists: {path!r} is not an absolute path.",
                hint='Pass a path like "/dev/tdx_guest".',
            )
        return cls("ConditionPathExists", _negated(path, negate))

    @classmethod
    def kernel_command_line(cls, word: str, *, negate: bool = False) -> Condition:
        """``ConditionKernelCommandLine=``: *word* (``debug``, ``key=value``) is on the
        kernel command line."""
        if not isinstance(word, str) or not word or any(ch.isspace() for ch in word):
            raise ValidationError(
                f"Condition.kernel_command_line: {word!r} is not one word.",
                hint='Pass one word, like "debug" or "tundravm.mode=dev".',
            )
        return cls("ConditionKernelCommandLine", _negated(word, negate))

    @classmethod
    def first_boot(cls, first: bool = True) -> Condition:
        """``ConditionFirstBoot=``: only on (or, with ``first=False``, never on) first boot."""
        return cls("ConditionFirstBoot", "yes" if first else "no")

    @property
    def entry(self) -> tuple[str, str]:
        """The ``(key, value)`` line it renders."""
        return self.key, self.value


def _negated(value: str, negate: bool) -> str:
    return f"!{value}" if negate else value


def condition_entries(owner: object, conditions: object) -> tuple[tuple[str, str], ...]:
    """Validate *owner*'s ``conditions=`` and return their lines, in declaration order."""
    items = tuple(conditions) if isinstance(conditions, (tuple, list)) else None
    if items is None or not all(isinstance(item, Condition) for item in items):
        raise _fail(
            owner,
            f"conditions= must be a tuple of Condition, got {conditions!r}.",
            hint='Use conditions=(Condition.virtualization("microsoft"),).',
        )
    return tuple(item.entry for item in items)


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
