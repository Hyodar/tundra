"""``tundravm attest`` and ``attest()``: a fake tdxs issuer's quote, a fake validator, a policy."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socketserver
import struct
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tundravm.attestation import (
    ATTESTATION_SCHEMA,
    CHECKS_NOTE,
    SIMULATOR_KEY,
    TRUST_NOTE,
    VERDICTS,
    Attestation,
    AttestationRecord,
    attest,
    parse_quote,
    read_attestation,
    render_attestation,
    report_data_for,
    tagged,
)
from tundravm.errors import AttestationError, MeasurementError, ValidationError

MRTD, R0, R1, R2, R3 = (bytes([n]) * 48 for n in (0x11, 0x20, 0x21, 0x22, 0x23))
NONCE = bytes(range(32))
TAGGED = tagged("operator", b"")
"""What a relayed tdxs hands its issuer for an empty request: the operator's tag over nothing."""


def tdx_quote(
    report_data: bytes,
    *,
    version: int = 4,
    tee_type: int = 0x81,
    rtmrs: tuple[bytes, ...] = (R0, R1, R2, R3),
    mrtd: bytes = MRTD,
    debug: bool = False,
) -> bytes:
    """A synthetic DCAP quote: 48-byte header, TD report body, an empty signature section;
    *debug* sets the TD's ``DEBUG`` attribute (bit 0 of ``TDATTRIBUTES``)."""
    body = bytearray(584)
    body[16:64] = b"\x5e" * 48  # mrseam
    body[120] = 1 if debug else 0  # td_attributes
    body[128:136] = b"\xe7\x02\x06\x00\x00\x00\x00\x00"  # xfam
    body[136:184] = mrtd
    body[232:280] = b"\x0a" * 48  # mrowner
    for index, value in enumerate(rtmrs):
        body[328 + 48 * index : 376 + 48 * index] = value
    body[520:584] = report_data
    header = struct.pack("<HHI", version, 2, tee_type) + bytes(40)
    descriptor = struct.pack("<HI", 2, 584) if version == 5 else b""
    return header + descriptor + bytes(body) + struct.pack("<I", 0)


COVERED = ("MRTD", "RTMR0", "RTMR1", "RTMR2")


def _entry(value: bytes | Mapping[str, object]) -> object:
    if isinstance(value, bytes):
        return value.hex()
    return {k: (v.hex() if isinstance(v, bytes) else v) for k, v in value.items()}


def policy(**registers: bytes | list[bytes | Mapping[str, object]]) -> dict[str, object]:
    """A schema 2 policy accepting *registers* (one value, or a list of accepted entries);
    the covered registers it does not accept are unverifiable."""
    accepted = {
        name: [_entry(v) for v in (value if isinstance(value, list) else [value])]
        for name, value in registers.items()
    }
    return {
        "schema_version": 2,
        "schema": "tundravm.policy/2",
        "scheme": "qemu",
        "tool": "tundravm-measure",
        "tool_version": "1.0",
        "artifact": {"path": "build/default/image.efi", "sha256": "f" * 64},
        "registers": accepted,
        "unverifiable": {n: "test: not predicted" for n in COVERED if n not in accepted},
    }


PEER = {
    **policy(RTMR1=R1, RTMR2=R2),
    "platform_approval": {"MRTD": [MRTD.hex()], "RTMR0": [R0.hex()]},
}
"""A qemu policy whose platform registers the operator approved: fully covered."""
UNAPPROVED = policy(RTMR1=R1, RTMR2=R2)
"""The same policy as a qemu measurement record holds it: no platform approval."""


