"""CI and code-review kit: `init`, `ci`, and `--format` on inspect/lint/diff/lock."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.diff import TreeDiff
from tundravm.formats import md_cell, resolve_format, workflow_command

RECIPE = """
from tundravm import File, Fragment, Package, Recipe, Service, User, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
PACKAGES = ("curl", "jq")
ITEMS = [
    File("/etc/motd", "hello\\n"),
    User("app", system=True),
    Service("app", "/usr/bin/app", user="app"),
]


def build() -> Recipe:
    items = (*(Package(name) for name in PACKAGES), *ITEMS)
    variants = (Variant("default", target="qemu"), Variant("azure", target="azure"))
    return Recipe("ci", Fragment("common", items=items), variants=variants)

"""
GHOST = 'ITEMS.append(Service("w", "/usr/bin/w", user="ghost"))\n'
DEFAULT = ("--variant", "default")
LOCKFILE = Path("build") / "tundravm.lock"

ANNOTATION = re.compile(r"^::(error|warning|notice) (?P<props>[^:]*)::(?P<message>.+)$")


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The recipe file, run from *tmp_path* so its build dir is ``tmp_path / "build"``."""
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def parse_annotations(text: str) -> list[tuple[str, dict[str, str], str]]:
    found = []
    for line in text.splitlines():
        match = ANNOTATION.match(line)
        if match is None:
            continue
        props = dict(item.split("=", 1) for item in match["props"].split(","))
        found.append((match[1], props, match["message"]))
    return found


def mutate(recipe: Path) -> None:
    text = recipe.read_text()
    recipe.write_text(text.replace('("curl", "jq")', '("curl", "jq", "htop")'))


# init


