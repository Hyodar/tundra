"""``attest`` and ``secrets deliver|unlock`` over the control-plane bootstrap, with a fake SSH
session (each request answered by a fake tdxs issuer) and a fake management port."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import PEER, FakeIssuer, _tcp, policy
from tundravm import bootstrap, instances
from tundravm.builds import Build
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR

ZERO = bytes(48)
SIMULATED = policy(RTMR0=ZERO, RTMR1=ZERO, RTMR2=ZERO)
TOKEN = b"s3cret-token-value"
PASSPHRASE = b"correct horse battery staple"


class FakeSsh:
    """Records every ssh argv and stdin; ``socat`` requests go to the issuer."""

    def __init__(self, issuer: FakeIssuer) -> None:
        self.issuer = issuer
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, argv: Sequence[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), stdin))
        if "socat -" in argv[-1]:
            reply = self.issuer(json.loads(stdin))
            return subprocess.CompletedProcess(
                list(argv), 0, json.dumps(reply).encode() + b"\n", b""
            )
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "operator"
    path.write_text("private\n")
    blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + bytes(36)).decode()
    path.with_name("operator.pub").write_text(f"ssh-ed25519 {blob} op@example\n")
    return path


@pytest.fixture
def instance(prover: Path, cli: Cli) -> instances.Instance:
    """A simulated local instance of the prover's dev build (no process runs)."""
    assert cli("build", "--dev").code == EXIT_OK
    build = Build.open((prover / "build/prover/latest-development").resolve())
    found = instances.create(
        prover / ".tundravm" / "instances", build, kind="run", platform="qemu", mode="simulated"
    )
    found.data["ports"] = {"8745": 18745, "22": 2222}
    found.save()
    return found


@pytest.fixture
def ssh(monkeypatch: pytest.MonkeyPatch) -> FakeSsh:
    fake = FakeSsh(FakeIssuer("simulator"))
    monkeypatch.setattr(bootstrap, "_run_ssh", fake)
    return fake


@pytest.fixture
def posted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    def post(url: str, body: bytes) -> int:
        calls.append((url, body))
        return 200

    monkeypatch.setattr(bootstrap, "_post", post)
    return calls


@pytest.fixture
def simulated_policy(tmp_path: Path) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(SIMULATED))
    return path


def _events(found: instances.Instance) -> list[dict[str, object]]:
    return [json.loads(line) for line in (found.path / "events.jsonl").read_text().splitlines()]


def test_attest_over_the_bootstrap_session(
    instance: instances.Instance,
    cli: Cli,
    key: Path,
    ssh: FakeSsh,
    posted: list[tuple[str, bytes]],
    simulated_policy: Path,
    tmp_path: Path,
) -> None:
    report = tmp_path / "attest-1.json"
    result = cli(
        "attest", instance.id, "--policy", simulated_policy, "--identity", key, "--output", report
    )
    assert result.code == EXIT_OK, result.out + result.err
    assert "verdict: simulated" in result.out
    assert f"channel: {bootstrap.UNVERIFIED}" in result.out
    [(url, body)] = posted
    assert url == "http://127.0.0.1:18745/"
    assert body.decode() == key.with_name("operator.pub").read_text().split()[1]
    argv = ssh.calls[0][0]
    assert argv[argv.index("-p") + 1] == "2222" and argv[-2] == "root@127.0.0.1"
    assert "StrictHostKeyChecking=accept-new" in argv
    assert json.loads(report.read_text())["schema"] == "tundravm.attestation/2"
    kinds = [event["event"] for event in _events(instance)]
    assert kinds == ["first-contact", "attested"]
    receipt = json.loads((instance.path / "instance.json").read_text())
    assert receipt["attested"]["verdict"] == "simulated"
    assert receipt["bootstrap"]["first_contact"] == "unverified"
    again = cli("attest", instance.id, "--policy", simulated_policy, "--identity", key, "--json")
    assert again.code == EXIT_OK
    assert len(posted) == 1  # the key is delivered once per instance
    assert json.loads(again.out)["schema"] == "tundravm.attest/1"


def test_attest_fails_on_a_mismatch(
    instance: instances.Instance, cli: Cli, key: Path, ssh: FakeSsh, posted: object, tmp_path: Path
) -> None:
    path = tmp_path / "peer.json"
    path.write_text(json.dumps(PEER))
    result = cli("attest", instance.id, "--policy", path, "--identity", key)
    assert result.code == EXIT_FAILURE
    assert "attested" not in json.loads((instance.path / "instance.json").read_text())


def test_attest_through_an_operator_relayed_issuer(
    instance: instances.Instance, cli: Cli, simulated_policy: Path
) -> None:
    with _tcp(FakeIssuer("simulator")) as url:
        result = cli("attest", instance.id, "--policy", simulated_policy, "--issuer", url)
    assert result.code == EXIT_OK, result.err
    assert "channel:" not in result.out
    assert _events(instance)[-1]["channel"] == f"issuer {url}"


