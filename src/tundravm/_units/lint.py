"""Lint over systemd unit text: every unit a variant ships, raw or generated.

``unit_findings`` takes the variant's units already parsed and the facts it needs about the
variant (which units are enabled, who the users are, whether packages could provide a unit),
so it has no dependency on the compiler state; ``tundravm.check`` builds the inputs.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from ..diagnostics import Diagnostic, Severity
from .directives import FALSE, TRUE, UNIT_SUFFIXES, check_value, is_known, kind_of
from .text import UnitText

STYLE_CODES = frozenset({"unit-exec-relative", "requires-without-after", "socket-service-started"})
"""Codes left out under the ``nethermind-v1`` dialect: its units are historical bytes."""

_INSTALL_KEYS = ("WantedBy", "RequiredBy", "UpheldBy", "Alias", "Also")
_EXEC_PREFIX = "@-:+!|"
_STATE_ROOTS = ("/var/lib/", "/var/log/", "/run/", "/var/cache/")
_MKDIR = re.compile(r"(^|/)(mkdir|install)$")


@dataclass(frozen=True, slots=True)
class ShippedUnit:
    """A unit or drop-in a variant ships: *subject* names it in findings, *path* is absolute."""

    subject: str
    path: str
    text: str
    generated: bool = False

    @property
    def name(self) -> str:
        """The unit the file configures (a drop-in's is its directory's ``<unit>.d``)."""
        parent = posixpath.basename(posixpath.dirname(self.path))
        if self.is_dropin:
            return parent[: -len(".d")]
        return posixpath.basename(self.path)

    @property
    def is_dropin(self) -> bool:
        return posixpath.dirname(self.path).endswith(".d")

    @property
    def own_section(self) -> str | None:
        """The type section its suffix allows (``""``: none), ``None`` for an unknown suffix."""
        return UNIT_SUFFIXES.get(posixpath.splitext(self.name)[1])


@dataclass(frozen=True, slots=True)
class Facts:
    """What the unit rules need to know about the variant beyond its unit text."""

    variant: str
    enabled: frozenset[str]
    has_user: Callable[[str], bool]
    has_group: Callable[[str], bool]
    packages: bool
    style: bool
    outputs: frozenset[str] = frozenset()
    """The variant's output targets (``qemu``, ``azure``, ``gcp``): the hosts its UKI boots on."""


def _finding(
    unit: ShippedUnit, facts: Facts, code: str, severity: Severity, message: str, hint: str
) -> Diagnostic:
    return Diagnostic(
        code=code,
        severity=severity,
        message=message,
        subject=unit.subject,
        variant=facts.variant,
        hint=hint,
    )


def _words(parsed: UnitText, section: str, key: str) -> list[str]:
    return [word for value in parsed.values(section, key) for word in value.split()]


def _per_unit(unit: ShippedUnit, parsed: UnitText, facts: Facts) -> Iterator[Diagnostic]:
    """Section, directive, value and command findings of one unit's own text."""
    own = unit.own_section
    if own is None:
        return
    allowed = {"Unit", "Install", own} - {""}
    for section in parsed.sections:
        if section.name not in allowed:
            yield _finding(
                unit,
                facts,
                "unit-section-invalid",
                "error",
                f"section [{section.name}] (line {section.line}) does not belong in this unit",
                f"Use {', '.join(f'[{name}]' for name in sorted(allowed))}.",
            )
            continue
        for directive in section.directives:
            yield from _directive(unit, facts, section.name, directive.key, directive.value)
            if directive.key.startswith("ExecStartPre") and _makes_state_dir(directive.value):
                yield _finding(
                    unit,
                    facts,
                    "state-dir-mkdir",
                    "info",
                    f"ExecStartPre= creates a state directory: {directive.value}",
                    "systemd creates it with StateDirectory=, LogsDirectory=, "
                    "RuntimeDirectory= or CacheDirectory=, owned by the service user.",
                )


