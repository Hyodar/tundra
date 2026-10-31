"""The manifest of a build directory, its signature, and ``verify`` against tampering."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tests.cli.conftest import Cli
from tundravm import manifest as mf
from tundravm._mkosi import MkosiResult
from tundravm.builds import Build
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.testing import FakeMkosi

SSH_KEYGEN = shutil.which("ssh-keygen")
needs_ssh_keygen = pytest.mark.skipif(SSH_KEYGEN is None, reason="ssh-keygen is not installed")


def test_the_manifest_binds_the_directory(built: Build) -> None:
    raw = (built.path / mf.MANIFEST).read_bytes()
    payload = json.loads(raw)
    assert payload["schema"] == "tundravm.manifest/1"
    assert raw == mf.canonical(payload)
    assert payload["id"] == mf.build_id(payload) == built.path.name
    every = {
        p.relative_to(built.path).as_posix()
        for p in built.path.rglob("*")
        if p.is_file() and not p.relative_to(built.path).as_posix().startswith("tree/")
    }
    assert set(payload["artifacts"]) == every - {"manifest.json", "observations.json"}
    for name, record in payload["artifacts"].items():
        assert record["sha256"] == mf.sha256_file(built.path / name), name
    digest, files = mf.tree_digest(built.path / "tree")
    assert payload["tree"] == {"path": "tree", "digest": digest, "files": files}
    assert payload["uki"]["sha256"] == mf.sha256_file(built.path / "payload.efi")
    assert payload["lock"]["sha256"] == mf.sha256_file(built.path / "inputs/tundravm.lock")
    assert payload["packages"] == {"state": "missing", "reason": "mkosi wrote no package manifest"}
    assert payload["kind"] == "production"
    assert payload["measurements"]["records"] == {"qemu/disk": "unknown", "qemu/uki": "unknown"}
    assert "measurements qemu/uki: unknown" in payload["unchecked"]
    assert payload["root_hash"] is None and payload["rootfs"] == "initramfs"
    sources = json.loads((built.path / "inputs/sources.json").read_text())["sources"]
    assert payload["sources"] == sources and sources[0]["pin"] == "1" * 40


def test_observations_are_not_identity(built: Build) -> None:
    observed = json.loads((built.path / mf.OBSERVATIONS).read_text())
    assert observed["schema"] == "tundravm.observations/1" and observed["id"] == built.id
    assert observed["runner"]["backend"] == "local_linux"
    assert observed["lock_written"] is True and observed["reproduction"] is None
    assert "observations.json" not in built.manifest["artifacts"]


@dataclass(slots=True)
class _SummaryWithPaths(FakeMkosi):
    """A fake mkosi whose ``summary --json`` names the tree it ran in, the build's workspace
    and a host setting, as the real one does."""

    def invoke(self, argv: Sequence[str], *, cwd: Path, **kwargs: Any) -> MkosiResult:
        result = FakeMkosi.invoke(self, argv, cwd=cwd, **kwargs)
        if not result.stdout:
            return result
        payload = json.loads(result.stdout)
        for image in payload["Images"]:
            image["ExtraTrees"] = [f"{Path(cwd).resolve()}/payload/mkosi.extra"]
            image["CacheDirectory"] = str(Path(cwd).resolve() / "cache")
        return replace(result, stdout=json.dumps(payload))


def test_the_summary_input_names_no_host_path(
    project: Path, cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _SummaryWithPaths().install(monkeypatch.setattr)
    assert cli("build").code == EXIT_OK
    built = Build.open((project / "build" / "demo" / "latest-production").resolve())
    text = (built.path / "inputs" / "summary.json").read_text()
    assert str(built.path.parent.parent) not in text and str(project) not in text
    assert "$TREE/payload/mkosi.extra" in text and "CacheDirectory" not in text


def test_an_untouched_build_verifies(built: Build, cli: Cli) -> None:
    result = cli("verify", "latest")
    assert result.code == EXIT_OK, result.out
    assert result.out.splitlines()[-1] == "verdict: pass"
    report = json.loads(cli("verify", built.id[:6], "--json").out)
    assert report["schema"] == "tundravm.verify/1" and report["passed"] is True
    states = {check["name"]: check["state"] for check in report["checks"]}
    assert states == {
        "manifest": "ok",
        "id": "ok",
        "artifacts": "ok",
        "tree": "ok",
        "uki": "ok",
        "lock": "ok",
        "sbom": "ok",
        "packages": "missing",
        "summary": "ok",
        "measurements": "unchecked",
        "reproduction": "unchecked",
        "signature": "missing",
    }


def _copy(build: Build, tmp_path: Path) -> Path:
    target = tmp_path / "copy" / build.id
    shutil.copytree(build.path, target, symlinks=True)
    for path in target.rglob("*"):
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
    return target


def _failed(cli: Cli, path: Path) -> dict[str, str]:
    result = cli("verify", str(path), "--json")
    assert result.code == EXIT_FAILURE, result.out
    report = json.loads(result.out)
    assert report["passed"] is False
    return {c["name"]: c["detail"] for c in report["checks"] if c["state"] == "failed"}


def _append_to_the_disk(copy: Path) -> None:
    with (copy / "qemu.raw").open("ab") as handle:
        handle.write(b"x")


def _add_a_file(copy: Path) -> None:
    (copy / "extra.bin").write_bytes(b"x")


def _remove_the_sbom(copy: Path) -> None:
    (copy / "sbom.spdx.json").unlink()


def _edit_a_tree_file(copy: Path) -> None:
    (copy / "tree/payload/mkosi.extra/srv/www/index.html").write_text("<h1>edited</h1>\n")


def _make_a_tree_file_executable(copy: Path) -> None:
    page = copy / "tree/payload/mkosi.extra/srv/www/index.html"
    page.chmod(page.stat().st_mode | stat.S_IXUSR)


def _retarget_a_tree_symlink(copy: Path) -> None:
    links = [p for p in (copy / "tree").rglob("*") if p.is_symlink()]
    link = links[0] if links else copy / "tree" / "added-link"
    target = os.readlink(link) + ".moved" if links else "nowhere"
    link.unlink(missing_ok=True)
    link.symlink_to(target)


@pytest.mark.parametrize(
    ("tamper", "check", "detail"),
    [
        (_append_to_the_disk, "artifacts", "qemu.raw differs"),
        (_add_a_file, "artifacts", "extra.bin is not in the manifest"),
        (_remove_the_sbom, "artifacts", "sbom.spdx.json is missing"),
        (_edit_a_tree_file, "tree", "tree/ hashes to"),
        (_make_a_tree_file_executable, "tree", "tree/ hashes to"),
        (_retarget_a_tree_symlink, "tree", "tree/ hashes to"),
    ],
    ids=[
        "changed-artifact",
        "unexpected-file",
        "missing-file",
        "edited-tree-file",
        "changed-tree-mode",
        "changed-symlink-target",
    ],
)
def test_tampering_fails_the_named_check(
    built: Build,
    cli: Cli,
    tmp_path: Path,
    tamper: Callable[[Path], None],
    check: str,
    detail: str,
) -> None:
    """A copy verifies until one change to it; then exactly the check that covers the change
    fails, with the detail that names it."""
    copy = _copy(built, tmp_path)
    assert cli("verify", str(copy)).code == EXIT_OK
    tamper(copy)
    found = _failed(cli, copy)
    assert set(found) == {check}
    assert detail in found[check]


def test_a_write_bit_alone_is_not_a_change(built: Build, cli: Cli, tmp_path: Path) -> None:
    copy = _copy(built, tmp_path)  # every file made writable
    assert cli("verify", str(copy)).code == EXIT_OK


def test_an_edited_manifest_fails(built: Build, cli: Cli, tmp_path: Path) -> None:
    copy = _copy(built, tmp_path)
    payload = json.loads((copy / "manifest.json").read_text())
    (copy / "manifest.json").write_text(json.dumps(payload))  # same content, not canonical
    assert "not in canonical form" in _failed(cli, copy)["manifest"]
    payload["targets"] = ["qemu", "gcp"]
    (copy / "manifest.json").write_bytes(mf.canonical(payload))
    assert "but its digest is" in _failed(cli, copy)["id"]


def test_a_renamed_directory_fails(built: Build, cli: Cli, tmp_path: Path) -> None:
    copy = _copy(built, tmp_path)
    renamed = copy.with_name("0" * 16)
    copy.rename(renamed)
    assert f"holds build {built.id}" in _failed(cli, renamed)["id"]


def test_a_directory_without_a_manifest_is_a_state_error(
    built: Build, cli: Cli, tmp_path: Path
) -> None:
    result = cli("verify", str(tmp_path))
    assert result.code == 2 and "is not a build directory" in result.err


def test_rebuild_matches_an_independent_build(built: Build, cli: Cli) -> None:
    result = cli("verify", "latest", "--rebuild", "--json")
    assert result.code == EXIT_OK, result.out + result.err
    checks = {c["name"]: c for c in json.loads(result.out)["checks"]}
    assert checks["rebuild"]["state"] == "ok"
    assert "matched" in checks["rebuild"]["detail"]


def test_rebuild_of_narrowed_targets(project: Path, cli: Cli) -> None:
    (project / "demo.py").write_text(
        (project / "demo.py")
        .read_text()
        .replace("    dev=DevAccess", '    targets=("gcp",),\n    dev=DevAccess')
    )
    assert cli("build").code == EXIT_OK
    result = cli("verify", "latest", "--rebuild")
    assert result.code == EXIT_OK, result.out + result.err
    assert "rebuild" in result.out


# ── signature ────────────────────────────────────────────────────────────


def _key(tmp_path: Path, name: str) -> tuple[Path, str]:
    assert SSH_KEYGEN is not None
    key = tmp_path / name
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)], check=True
    )
    return key, (tmp_path / f"{name}.pub").read_text().strip()


def _signers(tmp_path: Path, principal: str, public: str) -> Path:
    path = tmp_path / f"allowed-{principal}"
    path.write_text(f'{principal} namespaces="tundravm-manifest" {public}\n')
    return path


@needs_ssh_keygen
def test_a_signed_manifest_verifies_with_its_signer(
    project: Path, cli: Cli, tmp_path: Path
) -> None:
    key, public = _key(tmp_path, "release")
    result = cli("build", "--sign", key)
    assert result.code == EXIT_OK, result.err
    build = Build.open((project / "build/demo/latest-production").resolve())
    assert (build.path / mf.SIGNATURE).is_file()
    assert mf.SIGNATURE not in build.manifest["artifacts"]
    allowed = _signers(tmp_path, "you@example", public)
    ok = cli("verify", "latest", "--allowed-signers", allowed, "--require-signature", "--json")
    assert ok.code == EXIT_OK, ok.out
    report = json.loads(ok.out)
    assert report["signer"] == "you@example"
    unchecked = cli("verify", "latest")
    assert unchecked.code == EXIT_OK and "signature     unchecked" in unchecked.out
    assert cli("verify", "latest", "--require-signature").code == EXIT_FAILURE
    _, other = _key(tmp_path, "other")
    wrong = cli("verify", "latest", "--allowed-signers", _signers(tmp_path, "x@y", other))
    assert wrong.code == EXIT_FAILURE and "signature     failed" in wrong.out


@needs_ssh_keygen
def test_a_signature_survives_a_copy_to_another_checkout(
    project: Path, cli: Cli, tmp_path: Path
) -> None:
    key, public = _key(tmp_path, "release")
    assert cli("build", "--sign", key).code == EXIT_OK
    build = Build.open((project / "build/demo/latest-production").resolve())
    elsewhere = tmp_path / "second" / build.id
    shutil.copytree(build.path, elsewhere, symlinks=True)
    allowed = _signers(tmp_path, "you@example", public)
    result = cli("verify", elsewhere, "--allowed-signers", allowed, "--require-signature")
    assert result.code == EXIT_OK, result.out
    (elsewhere / "manifest.json").chmod(0o644)
    payload = json.loads((elsewhere / "manifest.json").read_text())
    payload["unchecked"] = []
    payload["id"] = mf.build_id(payload)
    (elsewhere / "manifest.json").write_bytes(mf.canonical(payload))
    tampered = cli("verify", elsewhere, "--allowed-signers", allowed, "--require-signature")
    assert tampered.code == EXIT_FAILURE


def test_require_signature_without_one_fails(built: Build, cli: Cli) -> None:
    result = cli("verify", "latest", "--require-signature")
    assert result.code == EXIT_FAILURE
    assert "no manifest.sig" in result.out
