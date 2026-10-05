"""``import_tree``/``tundravm import``: an mkosi tree back to a recipe that compiles to it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE, run_main
from tundravm import Imported, import_tree
from tundravm.declarative import compile, diff, lint, load
from tundravm.declarative.lifecycle import read_tree
from tundravm.errors import ValidationError
from tundravm.templates import TEMPLATES, render_recipe_template

FOREIGN_CONF = """\
[Distribution]
Distribution=debian
Release=bookworm
Architecture=x86-64

[Output]
Format=disk
ImageId=myapp

[Content]
Packages=
    curl
    nginx
Bootable=no
PostInstallationScripts=mkosi.postinst
Hostname=myapp
"""
FOREIGN_UNIT = """\
[Unit]
Description=My app
After=network-online.target

[Service]
ExecStart=/usr/bin/nginx -g 'daemon off;'
Restart=always

[Install]
WantedBy=multi-user.target
"""
FOREIGN_CONFIG = "listen 8080;\nworkers 4;\n"
FOREIGN_POSTINST = """\
#!/bin/sh
set -e
mkosi-chroot systemctl enable myapp.service
echo built > "$BUILDROOT/etc/myapp/stamp"
"""


def _template_tree(tmp_path: Path, template: str) -> Path:
    source = tmp_path / f"{template}.py"
    source.write_text(
        render_recipe_template(
            title=template,
            filename=source.name,
            base="debian/trixie",
            backend="inprocess",
            template=template,
        ),
        encoding="utf-8",
    )
    tree = tmp_path / "mkosi"
    compile(load(source)).write(tree)
    return tree


def _ruff_formatted(source: str) -> bool:
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", "--stdin-filename", "recipe.py", "-"],
        input=source,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0


def _errors(imported: Imported) -> list[str]:
    return [d.code for d in lint(imported.recipe) if d.level == "error"]


@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_template_trees_round_trip(tmp_path: Path, template: str) -> None:
    tree = _template_tree(tmp_path, template)
    imported = import_tree(tree, name=template)
    assert diff(compile(imported.recipe), tree) == ""
    assert "round trip: the recipe compiles back to this tree" in imported.notes
    assert imported.recipe.name == template
    assert _errors(imported) == []
    assert _ruff_formatted(imported.recipe_source)
    assert imported.coverage["declared"] > imported.coverage["verbatim"]


def test_service_template_imports_first_class_declarations(tmp_path: Path) -> None:
    imported = import_tree(_template_tree(tmp_path, "service"))
    kinds = {type(item).__name__ for item in imported.recipe.common.items}
    assert {"Service", "Init", "User", "File", "Package"} <= kinds
    assert [v.name for v in imported.recipe.variants] == ["default", "dev"]
    assert "parent=None" not in imported.recipe_source


def test_the_written_module_loads_and_compiles_back(tmp_path: Path) -> None:
    tree = _template_tree(tmp_path, "minimal")
    out = tmp_path / "recipes" / "imported.py"
    code, stdout = run_main("import", str(tree), "--out", str(out))
    assert code == 0
    assert stdout.startswith(f"wrote {out}\ncoverage: ")
    assert diff(compile(load(out)), tree) == ""


def test_surge_tree_round_trips_under_nethermind_v1() -> None:
    tree = SURGE_EXAMPLE / "mkosi"
    imported = import_tree(tree, dialect="nethermind-v1")
    assert imported.recipe.mkosi.dialect == "nethermind-v1"
    assert diff(compile(imported.recipe), tree) == ""
    assert "round trip: the recipe compiles back to this tree" in imported.notes
    assert [n for n in imported.notes if "not mapped" in n] == []
    assert _errors(imported) == []


def test_dialect_is_guessed_from_the_tree() -> None:
    imported = import_tree(SURGE_EXAMPLE / "mkosi")
    assert imported.recipe.mkosi.dialect == "nethermind-v1"
    assert any(note.startswith("dialect nethermind-v1 guessed") for note in imported.notes)


def _foreign_tree(root: Path) -> Path:
    (root / "mkosi.extra/etc/myapp").mkdir(parents=True)
    (root / "mkosi.extra/usr/lib/systemd/system").mkdir(parents=True)
    (root / "mkosi.conf").write_text(FOREIGN_CONF, encoding="utf-8")
    (root / "mkosi.extra/etc/myapp/myapp.conf").write_text(FOREIGN_CONFIG, encoding="utf-8")
    unit = root / "mkosi.extra/usr/lib/systemd/system/myapp.service"
    unit.write_text(FOREIGN_UNIT, encoding="utf-8")
    postinst = root / "mkosi.postinst"
    postinst.write_text(FOREIGN_POSTINST, encoding="utf-8")
    postinst.chmod(0o755)
    return root


def _conf_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    key = ""
    for line in text.splitlines():
        if line[:1].isspace() and line.strip():
            values[key].append(line.strip())
        elif "=" in line:
            key, _, value = line.partition("=")
            values.setdefault(key, []).extend([value] if value else [])
    return values


def test_foreign_tree_imports_and_compiles_to_the_same_image(tmp_path: Path) -> None:
    tree = _foreign_tree(tmp_path / "tree")
    imported = import_tree(tree, name="myapp")
    assert lint(imported.recipe) == ()
    assert _ruff_formatted(imported.recipe_source)
    assert "myapp: mkosi.postinst: imported as a postinst hook (scripts/ in the tree)" in (
        imported.notes
    )
    out = tmp_path / "out"
    compile(imported.recipe).write(out)
    variant = out / "myapp"
    assert [v.name for v in imported.recipe.variants] == ["myapp"]
    for rel in ("etc/myapp/myapp.conf", "usr/lib/systemd/system/myapp.service"):
        assert (variant / "mkosi.extra" / rel).read_bytes() == (
            tree / "mkosi.extra" / rel
        ).read_bytes()
    compiled = _conf_values((variant / "mkosi.conf").read_text(encoding="utf-8"))
    for key, value in _conf_values(FOREIGN_CONF).items():
        if key != "PostInstallationScripts":
            assert compiled[key] == value, key
    script = (variant / compiled["PostInstallationScripts"][0]).read_text(encoding="utf-8")
    assert FOREIGN_POSTINST.removeprefix("#!/bin/sh\n") in script
    again = import_tree(out, name="myapp")
    assert again.recipe == imported.recipe
    assert diff(compile(again.recipe), out) == ""


def test_large_and_binary_files_are_read_from_the_tree(tmp_path: Path) -> None:
    tree = _foreign_tree(tmp_path / "tree")
    blob = tree / "mkosi.extra/usr/share/myapp/blob.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(bytes(range(256)) * 8)
    out = tmp_path / "recipe.py"
    imported = import_tree(tree, out=out)
    assert "TREE = Path(__file__).resolve().parent / 'tree'" in imported.recipe_source.replace(
        '"', "'"
    )
    assert (
        "myapp: mkosi.extra/usr/share/myapp/blob.bin: binary or over 64 KiB; read from the tree"
        in imported.notes
    )
    written = tmp_path / "compiled"
    compile(imported.recipe).write(written)
    assert (written / "myapp/mkosi.extra/usr/share/myapp/blob.bin").read_bytes() == (
        blob.read_bytes()
    )


def test_build_state_is_skipped_with_a_note(tmp_path: Path) -> None:
    tree = _foreign_tree(tmp_path / "tree")
    (tree / "mkosi.sandbox/etc/apt").mkdir(parents=True)
    (tree / "mkosi.sandbox/etc/apt/extra.sources").write_text("Types: deb\n", encoding="utf-8")
    imported = import_tree(tree)
    assert any(
        note.startswith("myapp: mkosi.sandbox/etc/apt/extra.sources: skipped")
        for note in imported.notes
    )


def test_not_a_tree_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="holds no mkosi.conf"):
        import_tree(tmp_path)
    with pytest.raises(ValidationError, match="not a directory"):
        import_tree(tmp_path / "missing")


def test_cli_prints_the_module_and_reports_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = _template_tree(tmp_path, "minimal")
    code, stdout = run_main("import", str(tree), "--name", "demo")
    assert code == 0
    assert stdout.startswith('"""demo image recipe, imported from an mkosi tree')
    err = capsys.readouterr().err
    assert "coverage: " in err
    assert "note: round trip: the recipe compiles back to this tree" in err


def test_cli_json_and_overwrite(tmp_path: Path) -> None:
    tree = _template_tree(tmp_path, "minimal")
    out = tmp_path / "recipe.py"
    code, stdout = run_main("import", str(tree), "--out", str(out), "--format", "json")
    assert code == 0
    payload = json.loads(stdout)
    assert set(payload) == {"recipe_source", "recipe", "notes", "coverage"}
    assert payload["recipe_source"] == out.read_text(encoding="utf-8")
    assert set(payload["coverage"]) == {"declared", "verbatim"}
    code, _ = run_main("import", str(tree), "--out", str(out))
    assert code != 0
    code, _ = run_main("import", str(tree), "--out", str(out), "--force")
    assert code == 0


def test_repo_root_is_not_written(tmp_path: Path) -> None:
    before = {p.name for p in REPO_ROOT.iterdir()}
    import_tree(_template_tree(tmp_path, "minimal"))
    assert {p.name for p in REPO_ROOT.iterdir()} == before
    assert read_tree(tmp_path / "mkosi").entries
