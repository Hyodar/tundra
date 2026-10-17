"""``Unit(name, dropin=DropIn(...))`` on packaged units, the ``unit-extra`` lint, unit refs
that do not resolve, and ``tundravm.testing`` queries over the grown ``Service``."""

from __future__ import annotations

import pytest

from tests.lint.test_check_rules import recipe, report
from tundravm.declarative import BootStep, DropIn, Dynamic, Hardening, Service, Unit
from tundravm.errors import ValidationError
from tundravm.testing import fragment_plan

DROPIN = "/usr/lib/systemd/system/nginx.service.d/20-tundravm.conf"


def test_dropin_renders_next_to_the_packaged_unit() -> None:
    dropin = DropIn(
        exec_start="/usr/sbin/nginx -g 'daemon off;'",
        limits={"NOFILE": 65536},
        restart="always",
        requires=("network-online.target",),
        hardening=Hardening.relaxed(),
    )
    image = fragment_plan(Unit("nginx.service", enabled=True, dropin=dropin))
    assert image.file(DROPIN).text == (
        "[Unit]\nAfter=network-online.target\nRequires=network-online.target\n\n[Service]\n"
        'ExecStart=\nExecStart=/usr/sbin/nginx -g "daemon off;"\nRestart=always\n'
        "LimitNOFILE=65536\nProtectSystem=full\nPrivateTmp=yes\nNoNewPrivileges=yes\n"
        "ProtectKernelModules=yes\nProtectKernelTunables=yes\nProtectControlGroups=yes\n"
        "RestrictSUIDSGID=yes\nProtectKernelLogs=yes\nProtectClock=yes\n"
    )


def test_dropin_keeps_the_needs_dropin_and_takes_a_name() -> None:
    step = BootStep("prep", "true")
    unit = Unit("nginx", needs=[step.ready], dropin=DropIn("30-env", env={"A": "b"}))
    image = fragment_plan(step, unit)
    base = "/usr/lib/systemd/system/nginx.service.d/"
    assert image.file(base + "30-env.conf").text == "[Service]\nEnvironment=A=b\n"
    assert "Requires=" in image.file(base + "10-tundravm-needs.conf").text


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: DropIn(), "changes nothing"),
        (lambda: DropIn("10-tundravm-needs", restart="always"), "reserved"),
        (lambda: DropIn("a b", restart="always"), "invalid drop-in name"),
        (lambda: DropIn(restart="sometimes"), "invalid restart"),  # type: ignore[arg-type]
        (lambda: Unit("x.service", "[Unit]\n", dropin=DropIn(restart="always")), "ships its text"),
        (lambda: Unit("x.socket", dropin=DropIn(restart="always")), r"writes \[Service\]"),
        (lambda: DropIn(extra={"Service": {"Restart": "always"}}), "restart= renders"),
    ],
)
def test_dropin_validation(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make()  # type: ignore[operator]


def test_unit_extra_is_reported_as_info() -> None:
    found = [
        d
        for d in report(
            recipe(
                Service("probe", "/bin/true", extra={"Service": {"OOMScoreAdjust": "-500"}}),
                Unit("ssh.service", dropin=DropIn(extra={"Unit": {"StartLimitBurst": "3"}})),
            )
        )
        if d.code == "unit-extra"
    ]
    assert [(d.severity, d.subject) for d in found] == [
        ("info", "probe.service"),
        ("info", "ssh.service"),
    ]
    assert "Service.OOMScoreAdjust" in found[0].message


def test_an_undeclared_unit_ref_is_refused_at_lowering() -> None:
    with pytest.raises(ValidationError, match="unit-ref-undefined"):
        _ = fragment_plan(Service("app", "/bin/true", requires=(Service.ref("db"),))).tree
    image = fragment_plan(
        Service("db", "/bin/db"), Service("app", "/bin/true", requires=(Service.ref("db"),))
    )
    assert image.unit("app").requires("db.service") and image.unit("app").after("db.service")


def test_testing_queries_over_the_new_fields() -> None:
    image = fragment_plan(
        Service(
            "app",
            "/usr/bin/app",
            user=Dynamic(),
            state="app",
            logs="app",
            credentials={"tok": "/run/secrets/tok"},
            hardening=Hardening.strict(jit=True),
            before=("b.service",),
            part_of=("p.target",),
            binds_to=("d.device",),
            conflicts=("c.service",),
        )
    )
    unit = image.unit("app")
    assert unit.dynamic_user and unit.directories == {"state": "app", "logs": "app"}
    assert unit.credentials == {"tok": "/run/secrets/tok"}
    assert unit.hardening["ProtectSystem"] == ("strict",)
    assert "MemoryDenyWriteExecute" not in unit.hardening
    assert unit.hardening["SystemCallFilter"] == ("@system-service", "~@privileged @resources")
    assert unit.before("b.service") and unit.part_of("p.target")
    assert unit.binds_to("d.device") and unit.conflicts("c.service")
