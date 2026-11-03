"""Trust needs coverage: azure policies are refused, unapproved platform registers are partial."""

from __future__ import annotations

import hashlib
import json
from typing import cast

import pytest

from tests.cli.test_attest import (
    MRTD,
    NONCE,
    PEER,
    R0,
    R1,
    R2,
    UNAPPROVED,
    FakeIssuer,
    FakeValidator,
    policy,
)
from tundravm.attestation import attest, read_attestation
from tundravm.declarative.utils import Tdxs
from tundravm.errors import AttestationError, ValidationError


def _judged(peer: dict[str, object], **kwargs: object):  # type: ignore[no-untyped-def]
    return attest(
        "unix:/var/tdxs.sock",
        peer,
        validator="tcp://v:2",
        nonce=NONCE,
        transport=FakeIssuer(),
        validator_transport=FakeValidator(),
        **kwargs,  # type: ignore[arg-type]
    )


def _azure() -> dict[str, object]:
    reasons = {n: "Azure: the paravisor owns the TD" for n in ("MRTD", "RTMR0", "RTMR1", "RTMR2")}
    return {**policy(), "scheme": "azure", "unverifiable": reasons, "vtpm_pcrs": {"4": "ab"}}


def test_an_azure_policy_is_refused_even_with_an_accepting_validator() -> None:
    validator = FakeValidator()
    with pytest.raises(AttestationError, match="azure policy") as exc:
        attest(
            "unix:/x",
            _azure(),
            validator="tcp://v:2",
            transport=FakeIssuer(),
            validator_transport=validator,
        )
    assert "vTPM evidence" in str(exc.value.hint) and "not support" in str(exc.value.hint)
    assert validator.requests == []


def test_an_azure_policy_filled_with_tdx_registers_is_never_trusted_offline() -> None:
    record = _judged(PEER).record()
    full = policy(MRTD=MRTD, RTMR0=R0, RTMR1=R1, RTMR2=R2)
    forged = {**PEER, "scheme": "azure", "unverifiable": {}, "registers": full["registers"]}
    canonical = (json.dumps(forged, indent=2, sort_keys=True) + "\n").encode()
    stored = cast(dict[str, object], record["policy"])
    record["policy"] = {**stored, "sha256": hashlib.sha256(canonical).hexdigest()}
    found = read_attestation(record, policy=forged)
    assert found.policy_matches is True
    assert found.verdict == "partial" and "vtpm-unverified" in found.attestation.reasons


def test_an_unapproved_platform_with_an_accepting_validator_is_partial() -> None:
    result = _judged(UNAPPROVED)
    assert result.signature_status == "valid" and result.nonce_matches
    assert result.verdict == "partial" and not result.trusted and not result.passed
    assert result.reasons == ("platform-unverified",)
    accepted = _judged(UNAPPROVED, accept_partial=True)
    assert accepted.passed and not accepted.trusted and accepted.verdict == "partial"


def test_hiding_an_image_register_as_unverifiable_is_partial_not_trusted() -> None:
    hidden = {**PEER, **policy(RTMR2=R2)}
    hidden["platform_approval"] = PEER["platform_approval"]
    result = _judged(hidden)
    assert result.verdict == "partial" and result.reasons == ("image-unverified",)


def test_without_a_validator_an_unapproved_platform_is_partial_too() -> None:
    result = attest("unix:/x", UNAPPROVED, nonce=NONCE, transport=FakeIssuer())
    assert result.verdict == "partial" and not result.passed
    assert set(result.reasons) == {"platform-unverified"}


def test_a_full_policy_with_an_accepting_validator_is_trusted() -> None:
    assert _judged(PEER).verdict == "trusted"
    gcp_like = {**policy(MRTD=MRTD, RTMR0=R0, RTMR1=R1, RTMR2=R2), "scheme": "gcp"}
    assert _judged(gcp_like).verdict == "trusted"


def test_a_wrong_platform_approval_is_a_mismatch() -> None:
    wrong = {**UNAPPROVED, "platform_approval": {"MRTD": ["99" * 48], "RTMR0": [R0.hex()]}}
    result = _judged(wrong)
    assert result.verdict == "untrusted" and result.reasons == ("register-mismatch",)


@pytest.mark.parametrize(
    "approval",
    [{"RTMR1": [R1.hex()]}, {"MRTD": ["zz"]}, {"MRTD": []}, {}, ["MRTD"]],
)
def test_a_malformed_platform_approval_is_refused(approval: object) -> None:
    with pytest.raises(ValidationError, match="platform_approval|MRTD"):
        attest("unix:/x", {**UNAPPROVED, "platform_approval": approval}, transport=FakeIssuer())


def test_a_version_1_record_is_refused_with_a_re_attest_hint() -> None:
    record = {**_judged(PEER).record(), "schema": "tundravm.attestation/1"}
    with pytest.raises(ValidationError, match="did not require the platform") as exc:
        read_attestation(record)
    assert "Attest again" in str(exc.value.hint)


def test_a_version_2_record_is_refused_for_its_untagged_quote() -> None:
    record = {**_judged(PEER).record(), "schema": "tundravm.attestation/2"}
    with pytest.raises(ValidationError, match="no caller tag") as exc:
        read_attestation(record)
    assert "Attest again" in str(exc.value.hint)


def test_tdxs_from_policy_checks_the_approved_platform_values() -> None:
    verifier = Tdxs.from_policy(PEER)
    assert verifier.expected_measurements == {
        "mrtd": MRTD.hex(),
        "rtmr0": R0.hex(),
        "rtmr1": R1.hex(),
        "rtmr2": R2.hex(),
    }
