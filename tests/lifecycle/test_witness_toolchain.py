"""Evidence and witnesses record the toolchain; ``verify --min-witnesses`` compares them."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tests.lifecycle.test_witness import (
    NEEDS_SSH,
    RELEASE,
    _allowed,
    _codes,
    _key,
    _project,
    _verify,
)
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.evidence import toolchain_summary
from tundravm.toolchain import MkosiTool, NixpkgsPin, Toolchain
from tundravm.witness import STRENGTHENS_NOTE, Witness

LOCKED = Toolchain(
    tundravm="0.1.0+aa9d9dc",
    mkosi=MkosiTool("25.3", "nix", "56c02bc00adcf003215cc4bd"),
    systemd="257",
    nixpkgs=NixpkgsPin("56c02bc00adcf003215cc4bd", "sha256-9i/VTd="),
)
OTHER = Toolchain(
    tundravm="0.1.0+aa9d9dc",
    mkosi=MkosiTool("26", "host"),
    systemd="258",
)


def _witness(toolchain: Toolchain | None, *, matched: bool = True) -> Witness:
    row = {"variant": "default", "target": "uki", "path": "a.efi", "expected": "a" * 64}
    return Witness(
        release="node-1.0",
        recipe="node",
        version="1.0",
        manifest_sha256="m" * 64,
        recipe_digest="r" * 64,
        lock_sha256="l" * 64,
        builder="alice",
        backend={"name": "nix_mkosi"},
        simulated=False,
        artifacts=({**row, "actual": "a" * 64 if matched else "b" * 64, "match": matched},),
        verdict="matched" if matched else "different",
        created="2026-10-18T00:00:00Z",
        toolchain=toolchain,
        release_toolchain=LOCKED,
    )


def test_toolchain_summary_names_each_tool() -> None:
    assert toolchain_summary(None) == "not recorded"
    text = toolchain_summary(LOCKED.payload())
    assert text.startswith("mkosi 25.3 (nix @56c02bc00adc), systemd 257, nixpkgs 56c02bc00adc")
    assert text.endswith("tundravm 0.1.0+aa9d9dc")
    rosetta = Toolchain(tundravm="0.1.0", rosetta=True)
    assert toolchain_summary(rosetta.payload()).endswith("reproducible: not guaranteed (rosetta)")


def test_a_witness_reports_the_same_toolchain() -> None:
    found = _witness(LOCKED)
    assert "  toolchain same" in found.render()
    assert found.to_dict()["toolchain_comparison"] == []
    assert STRENGTHENS_NOTE not in found.render()


def test_a_match_on_another_toolchain_strengthens_the_release() -> None:
    text = _witness(OTHER).render()
    assert "toolchain different (mkosi 25.3 (nix @56c02bc00adc) vs 26 (host)" in text
    assert "nixpkgs 56c02bc00adc vs none" in text
    assert STRENGTHENS_NOTE in text
    assert STRENGTHENS_NOTE not in _witness(OTHER, matched=False).render()
    rows = _witness(OTHER).to_dict()["toolchain_comparison"]
    assert {"field": "systemd", "left": "257", "right": "258"} in rows  # type: ignore[operator]


def test_a_witness_without_a_toolchain_says_so() -> None:
    assert "toolchain unknown (the witness recorded no toolchain)" in _witness(None).render()


def test_evidence_shows_the_bake_toolchain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, monkeypatch)
    code, out = run_main("evidence")
    assert code in (EXIT_OK, EXIT_FAILURE), out
    line = next(row for row in out.splitlines() if row.startswith("  toolchain  "))
    assert "tundravm " in line
    code, out = run_main("evidence", "--format", "json")
    index = json.loads(out)
    index = index.get("evidence", index)
    assert index["bake"]["toolchain"]["tundravm"]


@NEEDS_SSH
def test_min_witnesses_counts_the_release_toolchain_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher = _key(tmp_path, "publisher")
    _project(tmp_path, monkeypatch, "--sign", str(publisher))
    keys = {name: _key(tmp_path, name) for name in ("alice", "bob", "carol")}
    allowed = _allowed(tmp_path, release=publisher, **keys)
    bake = importlib.import_module("tundravm.declarative._bake")
    for name, key in keys.items():
        with monkeypatch.context() as patch:
            if name == "bob":  # bob rebuilds on another toolchain
                patch.setattr(bake, "running_toolchain", lambda *_a, **_k: OTHER)
            argv = ("witness", str(RELEASE), "--builder", name, "--sign", str(key))
            assert run_main(*argv)[0] == EXIT_OK
    policy = ("--allowed-signers", str(allowed), "--allow-simulated", "--min-witnesses")
    code, witnesses = _verify(*policy, "2")
    assert code == EXIT_OK, witnesses
    assert witnesses["toolchains"] == "same"
    assert witnesses["counted"] == ["alice", "carol"]
    assert _codes(witnesses) == {"witness-toolchain-different"}
    notes = {note["code"] for note in witnesses["notes"]}  # type: ignore[attr-defined]
    assert "toolchain-diverse" in notes
    code, witnesses = _verify(*policy, "2", "--distinct-toolchains")
    assert code == EXIT_OK, witnesses
    assert witnesses["counted"] == ["alice", "bob"]
    assert _codes(witnesses) == {"witness-toolchain-repeated"}  # carol: alice's toolchain
    code, witnesses = _verify(*policy, "3", "--distinct-toolchains")
    assert code == EXIT_FAILURE
    code, text = run_main("verify", str(RELEASE), *policy, "2")
    assert "on the release's toolchain" in text
