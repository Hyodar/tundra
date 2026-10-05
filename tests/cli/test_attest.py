"""``tundravm attest`` and ``attest()``: a fake tdxs issuer's quote checked against a policy."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm import Attestation, DeploymentError, MeasurementError, ValidationError, attest
from tundravm.attestation import CHECKS_NOTE, parse_quote, report_data_for
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR

MRTD, R0, R1, R2, R3 = (bytes([n]) * 48 for n in (0x11, 0x20, 0x21, 0x22, 0x23))
NONCE = bytes(range(32))


def tdx_quote(
    report_data: bytes,
    *,
    version: int = 4,
    tee_type: int = 0x81,
    rtmrs: tuple[bytes, ...] = (R0, R1, R2, R3),
    mrtd: bytes = MRTD,
) -> bytes:
    """A synthetic DCAP quote: 48-byte header, TD report body, an empty signature section."""
    body = bytearray(584)
    body[16:64] = b"\x5e" * 48  # mrseam
    body[128:136] = b"\xe7\x02\x06\x00\x00\x00\x00\x00"  # xfam
    body[136:184] = mrtd
    body[232:280] = b"\x0a" * 48  # mrowner
    for index, value in enumerate(rtmrs):
        body[328 + 48 * index : 376 + 48 * index] = value
    body[520:584] = report_data
    header = struct.pack("<HHI", version, 2, tee_type) + bytes(40)
    descriptor = struct.pack("<HI", 2, 584) if version == 5 else b""
    return header + descriptor + bytes(body) + struct.pack("<I", 0)


def policy(**registers: bytes) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "rtmr",
        "tool": "measured-boot",
        "tool_version": "1.2",
        "artifact": {"path": "build/default/image.efi", "sha256": "f" * 64},
        "registers": {name: value.hex() for name, value in registers.items()},
    }


PEER = policy(RTMR0=R0, RTMR1=R1, RTMR2=R2)


class FakeIssuer:
    """Answers tdxs ``issue``/``metadata`` envelopes like ``tundra-tools`` tdxs does."""

    def __init__(self, platform: str = "tdx", **quote: object) -> None:
        self.platform = platform
        self.quote = quote
        self.requests: list[Mapping[str, object]] = []
        self.replay: bytes | None = None

    def __call__(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.requests.append(request)
        data = request["data"]
        assert isinstance(data, Mapping)
        if request["method"] == "metadata":
            zero = (bytes(48)).hex()
            names = ("mrtd", "mrowner", "mrseam", "rtmr0", "rtmr1", "rtmr2", "rtmr3")
            measurements = {name: zero for name in names} | {"xfam": bytes(8).hex()}
            meta = {
                "issuerType": "simulator",
                "userData": "",
                "nonce": "",
                "metadata": measurements,
            }
            return {"data": meta, "error": None}
        user_data = bytes.fromhex(str(data["userData"]))
        nonce = self.replay if self.replay is not None else bytes.fromhex(str(data["nonce"]))
        bound = report_data_for(user_data, nonce)
        raw = (
            bound[:32] + hashlib.sha256(bound[:32]).digest()
            if self.platform == "simulator"
            else tdx_quote(bound, **self.quote)  # type: ignore[arg-type]
        )
        document = {
            "raw_quote": base64.b64encode(raw).decode(),
            "user_data": base64.b64encode(user_data).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "platform": self.platform,
        }
        return {"data": {"document": json.dumps(document).encode().hex()}, "error": None}


# ── quote parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize("version", [4, 5])
def test_parse_quote_reads_the_td_report_body(version: int) -> None:
    quote = parse_quote(tdx_quote(report_data_for(b"", NONCE), version=version))
    assert quote.version == version
    assert quote.mrtd == MRTD.hex()
    assert quote.rtmrs == (R0.hex(), R1.hex(), R2.hex(), R3.hex())
    assert quote.report_data == hashlib.sha256(NONCE).digest() + bytes(32)
    assert quote.mrseam == "5e" * 48
    assert quote.mrowner == "0a" * 48
    assert quote.xfam == "e702060000000000"
    assert quote.registers()["RTMR3"] == R3.hex()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\x04\x00" * 10, "shorter than a TDX quote header"),
        (tdx_quote(bytes(64), tee_type=0), "not TDX"),
        (tdx_quote(bytes(64), version=3), "Unsupported TDX quote version 3"),
        (tdx_quote(bytes(64))[:300], "truncated"),
    ],
)
def test_parse_quote_refuses_what_is_not_a_tdx_quote(raw: bytes, message: str) -> None:
    with pytest.raises(MeasurementError, match=message) as caught:
        parse_quote(raw)
    assert caught.value.hint


# ── attest() with an injected transport ──────────────────────────────────


def test_attest_trusts_matching_registers_and_a_round_tripped_nonce() -> None:
    issuer = FakeIssuer()
    result = attest("unix:/var/tdxs.sock", PEER, nonce=NONCE.hex(), transport=issuer)
    assert isinstance(result, Attestation)
    assert result.trusted and result.verdict == "trusted"
    assert result.nonce_matches
    verdicts = {check.name: check.verdict for check in result.registers}
    assert verdicts == {
        "MRTD": "unchecked",
        "RTMR0": "match",
        "RTMR1": "match",
        "RTMR2": "match",
        "RTMR3": "unchecked",
    }
    assert issuer.requests == [
        {"method": "issue", "data": {"userData": "", "nonce": NONCE.hex()}},
    ]
    assert result.source == "quote" and result.quote_version == 4 and result.platform == "tdx"


def test_attest_checks_mrtd_and_rtmr3_when_the_policy_holds_them() -> None:
    peer = policy(RTMR0=R0, RTMR1=R1, RTMR2=R2, RTMR3=R3, MRTD=b"\x99" * 48)
    result = attest("tcp://h:1", peer, transport=FakeIssuer())
    checks = {check.name: check for check in result.registers}
    assert checks["RTMR3"].verdict == "match"
    assert checks["MRTD"].verdict == "mismatch"
    assert checks["MRTD"].expected == "99" * 48 and checks["MRTD"].actual == MRTD.hex()
    assert not result.trusted


def test_a_replayed_quote_fails_the_nonce_check() -> None:
    issuer = FakeIssuer()
    issuer.replay = b"\x01" * 32
    result = attest("tcp://h:1", PEER, nonce=NONCE, transport=issuer)
    assert not result.nonce_matches
    assert all(check.verdict != "mismatch" for check in result.registers)
    assert result.verdict == "untrusted"
    assert result.to_dict()["nonce"] == {
        "value": NONCE.hex(),
        "report_data": (hashlib.sha256(b"\x01" * 32).digest() + bytes(32)).hex(),
        "verdict": "mismatch",
    }


def test_simulator_registers_come_from_the_metadata_reply() -> None:
    zero = bytes(48)
    result = attest(
        "tcp://h:1",
        policy(RTMR0=zero, RTMR1=zero, RTMR2=zero),
        transport=FakeIssuer("simulator"),
    )
    assert result.source == "metadata" and result.quote_version is None
    assert result.platform == "simulator"
    assert result.trusted


def test_an_issuer_error_is_a_deployment_error() -> None:
    def failing(request: Mapping[str, object]) -> Mapping[str, object]:
        return {"data": None, "error": "issuer error: tdx: failed to get raw quote: no device"}

    with pytest.raises(DeploymentError, match="failed the issue request: issuer error"):
        attest("unix:/var/tdxs.sock", PEER, transport=failing)


def test_placeholder_policies_and_bad_nonces_are_refused() -> None:
    placeholder = {
        **PEER,
        "tool": "placeholder",
        "registers": {f"RTMR{i}": "a" * 64 for i in (0, 1, 2)},
    }
    with pytest.raises(MeasurementError, match="placeholder policy"):
        attest("tcp://h:1", placeholder, transport=FakeIssuer())
    with pytest.raises(ValidationError, match="not a non-empty hex string"):
        attest("tcp://h:1", PEER, nonce="zz", transport=FakeIssuer())


def test_to_dict_is_the_stable_json_shape() -> None:
    payload = attest("tcp://h:1", PEER, nonce=NONCE, transport=FakeIssuer()).to_dict()
    assert sorted(payload) == [
        "checks",
        "endpoint",
        "nonce",
        "platform",
        "policy",
        "quote_version",
        "registers",
        "source",
        "trusted",
        "verdict",
    ]
    assert payload["checks"] == CHECKS_NOTE
    assert payload["registers"]["RTMR0"] == {  # type: ignore[index]
        "actual": R0.hex(),
        "expected": R0.hex(),
        "verdict": "match",
    }


# ── over the wire: fake tdxs servers ─────────────────────────────────────


class _Lines(socketserver.StreamRequestHandler):
    """tdxs's own protocol: one JSON object per line in, one per line out."""

    issuer: FakeIssuer

    def handle(self) -> None:
        for line in self.rfile:
            reply = type(self).issuer(json.loads(line))
            self.wfile.write(json.dumps(reply).encode() + b"\n")


