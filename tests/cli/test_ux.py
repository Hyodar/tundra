"""First-run and daily-use CLI: completions, no-arg quickstart, suggestions, init doctor,
``inspect --diff-variants``."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, ProbeRunner, build_parser, main
from tundravm.completion import SHELLS, Shell, render_completion, verbs_of
from tundravm.errors import ValidationError

FORMAT_CHOICES = {
    choice
    for verb in verbs_of(build_parser())
    for arg in verb.options
    if "--format" in arg.flags
    for choice in arg.choices
}


def run(*argv: str, runner: ProbeRunner | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = main(list(argv), stdout=out, runner=runner)
    return code, out.getvalue()


# completion


@pytest.mark.parametrize("shell", SHELLS)
def test_completion_lists_every_verb_and_format_choice(shell: Shell) -> None:
    code, script = run("completion", shell)
    assert code == EXIT_OK
    assert script == render_completion(build_parser(), shell)
    assert "# install: " in script.splitlines()[2 if shell == "zsh" else 1]
    for verb in verbs_of(build_parser()):
        assert verb.name in script, verb.name
    assert FORMAT_CHOICES >= {"auto", "text", "json", "markdown", "github", "stat"}
    for choice in FORMAT_CHOICES:
        assert choice in script, choice
    for choices in ("inprocess lima local nix", "rtmr azure gcp", "qemu azure gcp"):
        assert choices in script


def test_zsh_script_is_an_autoloadable_compdef() -> None:
    script = render_completion(build_parser(), "zsh")
    assert script.startswith("#compdef tundravm\n")
    assert "'--format[Output format]:format:(text json markdown)'" in script
    assert "'1:manifest:_files'" in script  # measure's required MANIFEST
    assert "'1::recipe:_files'" in script  # RECIPE: optional under [tool.tundravm]


def test_fish_script_completes_files_for_recipes() -> None:
    script = render_completion(build_parser(), "fish")
    assert "complete -c tundravm -n '__fish_seen_subcommand_from inspect' -F" in script
    assert "-l format -x -a 'text json markdown'" in script
    assert "-n '__fish_seen_subcommand_from completion' -a 'bash zsh fish'" in script


BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_bash_script_parses() -> None:
    script = render_completion(build_parser(), "bash")
    result = subprocess.run([str(BASH), "-n"], input=script, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def _bash_complete(words: Sequence[str], cwd: Path) -> list[str]:
    script = render_completion(build_parser(), "bash")
    quoted = " ".join(f"'{word}'" for word in words)
    driver = (
        f"{script}\nCOMP_WORDS=({quoted})\nCOMP_CWORD={len(words) - 1}\n"
        '_tundravm\nprintf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    result = subprocess.run(
        [str(BASH), "--norc", "-c", driver], text=True, capture_output=True, cwd=cwd, check=True
    )
    return [line for line in result.stdout.splitlines() if line]


@pytest.mark.skipif(BASH is None, reason="bash is not installed")
def test_bash_completes_verbs_choices_flags_and_files(tmp_path: Path) -> None:
    (tmp_path / "node.py").write_text("", encoding="utf-8")
    assert _bash_complete(["tundravm", "ins"], tmp_path) == ["inspect"]
    assert _bash_complete(["tundravm", "lint", "r.py", "--format", ""], tmp_path) == [
        "auto",
        "text",
        "json",
        "github",
        "markdown",
    ]
    assert _bash_complete(["tundravm", "bake", "r.py", "--backend", "l"], tmp_path) == [
        "lima",
        "local",
    ]
    assert _bash_complete(["tundravm", "inspect", "r.py", "--diff"], tmp_path) == [
        "--diff-variants"
    ]
    assert _bash_complete(["tundravm", "inspect", "no"], tmp_path) == ["node.py"]
    assert _bash_complete(["tundravm", "completion", "z"], tmp_path) == ["zsh"]


# no arguments, --version, suggestions


def test_no_arguments_prints_help_and_quickstart() -> None:
    code, out = run()
    assert code == EXIT_OK
    assert out.startswith("usage: tundravm")
    quickstart = out.split("quickstart:\n", 1)[1].splitlines()
    assert [line.split("   ")[0].strip() for line in quickstart] == [
        "tundravm init . --name node",
        "tundravm inspect node.py",
        "tundravm lint node.py",
        "tundravm bake node.py --backend inprocess",
    ]


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("tundravm ")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["inpsect", "r.py"], "unknown command 'inpsect' (did you mean 'inspect'?)"),
        (["bkae"], "unknown command 'bkae' (did you mean 'bake'?)"),
        (["zzz"], "unknown command 'zzz' (choose from init, inspect, lint,"),
        (["--verison"], "unrecognized arguments: --verison (did you mean --version?)"),
        (
            ["lint", "r.py", "--fromat", "json", "--strcit"],
            "unrecognized arguments: --fromat json --strcit (did you mean --format, --strict?)",
        ),
        (["bake", "r.py", "--backnd=nix"], "(did you mean --backend?)"),
    ],
)
def test_usage_errors_suggest_the_closest_verb_or_flag(
    argv: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2
    assert expected in capsys.readouterr().err


def test_abbreviated_top_level_flag_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--vers"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("tundravm ")


# init runs doctor


def _runner(returncode: int, calls: list[str] | None = None) -> ProbeRunner:
    def runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if calls is not None:
            calls.append(argv[0])
        stdout = f"{argv[0]} 9.9.9\n" if returncode == 0 else ""
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "")

    return runner


def test_init_ends_with_the_backend_probe(tmp_path: Path) -> None:
    code, out = run("init", str(tmp_path), "--name", "node", "--backend", "nix", runner=_runner(0))
    assert code == EXIT_OK
    after = out.split("checking the nix backend (tundravm doctor --backend nix):\n", 1)[1]
    assert "next:" not in after
    assert "backend nix_mkosi: available" in after
    assert "  ok nix nix 9.9.9" in after
    assert "not ready" not in after


def test_init_reports_a_missing_backend_tool_without_failing(tmp_path: Path) -> None:
    code, out = run("init", str(tmp_path), "--name", "node", runner=_runner(1))
    assert code == EXIT_OK
    assert (tmp_path / "node.py").is_file()
    assert "backend lima_mkosi: unavailable" in out
    assert "  missing limactl — " in out
    assert out.rstrip().endswith(
        "the lima backend is not ready: install the missing tools above, or bake "
        "with --backend inprocess for a simulated build"
    )


def test_init_no_doctor_skips_the_probe(tmp_path: Path) -> None:
    calls: list[str] = []
    code, out = run(
        "init", str(tmp_path), "--name", "node", "--no-doctor", runner=_runner(0, calls)
    )
    assert code == EXIT_OK
    assert calls == []
    assert "checking the" not in out


def test_doctor_verb_uses_the_injected_runner() -> None:
    calls: list[str] = []
    code, out = run("doctor", "--backend", "nix", runner=_runner(0, calls))
    assert code == EXIT_OK
    assert "nix" in calls
    assert "  ok nix nix 9.9.9" in out


# inspect --diff-variants

TWO_VARIANTS = """
from tundravm import File, Fragment, Package, Recipe, Variant

