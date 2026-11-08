"""``tundravm.components``: the baseline an image implies, replaced by passing one, and
components whose exports wire into each other."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest

from tundravm import Image, Kernel, Package, Profile, Service
from tundravm._image import lower, recipe_of
from tundravm.components import Component, Endpoint
from tundravm.components._observability import PROXY_HOSTS, PROXY_HOSTS_SCRIPT
from tundravm.components.access import DevAccess, SshAccess
from tundravm.components.network import Egress, Firewall, Network
from tundravm.components.observability import (
    JournalExport,
    Observability,
    PrometheusRemoteWrite,
    Proxy,
    RecordingRule,
    SigV4,
)
from tundravm.components.tee import EVIDENCE, Tdxs, TeeBase
from tundravm.errors import ValidationError
from tundravm.ports import TCP
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
    repeated = Image("x", kernel=Kernel.default(cmdline=["spec_store_bypass_disable=on"]))
    assert "kernel-cmdline-repeated" in {d.code for d in plan(repeated).lint()}
    assert "kernel-cmdline-repeated" not in {d.code for d in plan(Image("x")).lint()}


PCS = "api.trustedservices.intel.com:443"


@pytest.mark.parametrize(
    ("component", "sends"),
    [
        (TeeBase(), Egress(hosts=["time.cloudflare.com:4460", "nts.netnod.se:4460"])),
        (TeeBase(time=False, dns=False), Egress()),
        (Network(), Egress(hosts=["1.1.1.1:853", "1.0.0.1:853"])),
        (Network(dns=["2620:fe::fe#dns.quad9.net"]), Egress(hosts=["[2620:fe::fe]:853"])),
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
        (lambda: Network(dns=["9.9.9.9"]), "names no TLS server name"),
        (lambda: Network(dns=["dns.quad9.net"]), "is not ADDRESS#TLS-NAME"),
        (lambda: Network(dns=[]), "names no resolver"),
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
    from tundravm.components.network import Egress

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


LOGS, METRICS = "https://logs.example", "https://metrics.example/api/v1/write"
GUEST_LOGS, GUEST_METRICS = "http://127.0.0.1:19532", "http://127.0.0.1:9091/api/v1/write"


@pytest.mark.parametrize(
    ("logs", "metrics", "profile", "receivers"),
    [
        (LOGS, METRICS, "production", (LOGS, METRICS)),
        (LOGS, METRICS, "dev", (GUEST_LOGS, GUEST_METRICS)),
        (LOGS, METRICS, "lab", (GUEST_LOGS, GUEST_METRICS)),
        (LOGS, METRICS, "release", (LOGS, METRICS)),
        ("http://127.0.0.2:19580", METRICS, "dev", ("http://127.0.0.2:19580", GUEST_METRICS)),
    ],
)
def test_a_development_profile_ships_to_the_in_guest_receivers(
    logs: str, metrics: str, profile: str, receivers: tuple[str, str]
) -> None:
    """A production profile writes to each url= with its credential. A development profile
    writes to the in-guest receiver instead, binds no credential and opens no egress for
    it; a receiver on the loopback stays, since nothing leaves the VM for it."""
    observability = Observability(
        logs=JournalExport(logs, credential=Secret("logs-client")),
        metrics=PrometheusRemoteWrite(metrics, credential=Secret("metrics-token"), export=[]),
    )
    image = Image(
        "x",
        observability=observability,
        network=Network(egress=Egress()),
        profiles={"lab": Profile(production=False), "release": Profile(extends="lab")},
        dev=DevAccess(password="tdx"),
    )
    planned = plan(image, profile)
    upload = planned.service("journal-export").exec_start or ""
    config = planned.file("/etc/tundravm/metrics-export.yml").text.replace('"', "")
    assert f"--url={receivers[0]} " in upload and f"url: {receivers[1]}\n" in config
    for exporter, receiver in zip(("journal-export", "metrics-export"), receivers, strict=True):
        bound = planned.service(exporter).values("Service", "LoadCredential")
        assert bool(bound) == (receiver not in (GUEST_LOGS, GUEST_METRICS)), exporter
    nftables = planned.file("/etc/nftables.conf").text
    assert ("metrics.example" in nftables) == (receivers[1] == METRICS)


AMP = "https://aps-workspaces.us-east-2.amazonaws.com/workspaces/ws-1/api/v1/remote_write"


def test_a_passthrough_maps_each_sink_name_to_its_address_and_sigv4_signs_the_write() -> None:
    """The exporters reach the sinks' own URLs, whose names resolve to the passthrough in
    /etc/hosts, with no proxy setting; Prometheus signs with the delivered AWS key file."""
    observability = Observability(
        logs=JournalExport("https://logs.example"),
        metrics=PrometheusRemoteWrite(
            AMP, credential=SigV4("us-east-2", Secret("metrics-aws")), export=[]
        ),
        proxy=Proxy("10.88.0.100", 443, passthrough=True),
    )
    planned = plan(Image("x", observability=observability), "production")
    assert planned.service("journal-export").values("Service", "Environment") == ()
    config = planned.file("/etc/tundravm/metrics-export.yml").text
    assert "proxy_url" not in config and "region: us-east-2" in config
    environment = planned.service("metrics-export").values("Service", "Environment")
    assert (
        "AWS_SHARED_CREDENTIALS_FILE=/run/credentials/metrics-export.service/metrics-aws"
        in environment
    )
    assert planned.file(PROXY_HOSTS).text == (
        "10.88.0.100 logs.example\n10.88.0.100 aps-workspaces.us-east-2.amazonaws.com\n"
    )
    assert observability.outbound().hosts == ("10.88.0.100:443",)


PASSTHROUGH = Proxy("10.0.0.9", 443, passthrough=True)


@pytest.mark.parametrize(
    ("build", "refusal"),
    [
        (lambda: Proxy("10.0.0.9", 443, passthrough=True, tls_name="p.example"), "no tls_name"),
        (lambda: SigV4("mars-1", Secret("k")), "not an AWS region"),
        (lambda: Observability(logs=JournalExport("http://l.example"), proxy=PASSTHROUGH), "443"),
        (lambda: Observability(logs=JournalExport("https://10.0.0.8"), proxy=PASSTHROUGH), "443"),
        (
            lambda: Observability(logs=JournalExport("https://l.example:9"), proxy=PASSTHROUGH),
            "443",
        ),
    ],
)
def test_a_passthrough_carries_only_named_https_sinks_on_its_port(
    build: Callable[[], object], refusal: str
) -> None:
    with pytest.raises(ValidationError, match=refusal):
        build()


IDENTITY = "|tundravm_image_digest|tundravm_image_rtmr|tundravm_mode"
HEALTH = (
    "up|node_boot_time_seconds|node_filesystem_avail_bytes|node_filesystem_size_bytes"
    "|node_memory_MemAvailable_bytes|node_memory_MemTotal_bytes|node_vmstat_oom_kill"
    "|tundravm_system_state|tundravm_units_failed"
)


UP = RecordingRule("node:up", "min(up)")
MIXED = [RecordingRule("local:ratio", "1", export=False), UP]
URL = "https://metrics.example"


@pytest.mark.parametrize(
    ("rules", "export", "url", "profile", "kept", "severity"),
    [
        ((), "health", URL, "production", HEALTH + IDENTITY, None),
        ((), ["node_load1", "up"], URL, "production", "node_load1|up" + IDENTITY, None),
        ((), ["health", "up", "node_load1"], None, "dev", f"{HEALTH}|node_load1{IDENTITY}", None),
        ((), [], URL, "production", IDENTITY[1:], None),
        ((), None, URL, "production", HEALTH + IDENTITY, "error"),
        ((), None, None, "production", HEALTH + IDENTITY, "warning"),
        ((), None, None, "dev", HEALTH + IDENTITY, "warning"),
        (MIXED, None, URL, "production", "node:up" + IDENTITY, None),
        (MIXED, ["health", "up"], URL, "production", f"node:up|{HEALTH}{IDENTITY}", None),
        (MIXED, ["node:up"], URL, "production", "rules say export= themselves", None),
        (MIXED, ["local:ratio"], URL, "production", "rules say export= themselves", None),
        ((), ["node:up"], URL, "production", "no rule records", None),
        ((), ["health", "node load"], URL, "production", "neither a preset", None),
    ],
)
def test_the_exported_rules_and_the_keep_list_leave(
    rules: list[RecordingRule],
    export: str | list[str] | None,
    url: str | None,
    profile: str,
    kept: str,
    severity: str | None,
) -> None:
    """The agent keeps the rules that export, then the named series and the presets', then
    the identity, and drops the rest before the write. A rule's record in export= is
    refused. No export= and no rules keeps health, an error only where a production profile
    writes out."""
    if "|" not in kept:
        with pytest.raises(ValidationError, match=kept):
            PrometheusRemoteWrite(url, rules=rules, export=export)
        return
    metrics = PrometheusRemoteWrite(url, rules=rules, export=export)
    relabel = metrics.config()["remote_write"][0]["write_relabel_configs"]  # type: ignore[index]
    assert relabel == [{"source_labels": ["__name__"], "regex": kept, "action": "keep"}]
    image = Image("x", observability=Observability(metrics=metrics), dev=DevAccess(password="tdx"))
    found = [d for d in plan(image, profile).lint() if d.code == "observability-export-unset"]
    assert [d.severity for d in found] == ([] if severity is None else [severity])


@pytest.mark.parametrize(
    ("build", "refusal"),
    [
        (lambda: RecordingRule("node_up", "min(up)"), "not a series name with a colon"),
        (lambda: RecordingRule("node:up", " "), "not a PromQL expression"),
        (lambda: PrometheusRemoteWrite(rules=UP), "not a list of RecordingRule"),  # type: ignore[arg-type]
        (lambda: PrometheusRemoteWrite(rules=["node:up"]), "is not a RecordingRule"),  # type: ignore[list-item]
        (lambda: PrometheusRemoteWrite(rules=[UP, UP]), "two rules record 'node:up'"),
        (lambda: RecordingRule("node:up", "min(up)", export="yes"), "not True or False"),  # type: ignore[arg-type]
    ],
)
def test_a_rule_records_a_colon_name_once(build: Callable[[], object], refusal: str) -> None:
    with pytest.raises(ValidationError, match=refusal):
        build()


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
