"""``attested``: attestation records bundled by evidence and release, re-derived by verify."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.cli.test_attest import MRTD, NONCE, R0, R1, R2, R3, FakeIssuer, FakeValidator
from tests.helpers import run_main
from tundravm import (
    Attestation,
    Backend,
    Evidence,
    File,
    Package,
    Recipe,
    ValidationError,
    Variant,
    attest,
    bake,
    evidence,
    lock,
    release,
    verify_evidence,
)
from tundravm.declarative import read_artifacts, write_lock
from tundravm.errors import StateError
from tundravm.release import STATE_NAME
from tundravm.verify import EvidenceVerification

EPOCH = 1700000000
CREATED = datetime.fromtimestamp(EPOCH, UTC)


def _recipe() -> Recipe:
    return Recipe(
        "node",
        items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\n")),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )


def _at_created() -> datetime:
    return CREATED


def _mkosi(argv: Any) -> Any:
    return subprocess.CompletedProcess(list(argv), 0, stdout="mkosi 26\n", stderr="")


def _policy(artifact_sha: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "schema": "tundravm.policy/2",
        "scheme": "qemu",
        "tool": "tundravm-measure",
        "tool_version": "1.0",
        "artifact": {"path": "default/image.qcow2", "sha256": artifact_sha},
        "registers": {
            "MRTD": [MRTD.hex()],
            "RTMR0": [R0.hex()],
            "RTMR1": [R1.hex()],
            "RTMR2": [R2.hex()],
        },
        "unverifiable": {},
    }


def _write_policy(path: Path, artifact_sha: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_policy(artifact_sha), indent=2, sort_keys=True) + "\n", "utf-8")
    return path


def _attested(
    policy: Path,
    target: Path,
    *,
    at: datetime = CREATED - timedelta(hours=1),
    validator: bool = True,
    rtmrs: tuple[bytes, ...] = (R0, R1, R2, R3),
) -> Path:
    """Attest a fake TD running the image *policy* measured; keep the record in *target*."""
    found: Attestation = attest(
        "unix:/var/tdxs.sock",
        policy,
        validator="tcp://verifier:7001" if validator else None,
        nonce=NONCE,
        transport=FakeIssuer(rtmrs=rtmrs),
        validator_transport=FakeValidator(),
        clock=lambda: at,
    )
    return found.save(target)


@pytest.fixture
def baked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A baked build whose default variant has a real (not placeholder) policy; records
    are judged at ``CREATED`` (the evidence's wall clock pinned)."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    monkeypatch.setattr(importlib.import_module("tundravm.evidence"), "_wall_clock", _at_created)
    recipe = _recipe()
    build = tmp_path / "build"
    locked = lock(recipe)
    write_lock(locked, build / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=build)
    (artifact,) = [a for a in read_artifacts(build) if a.variant == "default"]
    return build, _write_policy(build / "default" / "policy.json", artifact.sha256)


def _evidence(build: Path, *records: Path, **options: Any) -> Evidence:
    return evidence(
        _recipe(),
        build=build,
        runner=_mkosi,
        variants=["default"],
        attestations=records,
        **options,
    )


def _reasons(found: Evidence) -> list[tuple[str, str, str | None]]:
    return [(r["severity"], r["code"], r["subject"]) for r in found.reasons]


def _problems(result: EvidenceVerification) -> list[tuple[str, str | None, str | None]]:
    return [(c.state, c.code, c.subject) for c in result.problems]


def test_a_trusted_fresh_record_satisfies_attested(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    record = _attested(policy, tmp_path / "att.json")
    found = _evidence(build, record, require=("attested",))
    assert found.passed, found.reasons
    assert found.index["verdict"]["attested"] == "trusted"
    (entry,) = found.index["variants"]["default"]["attestations"]
    (artifact,) = [a for a in read_artifacts(build) if a.variant == "default"]
    assert entry == {
        "member": "variants/default/attestation-1.json",
        "verdict": "trusted",
        "observed_at": "2023-11-14T21:13:20Z",
        "policy_sha256": hashlib.sha256(policy.read_bytes()).hexdigest(),
        "artifact_sha256": artifact.sha256,
        "policy_matches": True,
        "inconsistencies": [],
        "source": str(record),
    }
    assert found.members[entry["member"]] == record.read_bytes()
    directory = found.write(build / "evidence")
    result = verify_evidence(directory, require=("attested",))
    assert result.passed, _problems(result)
    bundle = found.bundle(tmp_path / "evidence.tar.gz")
    assert verify_evidence(bundle, require=("attested",)).passed


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda p, t: _attested(p, t, at=CREATED - timedelta(days=2)), "attestation-stale"),
        (lambda p, t: _attested(p, t, validator=False), "attestation-untrusted"),
        (lambda p, t: _attested(p, t, rtmrs=(R0, R0, R2, R3)), "attestation-untrusted"),
        (None, "attestation-missing"),
    ],
)
def test_stale_untrusted_and_missing_records_leave_attested_incomplete(
    baked: tuple[Path, Path],
    tmp_path: Path,
    make: Callable[[Path, Path], Path] | None,
    code: str,
) -> None:
    build, policy = baked
    records = () if make is None else (make(policy, tmp_path / "att.json"),)
    found = _evidence(build, *records, require=("attested",))
    assert _reasons(found) == [("incomplete", code, "default/qemu")]
    assert found.verdict == "incomplete"
    assert _evidence(build, *records).passed  # not required: a note, not a reason
    directory = found.write(build / "evidence")
    assert ("missing", code, "default/qemu") in _problems(verify_evidence(directory))
    plain = _evidence(build, *records).write(build / "plain")
    assert ("missing", code, "default/qemu") in _problems(
        verify_evidence(plain, require=("attested",))
    )


def test_max_age_widens_the_window_at_evidence_and_at_verify(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    record = _attested(policy, tmp_path / "att.json", at=CREATED - timedelta(days=2))
    found = _evidence(build, record, require=("attested",), max_age=timedelta(days=3))
    assert found.passed
    assert found.index["attestation"] == {
        "max_age_seconds": 3 * 86400,
        "evaluated_at": "2023-11-14T22:13:20Z",
    }
    narrow = _evidence(build, record).write(build / "narrow")
    assert not verify_evidence(narrow, require=("attested",)).passed
    assert verify_evidence(narrow, require=("attested",), max_age=timedelta(days=3)).passed


def test_freshness_is_judged_by_the_wall_clock_not_source_date_epoch(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked  # SOURCE_DATE_EPOCH is CREATED, over a year before the clock
    today = CREATED + timedelta(days=400)
    fresh = _attested(policy, tmp_path / "fresh.json", at=today - timedelta(hours=1))
    found = _evidence(build, fresh, require=("attested",), clock=lambda: today)
    assert found.passed, found.reasons
    assert found.index["created"] == "2023-11-14T22:13:20Z"
    assert found.index["attestation"]["evaluated_at"] == "2024-12-18T22:13:20Z"
    old = _attested(policy, tmp_path / "old.json")  # observed at the old epoch
    stale = _evidence(build, old, require=("attested",), clock=lambda: today)
    assert _reasons(stale) == [("incomplete", "attestation-stale", "default/qemu")]
    directory = found.write(build / "fresh")
    assert verify_evidence(directory, require=("attested",)).passed  # judged as recorded
    later = verify_evidence(directory, require=("attested",), now=today + timedelta(days=2))
    assert ("missing", "attestation-stale", "default/qemu") in _problems(later)


def test_attested_needs_every_selected_variant_measured_and_attested(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked  # azure has no policy
    record = _attested(policy, tmp_path / "att.json")
    both = ["default", "azure"]
    found = evidence(_recipe(), build=build, runner=_mkosi, variants=both, attestations=[record])
    assert found.passed  # not required: notes
    required = evidence(
        _recipe(),
        build=build,
        runner=_mkosi,
        variants=both,
        attestations=[record],
        require=("attested",),
    )
    assert required.verdict == "incomplete"
    reasons = _reasons(required)
    assert ("incomplete", "attestation-missing", "azure") in reasons
    assert ("incomplete", "policy-missing", "azure") in reasons  # attested implies measured
    assert not any(subject == "default/qemu" for _, _, subject in reasons)
    plain = found.write(build / "both")
    problems = _problems(verify_evidence(plain, require=("attested",)))
    assert ("missing", "attestation-missing", "azure") in problems
    assert ("missing", "policy-missing", "azure") in problems


def test_a_record_against_another_policy_is_a_policy_mismatch(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    other = json.loads(policy.read_text())
    other["tool_version"] = "9.9"  # the same artifact and registers, another file
    elsewhere = tmp_path / "other-policy.json"
    elsewhere.write_text(json.dumps(other), encoding="utf-8")
    record = _attested(elsewhere, tmp_path / "att.json")
    found = _evidence(build, record, require=("attested",))
    assert _reasons(found) == [("incomplete", "attestation-policy-mismatch", "default/qemu")]


def test_a_record_of_another_artifact_is_refused(baked: tuple[Path, Path], tmp_path: Path) -> None:
    build, _ = baked
    foreign = _write_policy(tmp_path / "foreign.json", "9" * 64)
    record = _attested(foreign, tmp_path / "att.json")
    with pytest.raises(ValidationError, match="which no selected variant") as caught:
        _evidence(build, record)
    assert caught.value.hint


def _edit_register(path: Path) -> None:
    """Claim the quote's RTMR1 is what the policy expects, as a forger would."""
    record = json.loads(path.read_text(encoding="utf-8"))
    record["registers"]["RTMR1"].update(actual=R1.hex(), verdict="match")
    record["verdict"], record["reasons"] = "trusted", []
    path.write_text(json.dumps(record), encoding="utf-8")


