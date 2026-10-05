"""The CLI end to end over one ``Recipe`` file: init, inspect, lint, compile, diff, lock, ci,
bake, measure, deploy and doctor."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm.declarative import read_artifacts
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.testing import recipe_file, run_cli

RECIPE_SOURCE = """
from tundravm import File, Fragment, Package, Recipe, Unit, User, Variant
from tundravm.backends.inprocess import InProcessBackend

APP = "[Unit]\\nDescription=app\\n\\n[Service]\\nUser=app\\nExecStart=/usr/bin/true\\n"
backend = InProcessBackend()
recipe = Recipe(
    "demo",
    Fragment("common", items=(
        Package("curl"), Package("linux-image-amd64"), File("/etc/motd", "hi\\n"),
        User("app", shell="/bin/false"),
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
        Package("linux-image-amd64"),
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
    assert code == 0 and "variants:      default, azure\n" in out
    assert "  recipe_digest: " in out and "  tree_digest:   " in out
    assert (tmp_path / "mkosi" / "azure" / "mkosi.conf").is_file()
    code, out, _ = run_cli("compile", cli_recipe, "--out", "mkosi", "--check")
    assert (code, out.strip()) == (0, "tree is up to date with the recipe")
    code, out, _ = run_cli("diff", cli_recipe, "--against", "mkosi")
    assert code == 0

    code, out, _ = run_cli("lock", cli_recipe, "--lockfile", "app.lock")
    assert code == 0 and out.strip() == "locked app.lock"
    code, out, _ = run_cli("lock", cli_recipe, "--lockfile", "app.lock", "--check")
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
    code, out, err = run_cli(*args, "--param", "cpus=4", "--allow-simulated-artifact")
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
    assert run_cli("lock", cli_recipe, "--lockfile", "app.lock", "--variant", "default")[0] == 0
    code, _, err = run_cli("bake", cli_recipe, "--lockfile", "app.lock", "--out", "out", "-q")
    assert code == 2 and "E_LOCKFILE" in err
    code, _, err = run_cli(
        "bake", cli_recipe, "--lockfile", "app.lock", "--out", "out", "--variant", "default", "-q"
    )
    assert code == 0, err


def test_cli_bake_one_variant_against_a_lock_of_every_variant(cli_recipe: Path) -> None:
    assert run_cli("lock", cli_recipe, "--lockfile", "app.lock")[0] == 0
    for variant in ("azure", "default"):
        code, _, err = run_cli(
            "bake", cli_recipe, "--lockfile", "app.lock", "--out", variant, "--variant", variant
        )
        assert code == 0, err
        assert "E_LOCKFILE" not in err


def test_cli_bake_keeps_a_different_lockfile_in_out(cli_recipe: Path, tmp_path: Path) -> None:
    assert run_cli("lock", cli_recipe)[0] == 0  # build/tundravm.lock: every variant
    committed = (tmp_path / "build" / "tundravm.lock").read_text()
    assert run_cli("lock", cli_recipe, "--lockfile", "app.lock", "--variant", "default")[0] == 0
    assert (tmp_path / "app.lock").read_text() != committed
    code, _, err = run_cli("bake", cli_recipe, "--lockfile", "app.lock", "--variant", "default")
    assert code == 0, err
    assert (tmp_path / "build" / "tundravm.lock").read_text() == committed
    payload = json.loads((tmp_path / "build" / "bake-result.json").read_text())
    assert payload["declarative"]["lockfile"] == "app.lock"

    code, _, err = run_cli("bake", cli_recipe, "--out", "fresh", "--lockfile", "app.lock", "-q")
    assert code == 2 and "E_LOCKFILE" in err  # the subset lock is still what the bake reads
    assert (tmp_path / "fresh" / "tundravm.lock").read_text() == (tmp_path / "app.lock").read_text()
