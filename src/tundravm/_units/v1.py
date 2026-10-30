"""Unit texts kept as literals: shapes the typed renderer does not produce.

They disagree with each other on key order, continuation style and the final newline, so
no renderer order reproduces them (docs/design/systemd-units.md 3.2). Each is pinned byte
for byte by ``tests/compiler/test_unit_render.py``. Modules re-export them under their names.
"""

from __future__ import annotations

MINIMAL_TARGET = (
    "[Unit]\n"
    "Description=Minimal System\n"
    "Requires=basic.target\n"
    "Conflicts=rescue.service rescue.target\n"
    "After=basic.target rescue.service rescue.target\n"
    "AllowIsolate=yes\n"
    "\n"
    "[Install]\n"
    "WantedBy=default.target"
)
"""``/etc/systemd/system/minimal.target`` of nethermind-tdx's skeleton (no final newline)."""

SERIAL_CONSOLE = """\
[Unit]
Description=Serial Console
After=systemd-logind.service

[Service]
Type=oneshot
ExecStart=/bin/systemctl enable serial-getty@ttyS0.service
ExecStartPost=/bin/systemctl start serial-getty@ttyS0.service
RemainAfterExit=yes

[Install]
WantedBy=minimal.target
"""
"""``serial-console.service`` of ``DevTools``: enables the serial getty at boot."""

_NETWORK_SETUP = "After=network.target network-setup.service\nRequires=network-setup.service"
_NETWORK_ONLINE = "After=network-online.target\nWants=network-online.target"

_ONESHOT = """\
[Unit]
Description={description}
{ordering}

[Service]
Type=oneshot
ExecStart={exec_start}
RemainAfterExit=yes

[Install]
WantedBy=minimal.target
"""


def network_ordering(network_setup: bool) -> str:
    """The ``[Unit]`` lines waiting for the network: ``network-setup.service`` or online."""
    return _NETWORK_SETUP if network_setup else _NETWORK_ONLINE


def oneshot(description: str, ordering: str, exec_start: str) -> str:
    """The boot oneshot shape of ``runtime-init.service``."""
    return _ONESHOT.format(description=description, ordering=ordering, exec_start=exec_start)
