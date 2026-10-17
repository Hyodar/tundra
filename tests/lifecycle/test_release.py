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
    Lock,
    Package,
    Recipe,
    Release,
    Variant,
    lock,
    release,
    verify_release,
)
from tundravm.backends import InProcessBackend
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import write_lock
from tundravm.declarative.utils import DevTools
from tundravm.errors import LockfileError, StateError, ValidationError
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


def _recipe(*, extra: str = "jq", dev: bool = False) -> Recipe:
    variants: tuple[Variant, ...] = (
        Variant("default", outputs=("qemu",)),
        Variant("azure", outputs=("azure",)),
    )
    if dev:
        variants += (Variant("dev", inherits="default", add=DevTools(), release=False),)
    return Recipe(
        "node",
        items=(
            Package("curl"),
            Package("linux-image-amd64"),
            Package(extra),
            File("/etc/motd", "hi\n"),
        ),
        variants=variants,
    )


@dataclass(slots=True)
class Flavored(Counting):
    """A counting backend with one setting, which its fingerprint carries."""

    flavor: str = "plain"

    def fingerprint(self) -> dict[str, object]:
        return {"name": self.name, "flavor": self.flavor}


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
        options.setdefault("build", self.root / "build")
        return release(
            recipe,
            version=version,
            backend=self.backend,
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


def _spec(root: Path) -> dict[str, object]:
    spec: dict[str, object] = json.loads((root / STATE_NAME).read_text(encoding="utf-8"))["spec"]
    return spec


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
    published = [
        "RELEASE.md",
        "azure/disk.vhd",
        "azure/sbom-azure.spdx.json",
        "default/disk.qcow2",
        "default/sbom-qemu.spdx.json",
        "evidence.html",
        "evidence.tar.gz",
        "release.json",
    ]
    assert files == sorted([*published, "release-manifest.json"])
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "tundravm.release-manifest/1"
    assert [row["path"] for row in manifest["files"]] == published
    assert manifest["signer"] is None and manifest["version"] == "1.0"
    sizes = {row["path"]: row["size"] for row in manifest["files"]}
    assert sizes["release.json"] == (root / "release.json").stat().st_size
    index = json.loads((root / "release.json").read_text(encoding="utf-8"))
    assert index == result.to_dict()
    assert index["schema"] == "tundravm.release/1"
    assert (index["recipe"], index["version"], index["verdict"]) == ("node", "1.0", "pass")
    assert index["lock_sha256"] == result.lock_sha256 and len(result.lock_sha256) == 64
    assert [stage["name"] for stage in index["stages"]] == list(STAGES)
    by_name = {stage["name"]: stage for stage in index["stages"]}
    assert by_name["changes"]["execution"] == "skipped" and by_name["changes"]["verdict"] is None
    assert by_name["bake"]["execution"] == "completed"
    assert [by_name[name]["verdict"] for name in ("evidence", "verify", "notes")] == ["pass"] * 3
    assert index["spec"] == {
        "allow_ineligible": False,
        "backend": {"name": "inprocess"},
        "require": [],
        "signing": None,
        "since": None,
        "variants": ["default", "azure"],
    }
    assert index["spec_history"] == [] and index["skipped"] == []
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
    assert sorted(_state(root)) == ["bake", "fetch", "lock-check", "measure", "sbom"]
    assert _state(root)["sbom"]["execution"] == "failed"
    assert _state(root)["sbom"]["summary"] == "StateError: disk full"
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


def test_a_resume_inherits_the_spec_and_refuses_a_weaker_one(project: Project) -> None:
    root = project.root / "releases" / "node-1.0"
    failed = project.release(_recipe(), "1.0", require=("real",))
    assert failed.verdict == "fail" and not failed.finished
    assert {reason["code"] for reason in failed.reasons} == {"simulated-artifact"}
    assert (root / STATE_NAME).is_file()  # a failed release stays resumable
    assert json.loads((root / "release.json").read_text())["verdict"] == "fail"
    assert _spec(root)["require"] == ["real"]
    built = len(project.backend.built)
    again = project.release(_recipe(), "1.0")  # --require omitted: inherited, still fails
    assert again.verdict == "fail" and again.required == ("real",)
    assert all(stage.resumed for stage in again.stages)
    with pytest.raises(StateError, match="another specification") as raised:
        project.release(_recipe(), "1.0", require=())  # dropping a check weakens it
    assert 'require ["real"] -> []' in str(raised.value)
    assert raised.value.hint is not None and "--change-spec" in raised.value.hint
    assert _spec(root)["require"] == ["real"]  # the refused run changed nothing
    passed = project.release(_recipe(), "1.0", require=(), change_spec=True)
    rerun = [stage.name for stage in passed.stages if not stage.resumed]
    assert rerun == ["evidence", "verify", "changes", "notes"]
    assert passed.passed and len(project.backend.built) == built
    index = json.loads((root / "release.json").read_text(encoding="utf-8"))
    (entry,) = index["spec_history"]
    assert entry["changes"] == {"require": {"old": ["real"], "new": []}}
    assert index["spec"]["require"] == [] and index["required"] == []


def test_a_new_lockfile_reruns_every_stage(project: Project) -> None:
    other = project.root / "releases" / "node-2.0"
    project.release(_recipe(), "2.0", require=("real",))
    built = len(project.backend.built)
    project.lock(_recipe(extra="htop"))  # a new lockfile: everything again
    again = project.release(_recipe(extra="htop"), "2.0", require=(), change_spec=True)
    assert [stage.resumed for stage in again.stages] == [False] * len(STAGES)
    assert len(project.backend.built) == built + 2 * 2  # 2 variants x 2 builds
    assert again.passed and not (other / STATE_NAME).exists()


def test_a_changed_backend_setting_needs_change_spec_and_bakes_again(project: Project) -> None:
    root = project.root / "releases" / "node-1.0"
    project.backend = Flavored()
    project.release(_recipe(), "1.0", require=("real",))
    assert _spec(root)["backend"] == {"name": "inprocess", "flavor": "plain"}
    project.backend = Flavored(flavor="spicy")
    with pytest.raises(StateError, match="backend"):
        project.release(_recipe(), "1.0")
    again = project.release(_recipe(), "1.0", change_spec=True)
    assert [stage.name for stage in again.stages if not stage.resumed][0] == "bake"
    assert project.backend.built.count("default") == 2
    with pytest.raises(StateError, match="build"):
        project.release(_recipe(), "1.0", build=project.root / "elsewhere")


def test_a_package_manifest_the_sbom_reads_is_watched(project: Project) -> None:
    root = project.root / "releases" / "node-1.0"
    project.release(_recipe(), "1.0", require=("real",))
    watched = _state(root)["sbom"]["watched"]
    manifest = project.root / "build" / "default" / "default.manifest"
    assert watched[str(manifest.resolve())] == "absent"  # type: ignore[index]
    manifest.write_text("{}\n", encoding="utf-8")  # a manifest appeared: the SBOM is stale
    with pytest.raises(Exception, match="manifest"):
        project.release(_recipe(), "1.0")
    assert _state(root)["sbom"]["execution"] == "failed"


def test_release_skips_ineligible_variants_unless_allowed(project: Project) -> None:
    project.lock(_recipe(dev=True))
    lines: list[str] = []
    result = project.release(_recipe(dev=True), "1.0", progress=lines.append)
    assert result.variants == ("default", "azure")
    assert lines[0] == "skip variant dev: Variant(release=False): not for release"
    assert [dict(row) for row in result.skipped] == [
        {"variant": "dev", "reason": "Variant(release=False): not for release"}
    ]
    assert not (result.path / "dev").exists()
    with pytest.raises(ValidationError, match="not eligible for release") as raised:
        project.release(_recipe(dev=True), "1.1", variants=("dev",))
    assert raised.value.hint is not None and "--allow-ineligible" in raised.value.hint
    allowed = project.release(
        _recipe(dev=True),
        "1.2",
        variants=("dev",),
        allow_ineligible=True,
        build=project.root / "build-dev",
    )
    assert allowed.passed and allowed.variants == ("dev",)
    index = json.loads((allowed.path / "release.json").read_text(encoding="utf-8"))
    assert index["allow_ineligible"] is True and index["spec"]["allow_ineligible"] is True


def test_release_false_is_in_the_digest_and_true_is_not() -> None:
    from tundravm.declarative import lower

    plain = _recipe()
    marked = Recipe(
        "node",
        items=plain.items,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",), release=True),
        ),
    )
    unreleased = Recipe(
        "node",
        items=plain.items,
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", outputs=("azure",), release=False),
        ),
    )

    def digest(recipe: Recipe) -> str:
        return lower(recipe, variants=["default", "azure"]).select(("default", "azure")).digest()

    assert digest(marked) == digest(plain)
    assert digest(unreleased) != digest(plain)
    with pytest.raises(ValidationError, match="release="):
        Variant("dev", release="no")  # type: ignore[arg-type]


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
    assert changes.execution == "completed" and changes.summary.startswith("since node-1.0: ")
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
    items=(Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\\n")),
    variants=(Variant("default", outputs=("qemu",)),),
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
    assert code == EXIT_FAILURE, out  # --require real is inherited: still failing
    assert "[3/9] bake        kept" in out and "[6/9] evidence    kept" in out
    capsys.readouterr()
    code, _ = run_main("release", str(recipe), "--version", "1.0", "--require", "")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_STATE]" in err and 'require ["real"] -> []' in err and "--change-spec" in err
    code, out = run_main(
        "release", str(recipe), "--version", "1.0", "--require", "", "--change-spec"
    )
    assert code == EXIT_OK, out
    assert "[3/9] bake        kept" in out and "[6/9] evidence    done" in out
    assert out.splitlines()[-1] == "release releases/node-1.0: pass (simulated)"
    capsys.readouterr()
    assert run_main("release", str(recipe), "--version", "1.0")[0] == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_STATE]" in err and "resume with" not in err
    assert run_main("release", str(recipe), "--version", "2.0", "--resume")[0] == EXIT_SDK_ERROR
    assert "nothing to resume" in capsys.readouterr().err
    code, out = run_main("verify", "releases/node-1.0")
    assert code == EXIT_OK, out
    assert out.splitlines()[0].startswith("verify releases/node-1.0  verdict pass")


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


