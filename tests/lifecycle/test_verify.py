"""``verify``: an evidence bundle or directory checked on its own, tampered and signed."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import (
    Backend,
    Evidence,
    EvidenceVerification,
    File,
    Package,
    Recipe,
    Variant,
    bake,
    compile,
    evidence,
    lock,
    verify_evidence,
)
from tundravm import _ssh_sig as ssh_sig
from tundravm.backends import inprocess
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.declarative import read_artifacts, write_lock
from tundravm.declarative.bom import manifest_path
from tundravm.errors import StateError, ValidationError
from tundravm.evidence import _verdict, tree_rows, tree_rows_digest

EPOCH = "1700000000"
NEEDS_SSH = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _recipe() -> Recipe:
    return Recipe(
        "node",
        items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\n")),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )


def _mkosi(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, stdout="mkosi 26\n", stderr="")


@pytest.fixture
def epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", EPOCH)


def _baked(tmp_path: Path) -> tuple[Path, Evidence]:
    recipe = _recipe()
    build = tmp_path / "build"
    locked = lock(recipe)
    write_lock(locked, build / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=build)
    return build, evidence(recipe, build=build, runner=_mkosi)


def _problems(result: EvidenceVerification) -> list[tuple[str, str | None, str | None]]:
    return [(c.state, c.code, c.subject) for c in result.problems]


def test_a_clean_bundle_and_directory_pass_with_their_artifacts(
    tmp_path: Path, epoch: None
) -> None:
    build, found = _baked(tmp_path)
    bundle = found.bundle(tmp_path / "e.tar.gz")
    directory = found.write(build / "evidence")
    for path in (bundle, directory, directory / "evidence.json"):
        result = verify_evidence(path, build=build)
        assert result.passed, result.render()
        kinds = {c.kind for c in result.checks}
        assert kinds == {"member", "index", "artifact", "verdict"}
        assert {n["code"] for n in result.notes} == {"unsigned"}
    result = verify_evidence(bundle)
    assert result.passed
    assert {n["code"] for n in result.notes} == {"unsigned", "artifacts-unchecked"}
    payload = json.loads(result.render("json"))
    assert payload["schema"] == "tundravm.verify/1"
    assert payload["verdict"] == "pass"
    assert payload["recorded"] == {
        "created": "2023-11-14T22:13:20Z",
        "recipe": "node",
        "verdict": "pass",
    }
    subjects = {c["subject"] for c in payload["checks"] if c["kind"] == "index"}
    assert {
        "bake",
        "lint",
        "lockfile",
        "recipe",
        "variants.default",
        "variants.default.artifacts.qemu",
        "variants/default/sbom-qemu.spdx.json",
        "variants/default/tree.json",
    } <= subjects
    assert "verdict pass" in result.render() and 'id="verdict"' in result.html()


def test_tree_json_lists_every_emitted_file_and_its_digests(tmp_path: Path) -> None:
    _, found = _baked(tmp_path)
    manifest = json.loads(found.members["variants/default/tree.json"])
    assert manifest["schema"] == "tundravm.tree/1"
    assert manifest["compiled_variants"] == ["azure", "default"]
    motd = next(row for row in manifest["files"] if row["path"] == "mkosi.extra/etc/motd")
    assert motd["mode"] == "0644" and motd["size"] == 3 and motd["type"] == "file"
    tree = compile(_recipe(), lock=lock(_recipe()))
    assert manifest["tree_digest"] == tree.digest
    both = sorted(
        ((v, row) for v in ("azure", "default") for row in tree_rows(tree, v)),
        key=lambda pair: f"{pair[0]}/{pair[1]['path']}",
    )
    assert tree_rows_digest(both) == tree.digest
    alone = compile(_recipe(), variants=["default"], lock=lock(_recipe()))
    assert manifest["variant_digest"] == alone.digest


def test_a_flipped_byte_in_a_member_fails_it(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    lint = directory / "lint.json"
    data = bytearray(lint.read_bytes())
    data[0] ^= 1
    lint.write_bytes(bytes(data))
    result = verify_evidence(directory)
    assert result.verdict == "fail"
    assert _problems(result) == [("fail", "member-hash-mismatch", "lint.json")]


def test_a_dropped_member_is_missing_and_an_extra_one_fails(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    (directory / "variants" / "azure" / "tree.json").unlink()
    (directory / "notes.txt").write_text("hi\n", encoding="utf-8")
    result = verify_evidence(directory)
    assert result.verdict == "fail"
    assert _problems(result) == [
        ("missing", "member-missing", "variants/azure/tree.json"),
        ("fail", "index-inconsistent", "notes.txt"),
    ]


def test_a_changed_artifact_fails_and_a_gone_one_is_missing(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    bundle = found.bundle(tmp_path / "e.tar.gz")
    copies = tmp_path / "copies"
    shutil.copytree(build, copies)
    artifact = copies / "default" / "disk.qcow2"
    artifact.write_bytes(artifact.read_bytes()[:-1] + b"!")
    (copies / "azure" / "disk.vhd").unlink()
    result = verify_evidence(bundle, build=copies)
    assert _problems(result) == [
        ("missing", "artifact-missing", "azure/azure"),
        ("fail", "artifact-hash-mismatch", "default/qemu"),
    ]
    flat = tmp_path / "flat"
    flat.mkdir()
    shutil.copy(build / "default" / "disk.qcow2", flat / "disk.qcow2")
    shutil.copy(build / "azure" / "disk.vhd", flat / "disk.vhd")
    assert verify_evidence(bundle, build=flat).passed


def test_an_edited_index_is_inconsistent(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["verdict"]["integrity"] = "mismatch"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = verify_evidence(directory)
    assert _problems(result) == [("fail", "index-inconsistent", "recorded")]
    index["verdict"]["integrity"] = "verified"
    index["variants"]["default"]["artifacts"][0]["sha256"] = "0" * 64
    index_path.write_text(json.dumps(index), encoding="utf-8")
    codes = {
        (state, code, subject) for state, code, subject in _problems(verify_evidence(directory))
    }
    assert ("fail", "index-inconsistent", "variants.default.artifacts.qemu.sha256") in codes
    assert ("fail", "index-inconsistent", "variants/default/sbom-qemu.spdx.json") in codes


def _forge(directory: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """Edit evidence.json with *edit*, then store the verdict the edited facts give."""
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    edit(index)
    index["verdict"] = _verdict(index, tuple(index["verdict"]["required"]), [])
    index_path.write_text(json.dumps(index), encoding="utf-8")


def _relist(directory: Path, name: str) -> None:
    """List member *name* in evidence.json with its current sha256 and size."""
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    data = (directory / name).read_bytes()
    index["members"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    index_path.write_text(json.dumps(index), encoding="utf-8")


def test_an_omitted_bake_record_fails_as_a_core_member(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    (directory / "bake-result.json").unlink()

    def drop(index: dict[str, Any]) -> None:
        del index["members"]["bake-result.json"]

    _forge(directory, drop)
    result = verify_evidence(directory)
    assert result.verdict == "fail"
    assert _problems(result) == [("fail", "core-member-missing", "bake-result.json")]
    (directory / "lint.json").unlink()
    _forge(directory, lambda index: index["members"].pop("lint.json"))
    assert ("fail", "core-member-missing", "lint.json") in _problems(verify_evidence(directory))


def test_reproducibility_and_simulated_claims_are_derived_from_the_bake_record(
    tmp_path: Path,
) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    matched: dict[str, Any] = {
        "state": "matched",
        "second_build": ".reproduce",
        "differ": [],
        "error": None,
    }

    def claim(index: dict[str, Any]) -> None:
        index["bake"].update(simulated=False, reproducibility=matched)
        for record in index["variants"].values():
            record["reproducibility"] = "matched"
            for row in record["artifacts"]:
                row["simulated"] = False
        index["verdict"]["required"] = ["reproducible", "real"]

    _forge(directory, claim)
    assert json.loads((directory / "evidence.json").read_text())["verdict"]["overall"] == "pass"
    problems = _problems(verify_evidence(directory))
    assert {
        ("fail", "index-inconsistent", "bake.simulated"),
        ("fail", "index-inconsistent", "bake.reproducibility"),
        ("fail", "index-inconsistent", "variants.default.reproducibility"),
        ("fail", "index-inconsistent", "variants.default.artifacts.qemu.simulated"),
        ("fail", "index-inconsistent", "recorded"),
        ("missing", "reproducibility-unchecked", "default"),
        ("missing", "simulated-artifact", "azure/azure, default/qemu"),
    } <= set(problems)


def test_lint_counts_are_derived_from_lint_json(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    error = {"code": "E999", "severity": "error", "message": "x", "subject": None}
    (directory / "lint.json").write_text(json.dumps([error]), encoding="utf-8")
    _relist(directory, "lint.json")
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "lint.errors"),
        ("fail", "index-inconsistent", "lint.codes"),
        ("fail", "index-inconsistent", "recorded"),
        ("fail", "lint-errors", None),
    ]


def test_only_the_current_evidence_schema_is_read(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["schema"] == "tundravm.evidence/4" and "schema_version" not in index
    index_path.write_text(json.dumps({**index, "schema": "tundravm.evidence/2"}))
    assert _problems(verify_evidence(directory)) == [("fail", "schema-unsupported", "schema")]
    index_path.write_text(json.dumps({**index, "schema_version": 1}))
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "schema_version")
    ]


def test_a_lockfile_swapped_with_its_index_entry_is_caught(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    other = lock(Recipe("node", items=(Package("curl"),)))
    (directory / "tundravm.lock").write_text(other.text(), encoding="utf-8")
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    data = (directory / "tundravm.lock").read_bytes()
    index["members"]["tundravm.lock"] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    index_path.write_text(json.dumps(index), encoding="utf-8")
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "lockfile.sha256"),
        ("fail", "index-inconsistent", "lockfile.bound"),
        ("fail", "index-inconsistent", "variants.azure.sbom.azure.bound"),
        ("fail", "index-inconsistent", "variants.default.sbom.qemu.bound"),
        ("fail", "index-inconsistent", "recorded"),
        ("missing", "lock-unbound", None),
        ("missing", "sbom-unbound", "azure/azure"),
        ("missing", "sbom-unbound", "default/qemu"),
    ]


def test_requirements_are_checked_as_evidence_require_does(tmp_path: Path) -> None:
    _, found = _baked(tmp_path)
    bundle = found.bundle(tmp_path / "e.tar.gz")
    result = verify_evidence(bundle, require=("reproducible", "lock"))
    assert result.verdict == "incomplete"
    assert _problems(result) == [
        ("missing", "reproducibility-unchecked", "azure"),
        ("missing", "reproducibility-unchecked", "default"),
    ]
    assert result.required == ("reproducible", "lock")
    with pytest.raises(ValidationError):
        verify_evidence(bundle, require=("signed",))


def test_a_bundle_member_outside_the_root_fails(tmp_path: Path) -> None:
    _, found = _baked(tmp_path)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for name, data in [*found.files().items(), ("../escape", b"x")]:
            info = tarfile.TarInfo(name if name.startswith("../") else f"evidence/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    path = tmp_path / "odd.tar.gz"
    path.write_bytes(raw.getvalue())
    result = verify_evidence(path)
    assert _problems(result) == [("fail", "index-inconsistent", None)]
    assert not (tmp_path / "escape").exists()


def test_what_is_not_evidence_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        verify_evidence(tmp_path / "nothing.tar.gz")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValidationError, match="holds no evidence.json"):
        verify_evidence(tmp_path / "empty")
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tarball")
    with pytest.raises(ValidationError, match="not a readable tar.gz bundle"):
        verify_evidence(tmp_path / "junk.tar.gz")


# ── signatures ───────────────────────────────────────────────────────────


def _key(tmp_path: Path, name: str) -> tuple[Path, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True
    )
    return key, key.with_suffix(".pub").read_text(encoding="utf-8").strip()


def _allowed(tmp_path: Path, principal: str, public: str) -> Path:
    path = tmp_path / "allowed_signers"
    path.write_text(f'{principal} namespaces="tundravm-evidence" {public}\n', encoding="utf-8")
    return path


@NEEDS_SSH
def test_signed_evidence_verifies_and_tampering_breaks_it(tmp_path: Path, epoch: None) -> None:
    build, found = _baked(tmp_path)
    key, public = _key(tmp_path, "release")
    allowed = _allowed(tmp_path, "release@example.com", public)
    signed = found.sign(key)
    assert signed.signature is not None and b"BEGIN SSH SIGNATURE" in signed.signature
    bundle = signed.bundle(tmp_path / "e.tar.gz")
    with tarfile.open(bundle) as archive:
        assert "evidence/evidence.json.sig" in archive.getnames()
    result = verify_evidence(bundle, allowed_signers=allowed)
    assert result.passed, result.render()
    assert result.signer == "release@example.com"
    assert "1/1 pass, signed by release@example.com" in result.render()
    named = verify_evidence(bundle, allowed_signers=allowed, signer="release@example.com")
    assert named.passed
    stranger = verify_evidence(bundle, allowed_signers=allowed, signer="someone@example.com")
    assert _problems(stranger) == [("fail", "signature-invalid", "evidence.json.sig")]
    directory = signed.write(build / "evidence")
    index_path = directory / "evidence.json"
    index_path.write_text(index_path.read_text(encoding="utf-8").replace("node", "nodf"))
    tampered = verify_evidence(directory, allowed_signers=allowed)
    assert ("fail", "signature-invalid", "evidence.json.sig") in _problems(tampered)
    _, other = _key(tmp_path, "other")
    foreign = verify_evidence(
        bundle, allowed_signers=_allowed(tmp_path, "release@example.com", other)
    )
    assert _problems(foreign) == [("fail", "signature-invalid", "evidence.json.sig")]
    unsigned = found.bundle(tmp_path / "plain.tar.gz")
    missing = verify_evidence(unsigned, allowed_signers=allowed)
    assert _problems(missing) == [("missing", "signature-missing", "evidence.json.sig")]
    assert {n["code"] for n in verify_evidence(bundle).notes} >= {"signature-unchecked"}
    found.write(build / "evidence")
    assert not (directory / "evidence.json.sig").exists()


def test_a_missing_ssh_keygen_is_a_state_error_with_a_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key"
    key.write_text("not used\n", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(StateError) as raised:
        ssh_sig.sign(b"{}", key)
    assert raised.value.hint is not None and "openssh-client" in raised.value.hint
    with pytest.raises(ValidationError, match="does not exist"):
        ssh_sig.sign(b"{}", tmp_path / "absent")


RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "node",
    items=(Package("curl"), Package("linux-image-amd64")),
    variants=(Variant("default", outputs=("qemu",)),),
)
"""