def _directive(
    unit: ShippedUnit, facts: Facts, section: str, key: str, value: str
) -> Iterator[Diagnostic]:
    if key.startswith("X-"):
        return
    if not is_known(section, key):
        yield _finding(
            unit,
            facts,
            "unit-directive-unknown",
            "warning",
            f"[{section}] {key}= is not a systemd 257 directive",
            "Check the spelling against systemd.directives(7); systemd ignores unknown keys.",
        )
        return
    problem = check_value(section, key, value)
    if problem is not None:
        yield _finding(
            unit,
            facts,
            "unit-value-invalid",
            "error",
            f"[{section}] {key}={value} is invalid: {problem}",
            "systemd ignores an invalid assignment and keeps the default.",
        )
    kind = kind_of(section, key)
    if kind is not None and kind.name == "command" and value:
        program = value.lstrip(_EXEC_PREFIX).split(maxsplit=1)
        if program and not program[0].startswith(("/", "%", "$")):
            yield _finding(
                unit,
                facts,
                "unit-exec-relative",
                "warning",
                f"{key}= runs {program[0]!r} by a relative name",
                f"Spell the absolute path (/usr/bin/{program[0]} or /usr/sbin/{program[0]}); "
                "systemd's search path is not $PATH.",
            )


def _makes_state_dir(command: str) -> bool:
    words = command.lstrip(_EXEC_PREFIX).split()
    if not words or not _MKDIR.search(words[0]):
        return False
    if posixpath.basename(words[0]) == "install" and "-d" not in words:
        return False
    return any(word.startswith(_STATE_ROOTS) for word in words[1:])


def _accounts(unit: ShippedUnit, parsed: UnitText, facts: Facts) -> Iterator[Diagnostic]:
    """``User=``/``Group=``/``SocketUser=``/``SocketGroup=`` naming an account nobody makes."""
    section = unit.own_section
    if not section or any(v.lower() in TRUE for v in parsed.values(section, "DynamicUser")):
        return
    checks = (("User", facts.has_user), ("Group", facts.has_group))
    for prefix in ("", "Socket"):
        for key, known in checks:
            if unit.generated and not prefix and key == "User":
                continue
            for account in parsed.values(section, prefix + key):
                if "%" in account or account.isdigit() or known(account):
                    continue
                kind = "User" if key == "User" else "Group"
                yield _finding(
                    unit,
                    facts,
                    "service-user-missing",
                    "error",
                    f"{prefix}{key}={account} names a {kind.lower()} that is never created",
                    f"Declare {kind}({account!r}) in variant {facts.variant!r}.",
                )


def _relations(unit: ShippedUnit, parsed: UnitText, facts: Facts) -> Iterator[Diagnostic]:
    """Contradictory relations, and (raw text) ``Requires=`` without ordering."""
    after = set(_words(parsed, "Unit", "After"))
    before = set(_words(parsed, "Unit", "Before"))
    pulls = set(_words(parsed, "Unit", "Requires")) | set(_words(parsed, "Unit", "Wants"))
    conflicts = set(_words(parsed, "Unit", "Conflicts"))
    problems = [f"both pulls in and conflicts with {name}" for name in sorted(pulls & conflicts)]
    problems += [f"is ordered both before and after {name}" for name in sorted(after & before)]
    if unit.name in after | before | pulls | conflicts and not unit.is_dropin:
        problems.append("names itself in its own relations")
    for problem in problems:
        yield _finding(
            unit, facts, "relation-conflict", "error", problem, "Drop one of the relations."
        )
    if unit.generated:
        return
    for name in _words(parsed, "Unit", "Requires"):
        if name not in after and name != unit.name:
            yield _finding(
                unit,
                facts,
                "requires-without-after",
                "info",
                f"Requires={name} without After={name}: both start in parallel",
                f"Add After={name} so this unit starts once {name} is up.",
            )


