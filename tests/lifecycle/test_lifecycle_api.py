"""The declarative lifecycle functions: compile, lint, lock, bake, measure, doctor."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from tundravm._source import Source
from tundravm.backends import Requirement
from tundravm.declarative import (
    Artifact,
    Backend,
    Build,
    Debloat,
    Diagnostic,
    File,
    Fragment,
    Git,
    Install,
    Lock,
    Measurements,
    Package,
    Recipe,
    Tree,
    Unit,
    User,
    Variant,
    bake,
    compile,
    doctor,
    lint,
    lock,
    lock_status,
    lower,
    read_lock,
    write_lock,
)
from tundravm.declarative._compile import emit
from tundravm.declarative.utils import Tdxs
from tundravm.errors import LockfileError, MeasurementError, ValidationError
from tundravm.measure.policy import read_policy
from tundravm.testing import assert_clean, assert_diagnostic, assert_tree
from tundravm.toolchain import CHECKOUT

APP_UNIT = "[Unit]\nDescription=app\n\n[Service]\nUser=app\nExecStart=/usr/bin/true\n"
REPO = "https://example.com/app.git"
SHA = "a" * 40


def _recipe(*, motd: str = "hi\n", build: bool = False) -> Recipe:
    items: list[object] = [
        Package("curl"),
        Package("linux-image-amd64"),
        File("/etc/motd", motd),
        User("app", shell="/bin/false"),
        Unit("app.service", APP_UNIT, enabled=True),
    ]
    if build:
        items.append(
            Build(
                "app", Git(REPO, "main"), script="make", install=(Install("app", "/usr/bin/app"),)
            )
        )
    return Recipe(
        "demo",
        items=tuple(items),  # type: ignore[arg-type]
        variants=(
            Variant("default", outputs=("qemu",)),
            Variant("azure", inherits="default", outputs=("azure",)),
        ),
    )


def _resolver(calls: list[str]) -> object:
    def resolve(source: Source) -> str:
        calls.append(source.requested)
        return SHA

    return resolve


# ── compile ──────────────────────────────────────────────────────────────


def test_compile_tree_round_trip_matches_lowered_compile(tmp_path: Path) -> None:
    recipe = _recipe()
    tree = compile(recipe)
    assert isinstance(tree, Tree) and tree.profiles == ("default", "azure")
    paths = {e.path for e in tree.entries}
    assert {"mkosi.conf", "payload/mkosi.conf", "mkosi.images/azure/mkosi.conf"} <= paths
    assert compile(recipe).digest == tree.digest

    expected = tmp_path / "image"
    emit(lower(recipe).select(("default", "azure")), expected)
    written = tmp_path / "tree"
    (written / "stale").mkdir(parents=True)
    (written / "stale" / "mkosi.conf").write_text("old\n")
    tree.write(written)
    assert not (written / "stale").exists()
    assert_tree(tree, expected)


def test_compile_selected_variants(tmp_path: Path) -> None:
    only = compile(_recipe(), variants=("default",))
    assert only.profiles == ("default",)
    # without profile overlays a variant's tree is the whole tree: the payload and every
    # wrapper are shared, whichever variants compile
    assert only.digest == compile(_recipe()).digest
    with pytest.raises(ValidationError, match="Unknown profile"):
        compile(_recipe(), variants=("gcp",))

    changed = compile(_recipe(motd="bye\n"), variants=("default",))
    assert changed.digest != only.digest
    on_disk = tmp_path / "golden"
    only.write(on_disk)
    with pytest.raises(AssertionError, match="content differs"):
        assert_tree(changed, on_disk)


# ── lint ─────────────────────────────────────────────────────────────────


def test_lint_merges_declarative_and_compiler_diagnostics() -> None:
    def needs_jq(resolved: object) -> tuple[Diagnostic, ...]:
        return (Diagnostic("jq-missing", "warning", "add jq", subject="jq"),)

    recipe = Recipe(
        "demo",
        items=(
            Fragment("tool", items=(Package("curl"),), checks=(needs_jq,)),
            File("/etc/motd", "hi\n"),
            Debloat(extra_remove=("/etc/motd",)),  # deletes a declared file
        ),
    )
    found = lint(recipe)
    codes = [d.code for d in found]
    assert "jq-missing" in codes  # a fragment check
    assert "debloat-removes-declared-file" in codes  # a compiler rule on the lowered image
    assert "backend-missing" not in codes  # the backend is a bake() argument
    assert len(found) == len(set(found))
    assert_diagnostic(found, "jq-missing", subject="jq")
    assert_diagnostic(found, "debloat-removes-declared-file", profile="default")
    with pytest.raises(AssertionError, match="unexpected finding"):
        assert_clean(found)


def test_lint_returns_resolution_errors_without_lowering() -> None:
    recipe = Recipe(
        "demo",
        items=(File("/etc/motd", "a\n"), File("/etc//motd", "b\n")),
    )
    assert [d.code for d in lint(recipe)] == ["identity-collision"]


def test_lint_with_lock_reports_drift() -> None:
    locked = lock(_recipe())
    assert_clean(lint(_recipe(), lock=locked), warnings="allow")
    drifted = lint(_recipe(motd="bye\n"), lock=locked)
    assert_diagnostic(drifted, "lock-changed")


# ── lock ─────────────────────────────────────────────────────────────────


def test_lock_status_read_and_write(tmp_path: Path) -> None:
    calls: list[str] = []
    locked = lock(_recipe(build=True), resolver=_resolver(calls))  # type: ignore[arg-type]
    assert isinstance(locked, Lock) and calls == ["main"]
    assert [(p.identity, p.digest) for p in locked.pins] == [("app", SHA)]
    assert locked.pins[0].source == Git(REPO, "main")
    assert dict(locked.sections) and locked.compiler_version == CHECKOUT
    assert lock_status(_recipe(build=True), locked) == ()

    path = tmp_path / "tundravm.lock"
    write_lock(locked, path)
    assert read_lock(path) == locked
    assert path.read_text() == locked.text()

    stale = lock_status(_recipe(build=True, motd="bye\n"), locked)
    assert stale and all(d.severity == "error" for d in stale)
    assert any(d.subject.endswith("files") for d in stale)


def test_lock_keeps_previous_pins_unless_updated() -> None:
    first: list[str] = []
    locked = lock(_recipe(build=True), resolver=_resolver(first))  # type: ignore[arg-type]
    again: list[str] = []
    kept = lock(_recipe(build=True), previous=locked, resolver=_resolver(again))  # type: ignore[arg-type]
    assert again == [] and kept == locked
    updated: list[str] = []
    lock(_recipe(build=True), previous=locked, update=("app",), resolver=_resolver(updated))  # type: ignore[arg-type]
    assert updated == ["main"]
    with pytest.raises(ValidationError, match="unknown source"):
        lock(_recipe(build=True), previous=locked, update=("ghost",))
    with pytest.raises(LockfileError):
        lock(_recipe(build=True), offline=True)
    assert lock(_recipe(build=True), previous=locked, offline=True) == locked


# ── bake / measure / doctor ──────────────────────────────────────────────


def _baked(tmp_path: Path, lines: list[str] | None = None) -> tuple[Artifact, ...]:
    recipe = _recipe()
    return bake(
        recipe,
        lock=lock(recipe),
        backend=Backend("local"),
        build=tmp_path / "out",
        progress=None if lines is None else lines.append,
    )


def test_bake_writes_the_outputs_each_variant_declares(tmp_path: Path) -> None:
    lines: list[str] = []
    artifacts = _baked(tmp_path, lines)
    # each variant's artifacts are its declared outputs
    assert {(a.profile, a.target) for a in artifacts} == {("default", "qemu"), ("azure", "azure")}
    assert all(a.path.is_file() and len(a.sha256) == 64 for a in artifacts)
    assert all(a.image_digest == lock(_recipe()).image_digest for a in artifacts)
    assert all(a.tree_digest and a.lock_digest for a in artifacts)
    assert any("built 2 profiles" in line for line in lines)


def test_bake_one_variant_against_a_lock_of_every_variant(tmp_path: Path) -> None:
    locked = lock(_recipe())
    assert lock_status(_recipe(), locked, variants=("azure",)) == ()

    artifacts = bake(
        _recipe(), lock=locked, backend=Backend("local"), build=tmp_path, variants=("azure",)
    )

    assert {(a.profile, a.target) for a in artifacts} == {("azure", "azure")}
    with pytest.raises(LockfileError, match="stale"):
        bake(
            _recipe(motd="bye\n"),
            lock=locked,
            backend=Backend("local"),
            build=tmp_path / "stale",
            variants=("azure",),
        )


def test_lock_status_with_resolver_reports_moved_refs() -> None:
    locked = lock(_recipe(build=True), resolver=_resolver([]))  # type: ignore[arg-type]
    assert lock_status(_recipe(build=True), locked, resolver=lambda source: SHA) == ()

    moved = lock_status(_recipe(build=True), locked, resolver=lambda source: "b" * 40)

    assert [(d.code, d.subject) for d in moved] == [("lock-changed", "sources.app")]
    assert moved[0].message.endswith(": aaaaaaa -> bbbbbbb")


def test_measurements_to_dict_and_verify() -> None:
    found = Measurements(
        "qemu",
        (("RTMR1", "aa"), ("RTMR2", "bb")),
        "tundravm-measure 1.0",
        "d1",
        "x.efi",
        registers=(("RTMR1", ("aa",)), ("RTMR2", ("bb",))),
        unverifiable=(("MRTD", "host"), ("RTMR0", "host")),
    )

    assert found.to_dict() == {
        "artifact": "x.efi",
        "artifact_digest": "d1",
        "boot_payload": None,
        "scheme": "qemu",
        "tool": "tundravm-measure 1.0",
        "values": {"RTMR1": "aa", "RTMR2": "bb"},
        "registers": {"RTMR1": ["aa"], "RTMR2": ["bb"]},
        "unverifiable": {"MRTD": "host", "RTMR0": "host"},
    }
    assert found.verify({"RTMR1": "aa", "RTMR2": "bb"}) == ()
    assert found.verify({"RTMR1": "aa", "RTMR2": "cc", "RTMR3": "dd"}) == ("RTMR2", "RTMR3")


def test_measurements_policy_round_trips_into_a_tdxs_verifier(tmp_path: Path) -> None:
    values = {f"RTMR{i}": f"{i:x}" * 96 for i in range(4)}
    mrtd = [{"value": "e" * 96, "firmware": "fw-a"}, {"value": "f" * 96, "firmware": "fw-b"}]
    found = Measurements(
        "gcp",
        tuple(sorted(values.items())),
        "tundravm-measure 1.0",
        "d1",
        "build/x.raw",
        registers=(("MRTD", tuple(mrtd)), *((k, (v,)) for k, v in sorted(values.items()))),
        extra=(("firmware_table", "t1"),),
    )
    policy = found.to_policy()
    assert policy == {
        "schema_version": 2,
        "schema": "tundravm.policy/2",
        "scheme": "gcp",
        "tool": "tundravm-measure",
        "tool_version": "1.0",
        "artifact": {"path": "build/x.raw", "sha256": "d1"},
        "registers": {"MRTD": mrtd, **{k: [v] for k, v in values.items()}},
        "unverifiable": {},
        "firmware_table": "t1",
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    assert read_policy(path) == policy
    with pytest.raises(MeasurementError, match="one value per register"):
        Tdxs.from_policy(path)
    verifier = Tdxs.from_policy(path, firmware="fw-b")
    expected = {name.lower(): value for name, value in values.items()}
    expected["mrtd"] = "f" * 96
    assert verifier.expected_measurements == expected
    assert Tdxs.from_measurements(found, firmware="fw-b").expected_measurements == expected
    placeholder = replace(found, tool="placeholder")
    with pytest.raises(MeasurementError, match="placeholder"):
        placeholder.to_policy()


def test_doctor_with_fake_probe_runner() -> None:
    def ok(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "tool 1.0\n", "")

    def missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "")

    assert doctor(Backend("lima"), runner=ok) == ()
    found = doctor(Backend("lima"), runner=missing)
    assert found and {d.code for d in found} == {"tool-missing"}
    assert any(d.severity == "error" for d in found)
    with pytest.raises(ValidationError, match="Unknown backend"):
        Backend("docker")  # type: ignore[arg-type]
    assert isinstance(Requirement, type)