class FakeIssuer:
    """Answers tdxs ``issue``/``metadata`` envelopes like the ``tdxs`` of ``tools/`` does."""

    def __init__(self, platform: str = "tdx", **quote: object) -> None:
        self.platform = platform
        self.quote = quote
        self.requests: list[Mapping[str, object]] = []
        self.replay: bytes | None = None
        self.report_data: bytes | None = None
        self.caller: str | None = "operator"
        """The tag tdxs gives the relayed request's caller; ``None`` tags nothing."""

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
        if self.caller is not None:
            user_data = tagged(self.caller, user_data)
        nonce = self.replay if self.replay is not None else bytes.fromhex(str(data["nonce"]))
        bound = self.report_data or report_data_for(user_data, nonce)
        raw = (
            bound[:32] + hmac.new(SIMULATOR_KEY, bound[:32], hashlib.sha256).digest()
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
        "MRTD": "match",
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
    assert checks["MRTD"].values == ("99" * 48,) and checks["MRTD"].actual == MRTD.hex()
    assert checks["MRTD"].matched is None
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
        "report_data": (hashlib.sha256(TAGGED + b"\x01" * 32).digest() + bytes(32)).hex(),
        "expected_report_data": report_data_for(TAGGED, NONCE).hex(),
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
    # a simulator's registers are its constants: reported, never compared
    assert result.reasons == ("simulated", "platform-unverified", "image-unverified")
    assert {check.verdict for check in result.registers} == {"unchecked"}
    allowed = attest("tcp://h:1", PEER, transport=FakeIssuer("simulator"), allow_simulated=True)
    assert allowed.verdict == "simulated" and allowed.passed and not allowed.trusted
    with pytest.raises(MeasurementError, match="its tag does not verify"):
        attest("tcp://h:1", PEER, transport=_untagged(FakeIssuer("simulator")))


def _untagged(issuer: FakeIssuer) -> Callable[[Mapping[str, object]], Mapping[str, object]]:
    """*issuer*'s replies with the simulator quote's tag zeroed."""

    def send(request: Mapping[str, object]) -> Mapping[str, object]:
        reply = dict(issuer(request))
        if request["method"] != "issue":
            return reply
        data = dict(reply["data"])  # type: ignore[call-overload]
        document = json.loads(bytes.fromhex(str(data["document"])))
        raw = base64.b64decode(document["raw_quote"])
        document["raw_quote"] = base64.b64encode(raw[:32] + bytes(32)).decode()
        return {"data": {"document": json.dumps(document).encode().hex()}, "error": None}

    return send


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
        **UNAPPROVED,
        "tool": "placeholder",
        "registers": {f"RTMR{i}": ["a" * 64] for i in (1, 2)},
    }
    with pytest.raises(MeasurementError, match="placeholder policy"):
        attest("tcp://h:1", placeholder, transport=FakeIssuer())
    with pytest.raises(ValidationError, match="not a non-empty hex string"):
        attest("tcp://h:1", PEER, nonce="zz", transport=FakeIssuer())


@pytest.mark.parametrize(
    ("tag", "caller", "refused"),
    [
        ("endpoint", "endpoint", None),
        ("signer", None, None),
        ("signer", "endpoint", "issued to 'signer', not 'endpoint'"),
        (None, "endpoint", "carries no caller tag"),
        (None, None, "carries no caller tag"),
    ],
    ids=[
        "the endpoint's",
        "a relay's, any tag",
        "a service's as the endpoint's",
        "untagged",
        "untagged relay",
    ],
)
def test_the_quote_must_carry_the_callers_tag(
    tag: str | None, caller: str | None, refused: str | None
) -> None:
    issuer = FakeIssuer()
    issuer.caller = tag
    if refused is not None:
        with pytest.raises(AttestationError, match=refused) as exc:
            attest("tcp://h:1", PEER, nonce=NONCE, caller=caller, transport=issuer)
        assert exc.value.hint
        return
    result = attest("tcp://h:1", PEER, nonce=NONCE, caller=caller, transport=issuer)
    assert result.caller == tag and result.nonce_matches
    assert f"caller {tag}" in render_attestation(result, "text")
    assert read_attestation(result.record()).attestation.caller == tag


def test_to_dict_is_the_stable_json_shape() -> None:
    payload = attest("tcp://h:1", PEER, nonce=NONCE, transport=FakeIssuer()).to_dict()
    assert sorted(payload) == [
        "caller",
        "certificate",
        "checks",
        "debug",
        "guest_mode",
        "instance_key",
        "issuer",
        "modes",
        "nonce",
        "passed",
        "platform",
        "policy",
        "production",
        "quote_version",
        "reasons",
        "registers",
        "signature_status",
        "simulated",
        "source",
        "trusted",
        "validator",
        "validator_error",
        "verdict",
    ]
    assert payload["checks"] == CHECKS_NOTE
    assert payload["debug"] is payload["simulated"] is payload["production"] is False
    assert payload["signature_status"] == "unchecked" and payload["trusted"] is False
    assert payload["verdict"] in VERDICTS
    assert payload["registers"]["RTMR0"] == {  # type: ignore[index]
        "actual": R0.hex(),
        "expected": [{"label": "operator-approved", "value": R0.hex()}],
        "matched": "operator-approved",
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
    threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    ).start()
    try:
        yield f"tcp://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# ── retained records ─────────────────────────────────────────────────────

OBSERVED = datetime(2026, 10, 17, 9, 30, 5, tzinfo=UTC)


