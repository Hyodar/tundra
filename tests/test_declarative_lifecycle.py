"""The declarative lifecycle functions and the CLI grammar over a ``Recipe`` file."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import tundravm
from tundravm._source import Source
from tundravm.backends import Requirement
from tundravm.declarative import (
    Artifact,
    Azure,
    Backend,
    Build,
    Debloat,
    Diagnostic,
    File,
    Fragment,
    Gcp,
    Git,
    Install,
    Lock,
    Measurements,
    Package,
    Qemu,
    Recipe,
    Tree,
    Unit,
    User,
    Variant,
    bake,
    compile,
    deploy,
    diff,
    doctor,
    lint,
    lock,
    lock_status,
    lower,
    measure,
    read_artifacts,
    read_lock,
    write_lock,
)
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.errors import DeploymentError, LockfileError, MeasurementError, ValidationError
from tundravm.measure import PlaceholderMeasurementWarning
from tundravm.testing import (
    assert_clean,
    assert_diagnostic,
    assert_tree,
    fake_bake,
    recipe_file,
    run_cli,
)

APP_UNIT = "[Unit]\nDescription=app\n\n[Service]\nUser=app\nExecStart=/usr/bin/true\n"
REPO = "https://example.com/app.git"
SHA = "a" * 40


def _recipe(*, motd: str = "hi\n", build: bool = False) -> Recipe:
    items: list[object] = [
        Package("curl"),
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
        Fragment("common", items=tuple(items)),  # type: ignore[arg-type]
        variants=(
            Variant("default", target="qemu"),
            Variant("azure", parent="default", target="azure"),
        ),
    )


def _resolver(calls: list[str]) -> object:
    def resolve(source: Source) -> str:
        calls.append(source.requested)
        return SHA

    return resolve


# ── compile / diff ───────────────────────────────────────────────────────


def test_compile_tree_round_trip_matches_image_compile(tmp_path: Path) -> None:
    recipe = _recipe()
    tree = compile(recipe)
    assert isinstance(tree, Tree) and tree.variants == ("default", "azure")
    assert any(e.path == "default/mkosi.conf" for e in tree.entries)
    assert compile(recipe).digest == tree.digest

    expected = tmp_path / "image"
    lower(recipe).compile(expected, profiles=("default", "azure"))
    written = tmp_path / "tree"
    (written / "stale").mkdir(parents=True)
    (written / "stale" / "mkosi.conf").write_text("old\n")
    tree.write(written)
    assert not (written / "stale").exists()
    assert_tree(tree, expected)
    assert diff(tree, expected) == ""


def test_compile_selected_variants_and_diff(tmp_path: Path) -> None:
    only = compile(_recipe(), variants=("default",))
    assert only.variants == ("default",)
    assert not any(e.path.startswith("azure/") for e in only.entries)
    with pytest.raises(ValidationError, match="Unknown variant"):
        compile(_recipe(), variants=("gcp",))

    changed = compile(_recipe(motd="bye\n"), variants=("default",))
    text = diff(changed, only)
    assert "-hi" in text and "+bye" in text
    on_disk = tmp_path / "golden"
    only.write(on_disk)
    assert diff(changed, on_disk) == text
    with pytest.raises(AssertionError, match="content differs"):
        assert_tree(changed, on_disk)


# ── lint ─────────────────────────────────────────────────────────────────


def test_lint_merges_declarative_and_compiler_diagnostics() -> None:
    def needs_jq(resolved: object) -> tuple[Diagnostic, ...]:
        return (Diagnostic("jq-missing", "add jq", level="warning", subject="jq"),)

    recipe = Recipe(
        "demo",
        Fragment(
            "common",
            items=(
                Fragment("tool", items=(Package("curl"),), checks=(needs_jq,)),
                File("/etc/motd", "hi\n"),
                Debloat(extra_remove=("/etc/motd",)),  # deletes a declared file
            ),
        ),
    )
    found = lint(recipe)
    codes = [d.code for d in found]
    assert "jq-missing" in codes  # a fragment check
    assert "debloat-removes-declared-file" in codes  # a compiler rule on the lowered image
    assert "backend-missing" not in codes  # the backend is a bake() argument
    assert len(found) == len(set(found))
    assert_diagnostic(found, "jq-missing", subject="jq")
    assert_diagnostic(found, "debloat-removes-declared-file", variant="default")
    with pytest.raises(AssertionError, match="unexpected finding"):
        assert_clean(found)


def test_lint_returns_resolution_errors_without_lowering() -> None:
    recipe = Recipe(
        "demo",
        Fragment("common", items=(File("/etc/motd", "a\n"), File("/etc//motd", "b\n"))),
    )
    assert [d.code for d in lint(recipe)] == ["identity-collision"]


def test_lint_with_lock_reports_drift() -> None:
    locked = lock(_recipe())
    assert_clean(lint(_recipe(), lock=locked), strict=False)
    drifted = lint(_recipe(motd="bye\n"), lock=locked)
    assert_diagnostic(drifted, "lock-changed")


# ── lock ─────────────────────────────────────────────────────────────────


def test_lock_status_read_and_write(tmp_path: Path) -> None:
    calls: list[str] = []
    locked = lock(_recipe(build=True), resolver=_resolver(calls))  # type: ignore[arg-type]
    assert isinstance(locked, Lock) and calls == ["main"]
    assert [(p.identity, p.digest) for p in locked.pins] == [("app", SHA)]
    assert locked.pins[0].source == Git(REPO, "main")
    assert dict(locked.sections) and locked.compiler_version == tundravm.__version__
    assert lock_status(_recipe(build=True), locked) == ()

    path = tmp_path / "tundravm.lock"
    write_lock(locked, path)
    assert read_lock(path) == locked
    assert path.read_text() == locked.text()

    stale = lock_status(_recipe(build=True, motd="bye\n"), locked)
    assert stale and all(d.level == "error" for d in stale)
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


# ── bake / measure / deploy / doctor ─────────────────────────────────────


def _baked(tmp_path: Path, lines: list[str] | None = None) -> tuple[Artifact, ...]:
    recipe = _recipe()
    return bake(
        recipe,
        locked=lock(recipe),
        backend=Backend("inprocess"),
        out=tmp_path / "out",
        progress=None if lines is None else lines.append,
    )


def test_bake_in_process_writes_simulated_artifacts(tmp_path: Path) -> None:
    lines: list[str] = []
    artifacts = _baked(tmp_path, lines)
    assert {(a.variant, a.target) for a in artifacts} == {("default", "qemu"), ("azure", "azure")}
    assert all(a.simulated and a.path.is_file() and len(a.sha256) == 64 for a in artifacts)
    assert all(a.recipe_digest == lock(_recipe()).recipe_digest for a in artifacts)
    assert all(a.tree_digest and a.lock_digest for a in artifacts)
    assert any("baked 2 variants" in line for line in lines)
    assert read_artifacts(tmp_path / "out" / "bake-result.json") == artifacts
    assert read_artifacts(tmp_path / "out") == artifacts


def test_bake_refuses_a_stale_lock(tmp_path: Path) -> None:
    with pytest.raises(LockfileError, match="stale"):
        bake(
            _recipe(motd="bye\n"),
            locked=lock(_recipe()),
            backend=Backend("inprocess"),
            out=tmp_path / "out",
        )


def test_bake_one_variant_against_a_lock_of_every_variant(tmp_path: Path) -> None:
    locked = lock(_recipe())
    assert lock_status(_recipe(), locked, variants=("azure",)) == ()

    artifacts = bake(
        _recipe(), locked=locked, backend=Backend("inprocess"), out=tmp_path, variants=("azure",)
    )

    assert {(a.variant, a.target) for a in artifacts} == {("azure", "azure")}
    with pytest.raises(LockfileError, match="stale"):
        bake(
            _recipe(motd="bye\n"),
            locked=locked,
            backend=Backend("inprocess"),
            out=tmp_path / "stale",
            variants=("azure",),
        )


def test_lock_status_with_resolver_reports_moved_refs() -> None:
    locked = lock(_recipe(build=True), resolver=_resolver([]))  # type: ignore[arg-type]
    assert lock_status(_recipe(build=True), locked, resolver=lambda source: SHA) == ()

    moved = lock_status(_recipe(build=True), locked, resolver=lambda source: "b" * 40)

    assert [(d.code, d.subject) for d in moved] == [("lock-changed", "sources.app")]
    assert moved[0].message.endswith(": aaaaaaa -> bbbbbbb")


def test_measurements_to_json_and_verify(tmp_path: Path) -> None:
    found = Measurements("rtmr", (("rtmr0", "aa"), ("rtmr1", "bb")), "dstack-mr 1.0", "d1")
    path = tmp_path / "out" / "measurements.json"

    text = found.to_json(path)

    assert path.read_text() == text
    assert json.loads(text) == {
        "artifact_digest": "d1",
        "scheme": "rtmr",
        "tool": "dstack-mr 1.0",
        "values": {"rtmr0": "aa", "rtmr1": "bb"},
    }
    assert found.verify({"rtmr0": "aa", "rtmr1": "bb"}) == ()
    assert found.verify({"rtmr0": "aa", "rtmr1": "cc", "rtmr2": "dd"}) == ("rtmr1", "rtmr2")


def test_measure_rejects_simulated_unless_allowed(tmp_path: Path) -> None:
    artifact = next(a for a in _baked(tmp_path) if a.target == "qemu")
    with pytest.raises(MeasurementError, match="simulated"):
        measure(artifact)
    with pytest.warns(PlaceholderMeasurementWarning):
        found = measure(artifact, scheme="gcp", allow_placeholder=True)
    assert found.scheme == "gcp" and found.tool == "placeholder"
    assert found.artifact_digest == artifact.sha256 and found.values


def test_deploy_with_fake_qemu_runner(tmp_path: Path) -> None:
    artifact = next(a for a in _baked(tmp_path) if a.target == "qemu")
    seen: list[Sequence[str]] = []

    def fake_qemu(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    adapter = QemuDeployAdapter(runner=fake_qemu)
    with pytest.raises(DeploymentError, match="simulated"):
        deploy(artifact, using=Qemu(), adapter=adapter)
    with pytest.raises(DeploymentError, match="Cannot deploy a qemu artifact to azure"):
        deploy(artifact, using=Azure("acct"), allow_placeholder=True, adapter=adapter)
    found = deploy(
        artifact, using=Qemu(memory="4G", ssh_port=2223), allow_placeholder=True, adapter=adapter
    )
    assert found.target == "qemu" and found.id.startswith("qemu-default-")
    assert seen and "4G" in " ".join(seen[0]) and "2223" in " ".join(seen[0])


def test_doctor_with_fake_probe_runner() -> None:
    def ok(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "tool 1.0\n", "")

    def missing(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 1, "", "")

    assert doctor(Backend("lima"), runner=ok) == ()
    assert doctor(Backend("inprocess"), runner=missing) == ()
    found = doctor(Backend("lima"), runner=missing)
    assert found and {d.code for d in found} == {"tool-missing"}
    assert any(d.level == "error" for d in found)
    with pytest.raises(ValidationError, match="Unknown backend"):
        Backend("docker")  # type: ignore[arg-type]
    assert isinstance(Requirement, type)


def test_fake_bake_artifact_round_trips(tmp_path: Path) -> None:
    tree = compile(_recipe(), variants=("default",))
    artifact = fake_bake(tree, variant="default", target="qemu", out=tmp_path)
    assert artifact.simulated and artifact.tree_digest == tree.digest
    assert read_artifacts(tmp_path) == (artifact,)
    with pytest.raises(MeasurementError):
        measure(artifact)
    assert Gcp("p", "b").zone == "us-central1-a"


# ── CLI grammar ──────────────────────────────────────────────────────────

RECIPE_SOURCE = """
from tundravm import File, Fragment, Package, Recipe, Unit, User, Variant
from tundravm.backends.inprocess import InProcessBackend

