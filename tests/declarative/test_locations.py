"""Declarations know where they were written: diagnostics and origins point at the line."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import write_image_file
from tundravm import _location
from tundravm.check import render, render_github
from tundravm.declarative import Diagnostic, Recipe, Service, lint, lower
from tundravm.declarative.resolve import provenance, resolve
from tundravm.errors import ValidationError
from tundravm.recipe import load_file

IMAGE = """
from tundravm import File, Image, Profile, Service

app = Service("app", ["/usr/bin/app"], user="ghost")
image = Image(
    "located",
    services=[app],
    profiles={"dev": Profile(changes=[File.text("/etc/gone", "x\\n").ref.remove()])},
)
"""
VALID = IMAGE.replace('File.text("/etc/gone", "x\\n").ref.remove()', "app.ref.edit(user=None)")
"""The same image, every profile resolving (lowering resolves every profile)."""


def _finding(recipe: Recipe, code: str) -> Diagnostic:
    return next(d for d in lint(recipe, variants=["production"]) if d.code == code)


def _app(recipe: Recipe) -> Service:
    return next(i for i in recipe.items if isinstance(i, Service) and i.name == "app.service")


def _write(path: Path, source: str) -> Path:
    path.write_text(source, encoding="utf-8")
    return path


@pytest.fixture
def image_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return write_image_file(tmp_path, IMAGE, monkeypatch)


def test_resolution_and_compiler_diagnostics_carry_the_line(image_path: Path) -> None:
    recipe = load_file(image_path).recipe
    missing = next(d for d in lint(recipe, variants=["dev"]) if d.code == "remove-missing")
    assert missing.location == "image.py:8"
    with pytest.raises(ValidationError, match=r"which it does not inherit at image\.py:8"):
        resolve(recipe, variant="dev")
    # the dev profile shapes the tree production builds too: its error stops production's lint
    assert "remove-missing" in [d.code for d in lint(recipe, variants=["production"])]
    (image_path.parent / "valid").mkdir()
    valid = load_file(_write(image_path.parent / "valid" / "image.py", VALID)).recipe
    ghost = _finding(valid, "service-user-missing")
    assert (ghost.code, ghost.location) == ("service-user-missing", "valid/image.py:4")
    assert "    at: valid/image.py:4\n" in render([ghost])
    first = render_github([ghost], image_path).splitlines()[0]
    assert first.startswith("::error file=valid/image.py,line=4,title=service-user-missing::")


def test_moving_a_declaration_keeps_equality_and_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = load_file(write_image_file(tmp_path, VALID, monkeypatch)).recipe
    moved_source = VALID.replace("app = Service(", "\n\n\napp = Service(")
    moved = load_file(_write(tmp_path / "moved.py", moved_source)).recipe
    assert _app(here).declared_at.endswith(":4")
    assert _app(moved).declared_at.endswith(":7")
    assert _app(here) == _app(moved) and hash(_app(here)) == hash(_app(moved))
    assert repr(_app(here)) == repr(_app(moved))
    digests = (lower(r, variants=["production"]).digest() for r in (here, moved))
    assert len(set(digests)) == 1


def test_edits_keep_the_declaration_line_and_record_their_own(image_path: Path) -> None:
    edited_path = _write(image_path.parent / "edited.py", VALID)
    recipe = load_file(edited_path).recipe
    (item,) = (
        i
        for i in resolve(recipe, variant="dev").items
        if isinstance(i, Service) and i.name == "app.service"
    )
    assert item.declared_at == f"{edited_path}:4"
    declared, edited = provenance(recipe, variant="dev")[("Service", "app.service")]
    assert declared.describe() == "declared at edited.py:4 in the image"
    assert edited.describe() == "user edited at edited.py:8 in profile dev"


def test_a_skipped_frame_reached_through_dotdot_stays_skipped() -> None:
    # A console script started as `../.venv/bin/tundravm` runs under an unnormalized path.
    package = Path(_location.__file__).parent
    code = compile("found = caller_location()", f"{package}/../tundravm/cli.py", "exec")
    namespace: dict[str, object] = {"caller_location": _location.caller_location}
    exec(code, namespace)
    assert str(namespace["found"]).startswith(f"{__file__}:")