@NEEDS_SSH
def test_cli_signs_evidence_and_verifies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, epoch: None
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    key, public = _key(tmp_path, "release")
    allowed = _allowed(tmp_path, "release@example.com", public)
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe))[0] == EXIT_OK
    code, _ = run_main(
        "evidence", str(recipe), "--require", "", "--sign", str(key), "--bundle", "e.tar.gz"
    )
    assert code == EXIT_OK
    assert (tmp_path / "build" / "evidence" / "evidence.json.sig").is_file()
    code, out = run_main(
        "verify", "e.tar.gz", "--allowed-signers", str(allowed), "--build", "build"
    )
    assert code == EXIT_OK, out
    assert out.startswith("verify e.tar.gz  verdict pass")
    code, out = run_main("verify", "build/evidence", "--format", "json", "--html", "v.html")
    assert code == EXIT_OK
    assert json.loads(out)["schema"] == "tundravm.verify/1"
    assert 'class="banner pass"' in (tmp_path / "v.html").read_text(encoding="utf-8")
    (tmp_path / "build" / "evidence" / "lint.json").write_text("[ ]\n", encoding="utf-8")
    code, out = run_main("verify", "build/evidence", "--require", "reproducible")
    assert code == EXIT_FAILURE
    assert "  fail     member-hash-mismatch  lint.json:" in out
    assert "  missing  reproducibility-unchecked  default:" in out


