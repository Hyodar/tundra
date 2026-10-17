"""Versioned JSON: every ``--format json`` report carries ``schema``; SDK errors become one line."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from tests.helpers import run_main
from tundravm import lint
from tundravm.check import SECRET_HINT
from tundravm.cli import EXIT_SDK_ERROR, build_parser
from tundravm.recipe import load_file

REPORTS: dict[str, tuple[str, ...]] = {
    "inspect": ("inspect", "--format", "json"),
    "lint": ("lint", "--format", "json"),
    "status": ("status", "--format", "json"),
    "config": ("config", "--format", "json"),
    "evidence": ("evidence", "--format", "json"),
    "import": ("import", "mkosi", "--format", "json"),
    "measure": ("measure", "--variant", "default", "--allow-placeholder", "--format", "json"),
    "verify": ("verify", "build/evidence", "--format", "json"),
    "changes": ("changes", "--since", "build/evidence", "--format", "json"),
    "release": ("release", "--version", "1.0", "--format", "json"),
}
"""One JSON run per verb, from inside a scaffolded, compiled, locked and baked project."""

COVERED_ELSEWHERE = {"attest": "tests/cli/test_attest.py::test_cli_attest_json_and_markdown"}
"""JSON-capable verbs whose run needs more than a project (a tdxs issuer)."""


def _json_verbs() -> set[str]:
    parser = build_parser()
    verbs = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {
        name
        for name, command in verbs.choices.items()
        for action in command._actions
        if "--format" in action.option_strings and "json" in (action.choices or ())
    }


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("project")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(root)
        argv = ("init", ".", "--name", "node", "--backend", "inprocess", "--no-doctor")
        for command in (argv, ("lock",), ("compile",), ("bake", "-q"), ("evidence",)):
            assert run_main(*command)[0] in (0, 1), command
    return root


def test_every_json_capable_verb_is_checked() -> None:
    assert _json_verbs() == set(REPORTS) | set(COVERED_ELSEWHERE)


@pytest.mark.parametrize("verb", sorted(REPORTS))
def test_json_report_carries_its_schema(
    verb: str, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    code, out = run_main(*REPORTS[verb])
    assert code in (0, 1), out
    assert json.loads(out)["schema"] == f"tundravm.{verb}/1"


def test_evidence_json_on_disk_is_what_the_report_prints(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    printed = json.loads(run_main(*REPORTS["evidence"])[1])
    on_disk = json.loads((project / "build" / "evidence" / "evidence.json").read_text())
    assert printed == on_disk


def _error_line(err: str) -> dict[str, object]:
    lines = err.strip().splitlines()
    assert len(lines) == 1, err
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.parametrize(
    "argv",
    [
        ("lint", "--variant", "nope", "--format", "json"),  # a recipe verb
        ("measure", "--build", "nowhere", "--format", "json"),  # an artifact verb
        ("status", "--variant", "nope", "--format", "json"),  # a report verb
        ("evidence", "--build", "nowhere", "--format", "json"),  # an evidence verb
    ],
)
def test_an_sdk_error_under_json_is_one_json_line(
    argv: tuple[str, ...],
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(project)
    capsys.readouterr()
    code, _ = run_main(*argv)
    assert code == EXIT_SDK_ERROR
    payload = _error_line(capsys.readouterr().err)
    assert list(payload) == ["schema", "code", "message", "hint", "context"]
    assert payload["schema"] == "tundravm.error/1"
    assert str(payload["code"]).startswith("E_")
    assert payload["message"] and "Hint:" not in str(payload["message"])
    assert isinstance(payload["context"], dict)


def test_the_unknown_variant_error_line(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(project)
    capsys.readouterr()
    assert run_main("inspect", "--variant", "nope", "--format", "json")[0] == EXIT_SDK_ERROR
    assert _error_line(capsys.readouterr().err) == {
        "schema": "tundravm.error/1",
        "code": "E_VALIDATION",
        "message": "Unknown variant(s): nope.",
        "hint": "Declared variants: default, dev",
        "context": {"recipe": "node.py"},
    }


def test_without_json_the_error_stays_a_text_block(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(project)
    capsys.readouterr()
    assert run_main("lint", "--variant", "nope")[0] == EXIT_SDK_ERROR
    assert capsys.readouterr().err.startswith("error [E_VALIDATION]: Unknown variant(s): nope.")


SECRET_RECIPE = """\
from pathlib import Path
from tundravm import File, Fragment, Package, Recipe, Variant

recipe = Recipe(
    "leaky",
    Fragment("common", items=(
        Package("linux-image-amd64"),
        File("/etc/app.conf", content="api_token = s3cR3tV4lu3Xq9Zp2Lm7\\n"),
    )),
    variants=(Variant("default", target="qemu"),),
)
"""


def test_a_compiler_rule_hint_reaches_lint_and_its_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "leaky.py").write_text(SECRET_RECIPE, encoding="utf-8")
    recipe = load_file(tmp_path / "leaky.py").recipe
    (found,) = [d for d in lint(recipe) if d.code == "secret-in-file"]
    assert (found.severity, found.location, found.subject) == ("error", "default", "/etc/app.conf")
    assert found.hint == SECRET_HINT
    code, out = run_main("lint", "leaky.py", "--format", "json")
    assert code == 1
    payload = json.loads(out)
    assert payload["schema"] == "tundravm.lint/1"
    (entry,) = [d for d in payload["diagnostics"] if d["code"] == "secret-in-file"]
    assert entry == found.to_dict()
    assert entry["hint"] == SECRET_HINT
