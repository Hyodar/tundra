"""``deploy azure|gcp`` and its actions, against the cloud CLIs' recorded exchanges."""

from __future__ import annotations

import base64
import itertools
import json
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.cli.conftest import STARTER, Cli
from tests.cli.test_attest import PEER, FakeValidator, _tcp
from tests.cli.test_secrets import REGISTERS
from tundravm import bootstrap, deployments
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_OK
from tundravm.deploy._run import Fresh
from tundravm.deploy.azure import AzureDeployAdapter
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.testing import UPDATE_GOLDEN_ENV, FakeEndpoint

CLOUDS = Path(__file__).parents[1] / "fixtures" / "clouds"
EPOCH = "1761998400"


class RecordedCloud:
    """The ``gcloud`` and ``az`` CLIs as ``tests/fixtures/clouds/*.json`` record them: each
    call is answered by the exchange whose request its argv starts with (the longest), with
    ``--query KEY`` applied as the CLI applies it; a call nothing records fails the test."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exchanges = [
            exchange
            for name in ("gcp", "azure")
            for exchange in json.loads((CLOUDS / f"{name}.json").read_text())["exchanges"]
        ]
        assert all(e["SOURCE"].startswith("https://") for e in self.exchanges)

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        words = list(argv)
        self.calls.append(words)
        found = [e for e in self.exchanges if words[: len(e["request"])] == e["request"]]
        assert found, f"no recorded exchange answers {shlex.join(words)}"
        exchange = max(found, key=lambda e: len(e["request"]))
        stdout = exchange.get("stdout", "")
        if "json" in exchange:
            value = exchange["json"]
            if "--query" in words:
                value = value[words[words.index("--query") + 1]]
            stdout = json.dumps(value, indent=2)
        return subprocess.CompletedProcess(words, 0, stdout, exchange.get("stderr", ""))

    def take(self) -> list[str]:
        """The calls since the last ``take``, each as one shell line."""
        taken = [shlex.join(call) for call in self.calls]
        self.calls.clear()
        return taken


def _counted() -> Fresh:
    """Suffixes 1, 2, 3 ... in hex, and :data:`EPOCH`: every name a deploy draws, known."""
    numbers = itertools.count(1)
    return Fresh(hex=lambda length: f"{next(numbers):0{length}x}", epoch=lambda: EPOCH)


@pytest.fixture
def cloud(prover: Path, monkeypatch: pytest.MonkeyPatch) -> RecordedCloud:
    fake = RecordedCloud()
    fresh = _counted()

    def adapter(target: str) -> GcpDeployAdapter | AzureDeployAdapter:
        if target == "gcp":
            return GcpDeployAdapter(runner=fake, fresh=fresh)
        return AzureDeployAdapter(runner=fake, fresh=fresh)

    monkeypatch.setattr(deployments, "get_adapter", adapter)
    monkeypatch.setattr(deployments, "run_captured", fake)
    with (prover / "pyproject.toml").open("a") as handle:
        handle.write(
            '\n[tool.tundravm.gcp]\nproject = "p"\nbucket = "b"\nzone = "z"\n'
            '\n[tool.tundravm.azure]\nstorage_account = "acct"\n'
        )
    return fake


@pytest.fixture
def prover_build(prover: Path, cli: Cli) -> Build:
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    return Build.open((prover / "build/prover/latest-production").resolve())


def test_deploy_gcp_writes_a_receipt(
    prover_build: Build, prover: Path, cli: Cli, cloud: RecordedCloud
) -> None:
    result = cli("deploy", "gcp", "--build", "latest", "--volume", "data=create:100GiB")
    assert result.code == EXIT_OK, result.err
    assert f"deployed {prover_build.id} to gcp: instance prover-1" in result.out
    assert "address   203.0.113.7" in result.out
    assert "firewall  allow tcp:" in result.out and "8745" in result.out
    receipt = json.loads((prover / ".tundravm/instances/prover-1/instance.json").read_text())
    assert receipt["kind"] == "deploy" and receipt["platform"] == "gcp" and receipt["mode"] == "tdx"
    assert receipt["volumes"] == {"data": {"how": "create", "value": "100GiB"}}
    assert receipt["address"] == "203.0.113.7"
    assert receipt["build"]["id"] == prover_build.id


def test_every_declared_volume_needs_a_disk(
    prover_build: Build, cli: Cli, cloud: RecordedCloud
) -> None:
    result = cli("deploy", "gcp", "--build", "latest")
    assert result.code == EXIT_ERROR
    assert "Volume data needs a disk" in result.err and "data=create:SIZE" in result.err
    unknown = cli(
        "deploy",
        "gcp",
        "--build",
        "latest",
        "--volume",
        "data=create:1G",
        "--volume",
        "logs=create:1G",
    )
    assert unknown.code == EXIT_ERROR and "declares no volume logs" in unknown.err
    bad = cli("deploy", "gcp", "--build", "latest", "--volume", "data=100G")
    assert bad.code == EXIT_ERROR and "is not NAME=create:SIZE or NAME=existing:ID" in bad.err
    assert not any("create" in call and "instances" in call for call in cloud.calls)


DISK = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/tdx-vms"
    "/providers/Microsoft.Compute/disks/prover-data-000003"
)
AZURE_VOLUMES = [
    (
        "data=create:100GiB",
        EXIT_OK,
        [
            "az disk create --resource-group tdx-vms --name prover-data-000003 --location eastus "
            "--size-gb 100 --query id --output json",
            f"--attach-data-disks {DISK} --data-disk-delete-option Detach --output json",
            "az vm show --name tdx-prover-000004 --resource-group tdx-vms --output json",
        ],
        "deployed",
    ),
    (
        "data=existing:prover-data-000003",
        EXIT_OK,
        [
            "--attach-data-disks prover-data-000003 --data-disk-delete-option Detach --output json",
            "az vm show --name tdx-prover-000003 --resource-group tdx-vms --output json",
        ],
        "deployed",
    ),
    ("logs=create:1G", EXIT_ERROR, [], "declares no volume logs"),
    (
        "data=existing:other-disk",
        EXIT_ERROR,
        [
            "--attach-data-disks other-disk --data-disk-delete-option Detach --output json",
            "az vm show --name tdx-prover-000003 --resource-group tdx-vms --output json",
        ],
        "Azure reports the data disks {0: 'prover-data-000003'} by LUN, not {0: 'other-disk'}",
    ),
]


@pytest.mark.parametrize(("volume", "code", "calls", "shown"), AZURE_VOLUMES)
def test_azure_attaches_each_volume_at_its_lun(
    prover_build: Build,
    prover: Path,
    cli: Cli,
    cloud: RecordedCloud,
    volume: str,
    code: int,
    calls: list[str],
    shown: str,
) -> None:
    """The disk calls of ``deploy azure``: a new disk, then every disk attached at create in
    LUN order and the LUNs read back; an undeclared volume or another disk at the LUN is
    refused."""
    result = cli("deploy", "azure", "--build", "latest", "--volume", volume)
    assert result.code == code and shown in result.out + result.err, result.err
    made = [c for c in cloud.take() if c.startswith(("az disk", "az vm"))]
    assert [c if "vm create" not in c else c.split("NONE ", 1)[1] for c in made] == calls
    if code == EXIT_OK:
        receipt = json.loads((prover / ".tundravm/instances/prover-1/instance.json").read_text())
        assert receipt["volumes"]["data"]["lun"] == 0


def test_a_build_without_the_target_is_refused(built: Build, cli: Cli) -> None:
    result = cli("deploy", "gcp", "--build", "latest")
    assert result.code == EXIT_ERROR
    assert "has no gcp artifact" in result.err and "--target gcp" in result.err


def test_status_reports_as_json_and_takes_one_instance(
    prover_build: Build, cli: Cli, cloud: RecordedCloud
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    report = json.loads(cli("deploy", "status", "prover-1", "--json").out)
    assert report["schema"] == "tundravm.deploy/1" and report["address"] == "203.0.113.7"
    assert cli("deploy", "status", "prover-1", "more").code == EXIT_ERROR


@pytest.mark.parametrize(
    ("endpoint", "control_plane"), [(True, "not listening"), (False, "none declared")]
)
def test_status_probes_only_the_ports_the_firewall_opens(
    prover_build: Build,
    prover: Path,
    cli: Cli,
    cloud: RecordedCloud,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: bool,
    control_plane: str,
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    path = prover / ".tundravm/instances/prover-1/instance.json"
    receipt = json.loads(path.read_text())
    if not endpoint:
        del receipt["ports"]["8745"]
    path.write_text(json.dumps(receipt))
    probed: list[int] = []

    def reachable(host: str, port: int) -> bool:
        probed.append(port)
        return False

    monkeypatch.setattr(deployments, "reachable", reachable)
    report = json.loads(cli("deploy", "status", "prover-1", "--json").out)
    assert report["control_plane"] == control_plane
    assert sorted(map(str, probed)) == sorted(receipt["ports"].keys() - {"22"})


def test_run_actions_refuse_a_deployment(
    prover_build: Build, cli: Cli, cloud: RecordedCloud
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    result = cli("run", "status", "prover-1")
    assert result.code == EXIT_ERROR and "deploy status prover-1" in result.err


def test_upgrade_needs_a_policy_and_a_build(
    prover_build: Build, cli: Cli, cloud: RecordedCloud
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    assert "needs --policy FILE" in cli("deploy", "upgrade", "prover-1", "--build", "latest").err
    assert "needs --build BUILD" in cli("deploy", "upgrade", "prover-1", "--policy", "p.json").err
    assert "needs INSTANCE" in cli("deploy", "upgrade").err


def test_upgrade_refuses_a_volume_change(
    prover_build: Build,
    prover: Path,
    cli: Cli,
    cloud: RecordedCloud,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    (prover / "demo.py").write_text(
        STARTER.replace("    dev=DevAccess", '    targets=("gcp",),\n    dev=DevAccess')
    )
    monkeypatch.setenv("TUNDRAVM_BUILD", "demo-build")  # its own lock
    assert cli("lock", "demo.py").code == EXIT_OK
    assert cli("build", "demo.py").code == EXIT_OK
    monkeypatch.delenv("TUNDRAVM_BUILD")
    policy = tmp_path / "p.json"
    policy.write_text("{}")
    other = "demo-build/demo/latest-production"
    result = cli("deploy", "upgrade", "prover-1", "--build", other, "--policy", policy)
    assert result.code == EXIT_ERROR
    assert "The new build's volumes differ" in result.err
    assert not any(call[3:4] == ["stop"] for call in cloud.calls)


PLAN_SETTINGS = {
    "gcp": '\n[tool.tundravm.gcp]\nproject = "p"\nbucket = "b"\nzone = "z"\n',
    "azure": '\n[tool.tundravm.azure]\nstorage_account = "acct"\n',
}


@pytest.mark.parametrize("target", ["gcp", "azure"])
def test_plan_prints_every_step_and_call_and_runs_none(
    target: str, request: pytest.FixtureRequest, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan of the prover, its volume a new disk, on GCP and on Azure, compared with
    ``deploy-plan-TARGET.golden`` beside this file once the build's own values are named
    (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it); ``--json`` holds the same calls."""

    def refuse(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"--plan ran {list(argv)}")

    for module in ("deployments", "deploy.gcp", "deploy.azure"):
        monkeypatch.setattr(f"tundravm.{module}.run_captured", refuse)
    root: Path = request.getfixturevalue("prover")
    volumes = ("--volume", "data=create:100GiB")
    with (root / "pyproject.toml").open("a") as handle:
        handle.write(PLAN_SETTINGS[target])
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    build = Build.open(next((root / "build").glob("*/latest-production")).resolve())
    shown = cli("deploy", target, "--build", "latest", *volumes, "--plan")
    assert shown.code == EXIT_OK, shown.err
    data = json.loads(cli("deploy", target, "--build", "latest", *volumes, "--plan", "--json").out)
    assert data["schema"] == "tundravm.deploy/1"
    calls = shown.out.split("\ncalls:\n", 1)[1].splitlines()
    assert calls == [f"{n}. {shlex.join(call)}" for n, call in enumerate(data["calls"], 1)]
    assert not (root / ".tundravm" / "instances").exists()
    artifact = build.manifest["artifacts"][build.artifact(target).name]
    policy = next(s for s in data["steps"] if s["step"] == "policy")
    named = {
        str(root): "{root}",
        build.id: "{build}",
        artifact["sha256"]: "{sha256}",
        f"{artifact['size']} bytes": "{size} bytes",
        **{
            str(v[0]): f"{{{k}}}" for k, v in policy["registers"].items() if k in ("RTMR1", "RTMR2")
        },
        **{v: f"{{{k}}}" for k, v in policy["vtpm_pcrs"].items()},
    }
    text = shown.out
    for value, name in named.items():
        text = text.replace(value, name)
    golden = Path(__file__).with_name(f"deploy-plan-{target}.golden")
    if os.environ.get(UPDATE_GOLDEN_ENV) == "1":
        golden.write_text(text)
    assert text == golden.read_text()


