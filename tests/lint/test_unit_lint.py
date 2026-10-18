"""The unit lint: every unit a variant ships (raw ``Unit`` text, a ``File`` under a systemd
directory, a generated ``Service``) is parsed and checked (docs/design/systemd-units.md 4.2)."""

from __future__ import annotations

import pytest

from tests.lint.test_check_rules import recipe, report
from tundravm import File, Mkosi, Package, Recipe, Service, Unit, Variant
from tundravm._units.lint import Facts, ShippedUnit, unit_findings
from tundravm.check import Diagnostic

INSTALL = "\n[Install]\nWantedBy=minimal.target\n"


def service(body: str, unit: str = "[Unit]\nDescription=x\n") -> str:
    return f"{unit}\n[Service]\n{body}\n{INSTALL}"


def found(*items: object, code: str) -> list[Diagnostic]:
    return [d for d in report(recipe(*items)) if d.code == code]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "code", "severity"),
    [
        (
            "[Unit]\nDescription=x\n[Services]\nExecStart=/usr/bin/x\n",
            "unit-section-invalid",
            "error",
        ),
        (service("ExecStart=/usr/bin/x\nRestrat=always"), "unit-directive-unknown", "warning"),
        (service("ExecStart=/usr/bin/x\nRestart=onfailure"), "unit-value-invalid", "error"),
        (service("Type=daemon\nExecStart=/usr/bin/x"), "unit-value-invalid", "error"),
        (service("ExecStart=/usr/bin/x\nTimeoutStartSec=soon"), "unit-value-invalid", "error"),
        (service("ExecStart=ip link set lo up"), "unit-exec-relative", "warning"),
        (service("ExecStart=/usr/bin/x\nUser=ghost"), "service-user-missing", "error"),
        (service("ExecStart=/usr/bin/x\nGroup=ghosts"), "service-user-missing", "error"),
        ("[Service]\nExecStart=/usr/bin/x\n", "unit-enable-noop", "warning"),
        (
            service("ExecStart=/usr/bin/x", "[Unit]\nRequires=b.service\nConflicts=b.service\n"),
            "relation-conflict",
            "error",
        ),
        (
            service("ExecStart=/usr/bin/x", "[Unit]\nAfter=x.service\n"),
            "relation-conflict",
            "error",
        ),
        (
            service("ExecStart=/usr/bin/x", "[Unit]\nRequires=b.service\n"),
            "requires-without-after",
            "info",
        ),
        (service("ExecStart=/usr/bin/x"), "unit-raw", "info"),
    ],
)
def test_each_code_fires_on_raw_unit_text(text: str, code: str, severity: str) -> None:
    [diag] = found(Unit("x.service", text, enabled=True), code=code)
    assert (diag.severity, diag.subject) == (severity, "x.service")


def test_clean_unit_text_has_no_unit_findings() -> None:
    text = service("Type=oneshot\nExecStart=/usr/bin/x\nRemainAfterExit=yes\nUser=app")
    assert [d.code for d in report(recipe(Unit("x.service", text, enabled=True)))] == ["unit-raw"]


def test_drop_ins_are_checked_against_their_units_sections() -> None:
    dropin = File("/usr/lib/systemd/system/x.service.d/10.conf", "[Service]\nRestart=sometimes\n")
    [diag] = found(dropin, code="unit-value-invalid")
    assert diag.subject == "/usr/lib/systemd/system/x.service.d/10.conf"


def test_a_unit_shipped_as_a_file_is_linted_and_reported() -> None:
    text = service("ExecStart=/usr/bin/x\nAccept=yes")
    items = (File("/usr/lib/systemd/system/y.service", text),)
    assert {d.code for d in report(recipe(*items))} == {"unit-as-file", "unit-directive-unknown"}


def test_socket_without_service_and_eager_socket_service() -> None:
    socket = (
        "[Socket]\nListenStream=/run/a.sock\nAccept=false\n\n[Install]\nWantedBy=sockets.target\n"
    )
    [diag] = found(Unit("a.socket", socket, enabled=True), code="socket-service-unpaired")
    assert diag.severity == "warning"  # the variant installs packages that could ship it
    pair = (
        Unit("a.socket", socket, enabled=True),
        Unit("a.service", service("ExecStart=/usr/bin/a"), enabled=True),
    )
    assert [d.code for d in found(*pair, code="socket-service-started")] == [
        "socket-service-started"
    ]
    assert found(*pair, code="socket-service-unpaired") == []


