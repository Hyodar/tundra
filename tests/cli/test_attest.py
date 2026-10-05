"""``tundravm attest`` and ``attest()``: a fake tdxs issuer's quote, a fake validator, a policy."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import socketserver
import struct
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm import (
    Attestation,
    AttestationError,
    MeasurementError,
    ValidationError,
    attest,
)
from tundravm.attestation import CHECKS_NOTE, VERDICTS, parse_quote, report_data_for
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
        self.report_data: bytes | None = None

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
        bound = self.report_data or report_data_for(user_data, nonce)
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


class FakeValidator:
    """Answers tdxs ``validate`` envelopes: accepts, rejects, or reports it cannot judge."""

    def __init__(self, mode: str = "accept") -> None:
        self.mode = mode
        self.requests: list[Mapping[str, object]] = []

    def __call__(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self.requests.append(request)
        assert request["method"] == "validate"
        if self.mode == "reject":
            return {"data": None, "error": "validator error: tdx: quote signature invalid"}
        if self.mode == "broken":
            return {"data": None, "error": "validator error: validator not configured"}
        return {"data": {"userData": "", "valid": True}, "error": None}


# ── attest() with an injected transport ──────────────────────────────────


def test_without_a_validator_matching_registers_are_measurements_match_never_trusted() -> None:
    issuer = FakeIssuer()
    result = attest("unix:/var/tdxs.sock", PEER, nonce=NONCE.hex(), transport=issuer)
    assert isinstance(result, Attestation)
    assert result.verdict == "measurements-match"
    assert not result.trusted and result.passed
    assert result.signature_status == "unchecked" and result.validator is None
    assert result.nonce_matches and result.reasons == ()
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


def test_a_validator_that_accepts_makes_matching_registers_trusted() -> None:
    issuer, validator = FakeIssuer(), FakeValidator()
    result = attest(
        "unix:/var/tdxs.sock",
        PEER,
        validator="tcp://verifier:7001",
        nonce=NONCE,
        transport=issuer,
        validator_transport=validator,
    )
    assert result.verdict == "trusted" and result.trusted and result.passed
    assert result.signature_status == "valid" and result.validator_error is None
    [request] = validator.requests
    document = issuer(  # the same document the issuer handed out goes to the validator
        {"method": "issue", "data": {"userData": "", "nonce": NONCE.hex()}}
    )["data"]
    assert request == {
        "method": "validate",
        "data": {"document": document["document"], "nonce": NONCE.hex()},  # type: ignore[index]
    }


def test_a_validator_rejection_is_untrusted_even_when_registers_match() -> None:
    result = attest(
        "tcp://h:1",
        PEER,
        validator="tcp://v:2",
        transport=FakeIssuer(),
        validator_transport=FakeValidator("reject"),
    )
    assert result.verdict == "untrusted" and not result.passed
    assert result.signature_status == "invalid"
    assert result.validator_error == "validator error: tdx: quote signature invalid"
    assert result.reasons == ("validator-rejected",)


def test_a_register_mismatch_is_untrusted_with_a_validator_and_mismatch_without() -> None:
    peer = policy(RTMR0=R0, RTMR1=R1, RTMR2=R2, RTMR3=R3, MRTD=b"\x99" * 48)
    judged = attest(
        "tcp://h:1",
        peer,
        validator="tcp://v:2",
        transport=FakeIssuer(),
        validator_transport=FakeValidator(),
    )
    checks = {check.name: check for check in judged.registers}
    assert checks["RTMR3"].verdict == "match"
    assert checks["MRTD"].verdict == "mismatch"
    assert checks["MRTD"].expected == "99" * 48 and checks["MRTD"].actual == MRTD.hex()
    assert judged.verdict == "untrusted" and judged.reasons == ("register-mismatch",)
    unjudged = attest("tcp://h:1", peer, transport=FakeIssuer())
    assert unjudged.verdict == "measurements-mismatch" and not unjudged.passed


def test_a_replayed_quote_fails_the_nonce_check() -> None:
    issuer = FakeIssuer()
    issuer.replay = b"\x01" * 32
    result = attest("tcp://h:1", PEER, nonce=NONCE, transport=issuer)
    assert not result.nonce_matches
    assert all(check.verdict != "mismatch" for check in result.registers)
    assert result.verdict == "measurements-mismatch"
    assert result.reasons == ("nonce-mismatch",)
    assert result.to_dict()["nonce"] == {
        "value": NONCE.hex(),
        "report_data": (hashlib.sha256(b"\x01" * 32).digest() + bytes(32)).hex(),
        "expected_report_data": report_data_for(b"", NONCE).hex(),
        "verdict": "mismatch",
    }


def test_the_whole_64_byte_report_data_is_compared() -> None:
    issuer = FakeIssuer()
    issuer.report_data = hashlib.sha256(NONCE).digest() + b"\x01" * 32
    result = attest(
        "tcp://h:1",
        PEER,
        validator="tcp://v:2",
        nonce=NONCE,
        transport=issuer,
        validator_transport=FakeValidator(),
    )
    assert not result.nonce_matches
    assert result.report_data.endswith("01" * 32)
    assert result.verdict == "untrusted" and result.reasons == ("nonce-mismatch",)


def test_a_simulator_is_never_trusted() -> None:
    zero = bytes(48)
    peer = policy(RTMR0=zero, RTMR1=zero, RTMR2=zero)
    result = attest(
        "tcp://h:1",
        peer,
        validator="tcp://v:2",
        transport=FakeIssuer("simulator"),
        validator_transport=FakeValidator(),
    )
    assert result.source == "metadata" and result.quote_version is None
    assert result.platform == "simulator" and result.nonce_matches
    assert result.verdict == "simulated" and not result.trusted and not result.passed
    assert result.reasons == ("simulated",)
    allowed = attest("tcp://h:1", peer, transport=FakeIssuer("simulator"), allow_simulated=True)
    assert allowed.verdict == "simulated" and allowed.passed and not allowed.trusted
    mismatched = attest("tcp://h:1", PEER, transport=FakeIssuer("simulator"))
    assert mismatched.verdict == "measurements-mismatch"


def test_issuer_and_validator_failures_are_attestation_errors() -> None:
    def failing(request: Mapping[str, object]) -> Mapping[str, object]:
        return {"data": None, "error": "issuer error: tdx: failed to get raw quote: no device"}

    with pytest.raises(AttestationError, match="failed the issue request: issuer error") as exc:
        attest("unix:/var/tdxs.sock", PEER, transport=failing)
    assert exc.value.code == "E_ATTESTATION" and exc.value.hint
    with pytest.raises(AttestationError, match="could not judge the document") as exc:
        attest(
            "tcp://h:1",
            PEER,
            validator="tcp://v:2",
            transport=FakeIssuer(),
            validator_transport=FakeValidator("broken"),
        )
    assert "--validator" in (exc.value.hint or "")
    with pytest.raises(AttestationError, match="no data.valid flag"):
        attest(
            "tcp://h:1",
            PEER,
            validator="tcp://v:2",
            transport=FakeIssuer(),
            validator_transport=lambda _: {"data": {}, "error": None},
        )


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
        "issuer",
        "nonce",
        "passed",
        "platform",
        "policy",
        "quote_version",
        "reasons",
        "registers",
        "signature_status",
        "source",
        "trusted",
        "validator",
        "validator_error",
        "verdict",
    ]
    assert payload["checks"] == CHECKS_NOTE
    assert payload["signature_status"] == "unchecked" and payload["trusted"] is False
    assert payload["verdict"] in VERDICTS
    assert payload["registers"]["RTMR0"] == {  # type: ignore[index]
        "actual": R0.hex(),
        "expected": R0.hex(),
        "verdict": "match",
    }


# ── over the wire: fake tdxs servers ─────────────────────────────────────


Answer = Callable[[Mapping[str, object]], Mapping[str, object]]


class _Lines(socketserver.StreamRequestHandler):
    """tdxs's own protocol: one JSON object per line in, one per line out."""

    answer: Answer

    def handle(self) -> None:
        for line in self.rfile:
            reply = type(self).answer(json.loads(line))
            self.wfile.write(json.dumps(reply).encode() + b"\n")