# ── a whole lifecycle per cloud, every call against the recorded exchanges ──

G = "--project=p --zone=z"
GCE = "gcloud compute instances"
UPLOADED = "gs://b/tdx-images/tdx-prover-{n:08x}.tar.gz"
KEPT = "mode=rw,auto-delete=no"


def _launch(image: int, vm: int, disks: str = "") -> list[str]:
    uri = UPLOADED.format(n=image)
    return [
        f"gcloud storage cp {{artifact}} {uri}",
        f"gcloud compute images create tdx-prover-{image:08x} --project=p --source-uri={uri} "
        "--guest-os-features=UEFI_COMPATIBLE,GVNIC,TDX_CAPABLE",
        f"{GCE} create tdx-prover-{vm:06x} {G} --machine-type=c3-standard-4 "
        f"--image=tdx-prover-{image:08x} --confidential-compute-type=TDX "
        f"--maintenance-policy=TERMINATE {disks}--format=json",
        f"{GCE} describe tdx-prover-{vm:06x} {G} --format=json",
    ]


def _move(old: int, new: int) -> list[str]:
    return [
        f"{GCE} stop tdx-prover-{old:06x} {G}",
        f"{GCE} detach-disk tdx-prover-{old:06x} {G} --disk=prover-data-1",
        f"{GCE} attach-disk tdx-prover-{new:06x} {G} --disk=prover-data-1 "
        "--device-name=tundravm-data",
    ]


