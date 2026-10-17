"""The vendored systemd 257 directive table and the value kinds lint checks."""

from __future__ import annotations

import pytest

from tundravm._units.directives import SECTIONS, SYSTEMD_VERSION, check_value, is_known, kind_of


def test_table_is_systemd_257_with_every_unit_section() -> None:
    assert SYSTEMD_VERSION == 257
    assert {"Unit", "Install", "Service", "Socket", "Timer", "Mount", "Path"} <= set(SECTIONS)
    assert is_known("Service", "ExecStart") and is_known("Socket", "ExecStartPre")
    assert is_known("Unit", "ConditionPathExists") and is_known("Install", "WantedBy")
    assert not is_known("Service", "Restrat") and not is_known("Socket", "Restart")


@pytest.mark.parametrize(
    ("section", "key", "kind"),
    [
        ("Service", "Type", "enum"),
        ("Service", "Restart", "enum"),
        ("Socket", "Accept", "bool"),
        ("Service", "RemainAfterExit", "bool"),
        ("Service", "TimeoutStartSec", "time"),
        ("Timer", "OnBootSec", "time"),
        ("Service", "ExecStart", "command"),
        ("Unit", "After", "units"),
        ("Unit", "Description", None),
    ],
)
def test_kinds_follow_systemds_parsers(section: str, key: str, kind: str | None) -> None:
    found = kind_of(section, key)
    assert (found.name if found else None) == kind


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("Service", "Type", "notify-reload"),
        ("Service", "Restart", "on-failure"),
        ("Socket", "Accept", "false"),
        ("Service", "RemainAfterExit", "YES"),
        ("Service", "ProtectSystem", "strict"),
        ("Service", "ProtectHome", "yes"),
        ("Service", "TimeoutStartSec", "1min 30s"),
        ("Service", "RestartSec", "5"),
        ("Service", "TimeoutStopSec", "infinity"),
        ("Timer", "OnBootSec", "1.5h"),
        ("Service", "Restart", ""),
        ("Service", "TimeoutStartSec", "${TIMEOUT}"),
    ],
)
def test_valid_values_pass(section: str, key: str, value: str) -> None:
    assert check_value(section, key, value) is None


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("Service", "Type", "daemon"),
        ("Service", "Restart", "onfailure"),
        ("Socket", "Accept", "maybe"),
        ("Service", "ProtectSystem", "ro"),
        ("Service", "TimeoutStartSec", "soon"),
        ("Timer", "OnUnitActiveSec", "1 fortnight"),
    ],
)
def test_invalid_values_say_why(section: str, key: str, value: str) -> None:
    assert check_value(section, key, value)