def _trusted(rtmrs: tuple[bytes, ...] = (R0, R1, R2, R3)) -> Attestation:
    return attest(
        "unix:/var/tdxs.sock",
        PEER,
        validator="tcp://verifier:7001",
        nonce=NONCE,
        transport=FakeIssuer(rtmrs=rtmrs),
        validator_transport=FakeValidator(),
        clock=lambda: OBSERVED,
    )


def test_a_record_keeps_the_document_the_policy_and_the_validator_reply(tmp_path: Path) -> None:
    result = _trusted()
    path = result.save(tmp_path / "records" / "att.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == ATTESTATION_SCHEMA and record["trust"] == TRUST_NOTE
    assert record["observed_at"] == "2026-10-17T09:30:05Z"
    assert record["document"] == result.document and bytes.fromhex(record["document"])
    assert record["nonce"] == NONCE.hex() and record["user_data"] == ""
    assert record["report_data"] == {
        "expected": report_data_for(TAGGED, NONCE).hex(),
        "actual": report_data_for(TAGGED, NONCE).hex(),
        "verdict": "match",
    }
    canonical = (json.dumps(PEER, indent=2, sort_keys=True) + "\n").encode()
    assert record["policy"] == {
        "path": None,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "artifact": PEER["artifact"],
        "scheme": "qemu",
    }
    assert record["validator"] == {
        "endpoint": "tcp://verifier:7001",
        "reply": {"data": {"userData": "", "valid": True}, "error": None},
    }
    assert (record["verdict"], record["signature_status"], record["reasons"]) == (
        "trusted",
        "valid",
        [],
    )
    assert record["registers"]["RTMR1"] == {
        "actual": R1.hex(),
        "expected": [R1.hex()],
        "matched": None,
        "verdict": "match",
    }


def test_a_record_round_trips_and_re_derives_offline(tmp_path: Path) -> None:
    result = _trusted()
    path = result.save(tmp_path / "att.json")
    found = read_attestation(path)
    assert isinstance(found, AttestationRecord)
    assert found.consistent and found.inconsistencies == ()
    assert found.verdict == "trusted" and found.observed_at == "2026-10-17T09:30:05Z"
    assert found.artifact_sha256 == "f" * 64 and found.policy_matches is None
    assert found.attestation.record() == result.record()
    assert found.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_attestation(path.read_bytes()).consistent
    assert read_attestation(result.record(), policy=PEER).policy_matches is True


def test_an_edited_register_makes_the_record_inconsistent(tmp_path: Path) -> None:
    record = _trusted(rtmrs=(R0, bytes(48), R2, R3)).record()  # the quote's RTMR1 is wrong
    assert record["verdict"] == "untrusted"
    record["registers"]["RTMR1"]["actual"] = R1.hex()  # type: ignore[index]
    record["registers"]["RTMR1"]["verdict"] = "match"  # type: ignore[index]
    record["verdict"], record["reasons"] = "trusted", []
    found = read_attestation(record)
    assert not found.consistent and found.verdict == "untrusted"
    assert any(p.startswith("registers.RTMR1.actual:") for p in found.inconsistencies)
    assert any(p.startswith("verdict: the record says") for p in found.inconsistencies)


def test_an_edited_expectation_is_caught_against_the_policy(tmp_path: Path) -> None:
    record = _trusted(rtmrs=(R0, bytes(48), R2, R3)).record()
    for register in ("RTMR1",):
        record["registers"][register] = {  # type: ignore[index]
            "actual": bytes(48).hex(),
            "expected": [bytes(48).hex()],
            "matched": None,
            "verdict": "match",
        }
    record["verdict"], record["reasons"] = "trusted", []
    assert read_attestation(record).consistent  # nothing to contradict it without the policy
    found = read_attestation(record, policy=PEER)
    assert found.policy_matches is True and not found.consistent
    assert any(p.startswith("registers.RTMR1.expected:") for p in found.inconsistencies)
    other = read_attestation(record, policy=policy(RTMR0=R0, RTMR1=R1, RTMR2=R3))
    assert other.policy_matches is False


def test_a_forged_validator_reply_is_trusted_only_as_recorded(tmp_path: Path) -> None:
    record = _trusted().record()
    record["validator"] = {"endpoint": "tcp://verifier:7001", "reply": {"data": None, "error": "x"}}
    found = read_attestation(record)
    assert found.verdict == "untrusted"
    assert 'signature_status: the record says "valid", re-derived "invalid"' in (
        found.inconsistencies
    )


def test_records_that_are_not_records_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValidationError, match="not a JSON object") as caught:
        read_attestation(path)
    assert caught.value.hint
    with pytest.raises(ValidationError, match="schema"):
        read_attestation({"schema": "tundravm.attestation/0"})
    record = _trusted().record()
    record["document"] = "zz"
    with pytest.raises(ValidationError, match="not an attestation document"):
        read_attestation(record)


