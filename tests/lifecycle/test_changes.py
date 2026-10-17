"""``changes``: a previous evidence bundle against the recipe and lockfile as they are now."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import (
    Backend,
    Build,
    ChangeReport,
    File,
    Fragment,
    Git,
    Install,
    Lock,
    Package,
    Recipe,
    Variant,
    bake,
    changes,
    evidence,
    lock,
    read_artifacts,
)
from tundravm._source import Source
from tundravm.cli import EXIT_OK
from tundravm.declarative import lower, write_lock
from tundravm.declarative.bom import manifest_path

REPO = "https://example.com/app.git"
FIRST = "a" * 40
SECOND = "b" * 40
KERNEL = "linux-image-amd64"


def _recipe(*, extra: str = "jq", motd: str = "hi\n") -> Recipe:
    return Recipe(
        "node",
        Fragment(
            "common",
            items=(
                Package("curl"),
                Package("linux-image-amd64"),
                Package(extra),
                File("/etc/motd", motd),
                Build(
                    "app",
                    Git(REPO, "main"),
                    script="make",
                    install=(Install("app", "/usr/bin/app"),),
                ),
            ),
        ),
        variants=(Variant("default", target="qemu"),),
    )


def _mkosi(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), 0, stdout="mkosi 26\n", stderr="")


def _locked(recipe: Recipe, commit: str) -> Lock:
    def resolve(source: Source) -> str:
        return commit

    return lock(recipe, resolver=resolve)


def _bake(recipe: Recipe, locked: Lock, build: Path) -> Path:
    write_lock(locked, build / "tundravm.lock")
    bake(recipe, lock=locked, backend=Backend("inprocess"), build=build)
    return build


def _record_manifest(build: Path, versions: dict[str, str]) -> None:
    """A mkosi package manifest beside the artifact, recorded in bake-result.json as a bake does."""
    artifact = read_artifacts(build)[0]
    packages = [
        {"type": "deb", "name": name, "version": version, "architecture": "amd64"}
        for name, version in versions.items()
    ]
    config = {"name": "default", "distribution": "debian", "architecture": "x86-64"}
    text = json.dumps({"manifest_version": 1, "config": config, "packages": packages})
    manifest_path(artifact).write_text(text, encoding="utf-8")
    result = build / "bake-result.json"
    payload = json.loads(result.read_text(encoding="utf-8"))
    payload["variants"]["default"]["manifest_digests"]["qemu"] = hashlib.sha256(
        text.encode()
    ).hexdigest()
    result.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
def released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The evidence bundle of the first release: jq, motd "hi", app at FIRST."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    recipe = _recipe()
    build = _bake(recipe, _locked(recipe, FIRST), tmp_path / "v1")
    _record_manifest(build, {"curl": "7.88.1-10", "jq": "1.6-2.1", KERNEL: "6.12.48-1"})
    found = evidence(recipe, build=build, runner=_mkosi)
    return found.bundle(tmp_path / "v1.tar.gz")


def _subjects(report: ChangeReport, section: str) -> list[tuple[str, str | None, str]]:
    return [(c.change, c.variant, c.subject) for c in report.section(section)]


def test_an_unchanged_recipe_reports_nothing(tmp_path: Path, released: Path) -> None:
    recipe = _recipe()
    report = changes(recipe, since=released, lock=_locked(recipe, FIRST))
    assert report.changes == (), report.render()
    assert report.variants == ("default",)
    assert "no changes" in report.render()


def test_a_package_a_file_and_a_pin_that_changed(tmp_path: Path, released: Path) -> None:
    recipe = _recipe(extra="htop", motd="hello\n")
    locked = _locked(recipe, SECOND)
    build = _bake(recipe, locked, tmp_path / "v2")
    _record_manifest(build, {"curl": "8.5.0-2", "htop": "3.2.2-2", KERNEL: "6.12.48-1"})
    report = changes(recipe, since=released, lock=locked, build=build)
    recipe_rows = _subjects(report, "recipe")
    assert recipe_rows == [
        ("changed", None, "recipe digest"),
        ("changed", "default", "default tree"),
    ]
    sections = {c.subject: c for c in report.section("lockfile")}
    assert sections["variants.default.packages"].detail == "+htop -jq"
    assert "variants.default.files" in sections
    (pin,) = report.section("pins")
    assert (pin.change, pin.subject, pin.old, pin.new) == ("changed", "app", FIRST, SECOND)
    assert pin.detail == "ref main"
    packages = {(c.change, c.subject): (c.old, c.new) for c in report.section("packages")}
    assert packages == {
        ("added", "htop"): (None, "3.2.2-2"),
        ("removed", "jq"): ("1.6-2.1", None),
        ("changed", "curl"): ("7.88.1-10", "8.5.0-2"),
    }
    files = {c.subject: c for c in report.section("files")}
    motd = files["mkosi.extra/etc/motd"]
    assert (motd.change, motd.detail) == ("changed", "content")
    assert motd.origins == ("File(extra, /etc/motd) (declared in common)",)
    assert files["mkosi.conf"].change == "changed" and files["mkosi.conf"].origins == ()
    assert report.counts()["measurements"] == 0
    payload = json.loads(report.render("json"))
    assert payload["schema"] == "tundravm.changes/1"
    assert payload["counts"]["pins"] == 1


def test_markdown_groups_the_sections_with_counts(tmp_path: Path, released: Path) -> None:
    recipe = _recipe(extra="htop", motd="hello\n")
    report = changes(recipe, since=released, lock=_locked(recipe, SECOND))
    markdown = report.render("markdown")
    assert markdown.startswith("## Changes since evidence `2023-11-14T22:13:20Z`")
    assert "| Source pins | 1 |" in markdown
    assert "### Packages (2)" in markdown  # declared packages: no version change is claimed
    assert "| added | `default` | `htop` | — | — |" in markdown
    assert "| changed | `app` | ref main | `aaaaaaaaaaaa` | `bbbbbbbbbbbb` |" in markdown
    assert "| changed | `default` | `mkosi.extra/etc/motd` | content |" in markdown
    assert "<details><summary>Notes</summary>" in markdown
    text = report.render()
    assert "  pins          1" in text
    assert "  ~ app  aaaaaaaaaaaa -> bbbbbbbbbbbb  (ref main)" in text
    assert any("declared ones, unversioned" in note for note in report.notes)


def test_measurement_registers_compare_when_both_sides_have_policies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recipe = _recipe()
    locked = _locked(recipe, FIRST)
    build = _bake(recipe, locked, tmp_path / "v1")

    def policy(sha: str, rtmr1: str) -> str:
        registers = {"RTMR0": "ab" * 48, "RTMR1": rtmr1, "RTMR2": "cd" * 48}
        payload = {
            "schema_version": 1,
            "scheme": "rtmr",
            "tool": "measured-boot",
            "tool_version": "1.0",
            "artifact": {"path": "disk.qcow2", "sha256": sha},
            "registers": registers,
        }
        return json.dumps(payload)

    sha = read_artifacts(build)[0].sha256
    (build / "default" / "policy.json").write_text(policy(sha, "11" * 48), encoding="utf-8")
    bundle = evidence(recipe, build=build, runner=_mkosi).bundle(tmp_path / "v1.tar.gz")
    (build / "default" / "policy.json").write_text(policy(sha, "22" * 48), encoding="utf-8")
    report = changes(recipe, since=bundle, lock=locked, build=build)
    (register,) = report.section("measurements")
    assert (register.subject, register.old, register.new) == ("RTMR1", "11" * 48, "22" * 48)


def test_lowered_recipe_and_a_new_variant(tmp_path: Path, released: Path) -> None:
    recipe = Recipe(
        "node",
        _recipe().common,
        variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
    )
    report = changes(lower(recipe), since=released, lock=_locked(recipe, FIRST))
    assert ("added", "azure", "variant azure") in _subjects(report, "recipe")
    assert all(c.origins == () for c in report.section("files"))


RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "node",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"), Package("{extra}"))),
    variants=(Variant("default", target="qemu"),),
)
"""


def test_cli_prints_markdown_for_a_pull_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_recipe_file(tmp_path, RECIPE.format(extra="jq"), monkeypatch)
    assert run_main("lock", str(path))[0] == EXIT_OK
    assert run_main("bake", str(path))[0] == EXIT_OK
    assert run_main("evidence", str(path), "--bundle", "v1.tar.gz")[0] == EXIT_OK
    path.write_text(RECIPE.format(extra="htop"), encoding="utf-8")
    assert run_main("lock", str(path))[0] == EXIT_OK
    code, out = run_main("changes", str(path), "--since", "v1.tar.gz", "--format", "markdown")
    assert code == EXIT_OK
    assert "| `variants.default.packages` | `+htop -jq` |" in out
    code, out = run_main("changes", str(path), "--since", "v1.tar.gz", "--variant", "default")
    assert code == EXIT_OK
    assert "  + default  htop" in out