def _handler(answer: Answer) -> type[_Lines]:
    class Handler(_Lines):
        pass

    Handler.answer = staticmethod(answer)
    return Handler


@contextmanager
def _tcp(answer: Answer) -> Iterator[str]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _handler(answer))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"tcp://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tcp_endpoint() -> Iterator[tuple[str, FakeIssuer]]:
    issuer = FakeIssuer()
    with _tcp(issuer) as url:
        yield url, issuer


@pytest.fixture
def unix_endpoint(tmp_path: Path) -> Iterator[tuple[str, FakeIssuer]]:
    issuer = FakeIssuer()
    path = tmp_path / "tdxs.sock"
    server = socketserver.ThreadingUnixStreamServer(str(path), _handler(issuer))
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
def test_cli_attest_without_a_validator_is_measurements_match(
    endpoint: str, peer_file: Path, request: pytest.FixtureRequest
) -> None:
    url, issuer = request.getfixturevalue(endpoint)
    code, out = run_main(
        "attest", "--issuer", url, "--policy", str(peer_file), "--nonce", NONCE.hex()
    )
    assert code == EXIT_OK, out
    lines = out.splitlines()
    assert lines[0] == f"attestation {url} (tdx, quote v4)"
    assert lines[1] == f"policy: {peer_file}"
    assert lines[2] == "signature unchecked (no --validator)"
    assert lines[3] == f"nonce  match      {NONCE.hex()}"
    assert lines[4] == f"MRTD   unchecked  {MRTD.hex()}"
    assert lines[5] == f"RTMR0  match      {R0.hex()}"
    assert lines[-2] == f"note: {CHECKS_NOTE}"
    assert lines[-1] == "verdict: measurements-match"
    assert [r["method"] for r in issuer.requests] == ["issue"]


