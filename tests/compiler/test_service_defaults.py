"""The opinionated ``Service`` defaults (systemd-units design, migration step 7): the exact
unit a bare ``Service`` renders, the system user it declares, the lints on opt-outs, and the
debloat keep list that leaves ``timers.target`` alone outside ``nethermind-v1``."""

from __future__ import annotations

from tundravm._units.render import service_unit
from tundravm.declarative import (
    Debian,
    Hardening,
    Mkosi,
    Recipe,
    Service,
    Task,
    User,
    lint,
    lower,
)
from tundravm.declarative.state import _service
from tundravm.models import (
    DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP,
    LEGACY_DEBLOAT_SYSTEMD_UNITS_KEEP,
    ProfileState,
)

STRICT_LINES = (
    "ProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\nNoNewPrivileges=yes\n"
    "ProtectKernelModules=yes\nProtectKernelTunables=yes\nProtectControlGroups=yes\n"
    "RestrictSUIDSGID=yes\nMemoryDenyWriteExecute=yes\nPrivateDevices=yes\n"
    "ProtectKernelLogs=yes\nProtectClock=yes\nProtectHostname=yes\nRestrictRealtime=yes\n"
    "RestrictNamespaces=yes\nLockPersonality=yes\n"
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\nSystemCallArchitectures=native\n"
    "SystemCallFilter=@system-service\nSystemCallFilter=~@privileged @resources\n"
    "CapabilityBoundingSet=\nUMask=0077\n"
)


def rendered(service: Service) -> tuple[str, ProfileState]:
    profile = ProfileState(name="default")
    _service(profile, service)
    return service_unit(profile.services[0]), profile


def test_a_bare_service_renders_the_opinionated_unit() -> None:
    text, profile = rendered(Service("app", "/usr/bin/app", state="app"))
    assert text == (
        "[Unit]\nDescription=app\n\n"
        "[Service]\nType=exec\nExecStart=/usr/bin/app\nUser=app\n"
        "WorkingDirectory=/var/lib/app\nStateDirectory=app\n"
        "Restart=on-failure\nRestartSec=5s\n" + STRICT_LINES + "\n"
        "[Install]\nWantedBy=minimal.target\n"
    )
    [user] = profile.users
    assert (user.name, user.system, user.home, user.shell) == (
        "app",
        True,
        None,
        "/usr/sbin/nologin",
    )


def test_explicit_users_are_not_declared_twice() -> None:
    profile = ProfileState(name="default")
    _service(profile, Service("app", "/usr/bin/app"), users={"app"})
    _service(profile, Service("b", "/usr/bin/b", user="root"))
    assert profile.users == []
    assert [s.user for s in profile.services] == ["app", "root"]


def test_remain_after_exit_renders_on_a_service() -> None:
    text, _ = rendered(Service("once", "/usr/bin/once", type="oneshot", remain_after_exit=True))
    assert "Type=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/once\n" in text


def test_wants_orders_after_and_legacy_spellings_still_render() -> None:
    text, _ = rendered(Service("app", "/usr/bin/app", wants=("network-online.target",)))
    assert "After=network-online.target\nWants=network-online.target\n" in text
    legacy, _ = rendered(Service("app", "/usr/bin/app", security="strict", type=None))
    assert "Type=simple\n" in legacy and "ProtectSystem=strict\n" in legacy
    assert "PrivateDevices" not in legacy
    bare, _ = rendered(Service("app", "/usr/bin/app", security="none", restart_sec=None))
    assert "Protect" not in bare and "RestartSec" not in bare


def test_opt_outs_are_linted() -> None:
    recipe = Recipe(
        name="t",
        platform=Debian("trixie"),
        items=(
            Service("root", "/usr/bin/a", user="root"),
            Service("bare", "/usr/bin/b", hardening=Hardening.none()),
            Service("jit", "/usr/bin/c", hardening=Hardening.strict(jit=True)),
            Service("dev", "/usr/bin/d", hardening=Hardening.strict(devices=("/dev/tdx_guest",))),
            Task("once", "/usr/bin/e", user="root"),
        ),
    )
    found = {
        (d.code, d.subject, d.severity)
        for d in lint(recipe)
        if d.code.startswith("hardening") or d.code == "service-runs-as-root"
    }
    assert found == {
        ("service-runs-as-root", "root.service", "info"),
        ("service-runs-as-root", "once.service", "info"),
        ("hardening-disabled", "bare.service", "warning"),
        ("hardening-relaxed", "jit.service", "info"),
    }


def test_debloat_keeps_timers_target_except_in_the_historical_dialect() -> None:
    assert "timers.target" in DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP
    assert "timers.target" not in LEGACY_DEBLOAT_SYSTEMD_UNITS_KEEP
    items = (User("app"),)
    for mkosi, expected in ((Mkosi(), True), (Mkosi(dialect="nethermind-v1"), False)):
        recipe = Recipe(name="t", platform=Debian("trixie"), items=items, mkosi=mkosi)
        debloat = lower(recipe).state.profiles["default"].debloat
        assert ("timers.target" in debloat.effective_units_keep) is expected
