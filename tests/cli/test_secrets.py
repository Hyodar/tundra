"""``attest`` and ``secrets deliver|unlock`` against a fake attested endpoint
(``tundravm.testing.FakeEndpoint``): the channel binding of plan 6.3 and its refusals."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import MRTD, PEER, R0, R1, R2, R3, policy
from tundravm import bootstrap, instances
from tundravm._ssh_sig import fingerprint as fingerprint_of
from tundravm.builds import Build
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, EXIT_OK
from tundravm.testing import FakeEndpoint
from tundravm.testing._endpoint import HOST_KEY, INSTANCE_KEY, der_certificate, listener_key

SIMULATED = policy(RTMR0=bytes(48), RTMR1=bytes(48), RTMR2=bytes(48))
TOKEN = b"s3cret-token-value"
PASSPHRASE = b"correct horse battery staple"
REGISTERS = {"MRTD": MRTD, "RTMR0": R0, "RTMR1": R1, "RTMR2": R2, "RTMR3": R3}
LISTENER = listener_key()
"""The key of the prover's AttestedTLS(443) listener, which the fake's evidence binds."""


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
            "a service's quote",
            "service-quote",
            ("simulated", "development"),
            {},
            "issued to 'signer', not 'endpoint'",
        ),
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


def test_attest_tls_checks_the_certificate_against_the_same_evidence(
    instance: instances.Instance,
    cli: Cli,
    endpoint: FakeEndpoint,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--tls PORT`` prints the listener's fingerprint, checked on the one session's evidence."""
    endpoint.listeners = {443: LISTENER}
    monkeypatch.setattr(bootstrap, "_presented", endpoint.presented)
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    result = cli("attest", instance.id, "--policy", simulated, "--tls", "443")
    assert result.code == EXIT_OK, result.out + result.err
    fingerprint = "sha256:" + hashlib.sha256(der_certificate(LISTENER)).hexdigest()
    assert f"certificate   :443  {fingerprint}  (key bound to the quote)" in result.out
    assert "verdict: simulated" in result.out and len(endpoint.sessions) == 1
    assert _events(instance)[-1]["certificate"] == [443, fingerprint]
    report = cli("attest", instance.id, "--policy", simulated, "--tls", "443", "--json")
    assert f'"fingerprint": "{fingerprint}"' in report.out and '"port": 443' in report.out


@pytest.mark.parametrize(
    ("contract", "misbehave", "build", "endpoint_kwargs", "code", "said"),
    [
        (
            "wrong certificate",
            "wrong-certificate",
            ("simulated", "development"),
            {},
            EXIT_ERROR,
            "but the quote binds key sha256:",
        ),
        (
            "certificate bound, evidence for another nonce",
            "replay",
            ("simulated", "development"),
            {},
            EXIT_ERROR,
            "replayed evidence",
        ),
        (
            "simulated evidence against a production policy",
            None,
            ("tdx", "production"),
            {"mode": "tdx"},
            EXIT_FAILURE,
            "verdict: simulated",
        ),
    ],
)
def test_attest_tls_is_refused(
    prover: Path,
    cli: Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
    misbehave: str | None,
    build: tuple[Literal["simulated", "tdx"], str],
    endpoint_kwargs: dict[str, object],
    code: int,
    said: str,
) -> None:
    """A certificate the binding does not vouch for, or evidence that is not this session's,
    is a refusal; simulated evidence never passes a production build: nothing is written."""
    del contract
    found = _instance(prover, cli, mode=build[0], kind=build[1])
    fake = FakeEndpoint(misbehave=misbehave, listeners={443: LISTENER}, **endpoint_kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    monkeypatch.setattr(bootstrap, "_presented", fake.presented)
    if misbehave == "replay":
        fake.connect("h", 1).request("GET", "/evidence?nonce=" + "11" * 32)
    peer = PEER if build[1] == "production" else SIMULATED
    path = _file(tmp_path, "policy.json", json.dumps(peer))
    result = cli("attest", found.id, "--policy", path, "--tls", "443")
    assert result.code == code, result.out + result.err
    assert said in " ".join((result.out + result.err).split())
    assert "attested" not in json.loads((found.path / "instance.json").read_text())
    assert not instances.known_hosts(found).exists()


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


def _keypair(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A real ed25519 key pair (``ssh-keygen`` signs with it) and its public line."""
    path = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return path, " ".join(path.with_name(f"{name}.pub").read_text().split()[:2])


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")
@pytest.mark.parametrize(
    ("case", "owner", "recorded", "refusal"),
    [
        ("the first signed write pins its signer", None, None, None),
        ("the owner's key", "a", "a", None),
        ("another key is refused before anything is sent", "b", None, "is owned by SHA256:"),
        ("a volume back without its owner", None, "a", "came back without an owner"),
        ("a header the volume's own copy contradicts", "a", None, "owner token does not match"),
    ],
)
def test_an_operator_key_volume_takes_its_owners_writes_only(
    instance: instances.Instance,
    cli: Cli,
    endpoint: FakeEndpoint,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    owner: str | None,
    recorded: str | None,
    refusal: str | None,
) -> None:
    keys = {name: _keypair(tmp_path, name) for name in ("a", "b")}
    endpoint.pins = True
    endpoint.owner = None if owner is None else keys[owner][1]
    if "contradicts" in case:
        endpoint.mismatch = "volume data: owner token does not match the volume's owner"
    if recorded is not None:
        instance.data["owner"] = fingerprint_of(keys[recorded][1])
        instance.save()
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    attested = cli("attest", instance.id, "--policy", simulated, "--identity", keys["a"][0])
    if refusal is not None:
        assert attested.code == EXIT_ERROR and refusal in attested.err, case
        assert "fix: " in attested.err and endpoint.signers == []
        return
    assert attested.code == EXIT_OK, attested.err
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(PASSPHRASE + b"\n")))
    unlocked = cli("secrets", "unlock", instance.id, "data")
    assert unlocked.code == EXIT_OK, unlocked.err
    assert endpoint.signers == [keys["a"][1]] * 2 and endpoint.owner == keys["a"][1]
    fingerprint = fingerprint_of(keys["a"][1])
    assert f"owner: {fingerprint}" in unlocked.out
    status = json.loads(cli("run", "status", instance.id, "--json").out)
    assert status["owner"] == fingerprint


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")
def test_rotate_hands_the_volumes_to_a_key_that_proves_itself(
    instance: instances.Instance, cli: Cli, endpoint: FakeEndpoint, tmp_path: Path
) -> None:
    (old, old_public), (new, new_public) = (_keypair(tmp_path, n) for n in ("old", "new"))
    endpoint.pins, endpoint.owner = True, old_public
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    assert cli("attest", instance.id, "--policy", simulated, "--identity", old).code == EXIT_OK
    rotated = cli("secrets", "rotate", instance.id, "--identity", new)
    assert rotated.code == EXIT_OK, rotated.err
    fingerprint = fingerprint_of(new_public)
    assert endpoint.owner == new_public and endpoint.signers[-1] == old_public
    assert f"is owned by {fingerprint}" in rotated.out
    receipt = json.loads((instance.path / "instance.json").read_text())
    assert receipt["owner"] == fingerprint and receipt["operator"]["identity"] == str(new)