def test_a_tampered_record_fails_evidence_and_verify(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    record = _attested(policy, tmp_path / "att.json", rtmrs=(R0, R0, R2, R3))
    _edit_register(record)
    found = _evidence(build, record, require=("attested",))
    assert ("fail", "attestation-inconsistent", "default") in _reasons(found)
    assert found.verdict == "fail" and found.index["verdict"]["attested"] == "inconsistent"
    assert ("fail", "attestation-inconsistent", "default") in _problems(
        verify_evidence(found.write(build / "evidence"))
    )


def test_verify_re_derives_a_record_swapped_into_the_bundle(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    honest = _evidence(build, _attested(policy, tmp_path / "att.json"))
    directory = honest.write(build / "evidence")
    member = directory / "variants" / "default" / "attestation-1.json"
    bad = _attested(policy, tmp_path / "bad.json", rtmrs=(R0, R0, R2, R3))
    _edit_register(bad)
    shutil.copyfile(bad, member)
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    data = member.read_bytes()
    index["members"][str(member.relative_to(directory))] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")
    problems = _problems(verify_evidence(directory, require=("attested",)))
    assert ("fail", "index-inconsistent", "variants.default.attestations.0.verdict") in problems
    assert (
        "fail",
        "index-inconsistent",
        "variants.default.attestations.0.inconsistencies",
    ) in problems
    assert ("fail", "attestation-inconsistent", "default") in problems


def test_cli_evidence_bundles_records_and_verify_requires_attested(
    baked: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build, policy = baked
    record = _attested(policy, tmp_path / "att.json")
    project = tmp_path / "project"
    project.mkdir()
    (project / "node.py").write_text(
        "from tundravm import File, Fragment, Package, Recipe, Variant\n"
        "recipe = Recipe('node', items=(Package('curl'), "
        "Package('linux-image-amd64'), File('/etc/motd', 'hi\\n')), "
        "variants=(Variant('default', outputs=('qemu',)), "
        "Variant('azure', outputs=('azure',))))\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    argv = ("evidence", "node.py", "--build", str(build), "--variant", "default")
    code, _ = run_main(*argv, "--attestation", str(record), "--require", "attested")
    assert code == 0
    assert run_main("verify", str(build / "evidence"), "--require", "attested")[0] == 0
    later = ("verify", str(build / "evidence"), "--require", "attested", "--now")
    assert run_main(*later, "2030-01-01T00:00:00Z")[0] == 1  # judged then: stale
    with pytest.raises(SystemExit):
        run_main(*later, "soon")
    code, _ = run_main(*argv, "--require", "attested")
    assert code == 1
    with pytest.raises(SystemExit):
        run_main(*argv, "--attestation", str(record), "--max-age", "soon")


def test_release_bundles_records_and_a_resume_inherits_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(EPOCH))
    monkeypatch.setattr(importlib.import_module("tundravm.evidence"), "_wall_clock", _at_created)
    recipe = _recipe()
    lockfile = tmp_path / "build" / "tundravm.lock"
    write_lock(lock(recipe), lockfile)

    def cut(**options: Any) -> Any:
        return release(
            recipe,
            version="1.0",
            backend=Backend("inprocess"),
            variants=["default"],
            releases=tmp_path / "releases",
            build=tmp_path / "build",
            lock=lockfile,
            require=("attested",),
            **options,
        )

    first = cut()
    root = first.path
    assert not first.passed  # simulated: no policy is measured, so nothing can be attested
    codes = {(r["code"], r["subject"]) for r in first.reasons}
    assert {("attestation-missing", "default"), ("policy-missing", "default")} <= codes
    (artifact,) = [a for a in read_artifacts(tmp_path / "build") if a.variant == "default"]
    policy = _write_policy(tmp_path / "build" / "default" / "policy.json", artifact.sha256)
    record = _attested(policy, tmp_path / "att.json", at=CREATED - timedelta(days=2))
    second = cut(attestations=[record])  # the first run gave none: no --change-spec needed
    assert not second.passed
    assert ("attestation-stale", "default/qemu") in {
        (r["code"], r["subject"]) for r in second.reasons
    }
    spec = json.loads((root / STATE_NAME).read_text(encoding="utf-8"))["spec"]
    sha = hashlib.sha256(record.read_bytes()).hexdigest()
    assert spec["attestations"] == [{"path": str(record.resolve()), "sha256": sha}]
    assert "max_age" not in spec and second.spec["attestations"] == spec["attestations"]
    kept = cut()  # inherits the record: the evidence stage is kept
    stage = next(s for s in kept.stages if s.name == "evidence")
    assert stage.resumed and "1 attestations" in stage.summary and not kept.passed
    original = record.read_bytes()
    record.write_bytes(original + b" ")
    with pytest.raises(StateError, match="has changed"):
        cut()
    with pytest.raises(StateError, match="another specification"):
        cut(attestations=[record])
    record.write_bytes(original)
    final = cut(max_age=timedelta(days=3))  # a wider window, first given: evidence runs again
    stage = next(s for s in final.stages if s.name == "evidence")
    assert not stage.resumed and final.passed, final.reasons
    assert final.spec["max_age"] == 3 * 86400
    assert verify_evidence(root / "evidence.tar.gz", require=("attested",)).passed


def test_a_partial_record_leaves_attested_incomplete(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    """An authentic quote whose platform registers no policy value approves is not attested."""
    build, policy = baked
    data = json.loads(policy.read_text(encoding="utf-8"))
    for name in ("MRTD", "RTMR0"):
        del data["registers"][name]
        data["unverifiable"][name] = "qemu: depends on the host's TDVF"
    policy.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")
    record = _attested(policy, tmp_path / "att.json")
    assert json.loads(record.read_text(encoding="utf-8"))["verdict"] == "partial"
    found = _evidence(build, record, require=("attested",))
    assert ("incomplete", "attestation-partial", "default/qemu") in _reasons(found)
    assert found.index["verdict"]["attested"] == "partial"
    directory = found.write(build / "evidence")
    problems = _problems(verify_evidence(directory, require=("attested",)))
    assert ("missing", "attestation-partial", "default/qemu") in problems


def test_an_approved_platform_closes_the_unverifiable_gap_and_is_trusted(
    baked: tuple[Path, Path], tmp_path: Path
) -> None:
    build, policy = baked
    data = json.loads(policy.read_text(encoding="utf-8"))
    data["platform_approval"] = {n: data["registers"].pop(n) for n in ("MRTD", "RTMR0")}
    data["unverifiable"] = {n: "qemu: depends on the host's TDVF" for n in ("MRTD", "RTMR0")}
    policy.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "utf-8")
    record = _attested(policy, tmp_path / "att.json")
    found = _evidence(build, record, require=("attested",))
    assert found.passed, found.reasons
    assert found.index["verdict"]["attested"] == "trusted"
    assert verify_evidence(found.write(build / "evidence"), require=("attested",)).passed
