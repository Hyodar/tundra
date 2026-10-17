"""``tundravm witness`` and ``verify RELEASE_DIR --min-witnesses N``: independent rebuilds."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR

NEEDS_SSH = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
RELEASE = Path("releases/node-1.0")
WITNESSES = Path("releases/node-1.0.witnesses")


def _project(root: Path, monkeypatch: pytest.MonkeyPatch, *release: str) -> None:
    """A scaffolded, locked project with a passing release ``node-1.0``."""
    monkeypatch.chdir(root)
    init = ("init", ".", "--name", "node", "--backend", "inprocess", "--no-doctor")
    assert run_main(*init)[0] == EXIT_OK
    assert run_main("lock")[0] == EXIT_OK
    code, out = run_main("release", "--version", "1.0", *release)
    assert code == EXIT_OK, out


def _key(root: Path, name: str) -> Path:
    key = root / f"{name}.key"
    command = ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)]
    subprocess.run(command, check=True)
    return key


def _allowed(root: Path, **keys: Path) -> Path:
    """An allowed-signers file: principal ``release`` for the publisher, the rest as named."""
    lines = []
    for principal, key in keys.items():
        public = key.with_suffix(".key.pub").read_text().strip()
        spaces = (
            "tundravm-evidence,tundravm-release" if principal == "release" else "tundravm-witness"
        )
        lines.append(f'{principal} namespaces="{spaces}" {public}')
    target = root / "allowed_signers"
    target.write_text("\n".join(lines) + "\n")
    return target


def test_witness_matches_a_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    code, out = run_main("witness", str(RELEASE), "--builder", "alice", "--format", "json")
    assert code == EXIT_OK, out
    payload = json.loads(out)
    assert payload["schema"] == "tundravm.witness/1"
    assert payload["verdict"] == "matched"
    assert payload["simulated"] is True
    assert all(row["match"] for row in payload["artifacts"])
    on_disk = json.loads((WITNESSES / "witness-alice.json").read_text())
    assert on_disk == payload
    assert not (RELEASE / "witness-alice.json").exists()  # a release stays immutable


def test_a_different_rebuild_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    module = importlib.import_module("tundravm.witness")  # the function shadows it
    monkeypatch.setattr(module, "_file_sha256", lambda path: "0" * 64)
    code, out = run_main("witness", str(RELEASE), "--builder", "alice")
    assert code == EXIT_FAILURE, out
    assert "verdict different" in out
    assert "diffoscope" in out
    assert Path("build/witness-node-1.0").is_dir()  # kept for comparison


def test_another_recipe_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    recipe = Path("node.py")
    source = recipe.read_text()
    assert 'Package("ca-certificates")' in source
    edited = source.replace(
        'Package("ca-certificates")', 'Package("ca-certificates"), Package("htop")', 1
    )
    recipe.write_text(edited)
    code, _ = run_main("witness", str(RELEASE), "--builder", "alice")
    assert code == EXIT_SDK_ERROR
    assert not (WITNESSES / "witness-alice.json").exists()


def test_a_lockfile_that_is_not_the_release_s_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch)
    index = RELEASE / "release.json"
    payload = json.loads(index.read_text())
    payload["lock_sha256"] = "0" * 64
    index.write_text(json.dumps(payload))
    code, _ = run_main("witness", str(RELEASE), "--builder", "alice")
    assert code == EXIT_SDK_ERROR


def _verify(*flags: str) -> tuple[int, dict[str, object]]:
    code, out = run_main("verify", str(RELEASE), *flags, "--format", "json")
    report = json.loads(out)
    witnesses = report["witnesses"]
    assert isinstance(witnesses, dict)
    return code, witnesses


def _codes(witnesses: dict[str, object]) -> set[str]:
    problems = witnesses["problems"]
    assert isinstance(problems, list)
    return {str(problem["code"]) for problem in problems}


@NEEDS_SSH
def test_two_distinct_signed_witnesses_meet_the_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = _key(tmp_path, "publisher")
    _project(tmp_path, monkeypatch, "--sign", str(publisher))
    alice, bob = _key(tmp_path, "alice"), _key(tmp_path, "bob")
    allowed = _allowed(tmp_path, release=publisher, alice=alice, bob=bob)
    for name, key in (("alice", alice), ("bob", bob)):
        argv = ("witness", str(RELEASE), "--builder", name, "--sign", str(key))
        assert run_main(*argv)[0] == EXIT_OK
    policy = ("--allowed-signers", str(allowed), "--allow-simulated")
    code, witnesses = _verify(*policy, "--min-witnesses", "2")
    assert code == EXIT_OK, witnesses
    assert witnesses["counted"] == ["alice", "bob"]
    code, witnesses = _verify(*policy, "--min-witnesses", "3")
    assert code == EXIT_FAILURE
    assert _codes(witnesses) == {"witness-insufficient"}


@NEEDS_SSH
def test_the_publisher_unsigned_and_simulated_witnesses_do_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = _key(tmp_path, "publisher")
    _project(tmp_path, monkeypatch, "--sign", str(publisher))
    alice = _key(tmp_path, "alice")
    allowed = _allowed(tmp_path, release=publisher, alice=alice)
    argv = ("witness", str(RELEASE), "--builder", "release", "--sign", str(publisher))
    assert run_main(*argv)[0] == EXIT_OK
    assert run_main("witness", str(RELEASE), "--builder", "carol")[0] == EXIT_OK
    argv = ("witness", str(RELEASE), "--builder", "alice", "--sign", str(alice))
    assert run_main(*argv)[0] == EXIT_OK
    policy = ("--allowed-signers", str(allowed), "--min-witnesses", "1")
    code, witnesses = _verify(*policy, "--allow-simulated")
    assert code == EXIT_OK  # alice alone meets a policy of one
    assert witnesses["counted"] == ["alice"]
    assert {"witness-self", "witness-unsigned"} <= _codes(witnesses)
    code, witnesses = _verify(*policy)
    assert code == EXIT_FAILURE  # without --allow-simulated an in-process rebuild is excluded
    assert {"witness-simulated", "witness-insufficient"} <= _codes(witnesses)


def test_witness_flags_need_a_release_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path, monkeypatch)
    assert run_main("evidence")[0] in (EXIT_OK, EXIT_FAILURE)
    code, _ = run_main("verify", "build/evidence", "--min-witnesses", "1")
    assert code == EXIT_SDK_ERROR


def test_verify_passes_max_age_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    seen: dict[str, object] = {}
    module = importlib.import_module("tundravm.release")
    original = module.verify_evidence

    def spy(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "verify_evidence", spy)
    assert run_main("verify", str(RELEASE), "--max-age", "30m")[0] == EXIT_OK
    assert seen["max_age"] == 1800