ATTESTED = ("--policy", "{policy}", "--identity", "{key}", "--validator", "{validator}")
GCP_WALK = [
    (
        ("deploy", "gcp", "--build", "latest", "--volume", "data=existing:prover-data-1"),
        _launch(
            2, 3, "--disk=name=prover-data-1,device-name=tundravm-data,mode=rw,auto-delete=no "
        ),
        "address   203.0.113.7",
    ),
    (
        ("deploy", "status", "prover-1"),
        [f"{GCE} describe tdx-prover-000003 {G} --format=json"],
        "state          running",
    ),
    (
        ("deploy", "logs", "prover-1"),
        [f"{GCE} get-serial-port-output tdx-prover-000003 {G}"],
        "Reached target minimal.target",
    ),
    (("deploy", "mode", "prover-1"), [], "tdx on gcp, production build, issuer tdx: a TDX guest"),
    (
        ("deploy", "upgrade", "prover-1", "--build", "latest", *ATTESTED),
        [f"{GCE} describe tdx-prover-000003 {G} --format=json", *_launch(5, 6), *_move(3, 6)],
        "upgraded prover-1 -> prover-2",
    ),
    (
        ("deploy", "rollback", "prover-2", *ATTESTED),
        [
            f"{GCE} describe tdx-prover-000006 {G} --format=json",
            f"{GCE} start tdx-prover-000003 {G}",
            f"{GCE} describe tdx-prover-000003 {G} --format=json",
            *_move(6, 3),
        ],
        "rolled back prover-2 -> prover-1",
    ),
]


