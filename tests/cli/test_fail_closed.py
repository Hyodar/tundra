"""Plan 6.1 on the operator side: production evidence is never simulated or debug, and a
production build in a simulated instance gets nothing, whatever it answers.

The negative tests of the release predicate that need no TDX host are here: simulation,
partial coverage and every mode, build kind and verdict (``test_release_predicate``), and
debug attributes (``test_production_delivery_refuses_debug_attributes``). A replayed
quote fails the nonce check (``test_attest.py``); relay, replay and wrong-key against the
attested endpoint's channel binding are ``test_secrets.py::test_delivery_is_refused``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_attest import NONCE, PEER, UNAPPROVED, FakeIssuer, FakeValidator
from tests.cli.test_attest import tdx_quote as quote_bytes
from tests.cli.test_secrets import REGISTERS, SIMULATED, TOKEN
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
from tundravm.testing import FakeEndpoint

FAIL_CLOSED = (
    "a production build in a simulated instance is fail-closed: its services never "
    "start without TDX, and nothing is released to it"
)


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


def _result(verdict: str, *, production: bool) -> Attestation:
    if verdict == "simulated":
        found = _simulated(production=production)
    elif verdict == "partial":
        found = _judged(production=production, peer=UNAPPROVED)
    elif verdict == "measurements-match":
        found = attest("tcp://h:1", PEER, transport=FakeIssuer(), production=production)
    else:
        found = _judged(production=production)
    assert found.verdict == verdict
    return found


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


RELEASES: list[tuple[str, str, str, str | None]] = [
    ("tdx", "production", "trusted", None),
    (
        "tdx",
        "production",
        "measurements-match",
        "production delivery needs trusted, got measurements-match",
    ),
    ("tdx", "production", "partial", "production delivery needs trusted, got partial"),
    ("tdx", "production", "simulated", "a TDX instance answered with simulated evidence"),
    ("tdx", "development", "measurements-match", None),
    ("tdx", "development", "partial", "verdict partial"),
    ("simulated", "production", "trusted", FAIL_CLOSED),
    ("simulated", "production", "measurements-match", FAIL_CLOSED),
    ("simulated", "production", "partial", FAIL_CLOSED),
    ("simulated", "production", "simulated", FAIL_CLOSED),
    ("simulated", "development", "simulated", None),
    ("simulated", "development", "trusted", "a simulated instance answered trusted"),
    (
        "simulated",
        "development",
        "measurements-match",
        "a simulated instance answered measurements-match",
    ),
    ("simulated", "development", "partial", "a simulated instance answered partial"),
]


@pytest.mark.parametrize(
    ("mode", "kind", "verdict", "refused"),
    RELEASES,
    ids=[f"{mode}-{kind}-{verdict}" for mode, kind, verdict, _ in RELEASES],
)
def test_release_predicate(
    tmp_path: Path, mode: str, kind: str, verdict: str, refused: str | None
) -> None:
    """Each mode, build kind and verdict, released (``None``) or refused with the reason:
    simulated evidence and partial coverage never release on TDX, a production build in a
    simulated instance gets nothing whatever it answers, a development one simulated only."""
    found = _instance(tmp_path, mode, kind)
    evidence = _result(verdict, production=kind == "production")
    assert bootstrap.release_predicate(found, evidence) == refused


def test_production_delivery_refuses_debug_attributes(tmp_path: Path) -> None:
    found = _instance(tmp_path, "tdx", "production")
    refused = bootstrap.release_predicate(found, _judged(production=True, debug=True))
    assert refused is not None and "DEBUG attribute" in refused
    assert bootstrap.release_predicate(found, _judged(production=True)) is None


# ── deliver and unlock, end to end ───────────────────────────────────────


@pytest.fixture
def production(prover: Path, cli: Cli) -> instances.Instance:
    """A simulated local instance of the prover's production build (no process runs)."""
    assert cli("lock").code == EXIT_OK
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
    """Refused before the attested endpoint or the passphrase prompt is touched."""
    endpoint = FakeEndpoint()
    monkeypatch.setattr(bootstrap, "_tls", endpoint.connect)
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
    assert endpoint.sessions == []
    events = [
        json.loads(line) for line in (production.path / "events.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in events] == ["delivery-refused", "unlock-refused"]


def test_attest_reads_the_guest_gate_mode_from_the_evidence(
    prover: Path, cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence carries ``/run/tundravm/mode``; ``simulated`` there makes a TDX
    instance's evidence simulated, which a production build never accepts."""
    assert cli("lock").code == EXIT_OK
    assert cli("build").code == EXIT_OK
    build = Build.open((prover / "build/prover/latest-production").resolve())
    found = instances.create(
        prover / ".tundravm" / "instances", build, kind="run", platform="qemu", mode="tdx"
    )
    found.data["ports"] = {"8745": 18745, "22": 2222}
    found.save()
    endpoint = FakeEndpoint(platform="tdx", mode="simulated", registers=REGISTERS)
    monkeypatch.setattr(bootstrap, "_tls", endpoint.connect)
    policy = tmp_path / "peer.json"
    policy.write_text(json.dumps(PEER))
    result = cli("attest", found.id, "--policy", policy, "--json")
    assert result.code != EXIT_OK
    report = json.loads(result.out)
    assert report["verdict"] == "simulated" and report["guest_mode"] == "simulated"
    attested = [json.loads(line) for line in (found.path / "events.jsonl").read_text().splitlines()]
    assert attested[-1]["guest_mode"] == "simulated"