APP = "[Unit]\\nDescription=app\\n\\n[Service]\\nUser=app\\nExecStart=/usr/bin/true\\n"
backend = InProcessBackend()
recipe = Recipe(
    "demo",
    Fragment("common", items=(
        Package("curl"), File("/etc/motd", "hi\\n"), User("app", shell="/bin/false"),
        Unit("app.service", APP, enabled=True),
    )),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
    ),
)
"""


@pytest.fixture
def cli_recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return recipe_file(tmp_path, RECIPE_SOURCE)


def test_cli_init_inspect_and_lint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, out, _ = run_cli("init", "proj", "--name", "demo", "--backend", "inprocess")
    assert code == 0 and (tmp_path / "proj" / "demo.py").is_file()
    recipe = tmp_path / "proj" / "demo.py"

    code, out, _ = run_cli("inspect", recipe, "--json")
    payload = json.loads(out)
    assert code == 0 and set(payload["variants"]) == {"default", "dev"}
    assert len(payload["digest"]) == 64
    code, one, _ = run_cli("inspect", recipe, "--json", "--variant", "default")
    assert set(json.loads(one)["variants"]) == {"default"}
    assert json.loads(one)["digest"] != payload["digest"]
    code, text, _ = run_cli("inspect", recipe, "--format", "markdown")
    assert code == 0 and text.startswith("# tundravm: `demo.py`")

    code, out, _ = run_cli("lint", recipe)
    assert code == 0, out
    code, _, err = run_cli("lint", recipe, "--variant", "ghost")
    assert code == 2 and "Unknown variant" in err


def test_cli_lint_shows_declarative_and_fragment_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = """
    from tundravm import Debloat, Diagnostic, File, Fragment, Package, Recipe

    def warn(resolved):
        return (Diagnostic("custom-warning", "from a fragment", level="warning"),)

    recipe = Recipe("demo", Fragment("common", items=(
        Fragment("tool", items=(Package("curl"),), checks=(warn,)),
        File("/etc/motd", "hi\\n"),
        Debloat(extra_remove=("/etc/motd",)),
    )))
    """
    path = recipe_file(tmp_path, source)
    code, out, _ = run_cli("lint", path, "--format", "text")
    assert code == 0
    assert "warning custom-warning [default]" in out and "debloat-removes-declared-file" in out
    assert run_cli("lint", path, "--strict")[0] == 1
    code, out, _ = run_cli("lint", path, "--json")
    codes = {d["code"] for d in json.loads(out)["diagnostics"]}
    assert {"custom-warning", "debloat-removes-declared-file"} <= codes

    broken = recipe_file(
        tmp_path,
        """
        from tundravm import File, Fragment, Recipe
        recipe = Recipe("demo", Fragment("c", items=(File("/etc/a", "1"), File("/etc//a", "2"))))
        """,
        name="broken.py",
    )
    code, out, _ = run_cli("lint", broken, "--format", "text")
    assert code == 1 and "error identity-collision [common]" in out


def test_cli_compile_diff_lock_ci(cli_recipe: Path, tmp_path: Path) -> None:
    code, out, _ = run_cli("compile", cli_recipe, "--out", "mkosi")
    assert code == 0 and "variants: default, azure" in out
    assert (tmp_path / "mkosi" / "azure" / "mkosi.conf").is_file()
    code, out, _ = run_cli("compile", cli_recipe, "--out", "mkosi", "--check")
    assert (code, out.strip()) == (0, "tree is up to date with the recipe")
    code, out, _ = run_cli("diff", cli_recipe, "--against", "mkosi")
    assert code == 0

    code, out, _ = run_cli("lock", cli_recipe, "--path", "app.lock")
    assert code == 0 and out.strip() == "locked app.lock"
    code, out, _ = run_cli("lock", cli_recipe, "--path", "app.lock", "--check")
    assert (code, out.strip()) == (0, "lock is up to date")
    code, _, err = run_cli("lock", cli_recipe, "--offline", "--check")
    assert code == 2 and "not allowed with" in err

    code, out, _ = run_cli("compile", cli_recipe, "--out", "mkosi2", "--lockfile", "app.lock")
    assert code == 0
    code, out, _ = run_cli("ci", cli_recipe, "--out", "mkosi", "--lockfile", "app.lock")
    assert code == 0, out
    assert [line.split(":")[0] for line in out.splitlines()] == ["ok lint", "ok compile", "ok lock"]


def test_cli_bake_measure_deploy_doctor(
    cli_recipe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_cli("lock", cli_recipe)[0] == 0
    code, out, err = run_cli("bake", cli_recipe, "--out", "out", "--backend", "inprocess", "-q")
    assert code == 0, err
    manifest = tmp_path / "out" / "bake-result.json"
    assert f"next: tundravm deploy {Path('out') / 'bake-result.json'}" in out
    assert {a.variant for a in read_artifacts(manifest)} == {"default", "azure"}

    code, _, err = run_cli("measure", manifest, "--variant", "default")
    assert code == 2 and "simulated" in err
    code, out, err = run_cli(
        "measure", manifest, "--variant", "azure", "--scheme", "azure", "--allow-placeholder"
    )
    assert code == 0 and out.startswith("measurements azure (azure)") and "PLACEHOLDER" in err
    code, _, err = run_cli("measure", manifest, "--scheme", "rtmr")
    assert code == 2 and "pass --variant" in err

    def fake_qemu(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(
        "tundravm.declarative.lifecycle.get_adapter",
        lambda target: QemuDeployAdapter(runner=fake_qemu),
    )
    args = ("deploy", manifest, "--variant", "default", "--target", "qemu")
    code, _, err = run_cli(*args, "--param", "cpus=4")
    assert code == 2 and "simulated" in err
    code, out, err = run_cli(*args, "--param", "cpus=4", "--allow-placeholder")
    assert code == 0, err
    assert out.splitlines()[0] == "deployed default to qemu"
    code, _, err = run_cli(*args, "--param", "cores=4")
    assert code == 2 and "Unknown qemu parameter" in err
    code, _, err = run_cli(*args, "--param", "cpus=many")
    assert code == 2 and "integer" in err
    code, _, err = run_cli("deploy", manifest, "--variant", "azure", "--target", "azure")
    assert code == 2 and "storage_account" in err

    code, out, _ = run_cli("doctor", "--backend", "inprocess")
    assert code == 0 and "backend inprocess: available" in out
    code, out, _ = run_cli("doctor", cli_recipe)
    assert code == 0 and "lint: no findings" in out


def test_cli_bake_frozen_against_a_stale_lockfile(cli_recipe: Path, tmp_path: Path) -> None:
    assert run_cli("lock", cli_recipe, "--path", "app.lock", "--variant", "default")[0] == 0
    code, _, err = run_cli("bake", cli_recipe, "--lockfile", "app.lock", "--out", "out", "-q")
    assert code == 2 and "E_LOCKFILE" in err
    code, _, err = run_cli(
        "bake", cli_recipe, "--lockfile", "app.lock", "--out", "out", "--variant", "default", "-q"
    )
    assert code == 0, err


def test_cli_bake_one_variant_against_a_lock_of_every_variant(cli_recipe: Path) -> None:
    assert run_cli("lock", cli_recipe, "--path", "app.lock")[0] == 0
    for variant in ("azure", "default"):
        code, _, err = run_cli(
            "bake", cli_recipe, "--lockfile", "app.lock", "--out", variant, "--variant", variant
        )
        assert code == 0, err
        assert "E_LOCKFILE" not in err


def test_cli_bake_keeps_a_different_lockfile_in_out(cli_recipe: Path, tmp_path: Path) -> None:
    assert run_cli("lock", cli_recipe)[0] == 0  # build/tundravm.lock: every variant
    committed = (tmp_path / "build" / "tundravm.lock").read_text()
    assert run_cli("lock", cli_recipe, "--path", "app.lock", "--variant", "default")[0] == 0
    assert (tmp_path / "app.lock").read_text() != committed
    code, _, err = run_cli("bake", cli_recipe, "--lockfile", "app.lock", "--variant", "default")
    assert code == 0, err
    assert (tmp_path / "build" / "tundravm.lock").read_text() == committed
    payload = json.loads((tmp_path / "build" / "bake-result.json").read_text())
    assert payload["declarative"]["lockfile"] == "app.lock"

    code, _, err = run_cli("bake", cli_recipe, "--out", "fresh", "--lockfile", "app.lock", "-q")
    assert code == 2 and "E_LOCKFILE" in err  # the subset lock is still what the bake reads
    assert (tmp_path / "fresh" / "tundravm.lock").read_text() == (tmp_path / "app.lock").read_text()
