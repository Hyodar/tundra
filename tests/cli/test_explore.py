"""``tundravm explore`` and ``explore()``: one self-contained page that explains a recipe."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from tests.helpers import REPO_ROOT, SURGE_EXAMPLE, run_main, write_recipe_file
from tundravm import (
    Exploration,
    ValidationError,
    compile,
    explore,
    load,
    lock,
    read_artifacts,
    write_lock,
)
from tundravm.cli import EXIT_OK
from tundravm.declarative import File, Fragment, Package, Recipe, Variant
from tundravm.declarative.lower import lower
from tundravm.explore import EXPLORE_SCHEMA, TEXT_LIMIT
from tundravm.templates import render_recipe_template

EXTERNAL_ASSET = re.compile(
    r"""(?:\b(?:src|href|action|poster|srcset)\s*=\s*["']?\s*(?:https?:)?//)"""
    r"""|url\(\s*["']?\s*(?:https?:)?//|@import|<link\b|<script\s+src""",
    re.I,
)
"""Anything that would make the page fetch from the network."""

SURGE_PAGE_LIMIT = 3 * 1024 * 1024


def _template(tmp_path: Path, template: str) -> Path:
    path = tmp_path / f"{template}.py"
    path.write_text(
        render_recipe_template(
            title=template,
            filename=path.name,
            base="debian/trixie",
            backend="inprocess",
            template=template,
        ),
        encoding="utf-8",
    )
    return path


def _emitted(recipe: Recipe) -> dict[str, list[str]]:
    """Every compiled tree path by variant, relative to the variant's directory."""
    tree = compile(recipe)
    found: dict[str, list[str]] = {name: [] for name in tree.variants}
    for entry in tree.entries:
        top, _, rest = entry.path.partition("/")
        found[top].append(rest)
    return found


@pytest.mark.parametrize("template", ["service", "prover"])
def test_template_page_holds_every_variant_and_emitted_path(template: str, tmp_path: Path) -> None:
    recipe = load(_template(tmp_path, template))
    found = explore(recipe)
    page = found.html()
    for variant in recipe.variants:
        assert f"<th>{variant.name}</th>" in page
        assert f"<h3>{variant.name}</h3>" in page
    emitted = _emitted(recipe)
    assert emitted
    for name, paths in emitted.items():
        assert paths
        for path in paths:
            assert f'data-path="{html.escape(path)}"' in page, (name, path)
    assert EXTERNAL_ASSET.search(page) is None
    assert page.count("<script>") == 1


def test_a_known_file_carries_its_declaration_and_provenance(tmp_path: Path) -> None:
    found = explore(load(_template(tmp_path, "service")))
    page = found.html()
    record = next(
        f for f in found.to_dict()["trees"]["default"]["files"] if f["path"].endswith("app.conf")
    )
    assert record["declarations"] == ["File(extra, /etc/app/app.conf)"]
    assert record["image_path"] == "/etc/app/app.conf"
    assert "declared in common via base &gt; app (App)" in page
    anchor = re.search(r'<tr class="decl" id="(d\d+)"[^>]*File\(extra, /etc/app/app.conf\)', page)
    assert anchor is not None
    assert f'declared by <a href="#{anchor.group(1)}">' in page
    row = next(r for r in found.to_dict()["declarations"] if r["declaration"].startswith("File("))
    assert row["variants"]["dev"]["files"] == [
        {"group": "dev", "path": "mkosi.extra/etc/app/app.conf"}
    ]


def test_matrix_states_follow_the_overlays() -> None:
    recipe = Recipe(
        "overlays",
        Fragment("base", items=(Package("curl"), File("/etc/app.conf", "mode=dev\n"))),
        variants=(
            Variant("dev", target="qemu"),
            Variant(
                "prod",
                parent="dev",
                add=Fragment("extras", items=(Package("jq"),)),
                replace=(File("/etc/app.conf", "mode=prod\n"),),
                remove=(Package("curl"),),
            ),
        ),
    )
    rows = {r["declaration"]: r["variants"] for r in explore(recipe).to_dict()["declarations"]}
    assert rows["Package(curl, runtime)"]["dev"]["state"] == "present"
    assert rows["Package(curl, runtime)"]["prod"]["state"] == "removed"
    assert rows["Package(jq, runtime)"]["dev"]["state"] == "absent"
    assert rows["Package(jq, runtime)"]["prod"]["state"] == "added"
    replaced = rows["File(extra, /etc/app.conf)"]["prod"]
    assert replaced["state"] == "replaced"
    assert replaced["changed"] == ["content"]
    assert [o["action"] for o in replaced["origins"]] == ["declared", "replaced"]


def test_markup_in_names_and_contents_is_escaped() -> None:
    evil = "<script>alert(1)</script>"
    recipe = Recipe(
        evil,
        Fragment("base", items=(File("/etc/evil.html", f"{evil}\n"),)),
        variants=(Variant("default", target="qemu"),),
    )
    page = explore(recipe).html()
    assert page.count("<script>") == 1
    assert "<title>Explore: &lt;script&gt;alert(1)&lt;/script&gt;</title>" in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;\n</pre>" in page
    assert "alert(1)</script>" not in page


def test_json_round_trips(tmp_path: Path) -> None:
    found = explore(load(_template(tmp_path, "prover")), variants=["default", "azure"])
    data = json.loads(found.json())
    assert data == found.to_dict()
    assert data["schema"] == EXPLORE_SCHEMA
    assert [v["name"] for v in data["variants"]] == ["default", "azure"]
    again = Exploration.from_dict(data)
    assert again.html() == found.html()
    assert again.to_dict() == data
    with pytest.raises(ValidationError):
        Exploration.from_dict({"schema": "tundravm.inspect/1"})


def test_lock_state_missing_current_and_drifted(tmp_path: Path) -> None:
    recipe = load(_template(tmp_path, "service"))
    missing = explore(recipe, lock=tmp_path / "none.lock").to_dict()["lock"]
    assert missing["state"] == "missing"
    path = tmp_path / "tundravm.lock"
    write_lock(lock(recipe), path)
    current = explore(recipe, lock=path).to_dict()["lock"]
    assert current["state"] == "current"
    assert current["sections"] and current["drift"] == []
    changed = Recipe(
        recipe.name,
        Fragment("more", items=(recipe.common, Package("htop"))),
        variants=recipe.variants,
    )
    drifted = explore(changed, lock=path).to_dict()["lock"]
    assert drifted["state"] == "drifted"
    assert drifted["drift"]


def test_a_lowered_recipe_has_a_tree_but_no_declarations(tmp_path: Path) -> None:
    found = explore(lower(load(_template(tmp_path, "service"))), variants=["default"])
    data = found.to_dict()
    assert data["declarations"] == []
    assert data["recipe"]["name"] is None
    assert data["trees"]["default"]["files"]
    assert "No declarations" in found.html()


def test_surge_page_stays_small_and_truncates_large_files() -> None:
    recipe = load(SURGE_EXAMPLE / "image.py", extra_paths=[REPO_ROOT])
    found = explore(recipe)
    page = found.html()
    assert len(page.encode("utf-8")) < SURGE_PAGE_LIMIT
    truncated = [
        f for tree in found.to_dict()["trees"].values() for f in tree["files"] if f.get("truncated")
    ]
    assert truncated
    assert all(len(f["text"].encode("utf-8")) <= TEXT_LIMIT for f in truncated)
    assert f"truncated: showing the first {TEXT_LIMIT // 1024} KiB" in page
    assert EXTERNAL_ASSET.search(page) is None


def test_cli_writes_the_page_into_the_build_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_recipe_file(tmp_path, _template(tmp_path, "service").read_text(), monkeypatch)
    assert run_main("bake", str(path), "-q", "--variant", "default")[0] == EXIT_OK
    code, out = run_main("explore", str(path))
    assert code == EXIT_OK
    page_path = Path(out.strip())
    assert page_path == Path("build") / "explore.html"
    page = page_path.read_text(encoding="utf-8")
    digest = read_artifacts(tmp_path / "build")[0].sha256
    assert digest in page
    assert "<th>dev</th>" in page

    code, out = run_main("explore", str(path), "--variant", "dev", "--format", "json")
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["schema"] == "tundravm.explore/2"
    assert [v["name"] for v in data["variants"]] == ["dev"]
    assert data["bake"]["artifacts"][0]["sha256"] == digest

    target = tmp_path / "pages" / "service.html"
    code, out = run_main("explore", str(path), "--output", str(target))
    assert (code, out.strip()) == (EXIT_OK, str(target))
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_cli_without_a_bake_notes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_recipe_file(tmp_path, _template(tmp_path, "service").read_text(), monkeypatch)
    code, out = run_main("explore", str(path), "--format", "json")
    assert code == EXIT_OK
    data = json.loads(out)
    assert data["bake"] is None
    assert data["lock"]["state"] == "missing"
    assert any("no bake in build" in note for note in data["notes"])