def test_an_index_of_another_shape_is_inconsistent_not_a_crash(tmp_path: Path) -> None:
    build, found = _baked(tmp_path)
    directory = found.write(build / "evidence")
    index_path = directory / "evidence.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["variants"]["default"] = ["not", "a", "record"]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    result = verify_evidence(directory, build=build)
    assert result.verdict == "fail"
    assert {code for _, code, _ in _problems(result)} == {"index-inconsistent"}
    index_path.write_text("[]", encoding="utf-8")
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "evidence.json")
    ]


def test_the_sbom_is_rederived_from_its_bundled_manifest(tmp_path: Path) -> None:
    recipe = _recipe()
    build = tmp_path / "build"
    locked = lock(recipe)
    write_lock(locked, build / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=build, variants=["default"])
    artifact = read_artifacts(build)[0]
    packages = [{"type": "deb", "name": "curl", "version": "8.5.0-2", "architecture": "amd64"}]
    text = json.dumps({"manifest_version": 1, "config": {}, "packages": packages})
    manifest_path(artifact).write_text(text, encoding="utf-8")
    result = build / "bake-result.json"
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["variants"]["default"]["manifest_digests"]["qemu"] = hashlib.sha256(
        text.encode()
    ).hexdigest()
    result.write_text(json.dumps(payload), encoding="utf-8")
    directory = evidence(recipe, build=build, runner=_mkosi).write(build / "evidence")
    assert verify_evidence(directory).passed
    member = directory / "variants" / "default" / "manifest-qemu.json"
    member.write_text(text.replace("8.5.0-2", "9.9.9-1"), encoding="utf-8")
    _relist(directory, "variants/default/manifest-qemu.json")
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "variants.default.sbom.qemu.bound"),
        ("fail", "index-inconsistent", "variants/default/sbom-qemu.spdx.json"),
        ("fail", "index-inconsistent", "recorded"),
        ("missing", "sbom-unbound", "default/qemu"),
    ]
    member.unlink()
    _forge(directory, lambda index: index["members"].pop("variants/default/manifest-qemu.json"))
    assert ("fail", "core-member-missing", "variants/default/manifest-qemu.json") in _problems(
        verify_evidence(directory)
    )


