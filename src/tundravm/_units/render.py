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

_HARDENING_KEYS = (
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
        *_HARDENING_KEYS,
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


def _env_assignment(key: str, value: str) -> str:
    """``KEY=value``, double-quoted per systemd rules when *value* needs it."""
    assignment = f"{key}={value}"
    if value and not any(ch.isspace() or ch in "\"'\\" for ch in value):
        return assignment
    escaped = assignment.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def service_unit(svc: ServiceSpec) -> str:
    """The ``.service`` unit a generated ``Service`` (a ``ServiceSpec``) renders to."""
    unit: list[tuple[str, str]] = [("Description", svc.description or svc.name)]
    unit += [(key, " ".join(names)) for key, names in _relations(svc) if names]
    service: list[tuple[str, str]] = [("Type", str(svc.type or "simple"))]
    service += [("ExecStartPre", command_line(shlex.split(pre))) for pre in svc.exec_start_pre]
    if svc.command:
        service.append(("ExecStart", command_line(svc.command)))
    for key, value in (
        ("User", svc.user),
        ("Group", svc.group),
        ("WorkingDirectory", svc.working_dir),
        ("EnvironmentFile", svc.env_file),
    ):
        if value:
            service.append((key, value))
    service += [("Environment", _env_assignment(k, v)) for k, v in sorted(svc.env.items())]
    if svc.restart != "no":
        service += [("Restart", svc.restart), ("RestartSec", "5")]
    service += [(f"Limit{resource}", value) for resource, value in sorted(svc.limits.items())]
    if svc.kill_mode:
        service.append(("KillMode", svc.kill_mode))
    if svc.timeout_stop:
        service.append(("TimeoutStopSec", svc.timeout_stop))
    if svc.security_profile == "strict":
        service += STRICT_HARDENING
    install: list[tuple[str, str]] = [("WantedBy", svc.wanted_by or "minimal.target")]
    extra = svc.extra_unit or {}
    sections: list[tuple[str, Entries]] = []
    for name, own in (("Unit", unit), ("Service", service), ("Install", install)):
        added = sorted(extra.get(name, {}).items())
        sections.append((name, [*canonical(name, own), *added]))
    return render(sections)


def _relations(svc: ServiceSpec) -> list[tuple[str, Sequence[str]]]:
    return [("After", svc.after), ("Requires", svc.requires), ("Wants", svc.wants)]
