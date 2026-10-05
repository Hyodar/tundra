"""A build whose source differs between variants is pinned per variant: ``<variant>/<name>``."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import tundravm._source as source_module
from tests.helpers import run_main, source_dir, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.declarative import (
    Build,
    Fragment,
    Git,
    Install,
    Package,
    Recipe,
    Variant,
    compile,
    fetch,
    lock,
    lock_status,
    lower,
    read_lock,
)

pytestmark = pytest.mark.usefixtures("isolated_cwd")

URL = "https://git.example/app"
PINS = {"stable": "a" * 40, "dev": "b" * 40, "next": "c" * 40}
STABLE = Build("app", Git(URL, "stable"), script="make", install=(Install("app", "/usr/bin/app"),))


def _recipe(dev_ref: str = "dev", url: str = URL) -> Recipe:
    stable = replace(STABLE, source=Git(url, "stable"))
    return Recipe(
        "pins",
        Fragment("common", items=(Package("linux-image-amd64"), stable)),
        variants=(
            Variant("default", target="qemu"),
            Variant("dev", replace=(replace(stable, source=Git(url, dev_ref)),)),
            Variant("prod"),
        ),
    )


def _resolve(source: object) -> str:
    return PINS[source.ref]  # type: ignore[attr-defined]


def _contents(recipe: Recipe, variant: str, locked: object) -> str:
    tree = compile(recipe, variants=[variant], lock=locked)  # type: ignore[arg-type]
    return "\n".join(
        e.content.decode() for e in tree.entries if e.content and e.path.startswith(f"{variant}/")
    )


def test_each_variant_keeps_its_own_pin() -> None:
    recipe = _recipe()
    locked = lock(recipe, resolver=_resolve)
    assert [(f.name, f.ref, f.digest) for f in locked.lockfile.fetches] == [
        ("default/app", "stable", PINS["stable"]),
        ("dev/app", "dev", PINS["dev"]),
    ]
    assert lower(recipe).local_sources == frozenset({"app"})
    assert lock_status(recipe, locked) == ()
    assert lock_status(recipe, locked, variants=["dev"]) == ()
    assert lock_status(recipe, locked, variants=["prod"]) == ()


def test_each_variant_compiles_its_own_hook() -> None:
    recipe = _recipe()
    locked = lock(recipe, resolver=_resolve)
    dev = _contents(recipe, "dev", locked)
    default = _contents(recipe, "default", locked)
    assert source_dir("app", PINS["dev"], URL) in dev
    assert PINS["stable"][:12] not in dev
    assert source_dir("app", PINS["stable"], URL) in default
    assert "unpinned" not in dev + default


def test_a_lock_of_one_variant_covers_it_alone() -> None:
    recipe = _recipe()
    only_dev = lock(recipe, resolver=_resolve, variants=["dev"])
    assert [f.name for f in only_dev.lockfile.fetches] == ["dev/app"]
    assert source_dir("app", PINS["dev"], URL) in _contents(recipe, "dev", only_dev)


def test_drift_and_update_use_the_variant_address() -> None:
    locked = lock(_recipe(), resolver=_resolve)
    moved = _recipe(dev_ref="next")
    found = lock_status(moved, locked)
    assert [(d.code, d.subject) for d in found if d.subject.startswith("sources.")] == [
        ("lock-changed", "sources.dev.app")
    ]
    assert "sources.dev.app changed since the lock: bbbbbbb -> next" in {d.message for d in found}

    relocked = lock(moved, previous=locked, update=["dev/app"], resolver=_resolve)
    assert {f.name: f.digest for f in relocked.lockfile.fetches} == {
        "default/app": PINS["stable"],
        "dev/app": PINS["next"],
    }
    with pytest.raises(Exception, match="Cannot update unknown source"):
        lock(moved, previous=locked, update=["qa/app"], resolver=_resolve)


def test_updating_the_build_name_updates_every_variants_pin() -> None:
    locked = lock(_recipe(), resolver=_resolve)
    calls: list[str] = []

    def counting(source: object) -> str:
        calls.append(source.ref)  # type: ignore[attr-defined]
        return _resolve(source)

    lock(_recipe(), previous=locked, update=["app"], resolver=counting)
    assert sorted(calls) == ["dev", "stable"]


def _git(*args: str, cwd: Path) -> str:
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    done = subprocess.run(
        ["git", *identity, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, dict[str, str]]:
    """A local repository with ``stable`` and ``dev`` branches: ``(url, {branch: commit})``."""
    path = tmp_path / "upstream"
    path.mkdir()
    _git("init", "-q", "-b", "stable", cwd=path)
    (path / "app.c").write_text("stable\n", encoding="utf-8")
    _git("add", ".", cwd=path)
    _git("commit", "-qm", "stable", cwd=path)
    _git("checkout", "-q", "-b", "dev", cwd=path)
    (path / "app.c").write_text("dev\n", encoding="utf-8")
    _git("commit", "-qam", "dev", cwd=path)
    commits = {b: _git("rev-parse", b, cwd=path) for b in ("stable", "dev")}
    return path.as_uri(), commits


def test_fetch_checks_out_each_pin_once(tmp_path: Path, repo: tuple[str, dict[str, str]]) -> None:
    url, commits = repo
    recipe = _recipe(url=url)
    locked = lock(recipe, resolver=lambda s: commits[s.ref])  # type: ignore[union-attr]
    fetched = fetch(recipe, lock=locked, out=tmp_path / "out")
    assert [(f.name, f.pin) for f in fetched] == [
        ("default/app", commits["stable"]),
        ("dev/app", commits["dev"]),
    ]
    assert (fetched[1].path / "app.c").read_text(encoding="utf-8") == "dev\n"
    assert (fetched[0].path / "app.c").read_text(encoding="utf-8") == "stable\n"


RECIPE = """
from dataclasses import replace

