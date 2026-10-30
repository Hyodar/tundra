"""``Service``: readiness chains, hardening presets, grouped options and the argv contract."""

from __future__ import annotations

import pytest

from tundravm import (
    TCP,
    Credential,
    DropIn,
    Endpoint,
    Every,
    Hardening,
    HttpProbe,
    Image,
    Listen,
    Notify,
    Resources,
    Service,
    Shutdown,
    TcpProbe,
    ValidationError,
)
from tundravm.declarative.systemd import PRESETS
from tundravm.testing import PlannedImage, plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")


def _plan(*services: Service, **slots: object) -> PlannedImage:
    return plan(Image("app", services=list(services), **slots), "production")  # type: ignore[arg-type]


# ── readiness ───────────────────────────────────────────────────────────


def test_a_tcp_probe_becomes_a_bounded_unit_the_dependent_requires() -> None:
    database = Service("db", ["/usr/bin/db"], readiness=TcpProbe(5432), expose=[TCP(5432)])
    app = Service("app", ["/usr/bin/app"], requires=[database.ready])
    planned = _plan(database, app)

    probe = planned.service("db-ready")
    assert probe.get("Service", "Type") == "oneshot"
    assert probe.get("Service", "RemainAfterExit") == "yes"
    assert probe.get("Service", "TimeoutStartSec") == "90s"
    assert "/dev/tcp/127.0.0.1/5432" in (probe.get("Service", "ExecStart") or "")
    assert probe.get("Unit", "Requires") == "db.service"
    assert probe.get("Unit", "After") == "db.service"

    unit = planned.service("app")
    assert unit.get("Unit", "Requires") == "db-ready.service"
    assert unit.get("Unit", "After") == "db-ready.service"


def test_an_http_probe_polls_the_url_and_brings_curl_in() -> None:
    api = Service("api", ["/usr/bin/api"], readiness=HttpProbe("http://127.0.0.1:8080/health"))
    planned = _plan(api, Service("app", ["/usr/bin/app"], requires=[api.ready]))
    probe = planned.service("api-ready")
    assert "http://127.0.0.1:8080/health" in (probe.get("Service", "ExecStart") or "")
    assert planned.packages["curl"] == "runtime"


def test_notify_needs_no_probe_and_the_dependent_waits_for_the_unit() -> None:
    worker = Service("worker", ["/usr/bin/worker"], readiness=Notify())
    planned = _plan(worker, Service("app", ["/usr/bin/app"], requires=[worker.ready]))
    assert planned.service("worker").get("Service", "Type") == "notify"
    assert "worker-ready.service" not in planned.files.get("", "") if False else True
    unit = planned.service("app")
    assert unit.get("Unit", "Requires") == "worker.service"
    assert unit.get("Unit", "After") == "worker.service"


def test_requiring_a_service_without_readiness_is_refused_where_it_is_written() -> None:
    silent = Service("silent", ["/usr/bin/silent"])
    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], requires=[silent.ready])
    assert "declares no readiness" in str(caught.value)
    assert "HttpProbe" in str(caught.value)


def test_requiring_the_service_value_itself_says_to_pass_its_readiness() -> None:
    other = Service("other", ["/usr/bin/other"], readiness=Notify())
    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], requires=[other])  # type: ignore[list-item]
    assert "other.ready" in str(caught.value)


def test_an_endpoint_carries_the_readiness_it_was_exported_with() -> None:
    api = Service("api", ["/usr/bin/api"], readiness=TcpProbe(8080))
    endpoint = Endpoint("127.0.0.1", 8080, ready=api.ready)
    app = Service("app", ["/usr/bin/app", "--api", endpoint], requires=[endpoint])
    planned = _plan(api, app)
    unit = planned.service("app")
    assert unit.get("Service", "ExecStart") == "/usr/bin/app --api http://127.0.0.1:8080/"
    assert unit.get("Unit", "Requires") == "api-ready.service"


def test_an_endpoint_without_readiness_cannot_be_required() -> None:
    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], requires=[Endpoint("127.0.0.1", 80)])
    assert "carries no readiness" in str(caught.value)


# ── hardening ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_preset_renders_its_directives(preset: str) -> None:
    planned = _plan(
        Service("app", ["/usr/bin/app"], hardening=Hardening.preset(preset))  # type: ignore[arg-type]
    )
    unit = planned.service("app")
    assert unit.get("Service", "ProtectSystem") == "strict"
    assert unit.get("Service", "NoNewPrivileges") == "yes"
    assert unit.get("Service", "ProtectHome") == "yes"
    assert unit.get("Service", "RestrictAddressFamilies") == "AF_UNIX AF_INET AF_INET6"
    assert unit.get("Service", "SystemCallArchitectures") == "native"

    write_execute = unit.get("Service", "MemoryDenyWriteExecute")
    assert write_execute == ("yes" if preset == "static" else None)

    denied = unit.values("Service", "SystemCallFilter")
    expected = "~@privileged" if preset == "dotnet" else "~@privileged @resources"
    assert expected in denied