def test_init_writes_recipe_and_gitignore_without_ci(tmp_path: Path) -> None:
    project = tmp_path / "my-node"
    code, out = run_main("init", str(project), "--backend", "inprocess")
    assert code == EXIT_OK
    recipe = project / "my-node.py"
    assert f"created {recipe}" in out
    assert "InProcessBackend()" in recipe.read_text(encoding="utf-8")
    gitignore = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/build/*" in gitignore and "!/build/tundravm.lock" in gitignore
    assert not (project / ".github").exists()
    assert "tundravm compile my-node.py --out mkosi" in out
    assert "tundravm lock my-node.py" in out
    assert "tundravm ci my-node.py --out mkosi" in out

    code, payload = run_main("inspect", str(recipe), "--json")
    assert code == EXIT_OK and set(json.loads(payload)["variants"]) == {"default", "dev"}


def test_init_with_github_ci_writes_workflow(tmp_path: Path) -> None:
    code, out = run_main("init", str(tmp_path), "--name", "node", "--ci", "github")
    assert code == EXIT_OK
    workflow = tmp_path / ".github" / "workflows" / "tundravm.yml"
    assert f"created {workflow}" in out
    text = workflow.read_text(encoding="utf-8")
    assert "run: uv sync" in text
    assert 'uv run tundravm inspect node.py --format markdown >> "$GITHUB_STEP_SUMMARY"' in text
    assert "run: uv run tundravm ci node.py --out mkosi" in text
    assert "note: the workflow runs `uv sync`" in out


def test_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "node.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    code, _ = run_main("init", str(tmp_path), "--name", "node", "--ci", "github")
    assert code == EXIT_SDK_ERROR
    assert "Refusing to overwrite" in capsys.readouterr().err
    assert not (tmp_path / ".github").exists()

    code, out = run_main("init", str(tmp_path), "--name", "node", "--ci", "github", "--force")
    assert code == EXIT_OK
    assert f"overwrote {tmp_path / 'node.py'}" in out
    assert f"updated {tmp_path / '.gitignore'}" in out
    assert (tmp_path / ".gitignore").read_text().startswith("*.pyc\n\n# tundravm")

    code, out = run_main("init", str(tmp_path), "--name", "node", "--force")
    assert code == EXIT_OK and "kept" in out
    assert (tmp_path / ".gitignore").read_text().count("/build/*") == 1


def test_init_project_passes_ci_once_compiled_and_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run_main("init", "--name", "node", "--backend", "inprocess")[0] == EXIT_OK
    assert run_main("compile", "node.py", "--out", "mkosi")[0] == EXIT_OK
    assert run_main("lock", "node.py")[0] == EXIT_OK
    code, out = run_main("ci", "node.py", "--out", "mkosi")
    assert code == EXIT_OK, out


# inspect --format markdown


def test_inspect_markdown_two_variants(recipe: Path) -> None:
    code, out = run_main(
        "inspect", str(recipe), "--variant", "azure", "--variant", "default", "--format", "markdown"
    )
    assert code == EXIT_OK
    assert out.startswith("# tundravm: `recipe.py`\n")
    assert "## Variant `default`" in out and "## Variant `azure`" in out
    assert out.index("## Variant `azure`") < out.index("## Variant `default`")
    assert "**Parent:** `base` · **Fragments:** `common`" in out
    assert "**Parent:** none" not in out
    assert "| Package | Installed in |" in out and "| `curl` | image |" in out
    assert "| Path | Mode | Bytes | sha256 |" in out and "| `/etc/motd` | 0644 | 6 |" in out
    assert "### Users (1)" in out and "| `app` | yes |" in out
    assert "| Service | Command | User | Restart | Enabled |" in out
    assert "| `app` | `/usr/bin/app` | app |" in out


def test_inspect_markdown_collapses_long_package_lists(tmp_path: Path) -> None:
    recipe = tmp_path / "big.py"
    packages = ", ".join(f'Package("pkg{index:02d}")' for index in range(25))
    recipe.write_text(
        "from tundravm import Fragment, Package, Recipe\n"
        f"recipe = Recipe('big', Fragment('common', items=({packages},)))\n"
    )
    code, out = run_main("inspect", str(recipe), "--format", "markdown")
    assert code == EXIT_OK
    assert "<details><summary>Packages (25)</summary>\n\n| Package |" in out
    assert "</details>" in out


def test_inspect_json_alias_conflicts_with_format(recipe: Path) -> None:
    with pytest.raises(SystemExit):
        main(["inspect", str(recipe), "--json", "--format", "markdown"], stdout=io.StringIO())


# lint --format


def test_lint_github_lines_parse(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + GHOST)
    code, out = run_main("lint", str(recipe), "--format", "github", *DEFAULT)
    assert code == EXIT_FAILURE
    found = parse_annotations(out)
    assert len(found) == 1
    level, props, message = found[0]
    assert level == "error"
    assert props == {"file": "recipe.py", "title": "service-user-missing"}  # relative to CWD
    assert message.startswith("[default] w: ") and "(Declare" in message
    assert out.rstrip().endswith("1 error, 0 warnings, 0 infos")


def test_lint_github_strict_reports_warnings_as_errors(tmp_path: Path) -> None:
    recipe = tmp_path / "warn.py"
    recipe.write_text(
        "from tundravm import Fragment, Init, Package, Recipe\n"
        "recipe = Recipe('warn', Fragment('common', items=(\n"
        "    Package('curl'),\n"
        "    Init('one', 'echo 1\\n', priority=5),\n"
        "    Init('two', 'echo 2\\n', priority=5),\n"
        ")))\n"
    )
    code, out = run_main("lint", str(recipe), "--format", "github")
    assert code == EXIT_OK
    assert {level for level, _, _ in parse_annotations(out)} >= {"warning"}
    code, out = run_main("lint", str(recipe), "--format", "github", "--strict")
    assert code == EXIT_FAILURE
    assert "warning" not in {level for level, _, _ in parse_annotations(out)}


def test_lint_markdown_table(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + GHOST)
    code, out = run_main("lint", str(recipe), "--format", "markdown", *DEFAULT)
    assert code == EXIT_FAILURE
    assert out.startswith("| Level | Code | Variant | Subject | Message | Hint |\n|---|")
    assert "| error | `service-user-missing` | `default` | `w` |" in out
    assert "**1 error, 0 warnings, 0 infos**" in out


# diff --format


def test_diff_github_and_markdown_on_mutated_recipe(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    assert run_main("compile", str(recipe), "--out", str(tree), *DEFAULT)[0] == EXIT_OK
    mutate(recipe)

    code, out = run_main(
        "diff", str(recipe), "--against", str(tree), "--format", "github", *DEFAULT
    )
    assert code == EXIT_FAILURE
    [(level, props, message)] = parse_annotations(out)
    assert level == "warning"
    assert props == {"file": "tree/default/mkosi.conf", "title": "compiled tree drift"}
    assert "modified" in message
    assert "::group::unified diff\n::stop-commands::tundravm-" in out
    assert "+    htop\n" in out and out.endswith("::endgroup::\n")

    code, out = run_main(
        "diff", str(recipe), "--against", str(tree), "--format", "markdown", *DEFAULT
    )
    assert code == EXIT_FAILURE
    assert "| Status | File |\n|---|---|\n| modified | `default/mkosi.conf` |" in out
    assert "**1 file changed**" in out
    assert "```diff\n" in out and "+    htop\n" in out and out.endswith("```\n")


def test_tree_diff_markdown_truncates_long_diffs(recipe: Path, tmp_path: Path) -> None:
    lines = "".join(f"line {index}\\n" for index in range(500))
    recipe.write_text(recipe.read_text() + f'ITEMS.append(File("/etc/big", "{lines}"))\n')
    code, out = run_main(
        "diff", str(recipe), "--against", str(tmp_path / "empty"), "--format", "markdown"
    )
    assert code == EXIT_FAILURE
    fenced = out.split("```diff\n", 1)[1].split("\n```", 1)[0]
    assert len(fenced.splitlines()) == 400
    assert re.search(r"_Diff truncated: showing 400 of \d+ lines\.", out)


def test_compile_check_github_annotates_stale_files(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    run_main("compile", str(recipe), "--out", str(tree))
    mutate(recipe)
    code, out = run_main(
        "compile", str(recipe), "--out", str(tree), "--check", "--format", "github"
    )
    assert code == EXIT_FAILURE
    assert [props["file"] for _, props, _ in parse_annotations(out)] == [
        "tree/azure/mkosi.conf",
        "tree/default/mkosi.conf",
    ]


def test_markdown_helpers_escape() -> None:
    assert md_cell("a|b") == "a\\|b"
    assert md_cell("x`y", code=True) == "`` x`y ``"
    assert md_cell(None) == "—"
    assert workflow_command("error", "50% a\nb", file="a,b:c.py") == (
        "::error file=a%2Cb%3Ac.py::50%25 a%0Ab"
    )
    assert TreeDiff(changes=()).markdown() == "**Tree is up to date with the recipe.**\n"


# lock --check --format


def test_lock_check_github_and_markdown(recipe: Path, tmp_path: Path) -> None:
    assert run_main("lock", str(recipe), *DEFAULT)[0] == EXIT_OK
    code, out = run_main("lock", str(recipe), "--check", "--format", "github", *DEFAULT)
    assert (code, out) == (EXIT_OK, "lock is up to date\n")

    mutate(recipe)
    code, out = run_main("lock", str(recipe), "--check", "--format", "github", *DEFAULT)
    assert code == EXIT_FAILURE
    [(level, props, message)] = parse_annotations(out)
    assert level == "error"
    assert props == {"file": str(LOCKFILE), "title": "lock drift"}
    assert message.startswith("variants.default.packages changed: +htop.")

    code, out = run_main("lock", str(recipe), "--check", "--format", "markdown", *DEFAULT)
    assert code == EXIT_FAILURE
    assert "| changed | `variants.default.packages` | `+htop` |" in out


# ci


def test_ci_pass_prints_three_ok_lines(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run_main("compile", str(recipe), "--out", str(tree))
    run_main("lock", str(recipe))
    code, out = run_main("ci", str(recipe), "--out", str(tree))
    assert code == EXIT_OK
    assert out.splitlines() == [
        "ok lint: no findings",
        f"ok compile: {tree} is up to date",
        f"ok lock: {LOCKFILE} is up to date",
    ]


RUNTIME_RECIPE = """
from tundravm import (
    Disk, Fragment, Git, Key, Recipe, RuntimeTools, Secret, SecretFile, Secrets, Service,
    Variant,
)
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
TOOLS = RuntimeTools(Git("https://github.com/Hyodar/tundra-tools", "{sha}"))
KEY = Key("key_persistent")
ITEMS = (
    TOOLS,
    KEY,
    Disk("disk_persistent", "/persistent", key=KEY),
    Secrets(entries=(Secret("token", (SecretFile("/run/app/token"),)),)),
    Service("app", "/usr/bin/app"),
)
recipe = Recipe(
    "runtime",
    Fragment("runtime", items=ITEMS),
    variants=(Variant("default", target="qemu"), Variant("azure", target="azure")),
)
""".replace("{sha}", "a" * 40)


def test_ci_after_compile_and_lock_is_clean_with_runtime_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    recipe = tmp_path / "runtime.py"
    recipe.write_text(RUNTIME_RECIPE, encoding="utf-8")
    tree = tmp_path / "mkosi"
    assert run_main("compile", str(recipe), "--out", str(tree))[0] == EXIT_OK
    assert run_main("lock", str(recipe))[0] == EXIT_OK
    assert run_main("lock", str(recipe), "--check") == (EXIT_OK, "lock is up to date\n")

    code, out = run_main("ci", str(recipe), "--out", str(tree))

    assert (code, out.splitlines()[-1]) == (EXIT_OK, f"ok lock: {LOCKFILE} is up to date")
    assert (tree / "default" / "mkosi.extra" / "usr" / "bin" / "runtime-init").is_file()


def test_ci_stops_at_first_failing_step(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run_main("compile", str(recipe), "--out", str(tree))
    run_main("lock", str(recipe))
    mutate(recipe)
    code, out = run_main("ci", str(recipe), "--out", str(tree))
    assert code == EXIT_FAILURE
    lines = out.splitlines()
    assert lines[0] == "ok lint: no findings"
    assert "M  default/mkosi.conf" in lines
    assert "M  azure/mkosi.conf" in lines
    assert lines[-2].startswith(f"FAIL compile: 2 files stale in {tree}; run `tundravm compile")
    assert lines[-1] == "skip lock"
    assert not any(line.startswith("ok lock") for line in lines)


def test_ci_lint_failure_skips_the_rest(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + GHOST)
    code, out = run_main("ci", str(recipe))
    assert code == EXIT_FAILURE
    assert out.splitlines()[-3:] == [
        "FAIL lint: 2 errors, 0 warnings, 0 infos",
        "skip compile",
        "skip lock",
    ]


def test_ci_missing_lockfile_fails_lock_step(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run_main("compile", str(recipe), "--out", str(tree))
    code, out = run_main("ci", str(recipe), "--out", str(tree), "--format", "github")
    assert code == EXIT_FAILURE
    assert "::error title=tundravm ci%3A lock::[E_LOCKFILE]" in out
    assert out.splitlines()[-1].startswith("FAIL lock: [E_LOCKFILE]")


# --format auto


def test_auto_resolves_to_github_under_github_actions(
    recipe: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert resolve_format(None) == "text"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert resolve_format(None) == "github"
    assert resolve_format("auto") == "github"
    assert resolve_format(None, alias="json") == "json"
    assert resolve_format("markdown") == "markdown"

    run_main("lock", str(recipe))
    mutate(recipe)
    code, out = run_main("lock", str(recipe), "--check")
    assert code == EXIT_FAILURE and out.startswith("::error file=")
    code, out = run_main("lock", str(recipe), "--check", "--format", "text")
    assert out.splitlines()[:2] == [
        "~ variants.azure.packages: +htop",
        "~ variants.default.packages: +htop",
    ]
    code, out = run_main("diff", str(recipe), "--against", str(tmp_path / "none"), "--stat")
    assert out.startswith("A  azure/mkosi.conf")
    assert "\nA  default/mkosi.conf\n" in out
