"""Loading an image file: ``tundravm.load`` and the CLI's ``load_file`` find the one
``Image`` the file binds, and a file that fails says where."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import tundravm
from tundravm.errors import ValidationError
from tundravm.recipe import load_file

HEADER = "from tundravm import Debian, Image\n"


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("attribute", "release"),
    [(None, "trixie"), ("other", "sid"), ("third", "bookworm")],
    ids=["image", "attr-image", "attr-factory"],
)
def test_the_bound_image_or_a_named_one_or_a_factory_loads(
    tmp_path: Path, attribute: str | None, release: str
) -> None:
    path = _write(
        tmp_path,
        "image.py",
        HEADER + "image = Image('r', packages=['busybox'])\n"
        "other = Image('r', base=Debian('sid'), packages=['busybox'])\n"
        "def third():\n    return Image('r', base=Debian('bookworm'), packages=['busybox'])\n",
    )
    assert tundravm.load(path, attribute=attribute).base.release == release
    loaded = load_file(path, attribute=attribute)
    assert loaded.declared == tundravm.load(path, attribute=attribute)
    assert loaded.recipe.name == "r" and loaded.variants == ("production",)


@pytest.mark.parametrize(
    ("body", "attribute", "message"),
    [
        (HEADER + "image = Image('r')\n", "nope", "binds nothing to 'nope'"),
        (HEADER + "image = Image('r')\n", "Image", "binds 'Image' to a type, not an Image"),
        (HEADER + "def build():\n    return 3\n", "build", "binds 'build' to a function"),
        (HEADER + "a = Image('a')\nb = Image('b')\n", None, "defines several images: a, b"),
        ("from tundravm.declarative import Recipe\nrecipe = Recipe('r')\n", None, "not an Image"),
        (HEADER + "VALUE = 1\n", None, "does not define an Image"),
    ],
    ids=["unbound", "a-type", "factory-of-other", "several", "internal-model", "none"],
)
def test_a_file_without_the_image_asked_for_is_refused(
    tmp_path: Path, body: str, attribute: str | None, message: str
) -> None:
    path = _write(tmp_path, "image.py", body)
    with pytest.raises(ValidationError, match=message) as caught:
        tundravm.load(path, attribute=attribute)
    assert caught.value.hint
    with pytest.raises(ValidationError, match=message):
        load_file(path, attribute=attribute)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not found"):
        load_file(tmp_path / "missing.py")


@pytest.mark.parametrize(
    ("body", "line", "error"),
    [
        (HEADER + "image = Image('r',\n", 2, "SyntaxError: '(' was never closed"),
        ("import tundravm\nimport not_a_module_xyz\n", 2, "ModuleNotFoundError: No module"),
        (HEADER + "\nVALUE = 1 / 0\n", 3, "ZeroDivisionError: division by zero"),
    ],
    ids=["syntax", "import", "runtime"],
)
def test_a_file_that_fails_says_the_line_and_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, line: int, error: str
) -> None:
    monkeypatch.chdir(tmp_path)
    path = _write(tmp_path, "image.py", body)
    with pytest.raises(ValidationError) as caught:
        load_file(path)
    exc = caught.value
    assert exc.code == "E_VALIDATION"
    assert exc.context["location"] == f"image.py:{line}"
    assert exc.context["error"].startswith(error)
    assert exc.hint is not None and "python image.py" in exc.hint
    assert exc.__cause__ is not None
    assert not any(getattr(m, "__file__", None) == str(path) for m in sys.modules.values())


def test_errors_in_a_helper_module_point_at_the_helper(tmp_path: Path) -> None:
    _write(tmp_path, "helpers.py", "def items():\n    return [][0]\n")
    path = _write(tmp_path, "image.py", HEADER + "import helpers\nITEMS = helpers.items()\n")
    with pytest.raises(ValidationError) as caught:
        load_file(path)
    assert caught.value.context["location"].endswith("helpers.py:2")
    assert caught.value.context["error"].startswith("IndexError")


def test_an_sdk_error_the_file_raises_passes_through(tmp_path: Path) -> None:
    path = _write(tmp_path, "image.py", HEADER + "image = Image('r', base=Debian(1))\n")
    with pytest.raises(ValidationError) as caught:
        load_file(path)
    assert "location" not in caught.value.context


def test_a_sibling_module_resolves_and_an_edit_is_never_hidden_by_stale_bytecode(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "helper_pkgs.py", "PACKAGES = ['curl', 'jq']\n")
    body = "from helper_pkgs import PACKAGES\n" + HEADER + "image = Image('r', packages={})\n"
    path = _write(tmp_path, "image.py", body.format("PACKAGES"))
    assert list(tundravm.load(path).packages) == ["curl", "jq"]
    stamp = path.stat().st_mtime_ns
    path.write_text(body.format("['aaaaa']"), encoding="utf-8")  # the same size
    os.utime(path, ns=(stamp, stamp))  # and the same mtime
    assert list(tundravm.load(path).packages) == ["aaaaa"]
    assert not (tmp_path / "__pycache__").exists()


@pytest.mark.parametrize("order", ["ab", "aba", "bab"])
def test_two_projects_each_import_their_own_package_of_one_name(tmp_path: Path, order: str) -> None:
    images = {}
    for project, package in (("a", "curl"), ("b", "jq")):
        root = tmp_path / project
        (root / "parts").mkdir(parents=True)
        (root / "pyproject.toml").write_text("")
        _write(root / "parts", "__init__.py", f"PACKAGES = [{package!r}]\n")
        body = "from parts import PACKAGES\n" + HEADER + "image = Image('r', packages=PACKAGES)\n"
        images[project] = (_write(root, "image.py", body), package)
    for project in order:
        path, package = images[project]
        assert list(tundravm.load(path).packages) == [package]


@pytest.mark.parametrize("fixture", ["recipe", "example"])
def test_the_kernel_workflow_fixtures_are_image_files(fixture: str) -> None:
    """``kernel.yml`` builds each one's ``dev`` profile, which boots its probe service."""
    path = Path(__file__).parents[1] / "fixtures" / "kernel" / f"{fixture}.py"
    loaded = load_file(path, extra_paths=[Path(__file__).parents[2]])
    assert loaded.declared.kernel.built
    assert loaded.lowered().profile_names == ("dev", "production")
