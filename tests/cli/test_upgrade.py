"""``deploy upgrade``: plan 6.8's order, with a fake cloud, attested endpoint and validator."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import PEER, FakeValidator, _tcp
from tests.cli.test_deploy import FakeCloud, cloud, prover_build  # noqa: F401 (fixtures)
from tests.cli.test_secrets import REGISTERS
from tundravm import bootstrap, deployments
from tundravm.builds import Build
from tundravm.cli import EXIT_OK
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.testing import FakeEndpoint
from tundravm.testing._endpoint import FakeSession


def _logged(endpoint: FakeEndpoint, order: list[str]) -> bootstrap.Connector:
    """*endpoint*'s sessions, each POST recorded in *order*."""

    def connect(host: str, port: int) -> FakeSession:
        session = endpoint.connect(host, port)
        request = session.request

        def logged(method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
            if method == "POST":
                order.append(f"endpoint {path}")
            return request(method, path, body)

        session.request = logged  # type: ignore[method-assign]
        return session

    return connect


def test_upgrade_runs_the_replacement_in_order(
    prover_build: Build,  # noqa: F811
    prover: Path,
    cli: Cli,
    cloud: FakeCloud,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed = cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:disk-1")
    assert deployed.code == EXIT_OK
    image = prover / "prover.py"
    image.write_text(
        image.read_text().replace('Package("ca-certificates")', 'Package("ca-certificates"), "jq"')
    )
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    order: list[str] = []
    endpoint = FakeEndpoint(platform="tdx", mode="tdx", registers=REGISTERS)

    def logged_cloud(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(argv[:3]) == ["gcloud", "compute", "instances"] and argv[3] != "describe":
            order.append(f"gcloud {argv[3]}")
        return cloud(argv)

    monkeypatch.setattr(bootstrap, "_tls", _logged(endpoint, order))
    monkeypatch.setattr(deployments, "run_captured", logged_cloud)
    monkeypatch.setattr(
        deployments, "get_adapter", lambda target: GcpDeployAdapter(runner=logged_cloud)
    )
    monkeypatch.setattr(deployments, "_wait_ready", lambda instance, timeout: True)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"passphrase\n")))
    key = tmp_path / "op"
    key.write_text("k")
    key.with_name("op.pub").write_text("ssh-ed25519 " + base64.b64encode(bytes(51)).decode())
    policy = tmp_path / "peer.json"
    policy.write_text(json.dumps(PEER))
    token = tmp_path / "token"
    token.write_bytes(b"value")
    with _tcp(FakeValidator()) as validator:
        result = cli(
            "deploy",
            "upgrade",
            "prover-1",
            "--build",
            "latest",
            "--policy",
            policy,
            f"api-token=@{token}",
            "--identity",
            key,
            "--validator",
            validator,
        )
    assert result.code == EXIT_OK, result.out + result.err
    assert "upgraded prover-1 -> prover-2" in result.out
    steps = [line.split(" ", 1)[0] for line in result.out.splitlines() if line[:1].isdigit()]
    assert steps == ["1/7", "2/7", "3/7", "4/7", "5/7", "6/7", "7/7"]
    assert order.index("gcloud create") < order.index("endpoint /ssh-key")
    assert order.index("endpoint /credentials") < order.index("gcloud stop")
    assert order.index("gcloud stop") < order.index("gcloud detach-disk")
    assert order.index("gcloud detach-disk") < order.index("gcloud attach-disk")
    assert order.index("gcloud attach-disk") < order.index("endpoint /unlock")
    assert endpoint.received == {"api-token": b"value"}
    old = json.loads((prover / ".tundravm/instances/prover-1/instance.json").read_text())
    new = json.loads((prover / ".tundravm/instances/prover-2/instance.json").read_text())
    assert old["state"] == "retired" and old["successor"] == "prover-2"
    assert new["previous"] == "prover-1"
    assert new["cloud"]["disks"] == {"data": "prover-data-1"}


def test_a_candidate_that_does_not_attest_keeps_the_old_instance(
    prover_build: Build,  # noqa: F811
    prover: Path,
    cli: Cli,
    cloud: FakeCloud,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cli("deploy", "gcp", "--build", "latest", "--volume", "data=existing:d").code == 0
    endpoint = FakeEndpoint(platform="tdx", mode="tdx", registers=REGISTERS)
    monkeypatch.setattr(bootstrap, "_tls", endpoint.connect)
    key = tmp_path / "op"
    key.write_text("k")
    key.with_name("op.pub").write_text("ssh-ed25519 " + base64.b64encode(bytes(51)).decode())
    policy = tmp_path / "peer.json"
    policy.write_text(json.dumps(PEER))
    result = cli(
        "deploy", "upgrade", "prover-1", "--build", "latest", "--policy", policy, "--identity", key
    )
    assert result.code == 2
    assert "did not attest (production delivery needs trusted" in result.err
    assert "prover-1 keeps running and keeps its volumes" in result.err
    assert not any(call[3:4] in (["stop"], ["detach-disk"]) for call in cloud.calls)
