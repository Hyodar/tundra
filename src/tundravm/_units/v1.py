"""Historical unit texts: the bytes of the ``nethermind-v1`` golden tree, kept as literals.

These units disagree with each other on key order, continuation style, ``Accept=false`` and
the final newline, so no renderer order reproduces them (docs/design/systemd-units.md 3.2).
Each is pinned byte for byte by ``tests/compiler/test_unit_render.py`` and the golden tree.
Modules re-export them under their old names.
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
    """The boot oneshot shape runtime-init and Azure provisioning share."""
    return _ONESHOT.format(description=description, ordering=ordering, exec_start=exec_start)


AZURE_PROVISIONING = oneshot(
    "Azure Provisioning Completion", _NETWORK_SETUP, "/usr/bin/azure-complete-provisioning"
)
AZURE_PROVISIONING_ONLINE = oneshot(
    "Azure Provisioning Completion", _NETWORK_ONLINE, "/usr/bin/azure-complete-provisioning"
)


def runtime_init(*, network_setup: bool = True) -> str:
    """The ``nethermind-v1`` ``runtime-init.service``, which runs ``/usr/bin/runtime-init``."""
    return oneshot("Runtime Init", network_ordering(network_setup), "/usr/bin/runtime-init")


def tdxs_service(*, socket: str, user: str, group: str, config: str, log_level: str) -> str:
    """``tdxs.service``: indented continuation lines, ``Requires=`` without ``After=``."""
    return (
        f"[Unit]\nDescription=TDXS\nRequires={socket}\n\n"
        f"[Service]\nUser={user}\nGroup={group}\nWorkingDirectory=/home/{user}\n"
        "Type=notify\nExecStart=/usr/bin/tdxs \\\n"
        f"    --config {config} \\\n    --log-level {log_level}\nRestart=on-failure\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def tdxs_socket(*, path: str, mode: str, user: str, group: str) -> str:
    """``tdxs.socket``: spells ``Accept=false``."""
    return (
        "[Unit]\nDescription=TDXS Socket\n\n"
        f"[Socket]\nListenStream={path}\nSocketMode={mode}\nSocketUser={user}\n"
        f"SocketGroup={group}\nAccept=false\n\n"
        "[Install]\nWantedBy=sockets.target\n"
    )