def _activation(
    units: Sequence[tuple[ShippedUnit, UnitText]], facts: Facts
) -> Iterator[Diagnostic]:
    """Socket and timer pairing, eager socket-activated services, enabling without [Install]."""
    texts = {u.name: p for u, p in units if not u.is_dropin}
    known = set(texts) | facts.enabled
    severity: Severity = "warning" if facts.packages else "error"
    activated: dict[str, str] = {}
    for unit, parsed in units:
        stem, suffix = posixpath.splitext(unit.name)
        if unit.is_dropin or suffix not in (".socket", ".timer"):
            continue
        own = "Socket" if suffix == ".socket" else "Timer"
        explicit = parsed.values(own, "Service" if own == "Socket" else "Unit")
        accept = any(v.lower() in TRUE for v in parsed.values("Socket", "Accept"))
        target = explicit[-1] if explicit else f"{stem}@.service" if accept else f"{stem}.service"
        activated[target] = unit.name
        if target not in known:
            code = "socket-service-unpaired" if own == "Socket" else "timer-unit-missing"
            yield _finding(
                unit,
                facts,
                code,
                severity,
                f"activates {target}, which the variant neither ships nor enables",
                f"Ship {target} next to {unit.name}, or name the unit it starts with "
                f"{'Service' if own == 'Socket' else 'Unit'}=.",
            )
    for unit, parsed in units:
        if unit.is_dropin:
            continue
        install = any(parsed.values("Install", key) for key in _INSTALL_KEYS)
        trigger = activated.get(unit.name)
        if trigger and install and unit.name in facts.enabled:
            timer = trigger.endswith(".timer")
            yield _finding(
                unit,
                facts,
                "timer-service-enabled" if timer else "socket-service-started",
                "warning" if timer else "info",
                f"{trigger} starts it, and it is also enabled at boot",
                "Leave it out of [Install] and do not enable it; "
                f"enable {trigger} alone so it starts on demand.",
            )
        elif unit.name in facts.enabled and not install and unit.own_section is not None:
            yield _finding(
                unit,
                facts,
                "unit-enable-noop",
                "warning",
                "enabled, but it has no [Install] section, so systemctl enable does nothing",
                "Add [Install] WantedBy=, or stop enabling it.",
            )


def unit_findings(units: Sequence[ShippedUnit], facts: Facts) -> list[Diagnostic]:
    """Every unit-text finding for one variant's *units*."""
    parsed = [(unit, UnitText.parse(unit.text)) for unit in units]
    found: list[Diagnostic] = []
    for unit, text in parsed:
        found.extend(_per_unit(unit, text, facts))
        found.extend(_accounts(unit, text, facts))
        found.extend(_relations(unit, text, facts))
    found.extend(_activation(parsed, facts))
    found.extend(_cloud_glue(parsed, facts))
    if not facts.style:
        found = [d for d in found if d.code not in STYLE_CODES]
    return list(dict.fromkeys(found))


CLOUD_GLUE: dict[str, str] = {
    "azure-complete-provisioning.service": "microsoft",
    "walinuxagent.service": "microsoft",
    "waagent.service": "microsoft",
    "google-guest-agent.service": "google",
    "google-startup-scripts.service": "google",
    "google-shutdown-scripts.service": "google",
    "google-osconfig-agent.service": "google",
    "gcp-metadata-dns.service": "google",
}
"""Units that only make sense on one cloud -> the ``ConditionVirtualization=`` id that gates
them (``microsoft``: Azure, ``google``: Compute Engine)."""

_GLUE_TARGET = {"microsoft": "azure", "google": "gcp"}


def _cloud_glue(
    parsed: Sequence[tuple[ShippedUnit, UnitText]], facts: Facts
) -> Iterator[Diagnostic]:
    """``cloud-glue-unconditional``: a one-cloud unit (:data:`CLOUD_GLUE`) in a variant whose
    UKI also boots elsewhere, with no ``ConditionVirtualization=`` of its cloud in the unit
    or a drop-in: it would run, and fail, on the other outputs."""
    gated: dict[str, set[str]] = {}
    for unit, text in parsed:
        gated.setdefault(unit.name, set()).update(_words(text, "Unit", "ConditionVirtualization"))
    for unit, _ in parsed:
        kind = CLOUD_GLUE.get(unit.name)
        if kind is None or unit.is_dropin:
            continue
        others = sorted(facts.outputs - {_GLUE_TARGET[kind]})
        if not others or kind in gated[unit.name]:
            continue
        yield _finding(
            unit,
            facts,
            "cloud-glue-unconditional",
            "warning",
            f"{unit.name} only works on {_GLUE_TARGET[kind]} but has no "
            f"ConditionVirtualization={kind}; the variant's UKI also boots on "
            f"{', '.join(others)}",
            f'Add conditions=(Condition.virtualization("{kind}"),) to the Service/Task (or '
            f"ConditionVirtualization={kind} to the unit text) so it stays inert elsewhere.",
        )


__all__ = [
    "CLOUD_GLUE",
    "FALSE",
    "STYLE_CODES",
    "TRUE",
    "Facts",
    "ShippedUnit",
    "unit_findings",
]
