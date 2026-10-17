"""``release``: one immutable directory from a locked recipe, resumable stage by stage."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import (
    File,
    Fragment,
    Lock,
    Package,
    Recipe,
    Release,
    Variant,
    lock,
    release,
)
from tundravm.backends import InProcessBackend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import write_lock
from tundravm.errors import LockfileError, StateError
from tundravm.models import BakeRequest, BakeResult
from tundravm.release import STAGES, STATE_NAME

release_module = importlib.import_module("tundravm.release")  # `tundravm.release` is the function
NEEDS_SSH = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@dataclass(slots=True)
class Counting(InProcessBackend):
    """The in-process backend, counting the variants it builds."""

    built: list[str] = field(default_factory=list)

    def execute(self, request: BakeRequest) -> BakeResult:
        self.built.append(request.profile)
        return InProcessBackend.execute(self, request)


def _recipe(*, extra: str = "jq") -> Recipe:
    return Recipe(
        "node",
        Fragment(
            "common",
            items=(
                Package("curl"),
                Package("linux-image-amd64"),
                Package(extra),
                File("/etc/motd", "hi\n"),
            ),
        ),
        variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
    )


@dataclass(slots=True)
class Project:
    root: Path
    backend: Counting = field(default_factory=Counting)

    @property
    def lockfile(self) -> Path:
        return self.root / "build" / "tundravm.lock"

    def lock(self, recipe: Recipe) -> Lock:
        locked = lock(recipe)
        write_lock(locked, self.lockfile)
        return locked

    def release(self, recipe: Recipe, version: str, **options: object) -> Release:
        return release(
            recipe,
            version=version,
            backend=self.backend,
            build=self.root / "build",
            releases=self.root / "releases",
            lock=self.lockfile,
            **options,  # type: ignore[arg-type]
        )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    made = Project(tmp_path)
    made.lock(_recipe())
    return made


def _state(root: Path) -> dict[str, dict[str, object]]:
    payload = json.loads((root / STATE_NAME).read_text(encoding="utf-8"))
    assert payload["schema"] == "tundravm.release-state/1"
    stages: dict[str, dict[str, object]] = payload["stages"]
    return stages


def test_a_full_release_lays_out_an_immutable_directory(project: Project) -> None:
    lines: list[str] = []
    result = project.release(_recipe(), "1.0", progress=lines.append)
    root = project.root / "releases" / "node-1.0"
    assert result.path == root and result.passed and result.finished
    assert result.simulated and result.reproducibility == "matched"
    assert [line.split()[1] for line in lines] == list(STAGES)
    assert lines[2].startswith("[3/9] bake        done")
    assert "reproducible: matched" in lines[2]
    assert "skipped" in lines[3] and "simulated artifacts: nothing to measure" in lines[3]
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert files == [
        "RELEASE.md",
        "azure/disk.vhd",
        "azure/sbom-azure.spdx.json",
        "default/disk.qcow2",
        "default/sbom-qemu.spdx.json",
        "evidence.html",
        "evidence.tar.gz",
        "release.json",
    ]
    index = json.loads((root / "release.json").read_text(encoding="utf-8"))
    assert index == result.to_dict()
    assert index["schema"] == "tundravm.release/1"
    assert (index["recipe"], index["version"], index["verdict"]) == ("node", "1.0", "pass")
    assert index["lock_sha256"] == result.lock_sha256 and len(result.lock_sha256) == 64
    assert [stage["name"] for stage in index["stages"]] == list(STAGES)
    by_name = {stage["name"]: stage for stage in index["stages"]}
    assert by_name["changes"]["status"] == "skipped"
    assert set(by_name["bake"]["outputs"]) == {"default/disk.qcow2", "azure/disk.vhd"}
    artifacts = {row["path"]: row["sha256"] for row in index["artifacts"]}
    assert artifacts == by_name["bake"]["outputs"]
    notes = (root / "RELEASE.md").read_text(encoding="utf-8")
    assert notes.startswith("# node-1.0\n")
    assert "- Verdict: **pass**" in notes and "**Simulated**" in notes
    assert artifacts["default/disk.qcow2"] in notes
    assert "matched: a second build" in notes and "First release of this recipe" in notes
    assert not (root / STATE_NAME).exists()
    assert project.backend.built.count("default") == 2  # the bake and its second build


def test_a_finished_release_refuses_its_version_again(project: Project) -> None:
    project.release(_recipe(), "1.0")
    with pytest.raises(StateError, match="finished") as raised:
        project.release(_recipe(), "1.0")
    assert raised.value.hint is not None and "Bump the version" in raised.value.hint
    with pytest.raises(StateError, match="finished"):
        project.release(_recipe(), "1.0", restart=True)


def test_a_failed_stage_resumes_without_baking_again(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project.root / "releases" / "node-1.0"

    def broken(*args: object, **kwargs: object) -> object:
        raise StateError("disk full", hint="Free some space.")

    monkeypatch.setattr(release_module, "sbom", broken)
    with pytest.raises(StateError, match="disk full"):
        project.release(_recipe(), "1.0")
    assert sorted(_state(root)) == ["bake", "fetch", "lock-check", "measure"]
    assert not (root / "release.json").exists()
    built = len(project.backend.built)
    monkeypatch.undo()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    lines: list[str] = []
    result = project.release(_recipe(), "1.0", progress=lines.append)
    assert result.passed
    assert len(project.backend.built) == built  # the bake was kept
    shown = {line.split()[1]: line.split()[2] for line in lines}
    assert shown == {
        "lock-check": "kept",
        "fetch": "skipped",
        "bake": "kept",
        "measure": "skipped",
        "sbom": "done",
        "evidence": "done",
        "verify": "done",
        "changes": "skipped",
        "notes": "done",
    }
    assert [stage.resumed for stage in result.stages] == [True] * 4 + [False] * 5


def test_changed_inputs_rerun_from_the_first_changed_stage(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project.root / "releases" / "node-1.0"
    failed = project.release(_recipe(), "1.0", require=("real",))
    assert failed.verdict == "fail" and not failed.finished
    assert {reason["code"] for reason in failed.reasons} == {"simulated-artifact"}
    assert (root / STATE_NAME).is_file()  # a failed release stays resumable
    assert json.loads((root / "release.json").read_text())["verdict"] == "fail"
    built = len(project.backend.built)
    passed = project.release(_recipe(), "1.0")  # the requirement changed: evidence onwards
    rerun = [stage.name for stage in passed.stages if not stage.resumed]
    assert rerun == ["evidence", "verify", "changes", "notes"]
    assert passed.passed and len(project.backend.built) == built

    other = project.root / "releases" / "node-2.0"
    project.release(_recipe(), "2.0", require=("real",))
    project.lock(_recipe(extra="htop"))  # a new lockfile: everything again
    again = project.release(_recipe(extra="htop"), "2.0")
    assert [stage.resumed for stage in again.stages] == [False] * len(STAGES)
    assert len(project.backend.built) == built + 2 * 2 * 2  # 2 releases x 2 variants x 2 builds
    assert again.passed and not (other / STATE_NAME).exists()


def test_a_tampered_output_reruns_its_stage(project: Project) -> None:
    root = project.root / "releases" / "node-1.0"
    project.release(_recipe(), "1.0", require=("real",))
    (root / "default" / "sbom-qemu.spdx.json").write_text("{}\n", encoding="utf-8")
    result = project.release(_recipe(), "1.0")
    assert [stage.name for stage in result.stages if not stage.resumed][0] == "sbom"


def test_since_defaults_to_the_previous_release_and_lists_the_changes(
    project: Project,
) -> None:
    project.release(_recipe(), "1.0")
    project.lock(_recipe(extra="htop"))
    result = project.release(_recipe(extra="htop"), "1.1")
    assert result.since == "node-1.0"
    changes = next(stage for stage in result.stages if stage.name == "changes")
    assert changes.status == "done" and changes.summary.startswith("since node-1.0: ")
    notes = (result.path / "RELEASE.md").read_text(encoding="utf-8")
    assert "## Changes\n\nSince `node-1.0`." in notes
    assert "htop" in notes and "jq" in notes
    assert "\n### " in notes  # the changes report's headings sit under ## Changes


def test_a_drifted_lockfile_is_refused_and_left_alone(project: Project) -> None:
    before = project.lockfile.read_bytes()
    with pytest.raises(LockfileError, match="drifted") as raised:
        project.release(_recipe(extra="htop"), "1.0")
    assert raised.value.hint is not None and "never updates the lock" in raised.value.hint
    assert project.lockfile.read_bytes() == before
    assert project.backend.built == []


def test_a_release_needs_a_lockfile(tmp_path: Path) -> None:
    with pytest.raises(LockfileError, match="locked recipe"):
        release(
            _recipe(),
            version="1.0",
            backend=InProcessBackend(),
            build=tmp_path / "build",
            releases=tmp_path / "releases",
            lock=tmp_path / "missing.lock",
        )
    assert not (tmp_path / "releases").exists()


def _key(tmp_path: Path) -> Path:
    key = tmp_path / "release-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "ci", "-f", str(key)], check=True
    )
    return key


@NEEDS_SSH
def test_a_signed_release_verifies_with_the_signers_public_key(project: Project) -> None:
    key = _key(project.root)
    result = project.release(_recipe(), "1.0", sign=key)
    assert result.passed
    public = key.with_suffix(".pub").read_text(encoding="utf-8").split()
    assert result.signer == {"principal": "release", "public_key": " ".join(public[:2])}
    verify = next(stage for stage in result.stages if stage.name == "verify")
    assert verify.summary.endswith("checks pass, signed by release")
    with tarfile.open(result.path / "evidence.tar.gz") as archive:
        assert "evidence/evidence.json.sig" in archive.getnames()
    notes = (result.path / "RELEASE.md").read_text(encoding="utf-8")
    assert "the evidence is signed by `release`" in notes


RECIPE = """\
from tundravm import File, Fragment, Package, Recipe, Variant
from tundravm.backends import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "node",
    Fragment(
        "common",
        items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\\n")),
    ),
    variants=(Variant("default", target="qemu"),),
)
"""


def test_cli_release_transcript_exit_codes_and_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    code, out = run_main("release", str(recipe), "--version", "1.0", "--require", "real")
    assert code == EXIT_FAILURE, out
    lines = out.splitlines()
    assert lines[0].startswith("[1/9] lock-check  done")
    assert lines[-1] == "release releases/node-1.0: fail (simulated)"
    resume = f"resume with: tundravm release {recipe} --version 1.0 --require real"
    assert resume in capsys.readouterr().err
    code, out = run_main("release", str(recipe), "--version", "1.0", "--resume")
    assert code == EXIT_OK, out
    assert "[3/9] bake        kept" in out
    assert out.splitlines()[-1] == "release releases/node-1.0: pass (simulated)"
    capsys.readouterr()
    assert run_main("release", str(recipe), "--version", "1.0")[0] == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_STATE]" in err and "resume with" not in err
    assert run_main("release", str(recipe), "--version", "2.0", "--resume")[0] == EXIT_SDK_ERROR
    assert "nothing to resume" in capsys.readouterr().err


def test_cli_release_json_and_an_interrupted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(recipe))[0] == EXIT_OK

    def broken(*args: object, **kwargs: object) -> object:
        raise StateError("interrupted on purpose", hint="Run it again.")

    with monkeypatch.context() as patch:
        patch.setattr(release_module, "evidence", broken)
        code, _ = run_main("release", str(recipe), "--version", "1.0", "--format", "json")
    assert code == EXIT_SDK_ERROR
    (line,) = capsys.readouterr().err.strip().splitlines()[-1:]
    error = json.loads(line)
    assert error["schema"] == "tundravm.error/1"
    assert error["context"]["resume"] == f"tundravm release {recipe} --version 1.0"
    code, out = run_main("release", str(recipe), "--version", "1.0", "--format", "json")
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["schema"] == "tundravm.release/1" and payload["verdict"] == "pass"
    resumed = [stage["name"] for stage in payload["stages"] if stage["resumed"]]
    assert resumed == ["lock-check", "fetch", "bake", "measure", "sbom"]
    assert "[6/9] evidence" in capsys.readouterr().err  # stage lines move to stderr
