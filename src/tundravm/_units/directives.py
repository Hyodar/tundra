"""Directive lookup and value kinds over the vendored systemd 257 table (``_systemd257``).

A directive's kind comes from the parser systemd itself uses for it, so ``Accept=`` is a
boolean because systemd parses it with ``config_parse_bool``. Only the kinds lint checks are
modelled: ``bool``, ``enum``, ``time``, ``command``, ``units``, ``path``; anything else is
``None`` (accepted as is).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._systemd257 import SECTIONS, SYSTEMD_VERSION

__all__ = ["SECTIONS", "SYSTEMD_VERSION", "Kind", "is_known", "kind_of", "check_value"]

UNIT_SUFFIXES: dict[str, str] = {
    ".service": "Service",
    ".socket": "Socket",
    ".mount": "Mount",
    ".automount": "Automount",
    ".swap": "Swap",
    ".timer": "Timer",
    ".path": "Path",
    ".slice": "Slice",
    ".scope": "Scope",
    ".target": "",
    ".device": "",
}
"""Unit suffix -> its type-specific section (``""``: only ``[Unit]`` and ``[Install]``)."""

TRUE = frozenset({"1", "yes", "y", "true", "t", "on"})
FALSE = frozenset({"0", "no", "n", "false", "f", "off"})
BOOL_WORDS = TRUE | FALSE

_ENUMS: dict[str, frozenset[str]] = {
    "service_type": frozenset(
        {"simple", "exec", "forking", "oneshot", "dbus", "notify", "notify-reload", "idle"}
    ),
    "service_restart": frozenset(
        {"no", "on-success", "on-failure", "on-abnormal", "on-watchdog", "on-abort", "always"}
    ),
    "service_restart_mode": frozenset({"normal", "direct", "debug"}),
    "service_exit_type": frozenset({"main", "cgroup"}),
    "notify_access": frozenset({"none", "main", "exec", "all"}),
    "kill_mode": frozenset({"control-group", "process", "mixed", "none"}),
    "device_policy": frozenset({"auto", "closed", "strict"}),
    "protect_system": BOOL_WORDS | {"full", "strict"},
    "protect_home": BOOL_WORDS | {"read-only", "tmpfs"},
    "private_tmp": BOOL_WORDS | {"disconnected"},
    "protect_proc": frozenset({"noaccess", "invisible", "ptraceable", "default"}),
    "proc_subset": frozenset({"all", "pid"}),
}
"""Closed value sets per systemd parser (booleans compared case-insensitively)."""

_TIME_PARSERS = frozenset(
    {
        "sec",
        "sec_fix_0",
        "sec_def_infinity",
        "sec_def_unset",
        "service_timeout",
        "service_timeout_abort",
        "job_timeout_sec",
        "job_running_timeout_sec",
    }
)
_MONOTONIC_TIMERS = frozenset(
    {"OnActiveSec", "OnBootSec", "OnStartupSec", "OnUnitActiveSec", "OnUnitInactiveSec"}
)
_TIME_UNITS = (
    "usec|us|µs|μs|nsec|ns|msec|ms|seconds|second|sec|s|minutes|minute|min|months|month|M|m"
    "|hours|hour|hr|h|days|day|d|weeks|week|w|years|year|y"
)
_TIME = re.compile(rf"(?:\s*\d+(?:\.\d*)?\s*(?:{_TIME_UNITS})?)+\s*")


@dataclass(frozen=True, slots=True)
class Kind:
    """What a directive's value is: *name* plus, for ``enum``, the accepted *values*."""

    name: str
    values: frozenset[str] = frozenset()


def is_known(section: str, key: str) -> bool:
    """Whether systemd 257 knows ``[section] key=``."""
    return key in SECTIONS.get(section, {})


def kind_of(section: str, key: str) -> Kind | None:
    """The value kind of ``[section] key=``, ``None`` when unknown or unmodelled."""
    parser = SECTIONS.get(section, {}).get(key)
    if parser is None:
        return None
    if section == "Timer" and key in _MONOTONIC_TIMERS:
        return Kind("time")
    if parser == "bool":
        return Kind("bool", BOOL_WORDS)
    if parser in _ENUMS:
        return Kind("enum", _ENUMS[parser])
    if parser in _TIME_PARSERS:
        return Kind("time")
    if parser == "exec":
        return Kind("command")
    if parser == "unit_deps":
        return Kind("units")
    if parser == "unit_path_printf":
        return Kind("path")
    return None


def check_value(section: str, key: str, value: str) -> str | None:
    """Why *value* is invalid for ``[section] key=`` (``None`` when valid or unchecked).

    Empty values (resets) and values with ``%`` specifiers or ``$`` variables are not
    checked: systemd resolves those at load time.
    """
    kind = kind_of(section, key)
    if kind is None or not value or "%" in value or "$" in value:
        return None
    if kind.name in ("bool", "enum"):
        word = value.lower() if value.lower() in BOOL_WORDS else value
        if word not in kind.values:
            words = sorted(kind.values)
            if BOOL_WORDS <= kind.values:
                words = ["yes", "no", *sorted(kind.values - BOOL_WORDS)]
            return f"expected one of {', '.join(words)}"
        return None
    if kind.name == "time":
        if value == "infinity" or _TIME.fullmatch(value):
            return None
        return "expected a time span such as 30s, 5min or infinity"
    if kind.name == "path" and not value.lstrip("-").startswith("/"):
        return "expected an absolute path"
    return None
