"""`tundravm watch`: one line per change to the recipe or a module beside it."""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_OK, main
from tundravm.watch import Sources, Watch, python_files, tree_verdict, verdict

RECIPE = """
from tundravm import File, Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend
from pkgs import PACKAGES

backend = InProcessBackend()


def build() -> Recipe:
    packages = (Package(name) for name in ("linux-image-amd64", *PACKAGES))
    items = (*packages, File("/etc/motd", "hi\\n"))
    return Recipe("watched", Fragment("common", items=items), variants=(Variant("default"),))
"""
PKGS = 'PACKAGES = ("curl",)\n'
LINE = re.compile(r"^\d\d:\d\d:\d\d (.*)$")


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """``recipe.py`` importing ``pkgs.py`` beside it, run from *tmp_path*.

    Each test imports its own ``pkgs``; the in-process CLI would otherwise reuse the
    previous test's module, which lives under another directory.
    """
    (tmp_path / "pkgs.py").write_text(PKGS, encoding="utf-8")
    sys.modules.pop("pkgs", None)
    yield write_recipe_file(tmp_path, RECIPE, monkeypatch)
    sys.modules.pop("pkgs", None)


def _watch(*argv: str, ticks: Iterator[object]) -> tuple[int, list[str]]:
    """Run ``tundravm watch *argv`` for *ticks*; return the exit code and verdict lines."""
    out = io.StringIO()
    code = main(["watch", *argv, "--interval", "0.001"], stdout=out, ticks=ticks)
    header, *lines = out.getvalue().splitlines()
    assert header.startswith("watching ") and header.endswith("(Ctrl-C stops)")
    verdicts = []
    for line in lines:
        match = LINE.match(line)
        assert match is not None, line
        verdicts.append(match.group(1))
    return code, verdicts


def _edit_then_tick(path: Path, text: str) -> Iterator[object]:
    """Poll once unchanged, then write *text* to *path* and poll again."""
    yield "unchanged"
    path.write_text(text, encoding="utf-8")
    yield "changed"


def test_watch_prints_nothing_until_the_recipe_changes(recipe: Path) -> None:
    assert run_main("compile", str(recipe))[0] == EXIT_OK
    edited = recipe.read_text(encoding="utf-8").replace('"hi', '"hello')

    code, lines = _watch(str(recipe), ticks=_edit_then_tick(recipe, edited))

    assert code == EXIT_OK
    assert len(lines) == 2  # the check at start, then one line for the edit
    assert re.fullmatch(r"lint \d+ errors? \d+ warnings?; tree up to date", lines[0])
    assert lines[1].endswith("; tree stale (1 file)")
    assert run_main("compile", str(recipe), "--check")[0] != EXIT_OK  # watch never wrote


def test_watch_reloads_a_changed_sibling_module(recipe: Path) -> None:
    assert run_main("compile", str(recipe))[0] == EXIT_OK
    edited = 'PACKAGES = ("curl", "jq")\n'

    _, lines = _watch(str(recipe), ticks=_edit_then_tick(recipe.parent / "pkgs.py", edited))

    assert [line.split("; ")[1] for line in lines] == ["tree up to date", "tree stale (1 file)"]


def test_watch_write_recompiles_into_out_mkosi(recipe: Path) -> None:
    edited = recipe.read_text(encoding="utf-8").replace('"hi', '"hello')

    code, lines = _watch(
        str(recipe), "--out", "out", "--write", ticks=_edit_then_tick(recipe, edited)
    )

    assert code == EXIT_OK
    assert [line.split("; ")[1] for line in lines] == ["wrote out/mkosi", "wrote out/mkosi"]
    assert run_main("compile", str(recipe), "--out", "out/mkosi", "--check")[0] == EXIT_OK


def test_watch_checks_the_configured_tree_unless_out_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        run_main("init", ".", "--name", "node", "--backend", "inprocess", "--no-doctor")[0]
        == EXIT_OK
    )
    assert run_main("compile")[0] == EXIT_OK  # the table's tree: mkosi/

    assert _watch(ticks=iter(()))[1][0].endswith("; tree up to date")
    assert _watch("--out", "out", "--write", ticks=iter(()))[1][0].endswith("; wrote out/mkosi")
    assert (tmp_path / "out" / "mkosi").is_dir()


