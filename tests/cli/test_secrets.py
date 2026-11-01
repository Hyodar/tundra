"""``attest`` and ``secrets deliver|unlock`` against a fake attested endpoint
(``tundravm.testing.FakeEndpoint``): the channel binding of plan 6.3 and its refusals."""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path
from typing import Literal

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import MRTD, PEER, R0, R1, R2, R3, policy
from tundravm import bootstrap, instances
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, EXIT_OK
from tundravm.testing import FakeEndpoint
from tundravm.testing._endpoint import HOST_KEY, INSTANCE_KEY

SIMULATED = policy(RTMR0=bytes(48), RTMR1=bytes(48), RTMR2=bytes(48))
TOKEN = b"s3cret-token-value"
PASSPHRASE = b"correct horse battery staple"
REGISTERS = {"MRTD": MRTD, "RTMR0": R0, "RTMR1": R1, "RTMR2": R2, "RTMR3": R3}


@pytest.fixture
def key(tmp_path: Path) -> Path:
    path = tmp_path / "operator"
    path.write_text("private\n")
    blob = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(32)).decode()
    path.with_name("operator.pub").write_text(f"ssh-ed25519 {blob} op@example\n")
    return path


def _instance(
    prover: Path, cli: Cli, *, mode: Literal["simulated", "tdx"], kind: str
) -> instances.Instance:
    if kind == "production":
        assert cli("lock").code == EXIT_OK
        assert cli("build").code == EXIT_OK
    else:
        assert cli("build", "--dev").code == EXIT_OK
    build = Build.open((prover / f"build/prover/latest-{kind}").resolve())
    found = instances.create(
        prover / ".tundravm" / "instances", build, kind="run", platform="qemu", mode=mode
    )
    found.data["ports"] = {"8745": 18745, "22": 2222}
    found.save()
    return found


@pytest.fixture
def instance(prover: Path, cli: Cli) -> instances.Instance:
    """A simulated local instance of the prover's dev build (no process runs)."""
    return _instance(prover, cli, mode="simulated", kind="development")


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> FakeEndpoint:
    fake = FakeEndpoint()
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    return fake


def _file(tmp_path: Path, name: str, body: bytes | str) -> Path:
    path = tmp_path / name
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)
    return path


def _events(found: instances.Instance) -> list[dict[str, object]]:
    return [json.loads(line) for line in (found.path / "events.jsonl").read_text().splitlines()]


def test_attest_deliver_and_unlock_over_the_bound_session(
    instance: instances.Instance,
    cli: Cli,
    key: Path,
    endpoint: FakeEndpoint,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    attested = cli("attest", instance.id, "--policy", simulated, "--identity", key)
    assert attested.code == EXIT_OK, attested.out + attested.err
    assert "verdict: simulated" in attested.out
    assert f"instance key  {INSTANCE_KEY}  (bound to the quote)" in attested.out
    assert json.loads((instance.path / "instance.json").read_text())["instance_key"] == INSTANCE_KEY
    assert endpoint.ssh_keys == [" ".join(key.with_name("operator.pub").read_text().split()[:2])]
    known = instances.known_hosts(instance).read_text()
    assert known == f"[127.0.0.1]:2222 {HOST_KEY}\n"

    token = _file(tmp_path, "token.txt", TOKEN)
    delivered = cli("secrets", "deliver", instance.id, "--policy", simulated, f"api-token=@{token}")
    assert delivered.code == EXIT_OK, delivered.err
    assert endpoint.received == {"api-token": TOKEN}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(PASSPHRASE + b"\n")))
    unlocked = cli("secrets", "unlock", instance.id, "data")
    assert unlocked.code == EXIT_OK, unlocked.err
    assert endpoint.unlocked == {"data": PASSPHRASE}
    assert len(endpoint.sessions) == 3  # each command verifies on a session of its own
    for path in instance.path.iterdir():
        if path.is_file():
            assert TOKEN not in path.read_bytes() and PASSPHRASE not in path.read_bytes()
    kinds = [event["event"] for event in _events(instance)]
    assert kinds == ["attested", "delivered", "unlocked"]
    again = cli("secrets", "deliver", instance.id, "--policy", simulated, f"api-token=@{token}")
    assert again.code == EXIT_ERROR and "refused /credentials (409)" in again.err


