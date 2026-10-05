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
    File,
    Fragment,
    Package,
    Recipe,
    Variant,
    bake,
    evidence,
    lock,
)
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.declarative import lower, read_artifacts, write_lock

EPOCH = "1700000000"
NAME = "<script>alert(1)</script>"


def _recipe(name: str = "node") -> Recipe:
    return Recipe(
        name,
        Fragment(
            "common",
            items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\n")),
        ),
        variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
    )


def _mkosi(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, stdout="mkosi 26\n", stderr="")


def _baked(tmp_path: Path, recipe: Recipe) -> Path:
    out = tmp_path / "build"
    locked = lock(recipe)
    write_lock(locked, out / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), out=out)
    return out


@pytest.fixture
def epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", EPOCH)


def _evidence(recipe: Recipe, out: Path, **kwargs: object) -> Evidence:
    return evidence(recipe, out=out, runner=_mkosi, **kwargs)  # type: ignore[arg-type]


def test_index_records_every_member_with_its_sha256(tmp_path: Path, epoch: None) -> None:
    recipe = _recipe()
    out = _baked(tmp_path, recipe)
    found = _evidence(recipe, out)
    index = found.index
    assert found.passed
    assert index["created"] == "2023-11-14T22:13:20Z"
    assert index["verdict"] == {
        "integrity": "verified",
        "lint": "clean",
        "lock": "current",
        "overall": "pass",
        "reproducible": "not checked",
    }
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
        "evidence/variants/default/sbom-qemu.spdx.json",
    ]
    assert {(m.mtime, m.uid, m.gid, m.mode) for m in members} == {(int(EPOCH), 0, 0, 0o644)}


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
    assert not found.passed
    assert "tundravm.lock" not in found.members
    assert list(found.index["variants"]) == ["default"]
    assert any(note.startswith("no lockfile") for note in found.notes)
    assert any("no measurements policy for variant default" in note for note in found.notes)


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
    found = evidence(lower(recipe), out=_baked(tmp_path, recipe), runner=_mkosi)
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
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(Variant("default", target="qemu"),),
)
"""


def test_cli_writes_the_directory_bundle_and_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, epoch: None
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe))[0] == EXIT_OK
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
    code, out = run_main("evidence", str(recipe))
    assert code == EXIT_FAILURE
    assert "lock       missing" in out
