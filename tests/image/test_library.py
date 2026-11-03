"""``tundravm.components``: the baseline an image implies, replaced by passing one, and
components whose exports wire into each other."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tundravm import Image, Package, Service
from tundravm._image import lower, recipe_of
from tundravm.components import (
    EVIDENCE,
    Component,
    DevAccess,
    Egress,
    Endpoint,
    Firewall,
    JournalExport,
    Network,
    Observability,
    PrometheusRemoteWrite,
    Proxy,
    SshAccess,
    Tdxs,
    TeeBase,
)
from tundravm.components._observability import PROXY_HOSTS, PROXY_HOSTS_SCRIPT
from tundravm.errors import ValidationError
from tundravm.ports import TCP, UDP
from tundravm.secrets import Secret
from tundravm.sources import Git
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import TcpProbe
from tundravm.testing import PlannedImage, plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

MIRROR = Git("https://mirror.example/tools.git", "v1.2.0")

data = Volume("data", mount="/persistent", unlock=OperatorKey())
token = Secret("db-token")


class Db(Component, build_network=True):
    """A database on the loopback, its state on the volume, ready once its port answers."""

    port: int = 5432

    @property
    def endpoint(self) -> Endpoint:
        return self.service("db").endpoint(self.port)

    def compose(self) -> Iterator[object]:
        yield Package("busybox")
        yield Service(
            "db",
            ["/usr/bin/busybox", "nc", "-lk", "-p", str(self.port)],
            readiness=TcpProbe(self.port),
            state=data.directory("db"),
        )


class Client(Component):
    """A client of *db* that reads *token*."""

    db: Endpoint
    token: Secret

    def compose(self) -> Iterator[object]:
        yield Service("client", ["/usr/bin/busybox", "nc", self.db, self.token], requires=[self.db])


db = Db()
client = Client(db=db.endpoint, token=token)


def stack(**changes: object) -> Image:
    fields: dict[str, object] = {"components": [db, client]}
    fields.update(changes)
    return Image("stack", **fields)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def planned() -> PlannedImage:
    return plan(stack(), "production")


def _after(planned: PlannedImage, unit: str) -> list[str]:
    return " ".join(planned.unit(unit).values("Unit", "After")).split()


# ── Exports across components ───────────────────────────────────────────


def test_endpoints_carry_their_readiness_across_components(planned: PlannedImage) -> None:
    assert db.endpoint.url == "http://127.0.0.1:5432/"
    assert db.endpoint.ready == db.service("db").ready
    # The client waits for what proves the database ready (its probe), not for its exec.
    assert "db-ready.service" in _after(planned, "client.service")
    assert planned.unit("db-ready.service").get("Service", "Type") == "oneshot"
    assert planned.service("client").values("Service", "LoadCredential") == (
        "db-token:/run/tundravm/credentials/db-token",
    )
    assert planned.service("db").get("Service", "WorkingDirectory") == "/persistent/db"
    # A class that says build_network=True builds with the network on: the tree says so.
    assert recipe_of(stack()).mkosi.build_network


def test_component_readiness_names_what_proves_it() -> None:
    assert Tdxs().readiness() == (EVIDENCE,)
    assert db.readiness() == (db.service("db").ready,)
    assert Network().readiness() == (Network().online,)
    service = Service("x", ["/usr/bin/x"], requires=[Tdxs().ready])
    planned = plan(Image("x", services=[service]), "production")
    assert "tdxs.service" in _after(planned, "x.service")


def test_component_checks_report_what_the_contract_warns_about() -> None:
    clockless = Image("x", components=[TeeBase(time=False)])
    assert "tee-base-clock" in {d.code for d in plan(clockless).lint()}
    unchecked = Image("x", components=[Tdxs(validator="tdx")])
    assert "tdxs-validator-unchecked" in {d.code for d in plan(unchecked).lint()}


PCS = "api.trustedservices.intel.com:443"


@pytest.mark.parametrize(
    ("component", "sends"),
    [
        (TeeBase(nameservers=["9.9.9.9"]), Egress([TCP(4460), UDP(123)], ["9.9.9.9"])),
        (TeeBase(time=False, dns=False), Egress()),
        (Tdxs(), Egress()),
        (Tdxs(get_collateral=True), Egress(hosts=[PCS])),
        (
            Tdxs(check_revocations=True),
            Egress(hosts=[PCS, "certificates.trustedservices.intel.com:443"]),
        ),
        (
            Observability(
                logs=JournalExport("https://logs.example:19532"),
                metrics=PrometheusRemoteWrite("https://[fd00::9]/api/v1/write", export="health"),
            ),
            Egress(hosts=["logs.example:19532", "[fd00::9]:443"]),
        ),
        (
            Observability(logs=JournalExport("https://logs.example"), proxy="10.0.0.9:3128"),
            Egress(hosts=["10.0.0.9:3128"]),
        ),
        (SshAccess(from_delivery=True), Egress()),
        (DevAccess(password="tdx"), Egress()),
    ],
)
def test_each_shipped_component_says_where_it_sends(component: Component, sends: Egress) -> None:
    assert component.outbound() == sends


@pytest.mark.parametrize(
    ("network", "profile", "reported"),
    [
        (None, "production", True),
        (Network(egress=Egress(hosts=["10.0.0.0/8", "0.0.0.0/0"])), "production", True),
        (Network(egress=Egress(ports=[TCP(443)])), "production", False),
        (None, "dev", False),
    ],
)
def test_egress_unfiltered_reports_a_production_profile_that_sends_anywhere(
    network: Network | None, profile: str, reported: bool
) -> None:
    image = Image("x", network=network, dev=DevAccess(password="tdx"))
    found = {d.code for d in plan(image, profile).lint()}
    assert ("egress-unfiltered" in found) is reported


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            lambda: Observability(logs=JournalExport("https://l.example"), proxy="p.example:80"),
            "proxy 'p.example:80' is not IP:PORT",
        ),
        (lambda: Proxy("p.example", 3128), "is not an IP address"),
        (lambda: Proxy("10.0.0.9", 3128, tls_name="10.0.0.9"), "is an address"),
        (lambda: Image("x", components=[DevAccess(password="p")]), "dev=DevAccess"),
    ],
)
def test_component_configuration_is_checked(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make()  # type: ignore[operator]


# ── The baseline, replaced by passing one ───────────────────────────────


def test_an_explicit_tdxs_builds_the_evidence_and_the_volume_tools_from_its_source() -> None:
    image = stack(components=[db, client, Tdxs(source=MIRROR)])
    builds = lower(image).source_builds()
    for name in ("tdxs", "key-generation", "disk-encryption", "attested-endpoint"):
        assert builds[name].source.url == MIRROR.url, name
    assert len([c for c in image.components if isinstance(c, Tdxs)]) == 1
    with pytest.raises(ValidationError, match="2 Tdxs components"):
        recipe_of(stack(components=[Tdxs(), Tdxs(source=MIRROR)]))


def test_an_explicit_firewall_and_network_replace_the_defaults() -> None:
    from tundravm.components import Egress

    image = Image(
        "x",
        components=[Firewall(allow=[TCP(9100)]), TeeBase(journal_max_use="64M")],
        network=Network(egress=Egress(ports=[TCP(443)])),
    )
    rules = plan(image, "production").file("/etc/nftables.conf").text
    assert "tcp dport 9100 accept" in rules
    assert "hook output priority filter; policy drop" in rules  # the network's egress
    journal = plan(image, "production").file("/etc/systemd/journald.conf.d/50-tundravm-tee.conf")
    assert "RuntimeMaxUse = 64M" in journal.text
    with pytest.raises(ValidationError, match="network= and components= both"):
        recipe_of(Image("x", network=Network(), components=[Network()]))


def test_ssh_access_lets_in_only_keys() -> None:
    with pytest.raises(ValidationError, match="no key to accept"):
        SshAccess()
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
    image = Image("x", components=[SshAccess(keys=[key])], targets=("gcp",))
    found = {d.code for d in plan(image, "production").lint()}
    assert "ssh-keys-baked" in found


def test_dev_access_joins_the_control_plane_sshd_instead_of_a_second_one() -> None:
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
    image = stack(dev=DevAccess(ssh_keys=[key]))
    dev = plan(image, "dev")
    assert key in dev.file("/etc/ssh/authorized_keys.d/root").text
    assert "/etc/ssh/authorized_keys.d/root" not in plan(image, "production").files
    with pytest.raises(ValidationError, match="does not exist"):
        DevAccess(ssh_keys=["missing.pub"]).public_keys()


# ── Observability ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("proxy", "url", "allowed", "hosts"),
    [
        ("[fd00::9]:3128", "http://[fd00::9]:3128", "[fd00::9]:3128", None),
        (Proxy("10.0.0.9", 3128), "http://10.0.0.9:3128", "10.0.0.9:3128", None),
        (
            Proxy("10.0.0.9", 3128, tls_name="p.example"),
            "https://p.example:3128",
            "10.0.0.9:3128",
            "10.0.0.9 p.example\n",
        ),
        (
            Proxy("fd00::9", 3128, tls_name="p.example"),
            "https://p.example:3128",
            "[fd00::9]:3128",
            "fd00::9 p.example\n",
        ),
    ],
)
def test_the_exporters_reach_their_sinks_through_the_proxy(
    proxy: str | Proxy, url: str, allowed: str, hosts: str | None
) -> None:
    observability = Observability(
        logs=JournalExport("https://logs.example"),
        metrics=PrometheusRemoteWrite("https://metrics.example", export="health"),
        proxy=proxy,
    )
    planned = plan(Image("x", observability=observability), "production")
    environment = planned.service("journal-export").values("Service", "Environment")
    assert f"https_proxy={url}" in environment
    config = planned.file("/etc/tundravm/metrics-export.yml").text.replace('"', "")
    assert f"proxy_url: {url}" in config
    assert observability.outbound().hosts == (allowed,)
    step = f"+/bin/sh {PROXY_HOSTS_SCRIPT}"
    for unit in ("journal-export", "metrics-export"):
        dropin = planned.files.get(f"/usr/lib/systemd/system/{unit}.service.d/20-tundravm.conf")
        assert (step in (dropin.text if dropin else "")) == (hosts is not None), unit
    found = planned.files.get(PROXY_HOSTS)
    assert (found.text if found else None) == hosts


@pytest.mark.parametrize(
    ("observability", "exporter", "receiver", "url", "listen", "profile", "reported"),
    [
        (
            Observability(logs=JournalExport(credential=Secret("logs-client"))),
            "journal-export",
            "journal-sink",
            "--url=http://127.0.0.1:19532 ",
            "--listen-http=127.0.0.1:19532",
            "production",
            True,
        ),
        (
            Observability(logs=JournalExport(credential=Secret("logs-client"))),
            "journal-export",
            "journal-sink",
            "--url=http://127.0.0.1:19532 ",
            "--listen-http=127.0.0.1:19532",
            "dev",
            False,
        ),
        (
            Observability(metrics=PrometheusRemoteWrite(credential=Secret("metrics-token"))),
            "metrics-export",
            "metrics-sink",
            "url: http://127.0.0.1:9091/api/v1/write",
            "--web.listen-address=127.0.0.1:9091",
            "production",
            True,
        ),
    ],
)
def test_a_sink_without_a_url_ships_to_the_in_guest_receiver(
    observability: Observability,
    exporter: str,
    receiver: str,
    url: str,
    listen: str,
    profile: str,
    reported: bool,
) -> None:
    """The exporter reaches its receiver on the loopback once it is ready, binds no
    credential and opens no egress; only a production profile reports the unset sink."""
    image = Image("x", observability=observability, dev=DevAccess(password="tdx"))
    planned = plan(image, profile)
    rendered = planned.service(exporter).exec_start or ""
    if exporter == "metrics-export":
        rendered = planned.file("/etc/tundravm/metrics-export.yml").text.replace('"', "")
    assert url in rendered
    assert f"{receiver}-ready.service" in _after(planned, f"{exporter}.service")
    assert listen in (planned.service(receiver).exec_start or "")
    assert image.secrets == () and observability.outbound().hosts == ()
    codes = {d.code for d in planned.lint()}
    assert ("observability-sink-unset" in codes) == reported
    assert "observability-plaintext" not in codes


IDENTITY = "|tundravm_image_digest|tundravm_image_rtmr|tundravm_mode"
HEALTH = (
    "up|node_boot_time_seconds|node_filesystem_avail_bytes|node_filesystem_size_bytes"
    "|node_memory_MemAvailable_bytes|node_memory_MemTotal_bytes|node_vmstat_oom_kill"
    "|tundravm_system_state|tundravm_units_failed"
)


@pytest.mark.parametrize(
    ("export", "url", "profile", "regex", "severity"),
    [
        ("health", "https://metrics.example", "production", HEALTH + IDENTITY, None),
        (
            ["node_load1", "up"],
            "https://metrics.example",
            "production",
            "node_load1|up" + IDENTITY,
            None,
        ),
        (["health", "up", "node_load1"], None, "dev", f"{HEALTH}|node_load1{IDENTITY}", None),
        ([], "https://metrics.example", "production", IDENTITY[1:], None),
        (None, "https://metrics.example", "production", HEALTH + IDENTITY, "error"),
        (None, None, "production", HEALTH + IDENTITY, "warning"),
        (None, None, "dev", HEALTH + IDENTITY, "warning"),
    ],
)
def test_only_the_keep_list_leaves(
    export: str | list[str] | None,
    url: str | None,
    profile: str,
    regex: str,
    severity: str | None,
) -> None:
    """The agent keeps the named series and the presets' and drops the rest before the
    write; no export= keeps health and is an error only where a production profile writes
    out."""
    metrics = PrometheusRemoteWrite(url, export=export)
    relabel = metrics.config()["remote_write"][0]["write_relabel_configs"]  # type: ignore[index]
    assert relabel == [{"source_labels": ["__name__"], "regex": regex, "action": "keep"}]
    image = Image("x", observability=Observability(metrics=metrics), dev=DevAccess(password="tdx"))
    found = [d for d in plan(image, profile).lint() if d.code == "observability-export-unset"]
    assert [d.severity for d in found] == ([] if severity is None else [severity])
    with pytest.raises(ValidationError, match="neither a preset"):
        PrometheusRemoteWrite(export=["health", "node load"])


def test_observability_exports_logs_and_metrics_and_blocks_nothing() -> None:
    logs_key = Secret("logs-key")
    token = Secret("metrics-token")
    observability = Observability(
        logs=JournalExport("https://logs.example:19532", credential=logs_key),
        metrics=PrometheusRemoteWrite(
            "https://metrics.example/api/v1/write", credential=token, export="health"
        ),
    )
    image = Image("x", observability=observability)
    planned = plan(image, "production")
    upload = planned.service("journal-export")
    assert "--url=https://logs.example:19532" in (upload.exec_start or "")
    assert upload.values("Service", "SupplementaryGroups") == ("systemd-journal",)
    assert "credential-logs-key.service" in _after(planned, "journal-export.service")
    agent = planned.service("metrics-export")
    assert "--enable-feature=agent" in (agent.exec_start or "")
    config_text = planned.file("/etc/tundravm/metrics-export.yml").text
    assert "/run/credentials/metrics-export.service/metrics-token" in config_text
    assert "127.0.0.1:9100" in planned.file("/etc/default/prometheus-node-exporter").text
    assert {"systemd-journal-remote", "prometheus", "prometheus-node-exporter"} <= set(
        planned.packages
    )
    # Nothing waits for the exporters: an unreachable sink never holds an application back.
    units = [path for path in planned.files if path.startswith("/usr/lib/systemd/system/")]
    for unit in ("journal-export.service", "metrics-export.service"):
        dependents = [path for path in units if unit in planned.file(path).text]
        assert dependents == [f"/usr/lib/systemd/system/{unit}"], unit
    assert "max_shards: 4" in config_text
    plaintext = Image("x", observability=Observability(logs=JournalExport("http://logs:19532")))
    assert "observability-plaintext" in {d.code for d in plan(plaintext).lint()}
    with pytest.raises(ValidationError, match="exports nothing"):
        Observability()