@pytest.mark.parametrize(
    ("mode", "code", "verdict"),
    [("accept", EXIT_OK, "trusted"), ("reject", EXIT_FAILURE, "untrusted")],
)
def test_cli_attest_with_a_validator_socket(
    tcp_endpoint: tuple[str, FakeIssuer], peer_file: Path, mode: str, code: int, verdict: str
) -> None:
    url, _ = tcp_endpoint
    validator = FakeValidator(mode)
    with _tcp(validator) as checker:
        got, out = run_main(
            "attest", "--issuer", url, "--validator", checker, "--policy", str(peer_file)
        )
    assert got == code, out
    assert out.rstrip().endswith(f"verdict: {verdict}")
    assert [r["method"] for r in validator.requests] == ["validate"]
    if mode == "accept":
        assert f"signature valid ({checker})" in out
        assert "note:" not in out
    else:
        assert "signature invalid" in out and "quote signature invalid" in out
        assert "reasons: validator-rejected" in out


def test_cli_attest_exits_1_on_a_mismatch_and_prints_both_values(
    tcp_endpoint: tuple[str, FakeIssuer], peer_file: Path
) -> None:
    url, issuer = tcp_endpoint
    issuer.quote = {"rtmrs": (R0, b"\x77" * 48, R2, R3)}
    code, out = run_main("attest", "--issuer", url, "--policy", str(peer_file))
    assert code == EXIT_FAILURE
    assert f"RTMR1  mismatch   {'77' * 48}\n       policy     {R1.hex()}" in out
    assert "reasons: register-mismatch" in out
    assert out.rstrip().endswith("verdict: measurements-mismatch")


