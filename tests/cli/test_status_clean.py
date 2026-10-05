"""``tundravm status`` and ``tundravm clean`` on an in-process recipe and a scratch build dir."""

from __future__ import annotations

import io
import json
import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, source_dir, write_recipe_file
from tundravm import clean as clean_module
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import lock, write_lock
from tundravm.recipe import load_recipe
from tundravm.status import UP_TO_DATE

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "status",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)
"""

SOURCE_RECIPE = """
from tundravm.declarative import Build, Fragment, Git, Install, Package, Recipe

recipe = Recipe(
    "tool",
    Fragment(
        "tool",
        items=(
            Package("linux-image-amd64"),
            Build("tool", Git({url!r}, "main"), script="make", install=(Install("t", "/bin/t"),)),
        ),
    ),
)
"""

NIX_RECIPE = """
from tundravm import Fragment, Package, Recipe
from tundravm.backends import NixMkosiBackend

backend = NixMkosiBackend()
recipe = Recipe("nix", Fragment("common", items=(Package("curl"),)))
"""

BUILD = Path("build")


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def _status(*argv: str) -> dict[str, object]:
    code, out = run_main("status", *argv, "--format", "json")
    assert code == EXIT_OK
    payload: dict[str, object] = json.loads(out)
    return payload


def _verdict(payload: dict[str, object], section: str) -> object:
    entry = payload[section]
    assert isinstance(entry, dict)
    return entry["verdict"]


def _items(payload: dict[str, object], section: str) -> list[dict[str, object]]:
    entry = payload[section]
    assert isinstance(entry, dict)
    items: list[dict[str, object]] = entry["items"]
    return items


def _verdicts(payload: dict[str, object]) -> dict[str, object]:
    sections = ("lint", "lock", "sources", "tree", "artifacts", "backend")
    return {section: _verdict(payload, section) for section in sections}


# ── status ───────────────────────────────────────────────────────────────


def test_status_verdicts_and_next_follow_lock_compile_bake(recipe: Path) -> None:
    fresh = _status(str(recipe))
    assert _verdicts(fresh) == {
        "lint": "ok",
        "lock": "missing",
        "sources": "n/a",
        "tree": "missing",
        "artifacts": "missing",
        "backend": "ok",
    }
    assert fresh["next"] == f"tundravm lock {recipe}"

    assert run_main("lock", str(recipe))[0] == EXIT_OK
    locked = _status(str(recipe))
    assert _verdict(locked, "lock") == "ok"
    assert locked["next"] == f"tundravm compile {recipe} --out {BUILD / 'mkosi'}"

    assert run_main("compile", str(recipe))[0] == EXIT_OK
    compiled = _status(str(recipe))
    assert _verdict(compiled, "tree") == "ok"
    assert compiled["next"] == f"tundravm bake {recipe}"

    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    baked = _status(str(recipe))
    assert _verdicts(baked)["artifacts"] == "ok"
    assert baked["next"] == UP_TO_DATE


def test_status_text_has_one_line_per_item_and_exits_zero(recipe: Path) -> None:
    code, out = run_main("status", str(recipe))
    assert code == EXIT_OK
    labels = [line.split()[0] for line in out.splitlines()]
    assert labels == [
        "recipe",
        "lint",
        "lock",
        "source",
        "tree",
        "artifact",
        "artifact",
        "backend",
        "next:",
    ]
    assert out.splitlines()[2].split()[:2] == ["lock", "missing"]
    assert out.splitlines()[-1] == f"next: tundravm lock {recipe}"


def test_status_json_shape(recipe: Path) -> None:
    assert run_main("bake", str(recipe), "--variant", "default", "-q")[0] == EXIT_OK
    payload = _status(str(recipe))
    assert sorted(payload) == [
        "artifacts",
        "backend",
        "lint",
        "lock",
        "next",
        "recipe",
        "sources",
        "tree",
    ]
    recipe_entry = payload["recipe"]
    assert isinstance(recipe_entry, dict)
    assert recipe_entry["variants"] == ["default", "azure"]
    assert recipe_entry["base"] == "debian/trixie"
    assert len(str(recipe_entry["digest"])) == 64
    lint = payload["lint"]
    assert isinstance(lint, dict)
    assert {lint["errors"], lint["warnings"]} == {0}
    lock_entry = payload["lock"]
    assert isinstance(lock_entry, dict)
    assert lock_entry["present"] is False
    assert lock_entry["unpinned"] == []
    artifacts = payload["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts["present"] is True
    default, azure = _items(payload, "artifacts")
    assert default["verdict"] == "ok"
    assert default["simulated"] is True
    assert default["lockfile"] is None
    assert default["size"] == (BUILD / "default" / "disk.qcow2").stat().st_size
    assert len(str(default["sha256"])) == 64
    assert azure == {
        "verdict": "missing",
        "detail": f"azure: not in {BUILD / 'bake-result.json'}",
        "variant": "azure",
    }
    assert payload["next"] == f"tundravm lock {recipe}"


def test_status_markdown_has_a_table_per_section(recipe: Path) -> None:
    code, out = run_main("status", str(recipe), "--format", "markdown")
    assert code == EXIT_OK
    assert out.startswith("# tundravm status: ")
    headings = [line for line in out.splitlines() if line.startswith("## ")]
    assert headings == [
        "## Recipe",
        "## Lint",
        "## Lock",
        "## Sources",
        "## Tree",
        "## Artifacts",
        "## Backend",
    ]
    assert out.count("| Verdict | Detail |") == len(headings)
    assert out.rstrip().endswith(f"**Next:** `tundravm lock {recipe}`")


def test_status_variant_selects_items_and_next(recipe: Path) -> None:
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "--variant", "default", "-q")[0] == EXIT_OK
    whole = _status(str(recipe))
    assert [item["verdict"] for item in _items(whole, "artifacts")] == ["ok", "missing"]
    assert _verdict(whole, "tree") == "stale"  # the bake wrote only default's tree
    assert whole["next"] == f"tundravm compile {recipe} --out {BUILD / 'mkosi'}"

    one = _status(str(recipe), "--variant", "default")
    recipe_entry = one["recipe"]
    assert isinstance(recipe_entry, dict)
    assert recipe_entry["variants"] == ["default"]
    assert [item["variant"] for item in _items(one, "artifacts")] == ["default"]
    assert one["next"] == UP_TO_DATE

    azure = _status(str(recipe), "--variant", "azure")
    assert azure["next"] == f"tundravm compile {recipe} --variant azure --out {BUILD / 'mkosi'}"


def test_status_flags_artifacts_stale_after_editing_the_recipe(recipe: Path) -> None:
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    assert _status(str(recipe))["next"] == UP_TO_DATE

    recipe.write_text(RECIPE.replace('Package("curl"),', 'Package("curl"), Package("htop"),'))
    edited = _status(str(recipe))
    assert _verdicts(edited)["lock"] == "stale"
    assert _verdicts(edited)["tree"] == "stale"
    for item in _items(edited, "artifacts"):
        assert item["verdict"] == "stale"
        assert item["recipe_matches"] is False
        assert item["tree_matches"] is False
        assert "recipe and tree changed since the bake" in str(item["detail"])
    assert edited["next"] == f"tundravm lock {recipe}"

    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert _status(str(recipe))["next"] == f"tundravm compile {recipe} --out {BUILD / 'mkosi'}"
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    assert _status(str(recipe))["next"] == UP_TO_DATE


def test_status_verify_hashes_artifacts(recipe: Path) -> None:
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    unchecked = _items(_status(str(recipe)), "artifacts")
    assert {item["integrity"] for item in unchecked} == {"unchecked"}
    assert "integrity unchecked (--verify hashes it)" in str(unchecked[0]["detail"])

    verified = _status(str(recipe), "--verify")
    items = _items(verified, "artifacts")
    assert {(item["verdict"], item["integrity"]) for item in items} == {("ok", "verified")}
    assert "integrity verified" in str(items[0]["detail"])
    assert verified["next"] == UP_TO_DATE

    (BUILD / "default" / "disk.qcow2").write_bytes(b"swapped after the bake\n")
    assert {item["verdict"] for item in _items(_status(str(recipe)), "artifacts")} == {"ok"}
    changed = _status(str(recipe), "--verify")
    found = {item["variant"]: item for item in _items(changed, "artifacts")}
    assert (found["default"]["verdict"], found["default"]["integrity"]) == ("stale", "mismatch")
    assert "integrity mismatch" in str(found["default"]["detail"])
    assert found["azure"]["integrity"] == "verified"
    assert changed["next"] == f"tundravm bake {recipe}"


def test_status_out_and_lockfile_are_repeated_in_next(recipe: Path, tmp_path: Path) -> None:
    out = tmp_path / "elsewhere"
    lockfile = tmp_path / "pins.lock"
    assert run_main("lock", str(recipe), "--lockfile", str(lockfile))[0] == EXIT_OK
    payload = _status(str(recipe), "--out", str(out), "--lockfile", str(lockfile))
    assert payload["next"] == (
        f"tundravm compile {recipe} --out {out / 'mkosi'} --lockfile {lockfile}"
    )
    tree = payload["tree"]
    assert isinstance(tree, dict)
    assert tree["path"] == str(out / "mkosi")


def _git(*args: str, cwd: Path) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def test_status_reports_source_checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "main.go").write_text("package main\n", encoding="utf-8")
    _git("add", ".", cwd=upstream)
    _git("commit", "-q", "-m", "one", cwd=upstream)
    pin = _git("rev-parse", "HEAD", cwd=upstream)
    path = write_recipe_file(tmp_path, SOURCE_RECIPE.format(url=upstream.as_uri()), monkeypatch)

    unlocked = _status(str(path))
    lock_entry = unlocked["lock"]
    assert isinstance(lock_entry, dict)
    assert lock_entry["unpinned"] == ["tool"]
    (source,) = _items(unlocked, "sources")
    assert (source["verdict"], source["pin"]) == ("missing", None)

    write_lock(lock(load_recipe(path), resolver=lambda _: pin), BUILD / "tundravm.lock")
    locked = _status(str(path))
    (source,) = _items(locked, "sources")
    assert source["verdict"] == "missing"
    assert source["path"] == str(BUILD / ".sources" / source_dir("tool", pin, upstream.as_uri()))
    assert locked["next"] == f"tundravm fetch {path}"
    assert _verdict(locked, "backend") == "n/a"

    assert main(["fetch", str(path)], stdout=io.StringIO()) == EXIT_OK
    fetched = _status(str(path))
    (source,) = _items(fetched, "sources")
    assert (source["verdict"], source["fetched"]) == ("ok", True)
    assert fetched["next"] == f"tundravm compile {path} --out {BUILD / 'mkosi'}"


def test_status_probes_the_backend_with_the_injected_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_recipe_file(tmp_path, NIX_RECIPE, monkeypatch)
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(list(argv), 1, "", "")

    out = io.StringIO()
    assert main(["status", str(path), "--format", "json"], stdout=out, runner=runner) == EXIT_OK
    backend = json.loads(out.getvalue())["backend"]
    assert backend["verdict"] == "missing"
    assert backend["name"] == "nix_mkosi"
    assert backend["missing"]
    assert calls


# ── clean ────────────────────────────────────────────────────────────────


@pytest.fixture
def baked(recipe: Path) -> Path:
    """The recipe locked and baked into ``build/``, with mkosi state and a checkout added."""
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    (BUILD / ".mkosi" / "mkosi.tools").mkdir(parents=True)
    (BUILD / "mkosi" / "default" / "mkosi.cache").mkdir()
    (BUILD / ".sources" / "tool-0123456789ab").mkdir(parents=True)
    return recipe


def _listing(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.iterdir())


def test_clean_without_flags_lists_everything_and_removes_nothing(baked: Path) -> None:
    before = _listing(BUILD)
    code, out = run_main("clean", str(baked))
    assert code == EXIT_OK
    assert out.splitlines() == [
        f"would remove {BUILD / '.sources'}",
        f"would remove {BUILD / 'mkosi'}",
        f"would remove {BUILD / 'azure'}",
        f"would remove {BUILD / 'default'}",
        f"would remove {BUILD / 'bake-result.json'}",
        f"would remove {BUILD / '.mkosi'}",
        "pass --all to remove them, or --sources, --tree, --artifacts or --state for some "
        "(the lockfile stays unless --lockfile names it)",
    ]
    assert _listing(BUILD) == before


def test_clean_dry_run_then_real(baked: Path) -> None:
    code, out = run_main("clean", "--out", "build", "--artifacts", "--state", "--dry-run")
    assert code == EXIT_OK
    assert "would remove build/default" in out.splitlines()
    assert f"would remove {BUILD / 'mkosi' / 'default' / 'mkosi.cache'}" in out.splitlines()
    assert (BUILD / "default").is_dir()

    code, out = run_main("clean", "--out", "build", "--artifacts", "--state")
    assert code == EXIT_OK
    assert out.splitlines() == [
        f"removed {BUILD / 'azure'}",
        f"removed {BUILD / 'default'}",
        f"removed {BUILD / 'bake-result.json'}",
        f"removed {BUILD / '.mkosi'}",
        f"removed {BUILD / 'mkosi' / 'default' / 'mkosi.cache'}",
    ]
    assert _listing(BUILD) == [".sources", "mkosi", "tundravm.lock"]
    assert (BUILD / "mkosi" / "default" / "mkosi.conf").is_file()


def test_clean_all_keeps_the_lockfile_unless_named(baked: Path) -> None:
    code, _ = run_main("clean", str(baked), "--all")
    assert code == EXIT_OK
    assert _listing(BUILD) == ["tundravm.lock"]
    assert _status(str(baked))["next"] == f"tundravm compile {baked} --out {BUILD / 'mkosi'}"

    code, out = run_main("clean", str(baked), "--all", "--lockfile", "build/tundravm.lock")
    assert (code, out) == (EXIT_OK, "removed build/tundravm.lock\n")
    assert _listing(BUILD) == []
    assert run_main("clean", str(baked), "--all")[1] == "nothing to clean in build\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can delete read-only directories")
def test_clean_reports_an_undeletable_path_without_sudo(
    baked: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked_dir = BUILD / ".mkosi"
    locked_dir.chmod(0o555)
    monkeypatch.setattr(clean_module, "which", lambda _: None)
    try:
        code, out = run_main("clean", str(baked), "--state")
    finally:
        locked_dir.chmod(0o755)
    assert code == EXIT_FAILURE
    lines = out.splitlines()
    assert lines[0].startswith(f"not removed {locked_dir}: ")
    assert "not writable by this user and sudo is not available" in lines[0]
    assert lines[-1] == f"hint: remove them as their owner, e.g. `sudo rm -rf -- {locked_dir}`"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can delete read-only directories")
def test_clean_falls_back_to_sudo(baked: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    locked_dir = BUILD / ".mkosi"
    locked_dir.chmod(0o555)
    calls: list[list[str]] = []

    def fake_sudo(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        locked_dir.chmod(0o755)
        subprocess.run(["rm", "-rf", "--", argv[-1]], check=True)
        return 0

    monkeypatch.setattr(clean_module, "which", lambda _: "/usr/bin/sudo")
    monkeypatch.setattr(clean_module, "run_sudo", fake_sudo)
    code, out = run_main("clean", str(baked), "--state")
    assert code == EXIT_OK
    assert calls == [["sudo", "rm", "-rf", "--", str(locked_dir)]]
    assert f"removed {locked_dir}" in out.splitlines()
    assert not locked_dir.exists()


def test_clean_needs_a_recipe_or_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_main("clean", "--all")[0] == EXIT_SDK_ERROR
    assert "clean needs RECIPE or --out DIR." in capsys.readouterr().err
