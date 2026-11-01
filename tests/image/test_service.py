"""``Service``: readiness chains, hardening presets, grouped options and the argv contract."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tundravm import Image, Service
from tundravm.components import EVIDENCE, Component, Endpoint
from tundravm.declarative import Root, Task
from tundravm.declarative.systemd import PRESETS
from tundravm.errors import ValidationError
from tundravm.ports import TCP
from tundravm.secrets import Credential
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import (
    DropIn,
    Every,
    Hardening,
    HttpProbe,
    Listen,
    Notify,
    Resources,
    Shutdown,
    TcpProbe,
)
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
    assert unit.get("Unit", "Requires") == "db-ready.service tdx-evidence.service"
    assert unit.get("Unit", "After") == "db-ready.service tdx-evidence.service"


def test_an_http_probe_polls_the_url_and_brings_curl_in() -> None:
    api = Service("api", ["/usr/bin/api"], readiness=HttpProbe("http://127.0.0.1:8080/health"))
    planned = _plan(api, Service("app", ["/usr/bin/app"], requires=[api.ready]))
    probe = planned.service("api-ready")
    assert "http://127.0.0.1:8080/health" in (probe.get("Service", "ExecStart") or "")
    assert planned.packages["curl"] == "runtime"


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("http://127.0.0.1:8080/health", None),
        ("http://[::1]/", None),
        ("127.0.0.1:8080/health", "is not an http:// or https:// URL"),
        ("http://", "names no host"),
        ("https://:8443/health", "names no host"),
        ("http://127.0.0.1/a b", "holds spaces or quotes"),
    ],
)
def test_an_http_probe_url_is_one_http_url_with_a_host(url: str, refused: str | None) -> None:
    if refused is None:
        assert url in HttpProbe(url).command()[-1]
        return
    with pytest.raises(ValidationError, match=refused) as caught:
        HttpProbe(url)
    assert "\nfix: " in str(caught.value)


@pytest.mark.parametrize(
    ("spans", "sleeps"),
    [
        ({}, "sleep 0.5;"),
        ({"interval": "2s", "timeout": "10min"}, "sleep 2;"),
        ({"interval": "1min 30s"}, "sleep 90;"),
        ({"interval": "250ms"}, "sleep 0.25;"),
        ({"interval": "0"}, "interval="),
        ({"interval": "0.5x"}, "interval="),
        ({"timeout": "potato"}, "timeout="),
        ({"timeout": "infinity"}, "timeout="),
    ],
)
def test_a_probe_polls_every_interval_until_its_timeout_both_systemd_spans(
    spans: dict[str, str], sleeps: str
) -> None:
    for probe in (lambda: TcpProbe(8080, **spans), lambda: HttpProbe("http://127.0.0.1", **spans)):
        try:
            loop = probe().command()[-1]
        except ValidationError as exc:
            assert sleeps in str(exc) and "\nfix: " in str(exc), exc
        else:
            assert sleeps in loop, loop


def test_notify_needs_no_probe_and_the_dependent_waits_for_the_unit() -> None:
    worker = Service("worker", ["/usr/bin/worker"], readiness=Notify())
    planned = _plan(worker, Service("app", ["/usr/bin/app"], requires=[worker.ready]))
    assert planned.service("worker").get("Service", "Type") == "notify"
    assert "worker-ready.service" not in planned.files.get("", "") if False else True
    unit = planned.service("app")
    assert unit.get("Unit", "Requires") == "worker.service tdx-evidence.service"
    assert unit.get("Unit", "After") == "worker.service tdx-evidence.service"


def test_a_setup_tasks_output_skips_its_consumer_when_the_setup_wrote_nothing() -> None:
    """The fail-safe idiom: Wants= and After= on the task, never Requires=, and the
    condition on what it writes, so a failed setup leaves the consumer skipped."""
    setup = Task("render", ("/usr/bin/render", "/run/app/config"), user=Root())

    class Rendered(Component):
        def compose(self) -> Iterator[object]:
            yield setup
            yield Service("app", ["/usr/bin/app"], requires=[setup.output("/run/app/config")])

    unit = plan(Image("app", components=[Rendered()]), "production").service("app")
    assert unit.get("Unit", "Wants") == "render.service"
    assert "render.service" in (unit.get("Unit", "After") or "").split()
    assert unit.get("Unit", "Requires") == "tdx-evidence.service"
    assert unit.get("Unit", "ConditionPathExists") == "/run/app/config"


def test_a_service_endpoint_is_its_loopback_address_with_its_readiness() -> None:
    api = Service("api", ["/usr/bin/api"], readiness=TcpProbe(8080))
    endpoint = api.endpoint(8080, path="/health")
    assert (endpoint.url, endpoint.ready) == ("http://127.0.0.1:8080/health", api.ready)
    assert Service("app", ["/usr/bin/app"], requires=[endpoint]).requires == (endpoint,)

    class Api(Component):
        def compose(self) -> Iterator[Service]:
            yield api

    assert Api().service("api").endpoint(8080, path="/health") == endpoint


@pytest.mark.parametrize(
    ("given", "spelled"),
    [
        ({}, {"requires": [EVIDENCE], "groups": ["tdx"]}),
        ({"groups": ["disk"]}, {"requires": [EVIDENCE], "groups": ["tdx", "disk"]}),
        ({"timer": Every("1h")}, {"requires": [EVIDENCE], "groups": ["tdx"], "timer": Every("1h")}),
    ],
    ids=["service", "with its own group", "timer"],
)
def test_quotes_is_the_evidence_requirement_and_the_tdx_group(
    given: dict[str, object], spelled: dict[str, object]
) -> None:
    quoting = _plan(Service("app", ["/usr/bin/app"], quotes=True, **given))  # type: ignore[arg-type]
    written = _plan(Service("app", ["/usr/bin/app"], **spelled))  # type: ignore[arg-type]
    assert quoting.service("app").text == written.service("app").text
    assert "Requires=tdxs.service" in quoting.service("app").text
    with pytest.raises(ValidationError, match="quotes= holds 'yes'"):
        Service("app", ["/usr/bin/app"], quotes="yes")  # type: ignore[arg-type]


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


def test_an_endpoint_without_readiness_cannot_be_required() -> None:
    with pytest.raises(ValidationError) as caught:
        Service("app", ["/usr/bin/app"], requires=[Endpoint("127.0.0.1", 80)])
    assert "carries no readiness" in str(caught.value)


# ── hardening ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_every_preset_renders_its_directives(preset: str) -> None:
    planned = _plan(
        Service("app", ["/usr/bin/app"], hardening=preset)  # type: ignore[arg-type]
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


KVM = Hardening.preset("jit", devices=["/dev/kvm"])


@pytest.mark.parametrize(
    ("hardening", "expected"),
    [
        ("dotnet", Hardening.preset("dotnet")),
        ("relaxed", Hardening.relaxed()),
        (KVM, KVM),
        ("rust", "Name a preset ('static', 'jit', 'node', 'python', 'dotnet', 'relaxed')"),
    ],
)
def test_hardening_takes_a_preset_by_name(hardening: object, expected: Hardening | str) -> None:
    if isinstance(expected, str):
        with pytest.raises(ValidationError) as caught:
            Service("app", ["/usr/bin/app"], hardening=hardening)  # type: ignore[arg-type]
        assert expected in str(caught.value)
        return
    assert Service("app", ["/usr/bin/app"], hardening=hardening).hardening == expected  # type: ignore[arg-type]


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


@pytest.mark.parametrize(
    ("fields", "section", "key", "value"),
    [
        ({"environment": {"A": "b"}}, "Service", "Environment", "A=b"),
        ({"resources": Resources(memory_max="1G")}, "Service", "MemoryMax", "1G"),
        ({"shutdown": Shutdown(timeout="30s")}, "Service", "TimeoutStopSec", "30s"),
        ({"shutdown": Shutdown(signal="SIGINT")}, "Service", "KillSignal", "SIGINT"),
        ({"groups": ["adm"]}, "Service", "SupplementaryGroups", "adm"),
        ({"working_directory": "/srv"}, "Service", "WorkingDirectory", "/srv"),
        ({"logs": "rotate"}, "Service", "LogsDirectory", "rotate"),
        ({"runtime": "rotate"}, "Service", "RuntimeDirectory", "rotate"),
        (
            {"unit": DropIn("30-x", extra={"Unit": {"OnFailure": "x.service"}})},
            "Unit",
            "OnFailure",
            "x.service",
        ),
    ],
)
def test_a_timer_keeps_every_keyword(
    fields: dict[str, object], section: str, key: str, value: str
) -> None:
    service = Service("rotate", ["/usr/bin/rotate"], timer=Every("15m"), **fields)  # type: ignore[arg-type]
    unit = "rotate.service.d/30-x.conf" if "unit" in fields else "rotate.service"
    assert _plan(service).unit(unit).get(section, key) == value


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


def test_the_shell_escape_is_explicit() -> None:
    planned = _plan(Service("app", ["/bin/sh", "-eu", "-c", "echo hi"]))
    assert planned.service("app").get("Service", "ExecStart") == '/bin/sh -eu -c "echo hi"'


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("socket", Listen(7)),
        ("readiness", TcpProbe(8080)),
        ("expose", [TCP(8080)]),
        ("credentials", [Credential("jwt", "/etc/app/jwt")]),
        ("state", Volume("data", mount="/data", unlock=OperatorKey()).directory("app")),
    ],
)
def test_a_timer_refuses_what_only_a_long_lived_service_has(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=f"runs on a timer, so {field}= does not apply"):
        Service("app", ["/usr/bin/app"], timer=Every("1h"), **{field: value})  # type: ignore[arg-type]
