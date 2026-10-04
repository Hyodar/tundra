"""CI and code-review kit: `init`, `ci`, and `--format` on explain/check/diff/lock."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from tundravm.cli import EXIT_FAILURE, EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.diff import TreeDiff
from tundravm.formats import md_cell, resolve_format, workflow_command

RECIPE = """
from tundravm import Image
from tundravm.backends.inprocess import InProcessBackend
from tundravm.platforms import AzurePlatform

img = Image(build_dir=BUILD_DIR, backend=InProcessBackend())
img.install("curl", "jq")
img.file("/etc/motd", content="hello\\n")
img.user("app", system=True)
img.service("app", command="/usr/bin/app", user="app")
img.targets("qemu")
with img.profile("azure"):
    AzurePlatform().apply(img)
"""

ANNOTATION = re.compile(r"^::(error|warning|notice) (?P<props>[^:]*)::(?P<message>.+)$")


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    path = tmp_path / "recipe.py"
    build_dir = tmp_path / "build"
    path.write_text(f"BUILD_DIR = {str(build_dir)!r}\n" + RECIPE, encoding="utf-8")
    return path


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


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
    recipe.write_text(recipe.read_text().replace('"curl", "jq"', '"curl", "jq", "htop"'))


# init


def test_init_writes_recipe_and_gitignore_without_ci(tmp_path: Path) -> None:
    project = tmp_path / "my-node"
    code, out = run("init", str(project), "--backend", "inprocess")
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

    code, payload = run("explain", str(recipe), "--json", "--all-profiles")
    assert code == EXIT_OK and set(json.loads(payload)) == {"default", "dev"}


def test_init_with_github_ci_writes_workflow(tmp_path: Path) -> None:
    code, out = run("init", str(tmp_path), "--name", "node", "--ci", "github")
    assert code == EXIT_OK
    workflow = tmp_path / ".github" / "workflows" / "tundravm.yml"
    assert f"created {workflow}" in out
    text = workflow.read_text(encoding="utf-8")
    assert "run: uv sync" in text
    assert "uv run tundravm explain node.py --all-profiles --format markdown" in text
    assert '>> "$GITHUB_STEP_SUMMARY"' in text
    assert "run: uv run tundravm ci node.py --out mkosi" in text
    assert "note: the workflow runs `uv sync`" in out


def test_init_refuses_overwrite_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "node.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    code, _ = run("init", str(tmp_path), "--name", "node", "--ci", "github")
    assert code == EXIT_SDK_ERROR
    assert "Refusing to overwrite" in capsys.readouterr().err
    assert not (tmp_path / ".github").exists()

    code, out = run("init", str(tmp_path), "--name", "node", "--ci", "github", "--force")
    assert code == EXIT_OK
    assert f"overwrote {tmp_path / 'node.py'}" in out
    assert f"updated {tmp_path / '.gitignore'}" in out
    assert (tmp_path / ".gitignore").read_text().startswith("*.pyc\n\n# tundravm")

    code, out = run("init", str(tmp_path), "--name", "node", "--force")
    assert code == EXIT_OK and "kept" in out
    assert (tmp_path / ".gitignore").read_text().count("/build/*") == 1


def test_init_project_passes_ci_once_compiled_and_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert run("init", "--name", "node", "--backend", "inprocess")[0] == EXIT_OK
    assert run("compile", "node.py", "--out", "mkosi")[0] == EXIT_OK
    assert run("lock", "node.py")[0] == EXIT_OK
    code, out = run("ci", "node.py", "--out", "mkosi")
    assert code == EXIT_OK, out


# explain --format markdown


def test_explain_markdown_two_profiles(recipe: Path) -> None:
    code, out = run("explain", str(recipe), "--all-profiles", "--format", "markdown")
    assert code == EXIT_OK
    assert out.startswith("# tundravm: `recipe.py`\n")
    assert "## Profile `default`" in out and "## Profile `azure`" in out
    assert out.index("## Profile `azure`") < out.index("## Profile `default`")
    assert "**Extends:** none · **Modules:** none" in out
    assert "**Extends:** `default` · **Modules:** `AzurePlatform`" in out
    assert "| Package | Installed in |" in out and "| `curl` | image |" in out
    assert "| Path | Mode | Bytes | sha256 |" in out and "| `/etc/motd` | 0644 | 6 |" in out
    assert "### Users (1)" in out and "| `app` | yes |" in out
    assert "| Service | Command | User | Restart | Enabled |" in out
    assert "| `app` | `/usr/bin/app` | app |" in out


def test_explain_markdown_collapses_long_package_lists(tmp_path: Path) -> None:
    recipe = tmp_path / "big.py"
    names = ", ".join(f'"pkg{index:02d}"' for index in range(25))
    recipe.write_text(f"from tundravm import Image\nimg = Image()\nimg.install({names})\n")
    code, out = run("explain", str(recipe), "--format", "markdown")
    assert code == EXIT_OK
    assert "<details><summary>Packages (25)</summary>\n\n| Package |" in out
    assert "</details>" in out


def test_explain_json_alias_conflicts_with_format(recipe: Path) -> None:
    with pytest.raises(SystemExit):
        main(["explain", str(recipe), "--json", "--format", "markdown"], stdout=io.StringIO())


# check --format


def test_check_github_lines_parse(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + 'img.service("w", command="/usr/bin/w", user="ghost")\n')
    code, out = run("check", str(recipe), "--format", "github")
    assert code == EXIT_FAILURE
    found = parse_annotations(out)
    assert len(found) == 1
    level, props, message = found[0]
    assert level == "error"
    assert props == {"file": str(recipe), "title": "service-user-missing"}
    assert message.startswith("[default] w: ") and "(Declare it with" in message
    assert out.rstrip().endswith("1 error, 0 warnings, 0 infos")


def test_check_github_strict_reports_warnings_as_errors(tmp_path: Path) -> None:
    recipe = tmp_path / "warn.py"
    recipe.write_text(
        "from tundravm import Image\nimg = Image()\nimg.install('curl')\n"
        "img.runtime_init('echo 1\\n', priority=5)\n"
        "img.runtime_init('echo 2\\n', priority=5)\n"
    )
    code, out = run("check", str(recipe), "--format", "github")
    assert code == EXIT_OK
    assert {level for level, _, _ in parse_annotations(out)} >= {"warning"}
    code, out = run("check", str(recipe), "--format", "github", "--strict")
    assert code == EXIT_FAILURE
    assert "warning" not in {level for level, _, _ in parse_annotations(out)}


def test_check_markdown_table(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + 'img.service("w", command="/usr/bin/w", user="ghost")\n')
    code, out = run("check", str(recipe), "--format", "markdown")
    assert code == EXIT_FAILURE
    assert out.startswith("| Level | Code | Profile | Subject | Message | Hint |\n|---|")
    assert "| error | `service-user-missing` | `default` | `w` |" in out
    assert "**1 error, 0 warnings, 0 infos**" in out


# diff --format


def test_diff_github_and_markdown_on_mutated_recipe(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    assert run("compile", str(recipe), "--out", str(tree))[0] == EXIT_OK
    mutate(recipe)

    code, out = run("diff", str(recipe), "--against", str(tree), "--format", "github")
    assert code == EXIT_FAILURE
    [(level, props, message)] = parse_annotations(out)
    assert level == "warning"
    assert props == {"file": f"{tree}/default/mkosi.conf", "title": "compiled tree drift"}
    assert "modified" in message
    assert "::group::unified diff\n::stop-commands::tundravm-" in out
    assert "+    htop\n" in out and out.endswith("::endgroup::\n")

    code, out = run("diff", str(recipe), "--against", str(tree), "--format", "markdown")
    assert code == EXIT_FAILURE
    assert "| Status | File |\n|---|---|\n| modified | `default/mkosi.conf` |" in out
    assert "**1 file changed**" in out
    assert "```diff\n" in out and "+    htop\n" in out and out.endswith("```\n")


def test_tree_diff_markdown_truncates_long_diffs(recipe: Path, tmp_path: Path) -> None:
    lines = "".join(f"line {index}\\n" for index in range(500))
    recipe.write_text(recipe.read_text() + f'img.file("/etc/big", content="{lines}")\n')
    code, out = run(
        "diff", str(recipe), "--against", str(tmp_path / "empty"), "--format", "markdown"
    )
    assert code == EXIT_FAILURE
    fenced = out.split("```diff\n", 1)[1].split("\n```", 1)[0]
    assert len(fenced.splitlines()) == 400
    assert re.search(r"_Diff truncated: showing 400 of \d+ lines\.", out)


def test_compile_check_github_annotates_stale_files(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    run("compile", str(recipe), "--out", str(tree))
    mutate(recipe)
    code, out = run("compile", str(recipe), "--out", str(tree), "--check", "--format", "github")
    assert code == EXIT_FAILURE
    assert [props["file"] for _, props, _ in parse_annotations(out)] == [
        f"{tree}/default/mkosi.conf"
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
    assert run("lock", str(recipe))[0] == EXIT_OK
    code, out = run("lock", str(recipe), "--check", "--format", "github")
    assert (code, out) == (EXIT_OK, "lock is up to date\n")

    mutate(recipe)
    code, out = run("lock", str(recipe), "--check", "--format", "github")
    assert code == EXIT_FAILURE
    [(level, props, message)] = parse_annotations(out)
    assert level == "error"
    assert props == {"file": str(tmp_path / "build" / "tundravm.lock"), "title": "lock drift"}
    assert message.startswith("profiles.default.packages changed: +htop.")

    code, out = run("lock", str(recipe), "--check", "--format", "markdown")
    assert code == EXIT_FAILURE
    assert "| changed | `profiles.default.packages` | `+htop` |" in out


# ci


def test_ci_pass_prints_three_ok_lines(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run("compile", str(recipe), "--out", str(tree))
    run("lock", str(recipe))
    code, out = run("ci", str(recipe), "--out", str(tree))
    assert code == EXIT_OK
    assert out.splitlines() == [
        "ok check: no findings",
        f"ok compile: {tree} is up to date",
        f"ok lock: {tmp_path / 'build' / 'tundravm.lock'} is up to date",
    ]


def test_ci_stops_at_first_failing_step(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run("compile", str(recipe), "--out", str(tree))
    run("lock", str(recipe))
    mutate(recipe)
    code, out = run("ci", str(recipe), "--out", str(tree))
    assert code == EXIT_FAILURE
    lines = out.splitlines()
    assert lines[0] == "ok check: no findings"
    assert "M  default/mkosi.conf" in lines
    assert lines[-2].startswith(f"FAIL compile: 1 file stale in {tree}; run `tundravm compile")
    assert lines[-1] == "skip lock"
    assert not any(line.startswith("ok lock") for line in lines)


def test_ci_check_failure_skips_the_rest(recipe: Path) -> None:
    recipe.write_text(recipe.read_text() + 'img.service("w", command="/usr/bin/w", user="ghost")\n')
    code, out = run("ci", str(recipe))
    assert code == EXIT_FAILURE
    assert out.splitlines()[-3:] == [
        "FAIL check: 2 errors, 0 warnings, 0 infos",
        "skip compile",
        "skip lock",
    ]


def test_ci_missing_lockfile_fails_lock_step(recipe: Path, tmp_path: Path) -> None:
    tree = tmp_path / "mkosi"
    run("compile", str(recipe), "--out", str(tree))
    code, out = run("ci", str(recipe), "--out", str(tree), "--format", "github")
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

    run("lock", str(recipe))
    mutate(recipe)
    code, out = run("lock", str(recipe), "--check")
    assert code == EXIT_FAILURE and out.startswith("::error file=")
    code, out = run("lock", str(recipe), "--check", "--format", "text")
    assert out.startswith("~ profiles.default.packages: +htop")
    code, out = run("diff", str(recipe), "--against", str(tmp_path / "none"), "--stat")
    assert out.startswith("A  default/mkosi.conf")
