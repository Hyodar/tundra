"""``run switch`` and the mode history ``attest`` replays, against a fake attested endpoint
whose image has firewall modes (``tundravm.testing.FakeEndpoint(modes=...)``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.conftest import Cli
from tests.cli.test_secrets import SIMULATED, _events, _file, _instance
from tundravm import bootstrap, instances
from tundravm.attestation import mode_history, replay_rtmr3
from tundravm.cli import EXIT_ERROR, EXIT_FAILURE, EXIT_OK
from tundravm.testing import FakeEndpoint

MODES = ("production", "maintenance")


@pytest.fixture
def instance(prover: Path, cli: Cli) -> instances.Instance:
    """A simulated local instance of the prover's dev build (no process runs)."""
    return _instance(prover, cli, mode="simulated", kind="development")


@pytest.fixture
def switchable(monkeypatch: pytest.MonkeyPatch) -> FakeEndpoint:
    fake = FakeEndpoint(modes=MODES)
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    return fake


def test_a_window_opens_closes_and_shows_in_the_evidence(
    instance: instances.Instance, cli: Cli, switchable: FakeEndpoint, tmp_path: Path
) -> None:
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    first = cli("attest", instance.id, "--policy", simulated)
    assert first.code == EXIT_OK, first.err
    assert "modes  production; 0 switches since boot (RTMR3 replays them)" in first.out
    opened = cli("run", "switch", instance.id, "maintenance")
    assert opened.code == EXIT_OK, opened.err
    assert f"{instance.id}: maintenance since 412s of boot, closes at 2212s (switch 1" in opened.out
    switchable.clock = 530
    assert cli("run", "switch", instance.id, "production").code == EXIT_OK
    again = cli("attest", instance.id, "--policy", simulated)
    assert (
        "modes  production; 2 switches since boot: maintenance at 412s, production at 530s "
        "(RTMR3 replays them)" in again.out
    )
    status = cli("run", "status", instance.id)
    assert "firewall       production (2 switches since boot; seen " in status.out
    assert [e["event"] for e in _events(instance)].count("switched") == 2
    strict = _file(tmp_path, "strict.json", json.dumps({**SIMULATED, "modes": ["production"]}))
    refused = cli("attest", instance.id, "--policy", strict)
    assert refused.code == EXIT_FAILURE
    assert "mode-refused" in refused.out and "verdict: measurements-mismatch" in refused.out


@pytest.mark.parametrize(
    ("modes", "argv", "problem"),
    [
        ((), ("maintenance",), "has no firewall modes"),
        (MODES, ("debug",), "no firewall mode 'debug'"),
        (MODES, (), "needs INSTANCE and MODE"),
        (MODES, ("production",), "refused /mode (409)"),
    ],
)
def test_what_a_switch_refuses(
    instance: instances.Instance,
    cli: Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    modes: tuple[str, ...],
    argv: tuple[str, ...],
    problem: str,
) -> None:
    fake = FakeEndpoint(modes=modes)
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    assert cli("attest", instance.id, "--policy", simulated).code == EXIT_OK
    result = cli("run", "switch", instance.id, *argv)
    assert result.code == EXIT_ERROR and problem in result.err, result.err
    assert fake.mode_events == []


def test_a_log_that_does_not_replay_fails_the_attestation(
    instance: instances.Instance, cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeEndpoint(modes=MODES, misbehave="mode-log", mode_events=["tundravm:mode:x:1"])
    monkeypatch.setattr(bootstrap, "_tls", fake.connect)
    simulated = _file(tmp_path, "policy.json", json.dumps(SIMULATED))
    result = cli("attest", instance.id, "--policy", simulated)
    assert result.code == EXIT_FAILURE
    assert "RTMR3 does not replay them" in result.out and "mode-history-mismatch" in result.out


def test_the_history_replays_the_rtmr_extend_chain() -> None:
    lines = ("tundravm:mode:maintenance:412", "tundravm:mode:production:530")
    for given, rtmr3, allowed, ok in (
        ((), bytes(48).hex(), None, True),
        (lines, replay_rtmr3(lines), None, True),
        (lines, replay_rtmr3(lines[:1]), None, False),
        (lines, replay_rtmr3(lines), ("production",), False),
        (lines, None, None, False),
    ):
        assert mode_history(given, rtmr3, allowed).ok is ok, (given, allowed)
