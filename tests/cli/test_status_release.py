"""``tundravm status`` as a release-readiness view: ``release`` lines, ``next`` past the bake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm.cli import EXIT_FAILURE, EXIT_OK
from tundravm.errors import ValidationError
from tundravm.project import find_project
from tundravm.status import (
    BLOCKED,
    UP_TO_DATE,
    Invocation,
    ProjectStatus,
    StatusItem,
    _release_next,
)

RECIPE = """
from tundravm import Fragment, Package, Recipe, Variant
from tundravm.backends.inprocess import InProcessBackend

backend = InProcessBackend()
recipe = Recipe(
    "node",
    Fragment("common", items=(Package("curl"), Package("linux-image-amd64"))),
    variants=(
        Variant("default", target="qemu"),
        Variant("dev", parent="default", add=Fragment("dev", items=(Package("strace"),)),
                release=False),
    ),
)
"""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_recipe_file(tmp_path, RECIPE, monkeypatch)
    assert run_main("lock", str(path))[0] == EXIT_OK
    return path


def _status(*argv: str) -> dict[str, object]:
    code, out = run_main("status", *argv, "--format", "json")
    assert code == EXIT_OK
    payload: dict[str, object] = json.loads(out)
    return payload


def _release(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    entry = payload["release"]
    assert isinstance(entry, dict)
    return entry


def _codes(payload: dict[str, object], variant: str) -> list[str]:
    reasons = _release(payload)[variant]["reasons"]
    assert isinstance(reasons, list)
    return [str(reason["code"]) for reason in reasons]


def test_readiness_follows_a_second_build_then_release(recipe: Path) -> None:
    unbaked = _status(str(recipe))
    assert sorted(_release(unbaked)) == ["default"]  # dev is Variant(release=False)
    assert _codes(unbaked, "default") == ["not-baked"]
    assert unbaked["release_require"] == ["reproducible", "lock"]

    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    baked = _status(str(recipe))
    assert _release(baked)["default"]["ready"] is False
    assert _codes(baked, "default") == ["reproducibility-unchecked"]
    assert baked["next"] == f"tundravm bake {recipe} --verify-reproducible"

    assert run_main("bake", str(recipe), "--verify-reproducible", "-q")[0] == EXIT_OK
    ready = _status(str(recipe))
    assert _release(ready)["default"]["ready"] is True
    assert _release(ready)["default"]["reasons"] == []
    assert ready["next"] == (
        f"tundravm release {recipe} --version VERSION --require reproducible,lock"
    )
    assert ready["next"] != UP_TO_DATE

    assert run_main("release", str(recipe), "--version", "1.0")[0] == EXIT_OK
    released = _status(str(recipe), "--variant", "default")
    assert _release(released)["default"]["released"] == str(Path("releases") / "node-1.0")
    assert released["releases_in_progress"] == []
    assert released["next"] == UP_TO_DATE
    # the release baked only what it ships, so the dev variant is still to bake
    assert _status(str(recipe))["next"] == f"tundravm bake {recipe}"


def test_release_lines_in_text(recipe: Path) -> None:
    assert run_main("bake", str(recipe), "-q")[0] == EXIT_OK
    code, out = run_main("status", str(recipe))
    assert code == EXIT_OK
    lines = [line for line in out.splitlines() if line.startswith("release ")]
    assert lines == [
        "release   blocked  default: blocked (reproducibility-unchecked)  "
        "requires reproducible, lock"
    ]


def test_only_ineligible_variants_have_nothing_to_release(recipe: Path) -> None:
    assert run_main("bake", str(recipe), "--variant", "dev", "-q")[0] == EXIT_OK
    payload = _status(str(recipe), "--variant", "dev")
    assert _release(payload) == {}
    code, out = run_main("status", str(recipe), "--variant", "dev")
    assert "release   n/a      no selected variant is eligible for release" in out
    assert payload["next"] == UP_TO_DATE


def test_pyproject_require_blocks_and_next_never_says_up_to_date(
    recipe: Path, tmp_path: Path
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrecipe = "recipe.py"\nrequire = ["lock", "real"]\n', encoding="utf-8"
    )
    assert run_main("bake", "-q")[0] == EXIT_OK
    payload = _status()
    assert payload["release_require"] == ["lock", "real"]
    assert _codes(payload, "default") == ["simulated-artifact"]
    assert payload["next"] == (
        f"{BLOCKED}: default (simulated-artifact): simulated artifacts need a bake with a "
        "real --backend"
    )
    code, out = run_main("status")
    assert "everything is up to date" not in out
    assert "release   blocked  default: blocked (simulated-artifact)  requires lock, real" in out


def test_an_unfinished_release_is_pending_with_its_resume_command(recipe: Path) -> None:
    code, _ = run_main("release", str(recipe), "--version", "1.0", "--require", "real")
    assert code == EXIT_FAILURE
    payload = _status(str(recipe))
    pending = payload["releases_in_progress"]
    assert isinstance(pending, list) and len(pending) == 1
    row = pending[0]
    assert (row["name"], row["version"], row["verdict"]) == ("node-1.0", "1.0", "fail")
    assert row["reasons"] == ["simulated-artifact"]
    assert row["resume"] == f"tundravm release {recipe} --version 1.0"
    code, out = run_main("status", str(recipe), "--variant", "default")
    assert code == EXIT_OK
    assert (
        f"release   pending  node-1.0: in progress at {Path('releases') / 'node-1.0'}, "
        f"verdict fail (simulated-artifact)  resume with: tundravm release {recipe} "
        "--version 1.0"
    ) in out.splitlines()
    assert out.splitlines()[-1] == f"next: tundravm release {recipe} --version 1.0"

    assert (
        run_main(
            "release",
            str(recipe),
            "--version",
            "1.0",
            "--require",
            "reproducible,lock",
            "--change-spec",
        )[0]
        == EXIT_OK
    )
    assert _status(str(recipe))["releases_in_progress"] == []


def _blocked(variant: str, *codes: str) -> StatusItem:
    reasons = [{"severity": "incomplete", "code": code, "subject": variant} for code in codes]
    return StatusItem("release", "blocked", "", {"variant": variant, "reasons": reasons})


def _real(variant: str) -> StatusItem:
    return StatusItem("artifact", "ok", "", {"variant": variant, "simulated": False})


def _with(releases: tuple[StatusItem, ...], artifacts: tuple[StatusItem, ...]) -> ProjectStatus:
    item = StatusItem("x", "ok", "")
    return ProjectStatus(
        recipe=item,
        lint=item,
        lock=item,
        sources=(),
        tree=item,
        manifest=Path("build") / "bake-result.json",
        artifacts=artifacts,
        manifests=(),
        backend=item,
        next="",
        releases=releases,
        require=("measured", "attested"),
    )


def test_next_past_a_real_bake_is_measure_then_attest() -> None:
    call = Invocation(recipe="")
    policy = Path("build") / "default" / "policy.json"
    unmeasured = _with((_blocked("default", "policy-missing"),), (_real("default"),))
    assert _release_next(unmeasured, call) == (
        f"tundravm measure --variant default --export-policy {policy}"
    )
    unattested = _with((_blocked("default", "attestation-missing"),), (_real("default"),))
    record = Path("build") / "default" / "attestation.json"
    assert _release_next(unattested, call) == (
        f"tundravm attest --issuer ISSUER --validator VALIDATOR --policy {policy} --record {record}"
    )
    untrusted = _with((_blocked("default", "lint-errors"),), (_real("default"),))
    assert _release_next(untrusted, call) == (
        f"{BLOCKED}: default (lint-errors): tundravm evidence --require measured,attested says why"
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ('"lock"', "must be a list of strings"),
        ('["lock", "signed"]', "names unknown check(s): signed"),
    ],
)
def test_pyproject_require_is_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, message: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.tundravm]\nrequire = {value}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match=message.replace("(", r"\(").replace(")", r"\)")):
        find_project()


def test_pyproject_require_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tundravm]\nrequire = ["lock", "measured", "lock"]\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    project = find_project()
    assert project is not None and project.require == ("lock", "measured")