def test_timer_without_service_and_timer_service_enabled_at_boot() -> None:
    timer = "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n"
    [diag] = found(Unit("t.timer", timer, enabled=True), code="timer-unit-missing")
    assert diag.severity == "warning"
    both = (Unit("t.timer", timer, enabled=True), Service("t", "/usr/bin/t", type="oneshot"))
    [diag] = found(*both, code="timer-service-enabled")
    assert (diag.severity, diag.subject) == ("warning", "t")


def test_generated_services_report_state_directories_made_by_hand() -> None:
    pre = "+/usr/bin/install -d -o app -g app -m 0750 /var/lib/app2"
    [diag] = found(Service("app2", "/usr/bin/app", exec_start_pre=(pre,)), code="state-dir-mkdir")
    assert diag.severity == "info"
    assert (
        found(
            Service("app3", "/usr/bin/app", exec_start_pre=("+/usr/bin/install -d /persistent/a",)),
            code="state-dir-mkdir",
        )
        == []
    )


def test_unpaired_socket_is_an_error_when_no_package_could_ship_the_service() -> None:
    facts = Facts("v", frozenset(), lambda _: True, lambda _: True, False, True)
    socket = ShippedUnit(
        "a.socket", "/usr/lib/systemd/system/a.socket", "[Socket]\nListenStream=1\nAccept=yes\n"
    )
    [diag] = unit_findings([socket], facts)
    assert (diag.code, diag.severity) == ("socket-service-unpaired", "error")
    assert "a@.service" in diag.message


def test_a_package_named_after_the_account_provides_it() -> None:
    text = service("ExecStart=/usr/bin/prometheus-node-exporter\nUser=prometheus")
    items = (Unit("x.service", text, enabled=True),)
    assert found(*items, code="service-user-missing")
    assert found(*items, Package("prometheus-node-exporter"), code="service-user-missing") == []


def test_the_historical_dialect_keeps_correctness_codes_only() -> None:
    text = service("ExecStart=ip link\nRestart=onfailure", "[Unit]\nRequires=b.service\n")
    items = (*recipe().items, Unit("x.service", text, enabled=True))
    current = Recipe("c", items=items, variants=(Variant("default", outputs=("qemu",)),))
    historical = Recipe(
        "h", items=items, variants=current.variants, mkosi=Mkosi(dialect="nethermind-v1")
    )
    unit_codes = {"unit-exec-relative", "requires-without-after", "unit-raw", "unit-value-invalid"}
    assert {d.code for d in report(current)} & unit_codes == unit_codes
    assert {d.code for d in report(historical)} & unit_codes == {"unit-value-invalid"}


UNIT_CODES = frozenset(
    {
        "unit-section-invalid",
        "unit-directive-unknown",
        "unit-value-invalid",
        "unit-exec-relative",
        "service-user-missing",
        "socket-service-unpaired",
        "socket-service-started",
        "unit-enable-noop",
        "timer-unit-missing",
        "timer-service-enabled",
        "relation-conflict",
        "requires-without-after",
        "state-dir-mkdir",
        "unit-raw",
        "unit-as-file",
    }
)
EXPECTED_UNIT_FINDINGS: dict[str, set[str]] = {}
"""What the repository's own recipes report: nothing since ``Tdxs`` emits typed units (the
surge recipe keeps the historical texts, whose style codes are off under
``nethermind-v1``)."""


def test_repository_recipes_report_only_the_expected_unit_findings(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    from tests.compiler.test_lowering_parity import recipe_paths
    from tests.helpers import REPO_ROOT
    from tundravm.declarative import load
    from tundravm.declarative.lifecycle import check_report

    paths = recipe_paths(tmp_path_factory.mktemp("templates"))
    seen = {
        name: {
            d.code
            for d in check_report(load(path, extra_paths=[REPO_ROOT]), None, variants=None)
            if d.code in UNIT_CODES
        }
        for name, path in paths.items()
    }
    assert seen == {name: EXPECTED_UNIT_FINDINGS.get(name, set()) for name in paths}