def _codes(path: Path, **options: object) -> set[str]:
    found = verify_release(path, **options)  # type: ignore[arg-type]
    return {str(check.code) for check in found.problems}


def _fake_policy(run: object) -> object:
    """A measure stage that writes default/policy.json, so a release lists a policy."""
    root: Path = run.root  # type: ignore[attr-defined]
    (root / "default").mkdir(parents=True, exist_ok=True)
    (root / "default" / "policy.json").write_text('{"registers": {}}\n', encoding="utf-8")
    sha = release_module._file_sha256(root / "default" / "policy.json")
    return release_module._Outcome(
        "completed", "1 policies (fake)", outputs={"default/policy.json": sha}
    )


def test_verify_release_checks_every_file_against_the_manifest(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(release_module._STAGE_RUNNERS, "measure", _fake_policy)
    result = project.release(_recipe(), "1.0")
    root = result.path
    clean = verify_release(root)
    assert clean.passed, clean.render()
    assert clean.evidence is not None and clean.evidence.passed
    assert {note["code"] for note in clean.notes} == {"unsigned"}
    payload = clean.to_dict()
    assert payload["schema"] == "tundravm.release-verify/1" and payload["verdict"] == "pass"
    assert payload["recorded"] == {"name": "node-1.0", "version": "1.0", "created": result.created}
    text = clean.render()
    assert text.startswith(f"verify {root}  verdict pass")
    assert "  file       9/9 pass" in text and "  evidence   pass: " in text

    notes = root / "RELEASE.md"
    original = notes.read_bytes()
    notes.write_text(original.decode() + "Also: trust me.\n", encoding="utf-8")
    assert _codes(root) == {"file-hash-mismatch"}
    notes.write_bytes(original)

    policy = root / "default" / "policy.json"
    policy.write_text('{"registers": {"MRTD": "00"}}\n', encoding="utf-8")
    assert _codes(root) == {"file-hash-mismatch"}
    policy.unlink()
    assert _codes(root) == {"file-missing"}
    policy.write_text('{"registers": {}}\n', encoding="utf-8")
    assert verify_release(root).passed

    extra = root / "azure" / "README.txt"
    extra.write_text("added later\n", encoding="utf-8")
    found = verify_release(root)
    assert [(c.code, c.subject) for c in found.problems] == [("file-unlisted", "azure/README.txt")]
    extra.unlink()

    index = root / "release.json"
    payload = json.loads(index.read_text(encoding="utf-8"))
    index.write_text(json.dumps({**payload, "version": "9.9"}), encoding="utf-8")
    found = verify_release(root)
    assert {str(c.code) for c in found.problems} == {"file-hash-mismatch", "release-inconsistent"}
    assert ("release-inconsistent", "version") in [(c.code, c.subject) for c in found.problems]

    with pytest.raises(ValidationError, match="not a release directory"):
        verify_release(project.root / "build")


def test_cli_verify_of_a_release_directory(
    project: Project, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = project.release(_recipe(), "1.0")
    code, out = run_main("verify", str(result.path), "--format", "json")
    assert code == EXIT_OK
    assert json.loads(out)["schema"] == "tundravm.release-verify/1"
    (result.path / "evidence.html").write_text("<p>replaced</p>\n", encoding="utf-8")
    code, out = run_main("verify", str(result.path))
    assert code == EXIT_FAILURE
    assert "fail     file-hash-mismatch  evidence.html: " in out
    capsys.readouterr()
    assert run_main("verify", str(result.path), "--build", "build")[0] == EXIT_SDK_ERROR
    assert "--build does not apply to a release directory" in capsys.readouterr().err


def _allowed(path: Path, key: Path, principal: str = "release") -> Path:
    public = " ".join(key.with_suffix(".pub").read_text(encoding="utf-8").split()[:2])
    path.write_text(
        f'{principal} namespaces="tundravm-evidence,tundravm-release" {public}\n', encoding="utf-8"
    )
    return path


@NEEDS_SSH
def test_a_signed_release_manifest_verifies_and_a_resume_keeps_the_key(project: Project) -> None:
    key = _key(project.root)
    root = project.root / "releases" / "node-1.0"
    failed = project.release(_recipe(), "1.0", sign=key, require=("real",))
    assert not failed.passed and (root / "release-manifest.json.sig").is_file()
    spec = _spec(root)
    assert spec["signing"] == {
        "key": str(key),
        "public_key": failed.signer["public_key"],  # type: ignore[index]
        "fingerprint": spec["signing"]["fingerprint"],  # type: ignore[index]
    }
    assert str(spec["signing"]["fingerprint"]).startswith("SHA256:")  # type: ignore[index]
    with pytest.raises(StateError, match="signing"):
        project.release(_recipe(), "1.0", unsigned=True, require=(), change_spec=False)
    result = project.release(_recipe(), "1.0", require=(), change_spec=True)  # --sign inherited
    assert result.passed and result.signer is not None
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["signer"]["fingerprint"] == spec["signing"]["fingerprint"]  # type: ignore[index]
    allowed = _allowed(project.root / "allowed_signers", key)
    found = verify_release(root, allowed_signers=allowed)
    assert found.passed and found.signer == "release", found.render()
    assert found.evidence is not None and found.evidence.signer == "release"

    other = project.root / "other-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    stranger = _allowed(project.root / "stranger", other)
    assert "manifest-signature-invalid" in _codes(root, allowed_signers=stranger)
    signature = root / "release-manifest.json.sig"
    signature.unlink()
    assert _codes(root) == {"manifest-unsigned"}
