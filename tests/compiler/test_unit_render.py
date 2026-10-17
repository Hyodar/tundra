"""The one unit renderer (``_units.render``), systemd command quoting, and the historical
texts of ``_units.v1`` pinned byte for byte."""

from __future__ import annotations

import hashlib

import pytest

from tundravm._modules.tdxs import Tdxs
from tundravm._units import UnitText, v1
from tundravm._units.render import canonical, command_line, render, service_unit
from tundravm.models import ServiceSpec


@pytest.mark.parametrize(
    ("argv", "rendered"),
    [
        (("/usr/bin/app", "--port", "8080"), "/usr/bin/app --port 8080"),
        (("/bin/sh", "-c", "echo hi"), '/bin/sh -c "echo hi"'),
        (("/usr/bin/app", "--name", "a b"), '/usr/bin/app --name "a b"'),
        (("/usr/bin/date", "+%h %d"), '/usr/bin/date "+%%h %%d"'),
        (("/usr/bin/app", "--fmt", "%h"), "/usr/bin/app --fmt %%h"),
        (("/usr/bin/app", 'say "hi"'), '/usr/bin/app "say \\"hi\\""'),
        (("/usr/bin/app", "C:\\dir"), '/usr/bin/app "C:\\\\dir"'),
        (("/usr/bin/app", "it's"), '/usr/bin/app "it\'s"'),
        (("/usr/bin/app", ""), '/usr/bin/app ""'),
        (("/usr/bin/app", ";"), "/usr/bin/app \\;"),
        (("/bin/sh", "-c", "echo $HOME"), '/bin/sh -c "echo $$HOME"'),
        (("/usr/bin/app", "cost$5"), "/usr/bin/app cost$$5"),
        (("/usr/bin/app", "$PORT"), "/usr/bin/app $PORT"),
        (("/usr/bin/app", "--addr=${ADDR}"), "/usr/bin/app --addr=${ADDR}"),
        (("+/usr/bin/install", "-d", "/var/lib/app"), "+/usr/bin/install -d /var/lib/app"),
    ],
)
def test_commands_are_quoted_per_systemd(argv: tuple[str, ...], rendered: str) -> None:
    assert command_line(argv) == rendered


def test_service_exec_lines_keep_every_argument() -> None:
    """The 1.4.1 defect: argv used to be joined with spaces, losing quoting and ``%``."""
    svc = ServiceSpec(
        name="a",
        command=("/bin/sh", "-c", "echo 100% done"),
        exec_start_pre=("/usr/bin/printf '%s\\n' 'a b'",),
    )
    unit = UnitText.parse(service_unit(svc))
    assert unit.values("Service", "ExecStart") == ('/bin/sh -c "echo 100%% done"',)
    assert unit.values("Service", "ExecStartPre") == ('/usr/bin/printf "%%s\\\\n" "a b"',)


def test_a_plain_service_renders_todays_bytes() -> None:
    svc = ServiceSpec(
        name="app",
        command=("/usr/bin/app", "--port", "8080"),
        user="app",
        after=("runtime-init.service",),
        requires=("runtime-init.service",),
        env={"B": "two words", "A": "1"},
        restart="on-failure",
        limits={"NOFILE": "65536"},
        security_profile="strict",
        extra_unit={"Service": {"OOMScoreAdjust": "-500"}},
    )
    expected = (
        "[Unit]\nDescription=app\nAfter=runtime-init.service\nRequires=runtime-init.service\n\n"
        "[Service]\nType=simple\nExecStart=/usr/bin/app --port 8080\nUser=app\n"
        'Environment=A=1\nEnvironment="B=two words"\nRestart=on-failure\nRestartSec=5\n'
        "LimitNOFILE=65536\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n"
        "NoNewPrivileges=yes\nProtectKernelModules=yes\nProtectKernelTunables=yes\n"
        "ProtectControlGroups=yes\nRestrictSUIDSGID=yes\nMemoryDenyWriteExecute=yes\n"
        "OOMScoreAdjust=-500\n\n[Install]\nWantedBy=minimal.target\n"
    )
    assert service_unit(svc) == expected


def test_canonical_order_extends_todays_order() -> None:
    shuffled = [("User", "u"), ("TimeoutStartSec", "5min"), ("LimitNOFILE", "1"), ("Type", "exec")]
    shuffled += [("RemainAfterExit", "yes"), ("ExecStart", "/a"), ("StateDirectory", "s")]
    assert [key for key, _ in canonical("Service", shuffled)] == [
        "Type",
        "RemainAfterExit",
        "ExecStart",
        "User",
        "StateDirectory",
        "LimitNOFILE",
        "TimeoutStartSec",
    ]
    unit = [("ConditionPathExists", "/x"), ("Zeta", "1"), ("After", "a"), ("Description", "d")]
    assert [key for key, _ in canonical("Unit", unit)] == [
        "Description",
        "After",
        "ConditionPathExists",
        "Zeta",
    ]


def test_render_writes_sections_in_order_and_skips_empty_ones() -> None:
    text = render([("Unit", [("After", "a")]), ("Service", []), ("Install", [("WantedBy", "b")])])
    assert text == "[Unit]\nAfter=a\n\n[Install]\nWantedBy=b\n"
    assert render([("Unit", [("A", "b")])], trailing_newline=False) == "[Unit]\nA=b"
    assert UnitText.parse(text).text == text


HISTORICAL = {
    "minimal.target": (v1.MINIMAL_TARGET, "63aec6aa0cb073e8"),
    "serial-console.service": (v1.SERIAL_CONSOLE, "016dd361c7172e6c"),
    "azure-complete-provisioning.service": (v1.AZURE_PROVISIONING, "0085597d020c01bc"),
    "azure-complete-provisioning.service (online)": (
        v1.AZURE_PROVISIONING_ONLINE,
        "31b8f487face9da0",
    ),
    "runtime-init.service": (v1.runtime_init(), "a2cc9dd7c55d79f2"),
    "runtime-init.service (online)": (v1.runtime_init(network_setup=False), "e06ae5b08b14a8fe"),
    "tdxs.service": (Tdxs().render_service_unit(), "2456316154d84ccb"),
    "tdxs.socket": (Tdxs().render_socket_unit(), "26a94aee3e913541"),
}
"""Each historical text and the first 16 hex digits of its sha256, recorded from the
literals these replaced; the golden tree and the parity digests pin them too."""


@pytest.mark.parametrize("name", sorted(HISTORICAL))
def test_historical_texts_keep_their_bytes(name: str) -> None:
    text, digest = HISTORICAL[name]
    assert hashlib.sha256(text.encode()).hexdigest()[:16] == digest
    assert UnitText.parse(text).text == text