@pytest.mark.parametrize(
    ("contract", "misbehave", "build", "endpoint_kwargs", "refusal"),
    [
        ("relay", "relay", ("simulated", "development"), {}, "the session is not the one"),
        ("replay", "replay", ("simulated", "development"), {}, "replayed evidence"),
        ("wrong key", "wrong-key", ("simulated", "development"), {}, "another TLS key"),
        (
            "simulated in production",
            None,
            ("tdx", "production"),
            {"mode": "tdx"},
            "a TDX instance answered with simulated evidence",
        ),
        (
            "debug attributes",
            None,
            ("tdx", "production"),
            {"platform": "tdx", "mode": "tdx", "registers": REGISTERS, "debug": True},
            "the TD has the DEBUG attribute",
        ),
    ],
)
def test_delivery_is_refused(
    prover: Path,
    cli: Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
    misbehave: str | None,
    build: tuple[Literal["simulated", "tdx"], str],
    endpoint_kwargs: dict[str, object],
    refusal: str,
) -> None:
    """Plan 6.3's named rejections: nothing reaches the endpoint's credential store."""
    del contract
    found = _instance(prover, cli, mode=build[0], kind=build[1])
    fake = FakeEndpoint(misbehave=misbehave, **endpoint_kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    if misbehave == "replay":
        fake.connect("h", 1).request("GET", "/evidence?nonce=" + "11" * 32)
    peer = PEER if build[1] == "production" else SIMULATED
    token = _file(tmp_path, "token.txt", TOKEN)
    path = _file(tmp_path, "policy.json", json.dumps(peer))
    result = cli("secrets", "deliver", found.id, "--policy", path, f"api-token=@{token}")
    assert result.code == EXIT_ERROR, result.out + result.err
    assert refusal in " ".join(result.err.split())
    assert fake.received == {}
    assert not any(method == "POST" for s in fake.sessions for method, _ in s.requests)


def test_attest_fails_on_a_mismatch_and_writes_nothing(
    prover: Path, cli: Cli, key: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = _instance(prover, cli, mode="tdx", kind="development")
    fake = FakeEndpoint(platform="tdx", mode="tdx", registers={**REGISTERS, "RTMR1": R2})
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    path = _file(tmp_path, "peer.json", json.dumps(PEER))
    result = cli("attest", found.id, "--policy", path, "--identity", key)
    assert result.code == EXIT_FAILURE
    assert "attested" not in json.loads((found.path / "instance.json").read_text())
    assert not instances.known_hosts(found).exists() and fake.ssh_keys == []


def test_deliver_refuses_what_the_image_does_not_declare(
    instance: instances.Instance, cli: Cli, endpoint: FakeEndpoint, tmp_path: Path
) -> None:
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    token = _file(tmp_path, "token.txt", TOKEN)
    unknown = cli("secrets", "deliver", instance.id, "--policy", simulated, f"jwt=@{token}")
    assert unknown.code == EXIT_ERROR
    assert "declares no secret jwt" in unknown.err and "api-token" in unknown.err
    inline = cli("secrets", "deliver", instance.id, "--policy", simulated, "api-token=abc")
    assert inline.code == EXIT_ERROR and "is not NAME=@FILE" in inline.err
    assert endpoint.sessions == []


def test_unlock_needs_an_attestation_first(
    instance: instances.Instance, cli: Cli, endpoint: FakeEndpoint
) -> None:
    refused = cli("secrets", "unlock", instance.id, "data")
    assert refused.code == EXIT_ERROR
    assert "has not passed an attestation yet" in refused.err
    assert endpoint.sessions == []


def test_help_names_the_bound_channel(cli: Cli, capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (("attest", "--help"), ("secrets", "--help"), ("secrets", "deliver", "--help")):
        with pytest.raises(SystemExit):
            cli(*argv)
        said = " ".join(capsys.readouterr().out.split())
        assert "a TLS session whose key the quote binds" in said