def test_cli_attest_simulated_exits_1_unless_allowed(tmp_path: Path) -> None:
    zero = bytes(48)
    path = tmp_path / "zero.json"
    path.write_text(json.dumps(policy(RTMR0=zero, RTMR1=zero, RTMR2=zero)), encoding="utf-8")
    with _tcp(FakeIssuer("simulator")) as url:
        code, out = run_main("attest", "--issuer", url, "--policy", str(path))
        assert code == EXIT_FAILURE
        assert out.rstrip().endswith("verdict: simulated")
        code, out = run_main("attest", "--issuer", url, "--policy", str(path), "--allow-simulated")
        assert code == EXIT_OK
        assert out.rstrip().endswith("verdict: simulated")


def test_cli_attest_json_and_markdown(
    tcp_endpoint: tuple[str, FakeIssuer], peer_file: Path
) -> None:
    url, _ = tcp_endpoint
    code, out = run_main("attest", "--issuer", url, "--policy", str(peer_file), "--format", "json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["verdict"] == "measurements-match" and payload["trusted"] is False
    assert payload["passed"] is True and payload["signature_status"] == "unchecked"
    assert payload["policy"] == str(peer_file) and payload["issuer"] == url
    assert payload["registers"]["RTMR3"] == {
        "actual": R3.hex(),
        "expected": None,
        "verdict": "unchecked",
    }
    code, out = run_main(
        "attest", "--issuer", url, "--policy", str(peer_file), "--format", "markdown"
    )
    assert code == EXIT_OK
    assert out.startswith(f"# tundravm attest: `{url}`")
    assert "**Verdict:** `measurements-match`" in out
    assert "| Register | Verdict | Quote | Policy |" in out
    assert f"| RTMR0 | match | `{R0.hex()}` | `{R0.hex()}` |" in out


def test_cli_attest_exits_2_when_the_issuer_is_unreachable(
    tmp_path: Path, peer_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    code, _ = run_main("attest", "--issuer", f"tcp://127.0.0.1:{port}", "--policy", str(peer_file))
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_ATTESTATION]: Cannot reach the tdxs endpoint" in err
    assert "ssh -p 2222 -N -L ./tdxs.sock:/var/tdxs.sock" in err
    with _tcp(FakeIssuer()) as url:
        code, _ = run_main(
            "attest",
            "--issuer",
            url,
            "--validator",
            f"tcp://127.0.0.1:{port}",
            "--policy",
            str(peer_file),
        )
    assert code == EXIT_SDK_ERROR
    assert f"error [E_ATTESTATION]: Cannot reach the tdxs endpoint at tcp://127.0.0.1:{port}" in (
        capsys.readouterr().err
    )
    code, _ = run_main("attest", "--issuer", "ftp://x", "--policy", str(peer_file))
    assert code == EXIT_SDK_ERROR
    assert "Unsupported endpoint" in capsys.readouterr().err
    for url in ("http://127.0.0.1:7000/tdxs", "https://gateway.example/tdxs"):
        code, _ = run_main("attest", "--issuer", url, "--policy", str(peer_file))
        assert code == EXIT_SDK_ERROR
        err = capsys.readouterr().err
        assert f"error [E_VALIDATION]: Endpoint {url!r} is http" in err
        assert (
            "tdxs serves a unix socket or tcp; forward it with ssh -L ./tdxs.sock:/var/tdxs.sock"
            in " ".join(err.split())
        )
    code, _ = run_main(
        "attest", "--issuer", "tcp://h:1", "--policy", str(tmp_path / "missing.json")
    )
    assert code == EXIT_SDK_ERROR
    assert "error [E_VALIDATION]: Cannot read policy" in capsys.readouterr().err


def test_cli_attest_help_says_only_a_validator_makes_it_trusted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        run_main("attest", "--help")
    text = " ".join(capsys.readouterr().out.split())
    assert "With --validator a tdxs validator checks the quote's signature" in text
    assert "--endpoint" not in text and "--insecure" not in text