def test_gcp_lifecycle_makes_the_recorded_calls(
    prover_build: Build,
    cli: Cli,
    cloud: RecordedCloud,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deploy, status, logs, mode, upgrade and rollback, in order: each one's calls (each
    step's VM a fresh boot of the attested endpoint)."""
    monkeypatch.setattr(bootstrap, "read_passphrase", lambda volume: b"passphrase")
    monkeypatch.setattr(deployments, "_wait_ready", lambda instance, timeout: True)
    key = tmp_path / "op"
    key.write_text("k")
    key.with_name("op.pub").write_text("ssh-ed25519 " + base64.b64encode(bytes(51)).decode())
    policy = tmp_path / "peer.json"
    policy.write_text(json.dumps(PEER))
    with _tcp(FakeValidator()) as validator:
        values = {
            "artifact": prover_build.artifact("gcp"),
            "policy": policy,
            "key": key,
            "validator": validator,
        }
        for argv, calls, shown in GCP_WALK:
            endpoint = FakeEndpoint(platform="tdx", mode="tdx", registers=REGISTERS)
            monkeypatch.setattr(bootstrap, "_tls", endpoint.connect)
            result = cli(*(word.format(**values) for word in argv))
            assert result.code == EXIT_OK, (argv, result.err)
            assert shown in result.out, (argv, result.out)
            assert cloud.take() == [call.format(**values) for call in calls], argv


AZ_VM = "--name tdx-demo-000003 --resource-group tdx-vms"
SUBSCRIPTION = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/tdx-vms"
AZURE_WALK = [
    (
        ("deploy", "azure", "--build", "latest"),
        [
            "az storage container create --account-name acct --name tdx-images --output json",
            "az storage blob upload --account-name acct --container-name tdx-images "
            "--name azure-00000002.vhd --file {artifact} --type page --output json",
            "az storage account show --name acct --query id --output json",
            "az sig create --resource-group tdx-vms --gallery-name tdx_images --location eastus "
            "--output json",
            "az sig image-definition create --resource-group tdx-vms --gallery-name tdx_images "
            "--gallery-image-definition tdx-demo --location eastus --publisher tundravm "
            "--offer demo --sku demo --os-type Linux --os-state specialized "
            "--hyper-v-generation V2 --features SecurityType=ConfidentialVMSupported "
            "--output json",
            "az sig image-version create --resource-group tdx-vms --gallery-name tdx_images "
            f"--gallery-image-definition tdx-demo --gallery-image-version 1.0.{EPOCH} "
            "--location eastus --os-vhd-storage-account "
            f"{SUBSCRIPTION}/providers/Microsoft.Storage/storageAccounts/acct "
            "--os-vhd-uri https://acct.blob.core.windows.net/tdx-images/azure-00000002.vhd "
            "--query id --output json",
            "az vm create --resource-group tdx-vms --name tdx-demo-000003 --location eastus "
            f"--size Standard_DC2es_v5 --image {SUBSCRIPTION}/providers/Microsoft.Compute/"
            f"galleries/tdx_images/images/tdx-demo/versions/1.0.{EPOCH} --specialized "
            "--security-type ConfidentialVM --os-disk-security-encryption-type VMGuestStateOnly "
            "--enable-vtpm true --enable-secure-boot false --public-ip-sku Standard "
            "--nsg-rule NONE --output json",
        ],
        "address   203.0.113.9",
    ),
    (
        ("deploy", "status", "demo-1"),
        [f"az vm get-instance-view {AZ_VM} --output json"],
        "state          VM running",
    ),
    (
        ("deploy", "status", "demo-1", "--json"),
        [f"az vm get-instance-view {AZ_VM} --output json"],
        '"address": "203.0.113.9"',
    ),
    (
        ("deploy", "logs", "demo-1"),
        [f"az vm boot-diagnostics get-boot-log {AZ_VM}"],
        "Reached target minimal.target",
    ),
    (("deploy", "mode", "demo-1"), [], "tdx on azure, production build, issuer tdx: a TDX guest"),
]
AZURE_REFUSED = [
    (("deploy", "upgrade", "demo-1", "--build", "latest", "--policy", "{policy}"), "GCP instances"),
    (("deploy", "rollback", "demo-1", "--policy", "{policy}"), "replaced nothing"),
]


def test_azure_lifecycle_makes_the_recorded_calls(
    prover: Path, cli: Cli, cloud: RecordedCloud, tmp_path: Path
) -> None:
    """The starter on Azure: deploy, status, logs and mode make their calls; upgrade and
    rollback are refused before any."""
    (prover / "prover.py").write_text(
        STARTER.replace("    dev=DevAccess", '    targets=("azure",),\n    dev=DevAccess')
    )
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    build = Build.open((prover / "build/demo/latest-production").resolve())
    policy = tmp_path / "p.json"
    policy.write_text("{}")
    values = {"artifact": build.artifact("azure"), "policy": policy}
    for argv, calls, shown in AZURE_WALK:
        result = cli(*argv)
        assert result.code == EXIT_OK, (argv, result.err)
        assert shown in result.out, (argv, result.out)
        assert cloud.take() == [call.format(**values) for call in calls], argv
    for argv, refusal in AZURE_REFUSED:
        result = cli(*(word.format(**values) for word in argv))
        assert result.code == EXIT_ERROR and refusal in result.err, (argv, result.err)
        assert cloud.take() == [], argv
