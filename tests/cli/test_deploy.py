"""``deploy azure|gcp`` and its actions, with a fake cloud CLI."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.cli.conftest import STARTER, Cli
from tundravm import deployments, instances
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_OK
from tundravm.deploy.azure import AzureDeployAdapter
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.testing import UPDATE_GOLDEN_ENV

DESCRIBE = {
    "status": "RUNNING",
    "networkInterfaces": [{"accessConfigs": [{"natIP": "203.0.113.7"}]}],
    "disks": [
        {"deviceName": "persistent-disk-0", "source": "zones/z/disks/boot"},
        {"deviceName": "tundravm-data", "source": "projects/p/zones/z/disks/prover-data-1"},
    ],
}


class FakeCloud:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if "describe" in argv:
            return subprocess.CompletedProcess(list(argv), 0, json.dumps(DESCRIBE), "")
        if "get-serial-port-output" in argv:
            return subprocess.CompletedProcess(list(argv), 0, "Reached target minimal.target\n", "")
        return subprocess.CompletedProcess(list(argv), 0, "{}", "")


@pytest.fixture
def cloud(prover: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    fake = FakeCloud()

    def adapter(target: str) -> GcpDeployAdapter | AzureDeployAdapter:
        return GcpDeployAdapter(runner=fake) if target == "gcp" else AzureDeployAdapter(runner=fake)

    monkeypatch.setattr(deployments, "get_adapter", adapter)
    monkeypatch.setattr(deployments, "run_captured", fake)
    with (prover / "pyproject.toml").open("a") as handle:
        handle.write('\n[tool.tundravm.gcp]\nproject = "p"\nbucket = "b"\nzone = "z"\n')
    return fake


@pytest.fixture
def prover_build(prover: Path, cli: Cli) -> Build:
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    return Build.open((prover / "build/prover/latest-production").resolve())


def test_deploy_gcp_writes_a_receipt(
    prover_build: Build, prover: Path, cli: Cli, cloud: FakeCloud
) -> None:
    result = cli("deploy", "gcp", "--build", "latest", "--volume", "data=create:100GiB")
    assert result.code == EXIT_OK, result.err
    assert f"deployed {prover_build.id} to gcp: instance prover-1" in result.out
    assert "address   203.0.113.7" in result.out
    assert "firewall  allow tcp:" in result.out and "8745" in result.out
    create = next(call for call in cloud.calls if "create" in call and "instances" in call)
    assert "--confidential-compute-type=TDX" in create
    disk = next(arg for arg in create if arg.startswith("--create-disk="))
    assert (
        "size=100GiB" in disk and "device-name=tundravm-data" in disk and "auto-delete=no" in disk
    )
    upload = next(call for call in cloud.calls if call[:3] == ["gcloud", "storage", "cp"])
    assert upload[3] == str(prover_build.path / "gcp.tar.gz")
    receipt = json.loads((prover / ".tundravm/instances/prover-1/instance.json").read_text())
    assert receipt["kind"] == "deploy" and receipt["platform"] == "gcp" and receipt["mode"] == "tdx"
    assert receipt["volumes"] == {"data": {"how": "create", "value": "100GiB"}}
    assert receipt["address"] == "203.0.113.7"
    assert receipt["build"]["id"] == prover_build.id


def test_every_declared_volume_needs_a_disk(
    prover_build: Build, cli: Cli, cloud: FakeCloud
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


def test_azure_cannot_attach_a_volume(prover_build: Build, cli: Cli, cloud: FakeCloud) -> None:
    result = cli("deploy", "azure", "--build", "latest", "--volume", "data=existing:disk-1")
    assert result.code == EXIT_ERROR
    assert "identified by LUN" in result.err
    assert cloud.calls == []


def test_a_build_without_the_target_is_refused(built: Build, cli: Cli) -> None:
    result = cli("deploy", "gcp", "--build", "latest")
    assert result.code == EXIT_ERROR
    assert "has no gcp artifact" in result.err and "--target gcp" in result.err


def test_status_logs_and_mode(prover_build: Build, cli: Cli, cloud: FakeCloud) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    status = cli("deploy", "status", "prover-1")
    assert status.code == EXIT_OK
    assert "state          running" in status.out and "control_plane  not listening" in status.out
    report = json.loads(cli("deploy", "status", "prover-1", "--json").out)
    assert report["schema"] == "tundravm.deploy/1" and report["address"] == "203.0.113.7"
    assert "Reached target minimal.target" in cli("deploy", "logs", "prover-1").out
    mode = cli("deploy", "mode", "prover-1")
    assert (
        "tdx on gcp, production build: a TDX guest whose evidence a production policy" in mode.out
    )
    extra = cli("deploy", "status", "prover-1", "more")
    assert extra.code == EXIT_ERROR


def test_run_actions_refuse_a_deployment(prover_build: Build, cli: Cli, cloud: FakeCloud) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    result = cli("run", "status", "prover-1")
    assert result.code == EXIT_ERROR and "deploy status prover-1" in result.err


def test_upgrade_needs_a_policy_and_a_build(
    prover_build: Build, cli: Cli, cloud: FakeCloud
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    assert "needs --policy FILE" in cli("deploy", "upgrade", "prover-1", "--build", "latest").err
    assert "needs --build BUILD" in cli("deploy", "upgrade", "prover-1", "--policy", "p.json").err
    assert "needs INSTANCE" in cli("deploy", "upgrade").err


def test_upgrade_refuses_a_volume_change(
    prover_build: Build,
    prover: Path,
    cli: Cli,
    cloud: FakeCloud,
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


def test_upgrade_is_refused_on_azure(
    prover_build: Build, prover: Path, cli: Cli, tmp_path: Path
) -> None:
    found = instances.create(
        prover / ".tundravm" / "instances",
        prover_build,
        kind="deploy",
        platform="azure",
        mode="tdx",
    )
    policy = tmp_path / "p.json"
    policy.write_text("{}")
    result = cli("deploy", "upgrade", found.id, "--build", "latest", "--policy", policy)
    assert result.code == EXIT_ERROR
    assert "replaces GCP instances" in result.err and "identified by LUN" in result.err


def test_rollback_needs_a_replaced_instance(
    prover_build: Build, cli: Cli, cloud: FakeCloud, tmp_path: Path
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1").code == 0
    policy = tmp_path / "p.json"
    policy.write_text("{}")
    result = cli("deploy", "rollback", "prover-1", "--policy", policy)
    assert result.code == EXIT_ERROR and "replaced nothing" in result.err


PLAN_SETTINGS = {
    "gcp": '\n[tool.tundravm.gcp]\nproject = "p"\nbucket = "b"\nzone = "z"\n',
    "azure": '\n[tool.tundravm.azure]\nstorage_account = "acct"\n',
}


@pytest.mark.parametrize("target", ["gcp", "azure"])
def test_plan_prints_every_step_and_call_and_runs_none(
    target: str, request: pytest.FixtureRequest, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan of the prover on GCP (its volume a new disk) and of the starter on Azure,
    compared with ``deploy-plan-TARGET.golden`` beside this file once the build's own values
    are named (``TUNDRAVM_UPDATE_GOLDEN=1`` rewrites it); ``--json`` holds the same calls."""

    def refuse(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"--plan ran {list(argv)}")

    for module in ("deployments", "deploy.gcp", "deploy.azure"):
        monkeypatch.setattr(f"tundravm.{module}.run_captured", refuse)
    if target == "gcp":
        root: Path = request.getfixturevalue("prover")
        volumes: tuple[str, ...] = ("--volume", "data=create:100GiB")
    else:
        root = request.getfixturevalue("project")
        (root / "demo.py").write_text(
            STARTER.replace("    dev=DevAccess", '    targets=("azure",),\n    dev=DevAccess')
        )
        volumes = ()
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
