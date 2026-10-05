"""``tundravm sbom``: formats, ``--output``, ``--lockfile`` and a bake without a manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tests.lifecycle.test_sbom import MANIFEST
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Install,
    Package,
    Recipe,
    lock,
    read_artifacts,
    write_lock,
)

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "node",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)
"""

COMMIT = "c" * 40


@pytest.fixture
def baked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``build/`` after a lock and an in-process bake of both variants (no mkosi manifest)."""
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "--variant", "default", "--variant", "azure")[0] == 0
    return tmp_path / "build"


def _write_manifest(build: Path) -> None:
    """mkosi's manifest beside the default variant's artifact, as a real bake leaves it."""
    (artifact,) = read_artifacts(build)[1:]
    path = artifact.path.parent / "default.manifest"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")


def test_sbom_defaults_to_spdx_json(baked: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_manifest(baked)
    code, out = run_main("sbom", str(baked / "bake-result.json"), "--variant", "default")
    assert code == EXIT_OK
    document = json.loads(out)
    assert document["spdxVersion"] == "SPDX-2.3"
    names = {p["name"] for p in document["packages"]}
    assert {"default", "bsdutils", "systemd"} <= names
    assert "no mkosi package manifest" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("format", "marker"),
    [
        ("cyclonedx-json", '"bomFormat": "CycloneDX"'),
        ("text", "sbom default\n"),
        ("markdown", "## SBOM: default"),
    ],
)
def test_sbom_formats(baked: Path, format: str, marker: str) -> None:
    _write_manifest(baked)
    code, out = run_main("sbom", "--out", str(baked), "--variant", "default", "--format", format)
    assert code == EXIT_OK
    assert marker in out


def test_sbom_output_writes_the_file(
    baked: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_manifest(baked)
    target = tmp_path / "sbom" / "default.cdx.json"
    code, out = run_main(
        "sbom", "--variant", "default", "--format", "cyclonedx-json", "--output", str(target)
    )
    assert code == EXIT_OK and out == ""
    assert json.loads(target.read_text(encoding="utf-8"))["specVersion"] == "1.5"
    assert f"wrote cyclonedx-json {target}" in capsys.readouterr().err


def test_sbom_without_manifest_notes_it_and_lists_declared_packages(
    baked: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    code, out = run_main("sbom", str(baked), "--variant", "azure", "--format", "text")
    assert code == EXIT_OK
    assert "packages (3)" in out
    assert "  dmidecode          -        amd64  declared" in out
    assert "base             debian/trixie" in out
    assert f"note: no mkosi package manifest at {baked / 'azure' / 'azure.manifest'}" in out
    assert capsys.readouterr().err == ""
    code, out = run_main("sbom", str(baked), "--variant", "azure")
    assert code == EXIT_OK
    assert "no mkosi package manifest" in json.loads(out)["creationInfo"]["comment"]
    assert "note: no mkosi package manifest at" in capsys.readouterr().err


def test_sbom_lockfile_flag_adds_source_pins(baked: Path, tmp_path: Path) -> None:
    recipe = Recipe(
        "node",
        Fragment(
            "common",
            items=(
                Package("curl"),
                Build(
                    "app",
                    Git("https://example.com/app.git", "v1"),
                    script="make",
                    install=(Install("app", "/usr/bin/app"),),
                ),
            ),
        ),
    )
    path = tmp_path / "pins.lock"
    write_lock(lock(recipe, resolver=lambda _source: COMMIT), path)
    code, out = run_main(
        "sbom", str(baked), "--variant", "default", "--lockfile", str(path), "--format", "text"
    )
    assert code == EXIT_OK
    assert f"app   build  {COMMIT}  v1   https://example.com/app.git  /usr/bin/app" in out


def test_sbom_needs_a_variant_when_several_were_baked(baked: Path) -> None:
    assert run_main("sbom", str(baked))[0] == EXIT_SDK_ERROR


def test_sbom_rejects_manifest_and_out_together(baked: Path) -> None:
    code, _ = run_main("sbom", str(baked), "--out", str(baked), "--variant", "default")
    assert code == EXIT_SDK_ERROR
