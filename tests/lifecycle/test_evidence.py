"""``evidence``: an auditor's record of a bake, its index, bundle and HTML page."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import (
    Backend,
    Evidence,
    ExternalDelivery,
    File,
    Fragment,
    Mkosi,
    Package,
    Recipe,
    Secret,
    SecretEnv,
    SecretFile,
    Secrets,
    Variant,
    bake,
    evidence,
    lock,
    release,
    verify_evidence,
)
from tundravm.backends import InProcessBackend, inprocess
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.declarative import lower, measure, read_artifacts, write_lock
from tundravm.declarative.bom import manifest_path
from tundravm.declarative.lifecycle import select_artifact
from tundravm.errors import ArtifactError, ValidationError
from tundravm.evidence import _secrets
from tundravm.measure.policy import write_policy

EPOCH = "1700000000"
NAME = "<script>alert(1)</script>"


def _recipe(name: str = "node") -> Recipe:
    return Recipe(
        name,
        items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\n")),
        variants=(Variant("default", outputs=("qemu",)), Variant("azure", outputs=("azure",))),
    )


def _mkosi(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, stdout="mkosi 26\n", stderr="")


def _baked(tmp_path: Path, recipe: Recipe) -> Path:
    out = tmp_path / "build"
    locked = lock(recipe)
    write_lock(locked, out / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=out)
    return out


@pytest.fixture
def epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", EPOCH)


def _evidence(recipe: Recipe, out: Path, **kwargs: object) -> Evidence:
    return evidence(recipe, build=out, runner=_mkosi, **kwargs)  # type: ignore[arg-type]


def test_index_records_every_member_with_its_sha256(tmp_path: Path, epoch: None) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    found = _evidence(recipe, out)
    index = found.index
    assert found.passed
    assert index["created"] == "2023-11-14T22:13:20Z"
    assert index["verdict"] == {
        "artifacts": "simulated",
        "attested": "none",
        "integrity": "verified",
        "lint": "clean",
        "lock": "current",
        "measured": "none",
        "overall": "pass",
        "policy": "missing",
        "reasons": [],
        "recipe": "matches",
        "reproducible": "not checked",
        "required": [],
        "sbom": "declared",
        "tree": "matches",
    }
    assert index["attestation"] == {"max_age_seconds": 86400, "evaluated_at": None}
    assert index["variants"]["default"]["attestations"] == []
    assert any("reproducibility of default was not checked" in n for n in found.notes)
    assert any("artifacts are simulated" in n for n in found.notes)
    assert index["tools"]["mkosi"] == "mkosi 26"
    assert index["recipe"]["matches_bake"] is True
    assert index["lockfile"]["version"] == 4
    assert found.members["tundravm.lock"] == (out / "tundravm.lock").read_bytes()
    assert found.members["bake-result.json"] == (out / "bake-result.json").read_bytes()
    assert sorted(index["members"]) == sorted(found.members)
    for name, data in found.members.items():
        assert index["members"][name]["sha256"] == hashlib.sha256(data).hexdigest()
    default = index["variants"]["default"]
    (artifact,) = default["artifacts"]
    assert artifact["integrity"] == "verified"
    assert artifact["sha256"] == read_artifacts(out)[1].sha256
    assert default["tree_matches"] is True
    assert default["provenance"] == {"actions": {"declared": 3}, "fragments": {"common": 3}}
    spdx = json.loads(found.members[default["sbom"][0]["member"]])
    assert spdx["spdxVersion"] == "SPDX-2.3"


def test_bundle_is_deterministic(tmp_path: Path, epoch: None) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    first = _evidence(recipe, out).bundle(tmp_path / "one.tar.gz").read_bytes()
    second = _evidence(recipe, out).bundle(tmp_path / "two.tar.gz").read_bytes()
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        members = archive.getmembers()
    assert [m.name for m in members] == [
        "evidence/bake-result.json",
        "evidence/evidence.json",
        "evidence/lint.json",
        "evidence/tundravm.lock",
        "evidence/variants/azure/sbom-azure.spdx.json",
        "evidence/variants/azure/tree.json",
        "evidence/variants/default/sbom-qemu.spdx.json",
        "evidence/variants/default/tree.json",
    ]
    assert {(m.mtime, m.uid, m.gid, m.mode) for m in members} == {(int(EPOCH), 0, 0, 0o644)}


def test_index_records_the_secret_targets_and_who_delivers_them(tmp_path: Path) -> None:
    recipe = _recipe()
    assert _evidence(recipe, _baked(tmp_path, recipe)).index["variants"]["default"]["secrets"] == []
    token = Secret("token", (SecretFile("/run/token"), SecretEnv("TOKEN", "app")))
    secrets = Secrets(entries=(token,), delivery=ExternalDelivery("vault-agent", ("file",)))
    common = Fragment("common", items=(Package("linux-image-amd64"), secrets))
    rows = _secrets(lower(Recipe("node", items=(common,))), "default")
    assert [(r["kind"], r["location"], r["provider"], r["supported"]) for r in rows] == [
        ("ssh-key", "/root/.ssh/authorized_keys", "tundra-tools", True),
        ("file", "/run/token", "vault-agent", True),
        ("env", "TOKEN", "vault-agent", False),
    ]
    assert [(r["secret"], r["required"]) for r in rows] == [
        (None, True),
        ("token", True),
        ("token", True),
    ]


def test_html_has_the_verdict_banner_and_escapes_values(tmp_path: Path, epoch: None) -> None:
    recipe = _recipe(NAME)
    page = _evidence(recipe, _baked(tmp_path, recipe)).html()
    assert '<div class="banner pass" id="verdict"><strong>verdict: pass</strong>' in page
    assert "<span>integrity: verified</span><span>lock: current</span>" in page
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "http://" not in page and "https://" not in page  # no external assets


def test_missing_lockfile_and_policy_are_notes(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    (out / "tundravm.lock").unlink()
    found = _evidence(recipe, out, variants=["default"])
    assert found.index["lockfile"] is None
    assert found.index["verdict"]["lock"] == "missing"
    assert found.passed and found.reasons == ()
    assert "tundravm.lock" not in found.members
    assert list(found.index["variants"]) == ["default"]
    assert any(note.startswith("no lockfile") for note in found.notes)
    assert any("no measurements policy for variant default" in note for note in found.notes)


def _codes(found: Evidence) -> list[tuple[str, str, str | None]]:
    return [(r["severity"], r["code"], r["subject"]) for r in found.reasons]


def test_required_checks_that_are_missing_make_it_incomplete(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    (out / "tundravm.lock").unlink()
    found = _evidence(
        recipe, out, variants=["default"], require=("lock", "reproducible", "measured", "real")
    )
    assert found.verdict == "incomplete" and not found.passed
    assert found.index["verdict"]["required"] == ["reproducible", "measured", "real", "lock"]
    assert _codes(found) == [
        ("incomplete", "lock-missing", None),
        ("incomplete", "reproducibility-unchecked", "default"),
        ("incomplete", "policy-missing", "default"),
        ("incomplete", "simulated-artifact", "default/qemu"),
    ]
    assert found.reasons[0]["message"].endswith("(required: lock)")
    with pytest.raises(ValidationError, match="Unknown evidence requirement"):
        _evidence(recipe, out, require=("signed",))


def test_recorded_contradictions_fail_with_reasons(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    manifest = out / "bake-result.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in payload["variants"].values():
        entry["declarative"]["recipe_digest"] = "0" * 64
        entry["declarative"]["tree_digest"] = "1" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    read_artifacts(out)[0].path.unlink()
    found = _evidence(recipe, out, require=("lock",))
    assert found.verdict == "fail"
    assert found.index["verdict"]["recipe"] == "changed"
    assert found.index["verdict"]["tree"] == "changed"
    assert _codes(found) == [
        ("fail", "recipe-mismatch", None),
        ("fail", "artifact-missing", "azure/azure"),
        ("fail", "tree-mismatch", "azure"),
        ("fail", "tree-mismatch", "default"),
    ]
    page = found.html()
    assert '<div class="banner fail" id="verdict">' in page
    assert '<ul id="reasons"><li><code>fail recipe-mismatch</code>' in page
    text = found.render()
    assert "  fail        tree-mismatch  default: the compiled tree changed since the bake" in text


def _policy(artifact_sha: str, *, placeholder: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scheme": "rtmr",
        "tool": "placeholder" if placeholder else "measured-boot",
        "tool_version": "1.0",
        "artifact": {"path": "disk.qcow2", "sha256": artifact_sha},
        "registers": {f"RTMR{i}": ("cd" * 32 if placeholder else "ab" * 48) for i in range(3)},
    }


def test_placeholder_or_foreign_policy_fails(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    artifact = read_artifacts(out)[1]
    target = out / "default" / "policy.json"
    target.write_text(json.dumps(_policy(artifact.sha256, placeholder=True)), encoding="utf-8")
    found = _evidence(recipe, out, variants=["default"])
    assert _codes(found) == [("fail", "policy-placeholder", "default")]
    assert found.index["verdict"]["policy"] == "placeholder"
    target.write_text(json.dumps(_policy("9" * 64)), encoding="utf-8")
    found = _evidence(recipe, out, variants=["default"], require=("measured",))
    assert _codes(found) == [("fail", "policy-artifact-mismatch", "default")]
    target.write_text(json.dumps(_policy(artifact.sha256)), encoding="utf-8")
    found = _evidence(recipe, out, variants=["default"], require=("measured",))
    assert found.passed and found.index["verdict"]["policy"] == "verified"


def test_policy_is_copied_and_matched_to_the_artifact(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    artifact = read_artifacts(out)[1]
    policy = {
        "schema_version": 1,
        "scheme": "rtmr",
        "tool": "measured-boot",
        "tool_version": "1.0",
        "artifact": {"path": str(artifact.path), "sha256": artifact.sha256},
        "registers": {f"RTMR{i}": "ab" * 48 for i in range(3)},
    }
    (out / "default" / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    entry = _evidence(recipe, out).index["variants"]["default"]["policy"]
    assert entry["artifact_matches"] is True
    assert entry["placeholder"] is False
    assert entry["registers"] == ["RTMR0", "RTMR1", "RTMR2"]


def test_lowered_recipe_leaves_out_provenance(tmp_path: Path) -> None:
    recipe = _recipe()
    found = evidence(lower(recipe), build=_baked(tmp_path, recipe), runner=_mkosi)
    assert found.index["variants"]["default"]["provenance"] is None
    assert found.index["verdict"]["lock"] == "current"
    assert any("provenance summary is left out" in note for note in found.notes)


def test_changed_artifact_fails_integrity(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    read_artifacts(out)[1].path.write_bytes(b"tampered")
    found = _evidence(recipe, out)
    assert found.index["variants"]["default"]["artifacts"][0]["integrity"] == "mismatch"
    assert found.verdict == "fail"


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


def test_cli_writes_the_directory_bundle_and_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, epoch: None
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "--verify-reproducible")[0] == EXIT_OK
    code, out = run_main(
        "evidence", str(recipe), "--format", "json", "--bundle", "e.tar.gz", "--html", "e.html"
    )
    assert code == EXIT_OK
    index = json.loads(out)
    written = tmp_path / "build" / "evidence"
    assert json.loads((written / "evidence.json").read_text(encoding="utf-8")) == index
    for name, entry in index["members"].items():
        assert hashlib.sha256((written / name).read_bytes()).hexdigest() == entry["sha256"]
    assert (tmp_path / "e.tar.gz").is_file()
    assert 'id="verdict"' in (tmp_path / "e.html").read_text(encoding="utf-8")
    (tmp_path / "build" / "tundravm.lock").unlink()
    code, out = run_main("evidence", str(recipe), "--require", "")
    assert code == EXIT_OK
    assert "lock       missing" in out
    code, out = run_main("evidence", str(recipe))  # requires reproducible,lock by default
    assert code == EXIT_FAILURE
    assert "verdict incomplete" in out
    assert "  required   reproducible, lock" in out
    assert "  incomplete  lock-missing  -: no lockfile" in out


def test_the_bake_records_what_binds_its_sbom(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    payload = json.loads((out / "bake-result.json").read_text(encoding="utf-8"))
    lock_sha = hashlib.sha256((out / "tundravm.lock").read_bytes()).hexdigest()
    assert payload["lock_sha256"] == lock_sha
    assert payload["variants"]["default"]["manifest_digests"] == {"qemu": None}
    assert {a.lock_sha256 for a in read_artifacts(out)} == {lock_sha}
    assert {a.build for a in read_artifacts(out)} == {out}


def test_a_lockfile_other_than_the_bakes_is_refused_unless_allowed(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    other = lock(Recipe("node", items=(Package("curl"),)))
    with pytest.raises(ArtifactError) as raised:
        _evidence(recipe, out, lock=other)
    assert "Evidence would not describe the bake: the lockfile" in str(raised.value)
    assert raised.value.hint is not None and "--allow-unbound" in raised.value.hint
    found = _evidence(recipe, out, lock=other, allow_unbound=True, variants=["default"])
    assert found.verdict == "fail"  # the other lock also drifted from the recipe
    codes = {(r["severity"], r["code"]) for r in found.reasons}
    assert {("incomplete", "lock-unbound"), ("incomplete", "sbom-unbound")} <= codes
    assert found.index["lockfile"]["bound"] is False
    assert found.index["variants"]["default"]["sbom"][0]["bound"] is False


def _record(out: Path, reproducibility: dict[str, object]) -> None:
    manifest = out / "bake-result.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["declarative"]["reproducibility"] = reproducibility
    for name, entry in payload["variants"].items():
        differ = reproducibility.get("differ")
        listed = differ if isinstance(differ, list) else []
        own = [item for item in listed if str(item).startswith(f"{name}/")]
        entry["declarative"]["reproducibility"] = {**reproducibility, "differ": own}
    manifest.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("state", ["running", "error", "interrupted"])
def test_an_unfinished_second_build_is_incomplete_when_required(tmp_path: Path, state: str) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    _record(out, {"state": state, "second_build": ".reproduce", "differ": [], "error": None})
    found = _evidence(recipe, out, variants=["default"])
    assert found.passed and found.index["verdict"]["reproducible"] == state
    assert any(f"did not complete: it is {state}" in note for note in found.notes)
    found = _evidence(recipe, out, variants=["default"], require=("reproducible",))
    assert _codes(found) == [("incomplete", "reproducibility-incomplete", "default")]
    assert found.index["bake"]["reproducibility"]["state"] == state


def test_a_different_second_build_fails_only_its_variant(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    differ = {"state": "different", "second_build": ".reproduce", "differ": ["azure/azure"]}
    _record(out, {**differ, "error": None})
    found = _evidence(recipe, out, require=("reproducible",))
    assert _codes(found) == [("fail", "not-reproducible", "azure")]
    assert found.index["variants"]["default"]["reproducibility"] == "matched"
    assert found.index["verdict"]["reproducible"] == "different"


def _record_manifest(out: Path, versions: dict[str, str]) -> Path:
    """A mkosi manifest beside default/qemu, recorded in bake-result.json as a bake does."""
    artifact = read_artifacts(out)[1]
    packages = [
        {"type": "deb", "name": name, "version": version, "architecture": "amd64"}
        for name, version in versions.items()
    ]
    text = json.dumps({"manifest_version": 1, "config": {}, "packages": packages})
    path = manifest_path(artifact)
    path.write_text(text, encoding="utf-8")
    result = out / "bake-result.json"
    payload = json.loads(result.read_text(encoding="utf-8"))
    digest = hashlib.sha256(text.encode()).hexdigest()
    payload["variants"]["default"]["manifest_digests"]["qemu"] = digest
    result.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_a_recorded_manifest_is_bundled_and_its_loss_unbinds_the_sbom(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    manifest = _record_manifest(out, {"curl": "8.5.0-2", "linux-image-amd64": "6.12.48-1"})
    found = _evidence(recipe, out, require=("installed",))
    (entry,) = found.index["variants"]["default"]["sbom"]
    assert entry["manifest_member"] == "variants/default/manifest-qemu.json"
    assert found.members[entry["manifest_member"]] == manifest.read_bytes()
    assert (entry["bound"], entry["complete"], entry["missing"]) == (True, True, [])
    assert _codes(found) == [("incomplete", "sbom-incomplete", "azure/azure")]
    assert found.index["verdict"]["sbom"] == "declared"
    alone = _evidence(recipe, out, variants=["default"], require=("installed",))
    assert alone.passed and alone.index["verdict"]["sbom"] == "installed"
    manifest.unlink()
    gone = _evidence(recipe, out, variants=["default"])
    (entry,) = gone.index["variants"]["default"]["sbom"]
    assert (entry["bound"], entry["complete"], entry["missing"]) == (False, False, ["manifest"])
    assert entry["manifest_member"] is None
    assert _codes(gone) == [("incomplete", "sbom-material-missing", "default/qemu")]
    assert gone.index["verdict"]["sbom"] == "material-missing"


def test_measurement_coverage_is_per_artifact(tmp_path: Path) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    qemu, azure = read_artifacts(out)[1], read_artifacts(out)[0]
    second = out / "default" / "disk.vhd"
    second.write_bytes(azure.path.read_bytes())
    result = out / "bake-result.json"
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["variants"]["default"]["artifacts"]["azure"] = "default/disk.vhd"
    payload["variants"]["default"]["artifact_digests"]["azure"] = azure.sha256
    payload["variants"]["default"]["manifest_digests"]["azure"] = None
    result.write_text(json.dumps(payload), encoding="utf-8")
    policy = out / "default" / "policy.json"
    policy.write_text(json.dumps(_policy(qemu.sha256)), encoding="utf-8")
    found = _evidence(recipe, out, variants=["default"])
    record = found.index["variants"]["default"]
    assert record["policy"]["measures"] == {"target": "qemu", "sha256": qemu.sha256}
    assert [(c["target"], c["state"]) for c in record["coverage"]] == [
        ("azure", "unmeasured"),
        ("qemu", "measured"),
    ]
    assert record["coverage"][0]["reason"].startswith("its format (.vhd) is not measurable")
    assert found.passed and found.index["verdict"]["measured"] == "partial"
    assert any("default/azure is not measured" in note for note in found.notes)
    required = _evidence(recipe, out, variants=["default"], require=("measured",))
    assert _codes(required) == [("incomplete", "artifact-unmeasured", "default/azure")]


def test_a_subset_of_the_baked_variants_matches_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", EPOCH)
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    _evidence(recipe, out).write(out / "evidence")
    alone = _evidence(recipe, out, variants=["default"])
    assert alone.passed, alone.index["verdict"]["reasons"]
    assert alone.index["variants"]["default"]["tree_matches"] is True
    directory = alone.write(out / "evidence")  # over the evidence of both variants
    assert not (directory / "variants" / "azure").exists()
    assert verify_evidence(directory).passed
    released = release(
        recipe,
        version="1.0",
        backend=InProcessBackend(),
        variants=("default",),
        releases=tmp_path / "releases",
        build=out,
        lock=out / "tundravm.lock",
    )
    assert released.passed and released.variants == ("default",)
    # the release's bake of default keeps azure's entry, with its own tree digest
    assert sorted(a.variant for a in read_artifacts(out)) == ["azure", "default"]
    payload = json.loads((out / "bake-result.json").read_text(encoding="utf-8"))
    assert payload["variants_baked"] == ["default"]
    assert (out / "mkosi" / "azure").is_dir()  # the full bake's tree is still there
    again = _evidence(recipe, out)
    assert again.passed, again.index["verdict"]["reasons"]
    assert again.index["variants"]["default"]["tree_matches"] is True
    assert again.index["variants"]["azure"]["tree_matches"] is True


def _measured_boot(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """A fake ``measured-boot``: its version, or RTMR0..2 written to the output file."""
    if argv[1:] == ["--version"]:
        return subprocess.CompletedProcess(list(argv), 0, stdout="measured-boot 1.0\n")
    rtmr = {str(i): {"expected": "ab" * 48} for i in range(3)}
    Path(argv[2]).write_text(json.dumps({"rtmr": rtmr}), encoding="utf-8")
    return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")


def _uki_and_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Recipe, Path]:
    """One variant baked as the UKI ``disk.efi`` (qemu) and a disk booting it (azure), with
    the policy ``measure`` exports for the UKI."""
    monkeypatch.setitem(inprocess.TARGET_FILENAMES, "qemu", "disk.efi")
    recipe = Recipe(
        "node",
        items=(Package("curl"), Package("linux-image-amd64")),
        variants=(Variant("default", outputs=("qemu", "azure")),),
    )
    out = _baked(tmp_path, recipe)
    monkeypatch.setattr("tundravm.measure.rtmr.shutil.which", lambda tool: tool)
    monkeypatch.setattr("tundravm.measure.rtmr.run_tool", _measured_boot)
    uki = select_artifact(read_artifacts(out), variant="default", target="qemu")
    write_policy(measure(uki, allow_placeholder=True).to_policy(), out / "default" / "policy.json")
    return recipe, out


def test_a_disk_booting_the_measured_uki_is_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe, out = _uki_and_disk(tmp_path, monkeypatch)
    uki, disk = sorted(read_artifacts(out), key=lambda a: a.target != "qemu")
    assert (disk.boot_payload, disk.boot_payload_sha256) == (uki.path, uki.sha256)
    found = _evidence(recipe, out, require=("measured",))
    record = found.index["variants"]["default"]
    via = f"boot payload {uki.sha256[:12]}"
    assert [(c["target"], c["state"], c.get("via")) for c in record["coverage"]] == [
        ("azure", "measured", via),
        ("qemu", "measured", None),
    ]
    assert record["policy"]["measures"]["boot_payload"] == uki.sha256
    assert found.passed and found.index["verdict"]["measured"] == "full"
    assert f"default/azure  measured (via {via})" in found.render()


def test_a_disk_booting_another_uki_is_unmeasured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe, out = _uki_and_disk(tmp_path, monkeypatch)
    result = out / "bake-result.json"
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["variants"]["default"]["boot_payloads"]["azure"]["sha256"] = "9" * 64
    result.write_text(json.dumps(payload), encoding="utf-8")
    found = _evidence(recipe, out, require=("measured",))
    azure = found.index["variants"]["default"]["coverage"][0]
    assert (azure["state"], azure["reason"]) == (
        "unmeasured",
        f"it boots UKI {'9' * 12}, not the one the policy measured",
    )
    assert _codes(found) == [("incomplete", "artifact-unmeasured", "default/azure")]


def _layered(layout: str, motd: str = "hi\n") -> Recipe:
    return Recipe(
        "node",
        items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", motd)),
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant(
                "azure",
                inherits="default",
                add=Fragment("azure-bits", (Package("htop"), File("/etc/b", "b\n"))),
            ),
        ),
        mkosi=Mkosi(layout=layout),  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("layout", ["directories", "native"])
def test_tree_provenance_round_trips_in_both_layouts(
    tmp_path: Path, epoch: None, layout: str
) -> None:
    recipe = _layered(layout)
    out = _baked(tmp_path, recipe)
    found = _evidence(recipe, out)
    assert found.passed, found.reasons
    assert found.index["verdict"]["tree"] == "matches"
    rows = {
        name: {
            row["path"] for row in json.loads(found.members[f"variants/{name}/tree.json"])["files"]
        }
        for name in ("default", "azure")
    }
    inherited = "mkosi.extra/etc/motd" if layout == "native" else None
    if inherited is not None:  # the root is the default variant, and the child inherits it
        assert inherited in rows["default"] and inherited in rows["azure"]
        assert "mkosi.profiles/azure/mkosi.extra/etc/b" in rows["azure"]
        assert not any(path.startswith("mkosi.profiles/azure/") for path in rows["default"])
    assert all(rows.values())
    result = verify_evidence(found.write(tmp_path / "evidence"), build=out)
    assert result.passed, result.problems
    other = _evidence(_layered(layout, motd="changed\n"), out, allow_unbound=True)
    changed = {r["code"]: r["subject"] for r in other.reasons}
    assert changed.get("tree-mismatch") in ("default", "azure")
    assert other.index["variants"]["azure"]["tree_matches"] is False  # it inherits /etc/motd