# ── schema 2: several accepted values per register ──────────────────────

OTHER = b"\x77" * 48


def test_a_register_matches_any_accepted_value_and_reports_its_label() -> None:
    peer = policy(
        MRTD=MRTD,
        RTMR0=R0,
        RTMR1=[{"value": OTHER, "label": "old"}, {"value": R1, "firmware": "fw-a", "label": "c3"}],
        RTMR2=[OTHER, R2],
    )
    result = attest("tcp://h:1", peer, nonce=NONCE, transport=FakeIssuer())
    checks = {check.name: check for check in result.registers}
    assert checks["RTMR1"].verdict == "match" and checks["RTMR1"].matched == "fw-a c3"
    assert checks["RTMR1"].values == (OTHER.hex(), R1.hex())
    assert checks["RTMR2"].verdict == "match" and checks["RTMR2"].matched is None
    assert result.verdict == "measurements-match"
    assert f"RTMR1  match      {R1.hex()}  (fw-a c3)" in render_attestation(result, "text")
    registers = json.loads(render_attestation(result, "json"))["registers"]
    assert registers["RTMR1"]["matched"] == "fw-a c3"
    assert registers["RTMR1"]["expected"][1] == {
        "value": R1.hex(),
        "firmware": "fw-a",
        "label": "c3",
    }


def test_mrtd_and_rtmr0_of_different_firmwares_do_not_pair() -> None:
    peer = policy(
        MRTD=[{"value": MRTD, "firmware": "fw-a"}, {"value": OTHER, "firmware": "fw-b"}],
        RTMR0=[{"value": OTHER, "firmware": "fw-a"}, {"value": R0, "firmware": "fw-b"}],
        RTMR1=R1,
        RTMR2=R2,
    )
    result = attest("tcp://h:1", peer, nonce=NONCE, transport=FakeIssuer())
    checks = {check.name: check for check in result.registers}
    assert checks["MRTD"].verdict == "match" and checks["MRTD"].matched == "fw-a"
    assert checks["RTMR0"].verdict == "mismatch" and checks["RTMR0"].matched is None
    assert result.verdict == "measurements-mismatch" and result.reasons == ("register-mismatch",)
    judged = attest(
        "tcp://h:1",
        peer,
        validator="tcp://v:2",
        transport=FakeIssuer(),
        validator_transport=FakeValidator(),
    )
    assert judged.verdict == "untrusted"


def test_a_multi_value_record_round_trips_and_re_derives_offline(tmp_path: Path) -> None:
    peer = policy(
        MRTD=[{"value": OTHER, "firmware": "fw-b"}, {"value": MRTD, "firmware": "fw-a"}],
        RTMR0=[{"value": R0, "firmware": "fw-a", "label": "c3-standard-4"}],
        RTMR1=[OTHER, R1],
        RTMR2=R2,
    )
    result = attest(
        "tcp://h:1",
        peer,
        nonce=NONCE,
        validator="tcp://verifier:7001",
        transport=FakeIssuer(),
        validator_transport=FakeValidator(),
        clock=lambda: OBSERVED,
    )
    assert result.verdict == "trusted"
    path = result.save(tmp_path / "att.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["registers"]["RTMR0"]["matched"] == "fw-a c3-standard-4"
    found = read_attestation(path)
    assert found.consistent and found.verdict == "trusted"
    assert found.attestation.record() == result.record()
    against = read_attestation(path, policy=peer)
    assert against.policy_matches is True and against.consistent


def test_a_schema_1_policy_is_refused_with_a_re_export_hint() -> None:
    old = {
        "schema_version": 1,
        "scheme": "rtmr",
        "tool": "measured-boot",
        "artifact": {"path": "build/default/image.efi", "sha256": "f" * 64},
        "registers": {"RTMR0": R0.hex(), "RTMR1": R1.hex(), "RTMR2": R2.hex()},
    }
    with pytest.raises(ValidationError, match="schema_version 1") as exc:
        attest("tcp://h:1", old, nonce=NONCE, transport=FakeIssuer())
    assert exc.value.hint is not None and "measurements.json" in exc.value.hint