def test_watch_reports_a_broken_recipe_and_keeps_watching(recipe: Path) -> None:
    good = recipe.read_text(encoding="utf-8")

    def ticks() -> Iterator[object]:
        recipe.write_text(good + "def broken(:\n", encoding="utf-8")
        yield "broken"
        recipe.write_text(good, encoding="utf-8")
        yield "fixed"

    _, lines = _watch(str(recipe), ticks=ticks())

    assert len(lines) == 3
    assert lines[1].startswith("error [E_VALIDATION] ")
    assert re.fullmatch(r"lint 0 errors 0 warnings; tree missing \(\d+ files to write\)", lines[2])


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ``init`` service project in *tmp_path* (the CWD), compiled and locked."""
    monkeypatch.chdir(tmp_path)
    init = ("init", ".", "--name", "node", "--backend", "inprocess", "--no-doctor")
    assert run_main(*init)[0] == EXIT_OK
    assert run_main("compile")[0] == EXIT_OK
    assert run_main("lock")[0] == EXIT_OK
    return tmp_path / "node.py"


def _lint_summary() -> str:
    """The summary line of ``tundravm lint`` on the configured project."""
    out = io.StringIO()
    main(["lint"], stdout=out)
    return out.getvalue().splitlines()[-1]


def test_watch_counts_lockfile_drift_as_lint_does(project: Path) -> None:
    text = project.read_text(encoding="utf-8")
    project.write_text(text.replace('APP_VERSION = "0.1.0"', 'APP_VERSION = "0.2.0"'))

    _, lines = _watch(ticks=iter(()))

    assert _lint_summary() == "4 errors, 0 warnings, 0 infos"  # lock-changed, one per section
    assert lines == ["lint 4 errors 0 warnings; lock drifted (4 sections); tree stale (4 files)"]


def test_watch_names_the_first_error_a_fragment_check_raises(project: Path) -> None:
    good = project.read_text(encoding="utf-8")
    privileged = good.replace("APP_PORT = 8080", "APP_PORT = 80")

    def ticks() -> Iterator[object]:
        project.write_text(privileged, encoding="utf-8")
        yield "port 80"
        project.write_text(good, encoding="utf-8")
        yield "fixed"

    _, lines = _watch(ticks=ticks())

    assert lines == [
        "lint 0 errors 0 warnings; tree up to date",
        "lint 2 errors (app-privileged-port: app runs as user app and cannot bind port 80)",
        "lint 0 errors 0 warnings; tree up to date",
    ]


def test_watch_ctrl_c_exits_0(recipe: Path) -> None:
    def interrupted() -> Iterator[object]:
        raise KeyboardInterrupt
        yield

    out = io.StringIO()
    code = main(["watch", str(recipe)], stdout=out, ticks=interrupted())

    assert code == EXIT_OK
    assert out.getvalue().splitlines()[-1] == "stopped"


def test_watch_rejects_a_non_positive_interval(recipe: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        run_main("watch", str(recipe), "--interval", "0")
    assert exc.value.code == 2


def test_loop_runs_check_only_when_a_source_changes(tmp_path: Path) -> None:
    recipe = tmp_path / "image.py"
    recipe.write_text("x = 1\n", encoding="utf-8")
    checks: list[int] = []
    slept: list[float] = []

    def check() -> str:
        checks.append(len(checks))
        return verdict(errors=0, warnings=1, tree=tree_verdict(changed=2, exists=True))

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 2:
            (tmp_path / "lib.py").write_text("y = 2\n", encoding="utf-8")

    out = io.StringIO()
    loop = Watch(
        Sources.of(recipe),
        check,
        out,
        interval=0.5,
        sleep=sleep,
        clock=lambda: datetime(2026, 1, 1, 9, 5, 7),
    )

    assert loop.run(range(3)) == EXIT_OK
    assert slept == [0.5, 0.5, 0.5]
    assert len(checks) == 2  # start, then the new module; the other polls saw nothing
    assert out.getvalue().splitlines() == [
        "09:05:07 lint 0 errors 1 warning; tree stale (2 files)",
        "09:05:07 lint 0 errors 1 warning; tree stale (2 files)",
    ]


def test_sources_skip_hidden_cache_and_build_directories(tmp_path: Path) -> None:
    for name in ("image.py", "lib/mod.py", ".venv/x.py", "__pycache__/c.py", "build/mkosi/s.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    sources = Sources.of(tmp_path / "image.py")
    sources.ignore(tmp_path / "build")

    assert [path.relative_to(tmp_path.resolve()).as_posix() for path in sources.files()] == [
        "image.py",
        "lib/mod.py",
    ]
    assert len(list(python_files(tmp_path.resolve()))) == 3  # build/ is walked without skip
