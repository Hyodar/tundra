"""Plan 6.1 on the operator side: production evidence is never simulated or debug, and a
production build in a simulated instance gets nothing, whatever it answers.

The negative tests of the release predicate that need no TDX host are here: simulation,
debug attributes and partial coverage (``test_production_delivery_refuses_*``). A replayed
quote fails the nonce check (``test_attest.py``); the replay and wrong-recipient-key tests
of plan 6.3 need the attested endpoint's channel binding, which is tundra-tools' gate 3
protocol and does not exist yet.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import NONCE, PEER, UNAPPROVED, FakeIssuer, FakeValidator
from tests.cli.test_attest import tdx_quote as quote_bytes
from tests.cli.test_secrets import SIMULATED, TOKEN, FakeSsh
from tundravm import bootstrap, instances
from tundravm.attestation import (
    Attestation,
    attest,
    parse_quote,
    read_attestation,
    report_data_for,
)
from tundravm.builds import Build
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR

VERDICTS = ("trusted", "measurements-match", "partial", "simulated")


def _judged(*, production: bool, debug: bool = False, peer: object = PEER) -> Attestation:
    return attest(
        "tcp://h:1",
        peer,  # type: ignore[arg-type]
        validator="tcp://v:2",
        nonce=NONCE,
        production=production,
        transport=FakeIssuer(debug=debug),
        validator_transport=FakeValidator(),
    )


def _simulated(*, production: bool) -> Attestation:
    return attest(
        "tcp://h:1",
        SIMULATED,
        transport=FakeIssuer("simulator"),
        allow_simulated=True,
        production=production,
    )


def _result(verdict: str) -> Attestation:
    if verdict == "simulated":
        return _simulated(production=False)
    if verdict == "partial":
        return _judged(production=False, peer=UNAPPROVED)
    if verdict == "measurements-match":
        return attest("tcp://h:1", PEER, transport=FakeIssuer())
    return _judged(production=False)


def _instance(tmp_path: Path, mode: str, kind: str) -> instances.Instance:
    return instances.Instance(tmp_path, {"id": "x-1", "mode": mode, "build": {"kind": kind}})


# ── the verifier ─────────────────────────────────────────────────────────


def test_the_quote_carries_the_debug_attribute() -> None:
    bound = report_data_for(b"", NONCE)
    assert parse_quote(quote_bytes(bound, debug=True)).debug
    assert not parse_quote(quote_bytes(bound)).debug


def test_a_production_policy_rejects_debug_evidence() -> None:
    """A debug TD's quote is ``untrusted`` for a production build even when the validator
    accepts it and every register matches; a development check still says ``debug``."""
    production = _judged(production=True, debug=True)
    assert production.debug and production.verdict == "untrusted"
    assert not production.passed and not production.trusted
    assert "debug" in production.reasons
    development = _judged(production=False, debug=True)
    assert development.verdict == "trusted" and development.reasons == ("debug",)


def test_a_production_policy_rejects_simulated_evidence() -> None:
    """``allow_simulated`` never lets simulated evidence pass a production check."""
    production = _simulated(production=True)
    assert production.verdict == "simulated" and not production.passed
    assert _simulated(production=False).passed


def test_the_guest_gate_mode_makes_tdx_evidence_simulated() -> None:
    """A guest whose execution gate found no TDX says ``simulated``: whatever the quote
    claims, the evidence is simulated (the guest's word can only lower it)."""
    result = attest(
        "tcp://h:1",
        PEER,
        validator="tcp://v:2",
        guest_mode="simulated",
        transport=FakeIssuer(),
        validator_transport=FakeValidator(),
    )
    assert result.simulated and result.verdict == "simulated"
    assert result.to_dict()["simulated"] is True and result.to_dict()["guest_mode"] == "simulated"
    tdx = _judged(production=True)
    assert not tdx.simulated and tdx.verdict == "trusted"


def test_a_record_re_derives_debug_and_catches_it_edited() -> None:
    record = _judged(production=True, debug=True).record()
    assert record["debug"] is True and record["production"] is True
    found = read_attestation(record)
    assert found.consistent and found.verdict == "untrusted"
    record["debug"], record["verdict"], record["reasons"] = False, "trusted", []
    edited = read_attestation(record)
    assert not edited.consistent and edited.verdict == "untrusted"
    assert any(problem.startswith("debug:") for problem in edited.inconsistencies)


# ── the release predicate ────────────────────────────────────────────────


def test_production_delivery_refuses_simulated_evidence(tmp_path: Path) -> None:
    """A TDX instance of a production build answering with simulated evidence: refused."""
    found = _instance(tmp_path, "tdx", "production")
    refused = bootstrap.release_predicate(found, _simulated(production=True))
    assert refused == "a TDX instance answered with simulated evidence"


def test_production_delivery_refuses_debug_attributes(tmp_path: Path) -> None:
    found = _instance(tmp_path, "tdx", "production")
    refused = bootstrap.release_predicate(found, _judged(production=True, debug=True))
    assert refused is not None and "DEBUG attribute" in refused
    assert bootstrap.release_predicate(found, _judged(production=True)) is None


def test_production_delivery_refuses_partial_coverage(tmp_path: Path) -> None:
    """Unapproved platform registers leave the verdict ``partial``, which never releases,
    for a production build or a development one."""
    partial = _judged(production=True, peer=UNAPPROVED)
    assert partial.verdict == "partial"
    for kind in ("production", "development"):
        assert bootstrap.release_predicate(_instance(tmp_path, "tdx", kind), partial) is not None


@pytest.mark.parametrize("verdict", VERDICTS)
def test_a_production_build_in_a_simulated_instance_gets_nothing_whatever_it_answers(
    tmp_path: Path, verdict: str
) -> None:
    found = _instance(tmp_path, "simulated", "production")
    refused = bootstrap.release_predicate(found, _result(verdict))
    assert refused is not None and "fail-closed" in refused


@pytest.mark.parametrize("verdict", VERDICTS)
def test_a_development_build_in_a_simulated_instance_accepts_simulated_only(
    tmp_path: Path, verdict: str
) -> None:
    found = _instance(tmp_path, "simulated", "development")
    refused = bootstrap.release_predicate(found, _result(verdict))
    assert (refused is None) == (verdict == "simulated"), refused


# ── deliver and unlock, end to end ───────────────────────────────────────


@pytest.fixture
def production(prover: Path, cli: Cli) -> instances.Instance:
    """A simulated local instance of the prover's production build (no process runs)."""
    assert cli("build").code == EXIT_OK
    build = Build.open((prover / "build/prover/latest-production").resolve())
    found = instances.create(
        prover / ".tundravm" / "instances", build, kind="run", platform="qemu", mode="simulated"
    )
    found.data["ports"] = {"8745": 18745, "22": 2222}
    found.data["attested"] = {"policy": str(prover / "policy.json")}
    found.save()
    return found


def test_deliver_and_unlock_refuse_a_production_build_in_a_simulated_instance(
    production: instances.Instance,
    cli: Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused before the management port, SSH or the passphrase prompt is touched."""
    ssh = FakeSsh(FakeIssuer("simulator"))
    posted: list[str] = []

    def post(url: str, body: bytes) -> int:
        posted.append(url)
        return 200

    monkeypatch.setattr(bootstrap, "_run_ssh", ssh)
    monkeypatch.setattr(bootstrap, "_post", post)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(SIMULATED))
    token = tmp_path / "token.txt"
    token.write_bytes(TOKEN)
    deliver = cli("secrets", "deliver", production.id, "--policy", policy, f"api-token=@{token}")
    assert deliver.code == EXIT_SDK_ERROR
    assert "Nothing sent to prover-1: a production build in a simulated instance" in deliver.err
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"never asked\n")))
    unlock = cli("secrets", "unlock", production.id, "data")
    assert unlock.code == EXIT_SDK_ERROR and "fail-closed" in unlock.err
    assert ssh.calls == [] and posted == []
    events = [
        json.loads(line) for line in (production.path / "events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["delivery-refused", "unlock-refused"]


def test_attest_reads_the_guest_gate_mode_over_the_session(
    prover: Path, cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``attest`` takes ``/run/tundravm/mode`` from the guest; ``simulated`` there makes a
    TDX instance's evidence simulated, which a production build never accepts."""
    assert cli("build").code == EXIT_OK
    build = Build.open((prover / "build/prover/latest-production").resolve())
    found = instances.create(
        prover / ".tundravm" / "instances", build, kind="run", platform="qemu", mode="tdx"
    )
    found.data["ports"] = {"8745": 18745, "22": 2222}
    found.save()

    class Guest(FakeSsh):
        def __call__(self, argv, stdin):  # type: ignore[no-untyped-def]
            result = super().__call__(argv, stdin)
            if "/run/tundravm/mode" in argv[-1]:
                result.stdout = b"simulated\n"
            return result

    monkeypatch.setattr(bootstrap, "_run_ssh", Guest(FakeIssuer()))
    monkeypatch.setattr(bootstrap, "_post", lambda url, body: 200)
    key = tmp_path / "operator"
    key.write_text("private\n")
    key.with_name("operator.pub").write_text("ssh-ed25519 AAAA op@example\n")
    policy = tmp_path / "peer.json"
    policy.write_text(json.dumps(PEER))
    result = cli("attest", found.id, "--policy", policy, "--identity", key, "--json")
    assert result.code != EXIT_OK
    report = json.loads(result.out)
    assert report["verdict"] == "simulated" and report["guest_mode"] == "simulated"
    attested = [json.loads(line) for line in (found.path / "events.jsonl").read_text().splitlines()]
    assert attested[-1]["guest_mode"] == "simulated"