def test_a_measurements_file_without_a_prediction_is_refused(
    instance: instances.Instance,
    cli: Cli,
    key: Path,
    ssh: FakeSsh,
    posted: object,
    tmp_path: Path,
) -> None:
    payload = json.loads((instance.build.path / "measurements.json").read_text())
    payload["records"] = [
        {"target": r["target"], "boot": r["boot"], "state": "unknown", "reason": "no GPT"}
        for r in payload["records"]
    ]
    measurements = tmp_path / "measurements.json"
    measurements.write_text(json.dumps(payload))
    result = cli("attest", instance.id, "--policy", measurements, "--identity", key)
    assert result.code == EXIT_SDK_ERROR
    assert "has no qemu disk prediction (unknown)" in result.err


def test_deliver_puts_values_on_stdin_only(
    instance: instances.Instance,
    cli: Cli,
    key: Path,
    ssh: FakeSsh,
    posted: object,
    simulated_policy: Path,
    tmp_path: Path,
) -> None:
    token = tmp_path / "token.txt"
    token.write_bytes(TOKEN)
    result = cli(
        "secrets",
        "deliver",
        instance.id,
        "--policy",
        simulated_policy,
        f"api-token=@{token}",
        "--identity",
        key,
    )
    assert result.code == EXIT_OK, result.err
    assert result.out.startswith(f"delivered api-token to {instance.id} (verdict simulated)")
    writes = [(argv, stdin) for argv, stdin in ssh.calls if "credentials" in argv[-1]]
    [(argv, stdin)] = writes
    assert stdin == TOKEN
    assert "/run/tundravm/credentials/.api-token.part" in argv[-1]
    assert (
        "mv /run/tundravm/credentials/.api-token.part /run/tundravm/credentials/api-token"
        in argv[-1]
    )
    for argv, _ in ssh.calls:
        assert all(TOKEN.decode() not in word for word in argv)
    for path in instance.path.iterdir():
        if path.is_file():
            assert TOKEN not in path.read_bytes(), path
    delivered = _events(instance)[-1]
    assert delivered["event"] == "delivered" and delivered["secrets"] == ["api-token"]


def test_deliver_refuses_what_the_image_does_not_declare(
    instance: instances.Instance, cli: Cli, key: Path, simulated_policy: Path, tmp_path: Path
) -> None:
    token = tmp_path / "token.txt"
    token.write_bytes(TOKEN)
    unknown = cli("secrets", "deliver", instance.id, "--policy", simulated_policy, f"jwt=@{token}")
    assert unknown.code == EXIT_SDK_ERROR
    assert "declares no secret jwt" in unknown.err and "api-token" in unknown.err
    inline = cli("secrets", "deliver", instance.id, "--policy", simulated_policy, "api-token=abc")
    assert inline.code == EXIT_SDK_ERROR and "is not NAME=@FILE" in inline.err


def test_deliver_refuses_a_verdict_the_predicate_rejects(
    instance: instances.Instance, cli: Cli, key: Path, ssh: FakeSsh, posted: object, tmp_path: Path
) -> None:
    ssh.issuer = FakeIssuer()  # a TDX quote to a simulated instance
    path = tmp_path / "peer.json"
    path.write_text(json.dumps(PEER))
    token = tmp_path / "token.txt"
    token.write_bytes(TOKEN)
    result = cli(
        "secrets",
        "deliver",
        instance.id,
        "--policy",
        path,
        f"api-token=@{token}",
        "--identity",
        key,
    )
    assert result.code == EXIT_SDK_ERROR
    assert "Nothing delivered" in result.err
    assert not any("credentials" in argv[-1] for argv, _ in ssh.calls)
    assert _events(instance)[-1]["event"] == "delivery-refused"


def test_unlock_needs_an_attestation_first(
    instance: instances.Instance,
    cli: Cli,
    key: Path,
    ssh: FakeSsh,
    posted: object,
    simulated_policy: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(PASSPHRASE + b"\n")))
    refused = cli("secrets", "unlock", instance.id, "data", "--identity", key)
    assert refused.code == EXIT_SDK_ERROR
    assert "has not passed an attestation yet" in refused.err
    assert cli("attest", instance.id, "--policy", simulated_policy, "--identity", key).code == 0
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(PASSPHRASE + b"\n")))
    result = cli("secrets", "unlock", instance.id, "data", "--identity", key)
    assert result.code == EXIT_OK, result.err
    [(argv, stdin)] = [(a, s) for a, s in ssh.calls if a[-1].startswith("cat > ")]
    assert argv[-1] == "cat > /run/tundravm/unlock/data" and stdin == PASSPHRASE
    assert all(PASSPHRASE.decode() not in word for argv, _ in ssh.calls for word in argv)
    assert _events(instance)[-1]["event"] == "unlocked"
    unknown = cli("secrets", "unlock", instance.id, "nothing", "--identity", key)
    assert unknown.code == EXIT_SDK_ERROR and "declares no volume 'nothing'" in unknown.err


def test_a_missing_identity_is_named(
    instance: instances.Instance, cli: Cli, simulated_policy: Path, tmp_path: Path
) -> None:
    result = cli("attest", instance.id, "--policy", simulated_policy, "--identity", tmp_path / "x")
    assert result.code == EXIT_SDK_ERROR and "No SSH key pair at" in result.err


def test_help_says_the_channel_is_unverified(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (("attest", "--help"), ("secrets", "--help"), ("secrets", "deliver", "--help")):
        with pytest.raises(SystemExit):
            cli(*argv)
        assert "unverified first contact" in " ".join(capsys.readouterr().out.split())
