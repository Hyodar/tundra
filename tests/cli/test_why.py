"""``tundravm inspect --why`` and ``explain_why``: declarations, overlay steps and files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers import run_main, write_recipe_file
from tundravm import ValidationError, explain_why
from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR
from tundravm.declarative import Directory, Fragment, Recipe, Variant
from tundravm.declarative.resolve import Origin, provenance
from tundravm.recipe import load_recipe

RECIPE = """
from tundravm.declarative import File, Fragment, Init, Package, Recipe, Service, Variant
from tundravm.declarative.utils import Tdxs

app = Fragment(
    "app",
    items=(
        Package("curl"),
        File("/etc/app.conf", "mode=dev\\n"),
        Service("app", "/usr/bin/app", wanted_by="minimal.target"),
        Init("prepare", "echo prepare"),
    ),
)
recipe = Recipe(
    "two",
    Fragment("base", items=(Package("systemd"), app)),
    variants=(
        Variant("dev", target="qemu"),
        Variant(
            "prod",
            parent="dev",
            add=Tdxs(issuer="tdx"),
            replace=(File("/etc/app.conf", "mode=prod\\n"),),
            remove=(Package("curl"),),
        ),
    ),
)
"""


@pytest.fixture
def recipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_recipe_file(tmp_path, RECIPE, monkeypatch)


def test_why_a_replaced_file_shows_both_steps_and_its_destination(recipe: Path) -> None:
    code, out = run_main("inspect", str(recipe), "--variant", "prod", "--why", "/etc/app.conf")
    assert code == EXIT_OK
    assert out.splitlines() == [
        "why /etc/app.conf (variant prod)",
        "  File(extra, /etc/app.conf)",
        "    declared in common via base > app",
        "    replaced in variant prod",
        "files (in prod/):",
        "  mkosi.extra/etc/app.conf",
    ]
    dev = run_main("inspect", str(recipe), "--variant", "dev", "--why", "/etc/app.conf")[1]
    assert "replaced" not in dev


def test_why_a_service_lists_the_generated_dependencies(recipe: Path) -> None:
    subject = "/usr/lib/systemd/system/app.service"
    code, out = run_main("inspect", str(recipe), "--variant", "prod", "--why", subject)
    assert code == EXIT_OK
    assert "  Service(app.service)\n    declared in common via base > app\n" in out
    assert "  mkosi.extra/usr/lib/systemd/system/app.service\n" in out
    generated = out.split("generated:\n", 1)[1].splitlines()
    assert "  After=runtime-init.service (after_init=True)" in generated
    assert "  Requires=runtime-init.service (after_init=True)" in generated
    assert "  scripts/06-postinst.sh: mkosi-chroot systemctl enable app.service" in generated
    assert "  scripts/06-postinst.sh: minimal.target.wants/app.service link" in generated
    by_unit = run_main("inspect", str(recipe), "--variant", "prod", "--why", "unit:app")[1]
    assert by_unit.split("\n", 1)[1] == out.split("\n", 1)[1]


def test_why_names_the_composite_class_in_the_fragment_chain(recipe: Path) -> None:
    out = run_main("inspect", str(recipe), "--variant", "prod", "--why", "/etc/tdxs/config.yaml")[1]
    assert "    added in variant prod via tdxs (Tdxs)\n" in out
    assert "  mkosi.extra/etc/tdxs/config.yaml\n" in out


def test_why_json_reports_a_removed_package_without_files(recipe: Path) -> None:
    code, out = run_main(
        "inspect", str(recipe), "--variant", "prod", "--why", "package:curl", "--json"
    )
    assert code == EXIT_OK
    payload = json.loads(out)
    (entry,) = payload["declarations"]
    assert entry["declaration"] == "Package(curl, runtime)"
    assert entry["present"] is False
    assert [o["action"] for o in entry["origins"]] == ["declared", "removed"]
    assert entry["origins"][0]["fragments"] == [
        {"name": "base", "class": "Fragment"},
        {"name": "app", "class": "Fragment"},
    ]
    assert payload["files"] == []
    dev = json.loads(
        run_main("inspect", str(recipe), "--variant", "dev", "--why", "package:curl", "--json")[1]
    )
    assert dev["files"] == ["mkosi.conf (Packages=)"]
    assert dev["fragments"] == ["base", "app"]


def test_why_markdown_is_a_table_of_steps(recipe: Path) -> None:
    code, out = run_main(
        "inspect",
        str(recipe),
        "--variant",
        "prod",
        "--why",
        "/etc/app.conf",
        "--format",
        "markdown",
    )
    assert code == EXIT_OK
    assert out.startswith("# tundravm: why `/etc/app.conf` in `prod`\n")
    assert "| `File(extra, /etc/app.conf)` | replaced | prod | — |" in out


def test_why_an_init_points_at_runtime_init(recipe: Path) -> None:
    out = run_main("inspect", str(recipe), "--variant", "dev", "--why", "init:prepare")[1]
    assert "  Init(prepare)\n" in out
    assert "  mkosi.extra/usr/bin/runtime-init\n" in out


def test_why_unknown_subject_suggests_close_matches(
    recipe: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = run_main("inspect", str(recipe), "--variant", "prod", "--why", "/etc/app.cnf")
    assert code == EXIT_SDK_ERROR
    err = capsys.readouterr().err
    assert "error [E_VALIDATION]: Nothing in the variant matches '/etc/app.cnf'." in err
    assert "Close matches: /etc/app.conf" in err
    code, _ = run_main("inspect", str(recipe), "--variant", "prod", "--why", "unit:ap")
    assert code == EXIT_SDK_ERROR
    assert "unit:app.service" in capsys.readouterr().err


def test_why_needs_one_variant(recipe: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = run_main("inspect", str(recipe), "--why", "/etc/app.conf")
    assert code == EXIT_SDK_ERROR
    assert "--why explains one variant at a time" in capsys.readouterr().err


def test_explain_why_is_the_python_form(recipe: Path) -> None:
    loaded = load_recipe(recipe)
    why = explain_why(loaded, "prod", "/etc/app.conf")
    assert why.files == ("mkosi.extra/etc/app.conf",)
    (entry,) = why.declarations
    assert entry.origins == (
        Origin("declared", "common", (("base", "Fragment"), ("app", "Fragment"))),
        Origin("replaced", "prod"),
    )
    with pytest.raises(ValidationError, match="Nothing in the variant matches"):
        explain_why(loaded, "prod", "hook:missing")


def test_provenance_records_every_step(recipe: Path) -> None:
    trace = provenance(load_recipe(recipe), variant="prod")
    assert trace[("Package", "curl", "runtime")][-1] == Origin("removed", "prod")
    assert trace[("Package", "systemd", "runtime")] == (
        Origin("declared", "common", (("base", "Fragment"),)),
    )
    assert trace[("Unit", "tdxs.service")][0].fragments == (("tdxs", "Tdxs"),)


def test_why_under_a_directory_needs_an_emitted_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app"
    (source / "conf.d").mkdir(parents=True)
    (source / "app.conf").write_text("a\n", encoding="utf-8")
    (source / "conf.d" / "10-net.conf").write_text("b\n", encoding="utf-8")
    (source / "empty").mkdir()
    recipe = Recipe(
        "dir",
        Fragment("base", items=(Directory("/etc/app", source),)),
        variants=(Variant("dev", target="qemu"),),
    )
    why = explain_why(recipe, "dev", "/etc/app/app.conf")
    assert [entry.declaration for entry in why.declarations] == ["Directory(extra, /etc/app)"]
    assert why.files == ("mkosi.extra/etc/app/app.conf",)
    nested = explain_why(recipe, "dev", "/etc/app/conf.d")
    assert nested.files == ("mkosi.extra/etc/app/conf.d/10-net.conf",)
    assert explain_why(recipe, "dev", "/etc/app/empty").declarations == why.declarations
    whole = explain_why(recipe, "dev", "/etc/app")
    assert len(whole.files) == 2
    with pytest.raises(ValidationError, match="Nothing in the variant matches") as exc:
        explain_why(recipe, "dev", "/etc/app/typo.conf")
    assert "Close matches: /etc/app/app.conf" in (exc.value.hint or "")