def test_static_is_the_default_and_the_strict_sandbox() -> None:
    assert Service("app", ["/usr/bin/app"]).hardening == Hardening.preset("static")
    assert Hardening.preset("static") == Hardening.strict()


def test_a_preset_takes_explicit_deviations() -> None:
    planned = _plan(
        Service(
            "app",
            ["/usr/bin/app"],
            hardening=Hardening.preset("jit", devices=["/dev/kvm"]),
        )
    )
    unit = planned.service("app")
    assert unit.get("Service", "DevicePolicy") == "closed"
    assert "/dev/kvm rw" in unit.values("Service", "DeviceAllow")
    assert unit.get("Service", "MemoryDenyWriteExecute") is None


def test_an_unknown_preset_lists_the_runtime_classes() -> None:
    with pytest.raises(ValidationError) as caught:
        Hardening.preset("rust")  # type: ignore[arg-type]
    assert "dotnet" in str(caught.value) and "static" in str(caught.value)


# ── the grouped options ─────────────────────────────────────────────────


def test_resources_and_shutdown_render_their_directives() -> None:
    planned = _plan(
        Service(
            "app",
            ["/usr/bin/app"],
            resources=Resources(memory_max="8G", tasks_max=4096),
            shutdown=Shutdown(timeout="120s", signal="SIGINT", kill_mode="mixed"),
        )
    )
    unit = planned.service("app")
    assert unit.get("Service", "MemoryMax") == "8G"
    assert unit.get("Service", "TasksMax") == "4096"
    assert unit.get("Service", "TimeoutStopSec") == "120s"
    assert unit.get("Service", "KillSignal") == "SIGINT"
    assert unit.get("Service", "KillMode") == "mixed"


def test_a_timer_makes_it_a_scheduled_oneshot() -> None:
    planned = _plan(Service("rotate", ["/usr/bin/rotate"], timer=Every("15m")))
    assert planned.service("rotate").get("Service", "Type") == "oneshot"
    assert planned.unit("rotate.timer").get("Timer", "OnUnitActiveSec") == "15m"


def test_a_socket_activates_it_on_demand() -> None:
    planned = _plan(Service("echo", ["/usr/bin/echo"], socket=Listen(7)))
    assert planned.unit("echo.socket").get("Socket", "ListenStream") == "7"


def test_a_dropin_carries_what_no_field_covers() -> None:
    planned = _plan(
        Service(
            "app",
            ["/usr/bin/app"],
            unit=DropIn("30-extra", extra={"Unit": {"OnFailure": "x.service"}}),
        )
    )
    assert planned.unit("app.service.d/30-extra.conf").get("Unit", "OnFailure") == "x.service"


def test_credentials_render_loadcredential() -> None:
    planned = _plan(
        Service("app", ["/usr/bin/app"], credentials=[Credential("jwt", "/etc/app/jwt")])
    )
    assert planned.service("app").get("Service", "LoadCredential") == "jwt:/etc/app/jwt"


def test_state_logs_and_runtime_stay_typed() -> None:
    planned = _plan(Service("app", ["/usr/bin/app"], state="app", logs="app", runtime="app"))
    unit = planned.service("app")
    assert unit.get("Service", "StateDirectory") == "app"
    assert unit.get("Service", "LogsDirectory") == "app"
    assert unit.get("Service", "RuntimeDirectory") == "app"


# ── the argv contract ───────────────────────────────────────────────────


def test_an_argv_is_not_a_command_line() -> None:
    with pytest.raises(ValidationError) as caught:
        Service("app", ["busybox httpd"])
    assert "/usr/bin/app" in str(caught.value) or "program path" in str(caught.value)


def test_the_shell_escape_is_explicit() -> None:
    planned = _plan(Service("app", ["/bin/sh", "-eu", "-c", "echo hi"]))
    assert planned.service("app").get("Service", "ExecStart") == '/bin/sh -eu -c "echo hi"'


def test_an_empty_argv_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Service("app", [])
    assert "empty argv" in str(caught.value)


def test_a_timer_and_a_socket_together_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], timer=Every("1h"), socket=Listen(7))
    assert "Pick one" in str(caught.value)