def _handler(issuer: FakeIssuer) -> type[_Lines]:
    class Handler(_Lines):
        pass

    Handler.issuer = issuer
    return Handler


@pytest.fixture
def tcp_endpoint() -> Iterator[tuple[str, FakeIssuer]]:
    issuer = FakeIssuer()
    handler = _handler(issuer)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"tcp://127.0.0.1:{server.server_address[1]}", issuer
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def unix_endpoint(tmp_path: Path) -> Iterator[tuple[str, FakeIssuer]]:
    issuer = FakeIssuer()
    handler = _handler(issuer)
    path = tmp_path / "tdxs.sock"
    server = socketserver.ThreadingUnixStreamServer(str(path), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"unix:{path}", issuer
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def peer_file(tmp_path: Path) -> Path:
    path = tmp_path / "peer.json"
    path.write_text(json.dumps(PEER), encoding="utf-8")
    return path


@pytest.mark.parametrize("endpoint", ["tcp_endpoint", "unix_endpoint"])
def test_cli_attest_is_trusted_over_each_transport(
    endpoint: str, peer_file: Path, request: pytest.FixtureRequest
) -> None:
    url, issuer = request.getfixturevalue(endpoint)
    code, out = run_main(
        "attest", "--endpoint", url, "--policy", str(peer_file), "--nonce", NONCE.hex()
    )
    assert code == EXIT_OK, out
    lines = out.splitlines()
    assert lines[0] == f"attestation {url} (tdx, quote v4)"
    assert lines[1] == f"policy: {peer_file}"
    assert lines[2] == f"nonce  match      {NONCE.hex()}"
    assert lines[3] == f"MRTD   unchecked  {MRTD.hex()}"
    assert lines[4] == f"RTMR0  match      {R0.hex()}"
    assert lines[-2] == f"note: {CHECKS_NOTE}"
    assert lines[-1] == "verdict: trusted"
    assert [r["method"] for r in issuer.requests] == ["issue"]


def test_cli_attest_exits_1_when_untrusted_and_prints_both_values(
    tcp_endpoint: tuple[str, FakeIssuer], peer_file: Path
) -> None:
    url, issuer = tcp_endpoint
    issuer.quote = {"rtmrs": (R0, b"\x77" * 48, R2, R3)}
    code, out = run_main("attest", "--endpoint", url, "--policy", str(peer_file))
    assert code == EXIT_FAILURE
    assert f"RTMR1  mismatch   {'77' * 48}\n       policy     {R1.hex()}" in out
    assert out.rstrip().endswith("verdict: untrusted")


def test_cli_attest_json_and_markdown(
    tcp_endpoint: tuple[str, FakeIssuer], peer_file: Path
) -> None:
    url, _ = tcp_endpoint
    code, out = run_main(
        "attest", "--endpoint", url, "--policy", str(peer_file), "--format", "json"
    )
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["verdict"] == "trusted" and payload["trusted"] is True
    assert payload["policy"] == str(peer_file)
    assert payload["registers"]["RTMR3"] == {
        "actual": R3.hex(),
        "expected": None,
        "verdict": "unchecked",
    }
    code, out = run_main(
        "attest", "--endpoint", url, "--policy", str(peer_file), "--format", "markdown"
    )
    assert code == EXIT_OK
    assert out.startswith(f"# tundravm attest: `{url}`")
    assert "| Register | Verdict | Quote | Policy |" in out
    assert f"| RTMR0 | match | `{R0.hex()}` | `{R0.hex()}` |" in out


def test_cli_attest_exits_2_when_the_issuer_is_unreachable(
    tmp_path: Path, peer_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code, _ = run_main(
        "attest", "--endpoint", f"tcp://127.0.0.1:{port}", "--policy", str(peer_file)
    )
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_DEPLOYMENT]: Cannot reach the tdxs issuer" in err
    assert "ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock" in err
    code, _ = run_main("attest", "--endpoint", "ftp://x", "--policy", str(peer_file))
    assert code == EXIT_SDK_ERROR
    assert "Unsupported endpoint" in capsys.readouterr().err
    for url in ("http://127.0.0.1:7000/tdxs", "https://gateway.example/tdxs"):
        code, _ = run_main("attest", "--endpoint", url, "--policy", str(peer_file))
        assert code == EXIT_SDK_ERROR
        err = capsys.readouterr().err
        assert f"error [E_VALIDATION]: Endpoint {url!r} is http" in err
        assert (
            "tdxs serves a unix socket or tcp; forward it with ssh -L ./tdxs.sock:/var/tdxs.sock"
            in " ".join(err.split())
        )
    code, _ = run_main(
        "attest", "--endpoint", "tcp://h:1", "--policy", str(tmp_path / "missing.json")
    )
    assert code == EXIT_SDK_ERROR
    assert "error [E_VALIDATION]: Cannot read policy" in capsys.readouterr().err


def test_cli_attest_help_says_collateral_is_the_validators_job(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        run_main("attest", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "collateral verification is done by a Tdxs validator" in text
    assert "http" not in text and "--insecure" not in text