recipe = Recipe(
    "two",
    Fragment(
        "common",
        items=(Package("curl"), Package("jq"), File("/etc/motd", "hello\\n")),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant(
            "dev",
            parent="default",
            target="azure",
            add=Fragment("dev", items=(Package("strace"),)),
            replace=(File("/etc/motd", "dev\\n"),),
            remove=(Package("jq"),),
        ),
    ),
)
"""


@pytest.fixture
def two_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "two.py"
    path.write_text(TWO_VARIANTS, encoding="utf-8")
    return path


def test_diff_variants_text(two_variants: Path) -> None:
    code, out = run("inspect", str(two_variants), "--diff-variants", "default", "dev")
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0] == "variants default -> dev: 1 added, 1 removed, 1 changed"
    assert "  target: qemu -> azure" in lines
    assert "  + Package(strace, runtime)" in lines
    assert "  - Package(jq, runtime)" in lines
    assert any(line.startswith("  ~ File(") and line.endswith(": content") for line in lines)


def test_diff_variants_reversed_swaps_added_and_removed(two_variants: Path) -> None:
    code, out = run("inspect", str(two_variants), "--diff-variants", "dev", "default")
    assert code == EXIT_OK
    assert "  + Package(jq, runtime)" in out
    assert "  - Package(strace, runtime)" in out


def test_diff_variants_markdown_and_json(two_variants: Path) -> None:
    code, out = run(
        "inspect", str(two_variants), "--diff-variants", "default", "dev", "--format", "markdown"
    )
    assert code == EXIT_OK
    assert out.startswith("# tundravm: `two.py` variants `default` → `dev`")
    assert "Target: `qemu` → `azure`" in out
    assert "| Change | Declaration | Fields |" in out
    assert "| added | `Package(strace, runtime)` | — |" in out
    assert "| removed | `Package(jq, runtime)` | — |" in out
    assert "| content |" in out

    code, out = run("inspect", str(two_variants), "--diff-variants", "default", "dev", "--json")
    payload = json.loads(out)
    assert payload["added"] == ["Package(strace, runtime)"]
    assert payload["removed"] == ["Package(jq, runtime)"]
    assert [entry["fields"] for entry in payload["changed"]] == [["content"]]
    assert payload["targets"] == {"default": ["qemu"], "dev": ["azure"]}


def test_diff_variants_of_a_variant_with_itself_is_empty(two_variants: Path) -> None:
    code, out = run("inspect", str(two_variants), "--diff-variants", "dev", "dev")
    assert code == EXIT_OK
    assert out.strip() == "variants dev -> dev: 0 added, 0 removed, 0 changed"
    code, out = run(
        "inspect", str(two_variants), "--diff-variants", "dev", "dev", "--format", "markdown"
    )
    assert "No declaration differs." in out


def test_diff_variants_rejects_unknown_variants_and_variant_flag(
    two_variants: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run("inspect", str(two_variants), "--diff-variants", "default", "prod")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "Unknown variant(s): prod" in err and "Declared variants: default, dev" in err
    code, _ = run(
        "inspect", str(two_variants), "--diff-variants", "default", "dev", "--variant", "dev"
    )
    assert code == EXIT_SDK_ERROR
    assert "cannot be combined" in capsys.readouterr().err


# recipe files that fail to import


def test_a_broken_recipe_is_an_sdk_error_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recipe = write_recipe_file(tmp_path, "import tundravm\nrecipe = (\n", monkeypatch)

    code, _ = run_main("lint", str(recipe))

    err = capsys.readouterr().err
    assert code == EXIT_SDK_ERROR
    assert err.startswith("error [E_VALIDATION]: Recipe recipe.py failed to load: syntax error")
    assert "location: recipe.py:2" in err
    assert "Run `python recipe.py` to see the full traceback" in err
    assert "Traceback" not in err


def test_traceback_flag_reraises_with_the_original_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = write_recipe_file(tmp_path, "import not_a_module_xyz\n", monkeypatch)

    with pytest.raises(ValidationError) as excinfo:
        run_main("bake", str(recipe), "--traceback")

    assert isinstance(excinfo.value.__cause__, ModuleNotFoundError)
