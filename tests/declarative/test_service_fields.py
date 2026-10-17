"""The grown ``Service`` (systemd-units design, migration steps 4-5): each field renders its
directive in canonical order, validation refuses what systemd cannot run, and a ``Service``
that uses only the historical fields lowers to the historical bytes and digest."""

from __future__ import annotations

import pytest

from tundravm._units.render import service_unit
from tundravm.declarative import Dynamic, Hardening, SecretFile, Service, Unit
from tundravm.declarative.state import _service
from tundravm.errors import ValidationError
from tundravm.models import ProfileState

BASE = "[Unit]\nDescription=app\n\n[Service]\nType=simple\nExecStart=/usr/bin/app\n"
INSTALL = "\n[Install]\nWantedBy=minimal.target\n"


LEGACY: dict[str, object] = {"type": None, "restart": "no", "hardening": Hardening.none()}
"""The pre-step-7 defaults, pinned so each case shows only its own field's lines."""


def rendered(**fields: object) -> str:
    """The unit with *fields* over :data:`LEGACY`; the declared user's ``User=app`` line
    is left out unless *fields* sets ``user`` (tests/compiler/test_service_defaults.py
    covers it)."""
    profile = ProfileState(name="default")
    service = Service("app", "/usr/bin/app", **{**LEGACY, **fields})  # type: ignore[arg-type]
    _service(profile, service)
    text = service_unit(profile.services[0])
    return text if "user" in fields else text.replace("User=app\n", "", 1)


def test_historical_fields_render_the_historical_unit() -> None:
    assert rendered() == BASE + INSTALL
    expected = BASE + "Restart=on-failure\nRestartSec=5\n" + INSTALL
    assert rendered(restart="on-failure", restart_sec="5") == expected


@pytest.mark.parametrize(
    ("fields", "lines"),
    [
        ({"user": Dynamic()}, "DynamicUser=yes\n"),
        ({"groups": ["tdx", "eth"]}, "SupplementaryGroups=tdx eth\n"),
        ({"exec_start_post": ["/usr/bin/ready --ok"]}, "ExecStartPost=/usr/bin/ready --ok\n"),
        ({"exec_reload": "/bin/kill -HUP $MAINPID"}, "ExecReload=/bin/kill -HUP $MAINPID\n"),
        ({"state": "app"}, "WorkingDirectory=/var/lib/app\nStateDirectory=app\n"),
        ({"logs": "app"}, "LogsDirectory=app\n"),
        ({"runtime": "app"}, "RuntimeDirectory=app\n"),
        ({"cache": "app"}, "CacheDirectory=app\n"),
        ({"write_paths": ["/persistent/app", "/srv"]}, "ReadWritePaths=/persistent/app /srv\n"),
        ({"credentials": {"tok": "/run/secrets/tok"}}, "LoadCredential=tok:/run/secrets/tok\n"),
        ({"credentials": {"k": SecretFile("/run/k")}}, "LoadCredential=k:/run/k\n"),
        (
            {"env_file": ["/etc/default/app", "-/etc/app.env"]},
            "EnvironmentFile=/etc/default/app\nEnvironmentFile=-/etc/app.env\n",
        ),
        ({"timeout_start": "30s"}, "TimeoutStartSec=30s\n"),
        ({"timeout_stop": "10s"}, "TimeoutStopSec=10s\n"),
        ({"limits": {"NOFILE": 65536}}, "LimitNOFILE=65536\n"),
        ({"extra": {"Service": {"OOMScoreAdjust": "-500"}}}, "OOMScoreAdjust=-500\n"),
    ],
)
def test_each_service_field_renders_its_directive(fields: dict[str, object], lines: str) -> None:
    assert rendered(**fields) == BASE + lines + INSTALL


def test_restart_sec_replaces_or_drops_the_historical_line() -> None:
    assert "Restart=always\nRestartSec=2s\n" in rendered(restart="always", restart_sec="2s")
    assert "RestartSec" not in rendered(restart="always", restart_sec=None)


@pytest.mark.parametrize(
    ("fields", "unit"),
    [
        ({"requires": ["db.service"]}, "After=db.service\nRequires=db.service\n"),
        ({"binds_to": ["dev.device"]}, "After=dev.device\nBindsTo=dev.device\n"),
        ({"wants": ["x.service"]}, "After=x.service\nWants=x.service\n"),
        ({"before": ["y.service"]}, "Before=y.service\n"),
        ({"part_of": ["z.target"]}, "PartOf=z.target\n"),
        ({"conflicts": ["w.service"]}, "Conflicts=w.service\n"),
        (
            {"after": ["a.service"], "requires": ["a.service"]},
            "After=a.service\nRequires=a.service\n",
        ),
    ],
)
def test_relations_render_and_dependencies_order_after(
    fields: dict[str, object], unit: str
) -> None:
    expected = BASE.replace("Description=app\n", "Description=app\n" + unit)
    assert rendered(**fields) == expected + INSTALL


def test_relation_refs_name_their_unit() -> None:
    text = rendered(requires=[Service.ref("tdxs"), Unit.ref("tdxs.socket")])
    assert "Requires=tdxs.service tdxs.socket\n" in text


