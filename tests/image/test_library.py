"""``tundravm.components``: the baseline an image implies, replaced by passing one, and the
workloads whose exports wire into each other."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tundravm import Image, Service
from tundravm._image import lower, recipe_of
from tundravm.components import (
    EVIDENCE,
    Component,
    DevAccess,
    Egress,
    Endpoint,
    Firewall,
    JournalExport,
    Nethermind,
    Network,
    Observability,
    PrometheusRemoteWrite,
    Raiko,
    SshAccess,
    TaikoClient,
    Tdxs,
    TeeBase,
)
from tundravm.errors import ValidationError
from tundravm.ports import TCP, UDP
from tundravm.secrets import Secret
from tundravm.sources import Git
from tundravm.storage import OperatorKey, Volume
from tundravm.testing import PlannedImage, plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

MIRROR = Git("https://mirror.example/tundra-tools.git", "v1.2.0")

data = Volume("data", mount="/persistent", unlock=OperatorKey())
jwt = Secret("jwt")
chain = Secret("taiko-chain")
config = Secret("raiko-config")
execution = Nethermind(jwt=jwt, state=data.directory("nethermind"))
driver = TaikoClient(engine=execution.engine_api, rpc=execution.rpc, jwt=jwt, chain=chain)
prover = Raiko(config=config, l2_rpc=execution.rpc, state=data.directory("raiko"))


def stack(**changes: object) -> Image:
    fields: dict[str, object] = {
        "components": [execution, driver, prover],
        "volumes": [data],
        "secrets": [jwt, chain, config],
    }
    fields.update(changes)
    return Image("stack", **fields)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def planned() -> PlannedImage:
    return plan(stack(), "production")


def _after(planned: PlannedImage, unit: str) -> list[str]:
    return " ".join(planned.unit(unit).values("Unit", "After")).split()


# ── Exports across components ───────────────────────────────────────────


def test_endpoints_carry_their_readiness_across_components(planned: PlannedImage) -> None:
    assert execution.engine_api.url == "http://127.0.0.1:8551/"
    assert execution.ws.url == "ws://127.0.0.1:8545/"
    assert execution.rpc.ready == execution.service("nethermind").ready
    # The driver waits for what proves Nethermind ready (its probe), not for its exec.
    assert "nethermind-ready.service" in _after(planned, "taiko-client.service")
    assert planned.unit("nethermind-ready.service").get("Service", "Type") == "oneshot"
    # Raiko waits for the evidence service and the execution client it proves.
    raiko = _after(planned, "raiko.service")
    assert "tdxs.service" in raiko and "nethermind-ready.service" in raiko
    assert planned.service("raiko").values("Service", "SupplementaryGroups") == ("tdx",)


def test_component_readiness_names_what_proves_it() -> None:
    assert Tdxs().readiness() == (EVIDENCE,)
    assert prover.readiness() == (prover.service("raiko").ready,)
    assert Network().readiness() == (Network().online,)
    service = Service("x", ["/usr/bin/x"], requires=[Tdxs().ready])
    planned = plan(Image("x", services=[service]), "production")
    assert "tdxs.service" in _after(planned, "x.service")


def test_the_workloads_pin_their_sources_and_read_their_secrets(planned: PlannedImage) -> None:
    builds = lower(stack()).source_builds()
    assert {"nethermind", "taiko-client", "raiko"} <= set(builds)
    nethermind = planned.service("nethermind")
    assert "--JsonRpc.JwtSecretFile=${CREDENTIALS_DIRECTORY}/jwt" in (nethermind.exec_start or "")
    assert nethermind.get("Service", "WorkingDirectory") == "/persistent/nethermind"
    assert nethermind.get("Service", "MemoryDenyWriteExecute") is None  # the dotnet preset
    driver_unit = planned.service("taiko-client")
    assert driver_unit.values("Service", "LoadCredential") == (
        "jwt:/run/tundravm/credentials/jwt",
        "taiko-chain:/run/tundravm/credentials/taiko-chain",
    )
    assert "--l2.auth=http://127.0.0.1:8551/" in (driver_unit.exec_start or "")
    raiko = planned.service("raiko")
    assert raiko.values("Service", "DeviceAllow") == ("/dev/tdx_guest rw",)
    # Their builds fetch crates, modules and NuGet packages: the tree says so.
    assert recipe_of(stack()).mkosi.build_network


def test_component_checks_report_what_the_contract_warns_about() -> None:
    volatile = Image("x", components=[Nethermind(jwt=jwt)], secrets=[jwt])
    found = {d.code for d in plan(volatile, "production").lint()}
    assert "nethermind-volatile-state" in found
    public = Raiko(config=config, listen="0.0.0.0:8080")
    found = {d.code for d in plan(Image("x", components=[public], secrets=[config])).lint()}
    assert "raiko-listens-publicly" in found
    clockless = Image("x", components=[TeeBase(time=False)])
    assert "tee-base-clock" in {d.code for d in plan(clockless).lint()}
    unchecked = Image("x", components=[Tdxs(validator="tdx")])
    assert "tdxs-validator-unchecked" in {d.code for d in plan(unchecked).lint()}


PCS = "api.trustedservices.intel.com:443"
REMOTE = Endpoint("10.0.0.7", 8545, ready=execution.rpc.ready)
TAIKO: dict[str, Any] = {
    "engine": execution.engine_api,
    "rpc": execution.rpc,
    "jwt": jwt,
    "chain": chain,
}


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
                metrics=PrometheusRemoteWrite("https://[fd00::9]/api/v1/write"),
            ),
            Egress(hosts=["logs.example:19532", "[fd00::9]:443"]),
        ),
        (
            Observability(logs=JournalExport("https://logs.example"), proxy="10.0.0.9:3128"),
            Egress(hosts=["10.0.0.9:3128"]),
        ),
        (execution, Egress([TCP(30303), UDP(30303)])),
        (driver, Egress([TCP(443)])),
        (
            TaikoClient(engine=REMOTE, rpc=REMOTE, jwt=jwt, chain=chain),
            Egress([TCP(443)], ["10.0.0.7:8545"]),
        ),
        (
            TaikoClient(**TAIKO, l1=["l1.example:8546", "10.0.0.9:5052"]),
            Egress(hosts=["l1.example:8546", "10.0.0.9:5052"]),
        ),
        (prover, Egress(hosts=[PCS])),
        (Raiko(config=config, l2_rpc=REMOTE), Egress(hosts=[PCS, "10.0.0.7:8545"])),
        (Raiko(config=config, l1=["10.0.0.9:8545"]), Egress(hosts=[PCS, "10.0.0.9:8545"])),
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
        (lambda: Nethermind(jwt="jwt"), "Secret the image declares"),  # type: ignore[arg-type]
        (
            lambda: TaikoClient(
                engine=Endpoint("127.0.0.1", 1), rpc=execution.rpc, jwt=jwt, chain=chain
            ),
            "readiness",
        ),
        (lambda: Raiko(config=config, listen="8080"), "HOST:PORT"),
        (lambda: TaikoClient(**TAIKO, l1="l1.example:8546"), "a list of HOST:PORT"),
        (lambda: Raiko(config=config, l1=["l1.example"]), "'l1.example' is not HOST:PORT"),
        (
            lambda: Observability(logs=JournalExport("https://l.example"), proxy="p.example:80"),
            "proxy 'p.example:80' is not IP:PORT",
        ),
        (lambda: Image("x", components=[DevAccess(password="p")]), "dev=DevAccess"),
        (lambda: stack(secrets=[jwt, config]), "taiko-chain"),
    ],
)
def test_component_configuration_is_checked(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make()  # type: ignore[operator]


# ── The baseline, replaced by passing one ───────────────────────────────


def test_an_explicit_tdxs_builds_the_evidence_and_the_volume_tools_from_its_source() -> None:
    image = stack(components=[execution, driver, prover, Tdxs(source=MIRROR)])
    builds = lower(image).source_builds()
    for name in ("tdxs", "key-generation", "disk-encryption", "secret-delivery"):
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


def test_observability_exports_logs_and_metrics_and_blocks_nothing() -> None:
    logs_key = Secret("logs-key")
    token = Secret("metrics-token")
    observability = Observability(
        logs=JournalExport("https://logs.example:19532", credential=logs_key),
        metrics=PrometheusRemoteWrite("https://metrics.example/api/v1/write", credential=token),
    )
    image = Image("x", observability=observability, secrets=[logs_key, token])
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
    proxied = replace(observability, proxy="[fd00::9]:3128")
    planned = plan(Image("x", observability=proxied, secrets=[logs_key, token]), "production")
    environment = planned.service("journal-export").values("Service", "Environment")
    assert "https_proxy=http://[fd00::9]:3128" in environment
    config_text = planned.file("/etc/tundravm/metrics-export.yml").text
    assert 'proxy_url: "http://[fd00::9]:3128"' in config_text and "max_shards: 4" in config_text
    plaintext = Image("x", observability=Observability(logs=JournalExport("http://logs:19532")))
    assert "observability-plaintext" in {d.code for d in plan(plaintext).lint()}
    with pytest.raises(ValidationError, match="exports nothing"):
        Observability()