def _policy(artifact: dict[str, str]) -> dict[str, object]:
    """A schema 2 policy accepting one value for each of MRTD and RTMR0..RTMR2."""
    return {
        "schema_version": 2,
        "scheme": "gcp",
        "tool": "tundravm-measure",
        "tool_version": "1.0",
        "artifact": artifact,
        "registers": {name: ["ab" * 48] for name in ("MRTD", "RTMR0", "RTMR1", "RTMR2")},
        "unverifiable": {},
    }


def test_measurement_coverage_is_derived_from_the_policy(tmp_path: Path) -> None:
    build, _ = _baked(tmp_path)
    artifact = read_artifacts(build)[1]
    policy = _policy({"path": "disk.qcow2", "sha256": artifact.sha256})
    (build / "default" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    found = evidence(_recipe(), build=build, runner=_mkosi, variants=["default"])
    directory = found.write(build / "evidence")
    assert verify_evidence(directory, require=("measured",)).passed

    def claim(index: dict[str, Any]) -> None:
        index["variants"]["default"]["policy"]["measures"]["target"] = "azure"

    _forge(directory, claim)
    assert _problems(verify_evidence(directory)) == [
        ("fail", "index-inconsistent", "variants.default.policy.measures"),
    ]


def test_measurement_through_a_boot_payload_is_rederived_from_the_bake_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(inprocess.TARGET_FILENAMES, "qemu", "disk.efi")
    recipe = Recipe(
        "node",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu", "azure")),),
    )
    build = tmp_path / "build"
    bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), build=build)
    uki = next(a for a in read_artifacts(build) if a.target == "qemu")
    policy = _policy({"path": str(uki.path), "sha256": uki.sha256})
    (build / "default" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    found = evidence(recipe, build=build, runner=_mkosi, require=("measured",))
    assert found.index["verdict"]["measured"] == "full"
    directory = found.write(build / "evidence")
    assert verify_evidence(directory, require=("measured",)).passed

    member = directory / "bake-result.json"
    payload = json.loads(member.read_text(encoding="utf-8"))
    payload["variants"]["default"]["boot_payloads"]["azure"]["sha256"] = "9" * 64
    member.write_text(json.dumps(payload), encoding="utf-8")
    _relist(directory, "bake-result.json")
    problems = _problems(verify_evidence(directory, require=("measured",)))
    assert {
        ("fail", "index-inconsistent", "variants.default.artifacts.azure.boot_payload"),
        ("fail", "index-inconsistent", "variants.default.coverage"),
        ("missing", "artifact-unmeasured", "default/azure"),
    } <= set(problems)