def test_disabled_service_has_no_install_section() -> None:
    assert rendered(enabled=False) == BASE


def test_hardening_profiles_render_in_canonical_order() -> None:
    strict = rendered(hardening=Hardening.strict())
    assert strict == BASE + "".join(f"{k}={v}\n" for k, v in STRICT) + INSTALL
    relaxed = rendered(hardening=Hardening.relaxed())
    assert "ProtectSystem=full\nPrivateTmp=yes\nNoNewPrivileges=yes\n" in relaxed
    assert "SystemCallFilter" not in relaxed and "MemoryDenyWriteExecute" not in relaxed
    assert rendered(hardening=Hardening.none()) == BASE + INSTALL
    legacy = rendered(hardening=None, security="strict")
    assert "ProtectSystem=strict\n" in legacy and "PrivateDevices" not in legacy
    assert rendered(hardening=None, security="none") == BASE + INSTALL


def test_hardening_overrides() -> None:
    text = rendered(hardening=Hardening.strict(devices=("/dev/tdx_guest",), jit=True))
    assert "DevicePolicy=closed\nDeviceAllow=/dev/tdx_guest rw\n" in text
    assert "PrivateDevices" not in text and "MemoryDenyWriteExecute" not in text
    caps = rendered(hardening=Hardening.strict(capabilities=("CAP_NET_BIND_SERVICE",)))
    assert "CapabilityBoundingSet=CAP_NET_BIND_SERVICE\n" in caps
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE\n" in caps
    unix = rendered(hardening=Hardening.strict(network="unix", kernel=False))
    assert "RestrictAddressFamilies=AF_UNIX\n" in unix and "ProtectClock" not in unix


STRICT = [
    *(
        (key, "yes" if key != "ProtectSystem" else "strict")
        for key in "ProtectSystem ProtectHome PrivateTmp NoNewPrivileges ProtectKernelModules"
        " ProtectKernelTunables ProtectControlGroups RestrictSUIDSGID MemoryDenyWriteExecute"
        " PrivateDevices ProtectKernelLogs ProtectClock ProtectHostname RestrictRealtime"
        " RestrictNamespaces LockPersonality".split()
    ),
    ("RestrictAddressFamilies", "AF_UNIX AF_INET AF_INET6"),
    ("SystemCallArchitectures", "native"),
    ("SystemCallFilter", "@system-service"),
    ("SystemCallFilter", "~@privileged @resources"),
    ("CapabilityBoundingSet", ""),
    ("UMask", "0077"),
]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"type": "daemon"}, "invalid type"),
        ({"restart": "onfailure"}, "invalid restart"),
        ({"restart_sec": "soon"}, "not a systemd time span"),
        ({"timeout_start": "5 parsecs"}, "not a systemd time span"),
        ({"state": "/var/lib/app"}, "must be relative"),
        ({"logs": "a b"}, "one directory name"),
        ({"write_paths": ["relative"]}, "absolute path"),
        ({"credentials": {"bad name": "/run/x"}}, "invalid credential name"),
        ({"credentials": {"tok": "run/x"}}, "absolute path"),
        ({"groups": ["Bad"]}, "invalid group name"),
        ({"requires": "db.service"}, "must be a list of unit names or refs"),
        ({"after": [Unit.ref("x.service").remove()]}, "invalid unit name"),
        ({"hardening": "strict"}, "must be a Hardening"),
        ({"hardening": Hardening.strict(), "security": "strict"}, "both security= and hardening="),
        ({"enabled": False, "wanted_by": "multi-user.target"}, "not enabled"),
        ({"extra": {"Services": {"Nice": "5"}}}, "section 'Services' is not valid"),
        ({"extra": {"Service": {"User": "root"}}}, "typed field user= renders"),
        ({"extra": {"Service": {"LimitNPROC": "5"}}}, "typed field limits= renders"),
        ({"extra": {"Service": {"Restrat": "always"}}}, "unknown to systemd"),
        ({"extra": {"Service": {"Nice": "5\n"}}}, "must be one line"),
    ],
)
def test_service_validation(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Service("app", "/usr/bin/app", **fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"devices": ("tdx_guest",)}, "under /dev/"),
        ({"capabilities": ("NET_ADMIN",)}, "invalid capability"),
        ({"network": "ipv6"}, "invalid network"),
        ({"umask": "77"}, "invalid umask"),
        ({"jit": "yes"}, "True or False"),
    ],
)
def test_hardening_validation(fields: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Hardening.strict(**fields)


def test_unset_fields_stay_out_of_the_digest_payload() -> None:
    profile = ProfileState(name="default")
    legacy = {**LEGACY, "user": "app", "restart": "on-failure", "restart_sec": "5"}
    _service(profile, Service("app", "/usr/bin/app", **legacy))  # type: ignore[arg-type]
    assert profile.services[0].extras() == {"hardening": []}
    _service(profile, Service("b", "/usr/bin/b", security="strict", restart_sec=None))
    assert profile.services[1].extras() == {"type": "exec", "restart_sec": ""}
