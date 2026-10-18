"""The one unit renderer: ordered sections of ``(key, value)`` entries -> unit text.

``render`` writes entries in the order given (module units whose bytes are fixed build them
explicitly); ``canonical`` orders a section by the canonical key table first, so a generated
``Service`` renders the same bytes however its entries were collected. Commands go through
``command_line``, which quotes per systemd.service(5) "Command lines".
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from .text import Directive, Entry, Other, Section, UnitText

if TYPE_CHECKING:
    from ..models import ServiceSpec

Entries = Sequence[tuple[str, str]]

STRICT_HARDENING: tuple[tuple[str, str], ...] = (
    ("ProtectSystem", "strict"),
    ("ProtectHome", "yes"),
    ("PrivateTmp", "yes"),
    ("NoNewPrivileges", "yes"),
    ("ProtectKernelModules", "yes"),
    ("ProtectKernelTunables", "yes"),
    ("ProtectControlGroups", "yes"),
    ("RestrictSUIDSGID", "yes"),
    ("MemoryDenyWriteExecute", "yes"),
)
"""``security="strict"``: today's nine lines, in today's order (a reviewed contract)."""

HARDENING_KEYS = (
    *(key for key, _ in STRICT_HARDENING),
    *"PrivateDevices DevicePolicy DeviceAllow ProtectKernelLogs ProtectClock ProtectHostname"
    " RestrictRealtime RestrictNamespaces LockPersonality RestrictAddressFamilies"
    " SystemCallArchitectures SystemCallFilter CapabilityBoundingSet AmbientCapabilities"
    " UMask".split(),
)

CANONICAL: dict[str, tuple[str, ...]] = {
    "Unit": (
        *"Description Documentation DefaultDependencies After Requires Wants Before PartOf"
        " BindsTo Conflicts RequiresMountsFor".split(),
    ),
    "Service": (
        *"Type RemainAfterExit ExecStartPre ExecStart ExecStartPost ExecReload User Group"
        " DynamicUser SupplementaryGroups WorkingDirectory EnvironmentFile Environment"
        " LoadCredential StateDirectory LogsDirectory RuntimeDirectory CacheDirectory"
        " ReadWritePaths Restart RestartSec Limit* KillMode TimeoutStartSec"
        " TimeoutStopSec".split(),
        *HARDENING_KEYS,
    ),
    "Socket": tuple("ListenStream ListenDatagram SocketMode SocketUser SocketGroup Accept".split()),
    "Timer": tuple(
        "OnBootSec OnUnitActiveSec OnCalendar Persistent RandomizedDelaySec Unit".split()
    ),
    "Mount": ("What", "Where", "Type", "Options"),
    "Install": ("WantedBy", "RequiredBy", "Also"),
}
"""Key order per section (docs/design/systemd-units.md 3.1): a superset of the order
``Service`` units always had, so new keys slot in and old ones never move. ``Condition*``
keys keep declaration order after the relations; keys outside the table follow, sorted."""


def _rank(section: str, key: str) -> tuple[int, str]:
    order = CANONICAL.get(section, ())
    probe = "Limit*" if key.startswith("Limit") and section == "Service" else key
    if probe in order:
        return order.index(probe), key if probe == "Limit*" else ""
    if section == "Unit" and key.startswith(("Condition", "Assert")):
        return len(order), ""
    return len(order) + 1, key


def canonical(section: str, entries: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """*entries* in canonical order; equal keys keep their order (``ExecStartPre``, ...)."""
    return sorted(entries, key=lambda entry: _rank(section, entry[0]))


def build(sections: Sequence[tuple[str, Entries]], *, trailing_newline: bool = True) -> UnitText:
    """A :class:`UnitText` of *sections* in order, one blank line between them; an empty
    section is left out."""
    blocks: list[Section] = []
    line = 1
    kept = [(name, entries) for name, entries in sections if entries]
    for index, (name, entries) in enumerate(kept):
        body: list[Entry] = []
        for offset, (key, value) in enumerate(entries, start=line + 1):
            body.append(Directive(key, value, (f"{key}={value}",), offset))
        line += len(entries) + 1
        if index < len(kept) - 1:
            body.append(Other("", line))
            line += 1
        blocks.append(Section(name, f"[{name}]", line - len(body) - 1, tuple(body)))
    return UnitText((), tuple(blocks), trailing_newline)


def render(sections: Sequence[tuple[str, Entries]], *, trailing_newline: bool = True) -> str:
    """Unit text of *sections*, entries in the order given."""
    return build(sections, trailing_newline=trailing_newline).text


_ENV_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")
_WHOLE_ENV_WORD = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def _dollars(word: str) -> str:
    """*word* with every ``$`` doubled except systemd variable references (``${NAME}``)."""
    parts = _ENV_REF.split(word)
    refs = _ENV_REF.findall(word)
    out = parts[0].replace("$", "$$")
    for ref, part in zip(refs, parts[1:], strict=True):
        out += ref + part.replace("$", "$$")
    return out


def command_word(word: str) -> str:
    """One argument as systemd reads it back unchanged.

    ``%`` doubles (specifiers expand on the whole line), ``$`` doubles unless it starts a
    ``${NAME}`` reference or the word is a whole ``$NAME`` (systemd's environment
    substitution, which ``env=``/``env_file=`` feed). A word with whitespace, a quote or a
    backslash, or an empty word, is double-quoted with ``\\`` and ``"`` escaped; a lone ``;``
    is escaped with a backslash.
    """
    if word == ";":
        return "\\;"
    text = word if _WHOLE_ENV_WORD.fullmatch(word) else _dollars(word)
    text = text.replace("%", "%%")
    if text and not any(ch.isspace() or ch in "\"'\\" for ch in text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def command_line(argv: Sequence[str]) -> str:
    """*argv* as one systemd command line (``ExecStart=`` value)."""
    return " ".join(command_word(word) for word in argv)


def env_assignment(key: str, value: str) -> str:
    """``KEY=value``, double-quoted per systemd rules when *value* needs it."""
    assignment = f"{key}={value}"
    if value and not any(ch.isspace() or ch in "\"'\\" for ch in value):
        return assignment
    escaped = assignment.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def service_unit(svc: ServiceSpec) -> str:
    """The ``.service`` unit a generated ``Service`` (a ``ServiceSpec``) renders to.

    Every field maps to its directive through the canonical key table; a field left at its
    default writes nothing, so a ``Service`` using only the historical fields renders the
    historical bytes.
    """
    unit: list[tuple[str, str]] = [("Description", svc.description or svc.name)]
    unit += [(key, " ".join(names)) for key, names in _relations(svc) if names]
    unit += svc.conditions
    service: list[tuple[str, str]] = [("Type", str(svc.type or "simple"))]
    if svc.remain_after_exit:
        service.append(("RemainAfterExit", "yes"))
    service += [("ExecStartPre", command_line(shlex.split(pre))) for pre in svc.exec_start_pre]
    if svc.command:
        service.append(("ExecStart", command_line(svc.command)))
    service += [("ExecStartPost", command_line(shlex.split(cmd))) for cmd in svc.exec_start_post]
    if svc.exec_reload:
        service.append(("ExecReload", command_line(shlex.split(svc.exec_reload))))
    service += _identity(svc)
    service += [("EnvironmentFile", path) for path in svc.env_files]
    service += [("Environment", env_assignment(k, v)) for k, v in sorted(svc.env.items())]
    service += [("LoadCredential", f"{k}:{v}") for k, v in sorted(svc.credentials.items())]
    for key, value in (
        ("StateDirectory", svc.state),
        ("LogsDirectory", svc.logs),
        ("RuntimeDirectory", svc.runtime),
        ("CacheDirectory", svc.cache),
        ("ReadWritePaths", " ".join(svc.write_paths)),
    ):
        if value:
            service.append((key, value))
    if svc.restart != "no":
        service.append(("Restart", svc.restart))
        restart_sec = "5" if svc.restart_sec is None else svc.restart_sec
        if restart_sec:
            service.append(("RestartSec", restart_sec))
    service += [(f"Limit{resource}", value) for resource, value in sorted(svc.limits.items())]
    for key, value in (
        ("KillMode", svc.kill_mode),
        ("TimeoutStartSec", svc.timeout_start),
        ("TimeoutStopSec", svc.timeout_stop),
    ):
        if value:
            service.append((key, value))
    if svc.hardening is not None:
        service += svc.hardening
    elif svc.security_profile == "strict":
        service += STRICT_HARDENING
    install: list[tuple[str, str]] = []
    if svc.enabled:
        install.append(("WantedBy", svc.wanted_by or "minimal.target"))
    extra = svc.extra_unit or {}
    sections: list[tuple[str, Entries]] = []
    for name, own in (("Unit", unit), ("Service", service), ("Install", install)):
        added = [
            (key, line)
            for key, value in sorted(extra.get(name, {}).items())
            for line in ((value,) if isinstance(value, str) else value)
        ]
        sections.append((name, [*canonical(name, own), *added]))
    return render(sections)


def _identity(svc: ServiceSpec) -> list[tuple[str, str]]:
    """``User=`` through the first ``EnvironmentFile=``, in canonical order."""
    entries: list[tuple[str, str]] = []
    for key, value in (
        ("User", svc.user),
        ("Group", svc.group),
        ("DynamicUser", "yes" if svc.dynamic_user else None),
        ("SupplementaryGroups", " ".join(svc.groups)),
        ("WorkingDirectory", svc.working_dir),
        ("EnvironmentFile", svc.env_file),
    ):
        if value:
            entries.append((key, value))
    return entries


def _relations(svc: ServiceSpec) -> list[tuple[str, Sequence[str]]]:
    return [
        ("After", svc.after),
        ("Requires", svc.requires),
        ("Wants", svc.wants),
        ("Before", svc.before),
        ("PartOf", svc.part_of),
        ("BindsTo", svc.binds_to),
        ("Conflicts", svc.conflicts),
        ("RequiresMountsFor", svc.mounts_for),
    ]


def activation_unit(kind: str, unit: Entries, own: Entries, install: Entries = ()) -> str:
    """A generated ``.socket``, ``.timer`` or ``.mount`` unit (*kind* names its section),
    every section in canonical order; an empty ``[Install]`` is left out."""
    return render(
        [
            ("Unit", canonical("Unit", unit)),
            (kind, canonical(kind, own)),
            ("Install", canonical("Install", install)),
        ]
    )


def dropin_unit(
    svc: ServiceSpec, *, restart: str | None = None, reset_exec_start: bool = False
) -> str:
    """A drop-in of the fields *svc* sets: no defaults (``Description``, ``Type``,
    ``[Install]``) are written, ``Restart=`` only from *restart* and ``RestartSec=`` only
    when given. With *reset_exec_start* the ``ExecStart=`` reset line precedes the command."""
    unit = [("Description", svc.description)] if svc.description else []
    unit += [(key, " ".join(names)) for key, names in _relations(svc) if names]
    unit += svc.conditions
    service: list[tuple[str, str]] = []
    service += [("ExecStartPre", command_line(shlex.split(pre))) for pre in svc.exec_start_pre]
    if svc.command:
        service += [("ExecStart", "")] * reset_exec_start
        service.append(("ExecStart", command_line(svc.command)))
    service += [("ExecStartPost", command_line(shlex.split(cmd))) for cmd in svc.exec_start_post]
    if svc.exec_reload:
        service.append(("ExecReload", command_line(shlex.split(svc.exec_reload))))
    service += _identity(svc)
    service += [("EnvironmentFile", path) for path in svc.env_files]
    service += [("Environment", env_assignment(k, v)) for k, v in sorted(svc.env.items())]
    service += [("LoadCredential", f"{k}:{v}") for k, v in sorted(svc.credentials.items())]
    for key, value in (
        ("StateDirectory", svc.state),
        ("LogsDirectory", svc.logs),
        ("RuntimeDirectory", svc.runtime),
        ("CacheDirectory", svc.cache),
        ("ReadWritePaths", " ".join(svc.write_paths)),
        ("Restart", restart),
        ("RestartSec", svc.restart_sec),
    ):
        if value:
            service.append((key, value))
    service += [(f"Limit{resource}", value) for resource, value in sorted(svc.limits.items())]
    for key, value in (
        ("KillMode", svc.kill_mode),
        ("TimeoutStartSec", svc.timeout_start),
        ("TimeoutStopSec", svc.timeout_stop),
    ):
        if value:
            service.append((key, value))
    service += svc.hardening or ()
    extra = svc.extra_unit or {}
    names = ["Unit", "Service", *(name for name in extra if name not in ("Unit", "Service"))]
    sections: list[tuple[str, Entries]] = []
    for name in names:
        own = {"Unit": unit, "Service": service}.get(name, [])
        added = [
            (key, line)
            for key, value in sorted(extra.get(name, {}).items())
            for line in ((value,) if isinstance(value, str) else value)
        ]
        sections.append((name, [*canonical(name, own), *added]))
    return render(sections)