from tundravm import Build, Fragment, Git, Install, Package, Recipe, Variant

INSTALL = (Install("app", "/usr/bin/app"),)
STABLE = Build("app", Git({url!r}, "stable"), script="make", install=INSTALL)
recipe = Recipe(
    "pins",
    Fragment("common", items=(Package("linux-image-amd64"), STABLE)),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", replace=(replace(STABLE, source=Git({url!r}, "dev")),)),
    ),
)
"""


def test_cli_lock_update_takes_the_variant_address(
    tmp_path: Path, repo: tuple[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    url, commits = repo
    upstream = Path(url.removeprefix("file://"))
    monkeypatch.setattr(
        source_module, "default_resolver", lambda s: _git("rev-parse", s.ref, cwd=upstream)
    )
    path = write_recipe_file(tmp_path, RECIPE.format(url=url), monkeypatch)
    lock_path = tmp_path / "pins.lock"
    assert run_main("lock", str(path), "--lockfile", str(lock_path))[0] == EXIT_OK
    pins = {f.identity: f.digest for f in read_lock(lock_path).pins}
    assert pins == {"default/app": commits["stable"], "dev/app": commits["dev"]}

    (upstream / "app.c").write_text("dev 2\n", encoding="utf-8")
    _git("commit", "-qam", "dev 2", cwd=upstream)
    assert run_main("lock", str(path), "--lockfile", str(lock_path), "--update", "dev/app")[0] == (
        EXIT_OK
    )
    pins = {f.identity: f.digest for f in read_lock(lock_path).pins}
    assert pins["dev/app"] == _git("rev-parse", "dev", cwd=upstream)
    assert pins["default/app"] == commits["stable"]

    path.write_text(RECIPE.format(url=url).replace('"dev")', '"main")'), encoding="utf-8")
    code, out = run_main("lock", str(path), "--lockfile", str(lock_path), "--check")
    assert code == EXIT_FAILURE
    assert "~ sources.dev.app: " in out
