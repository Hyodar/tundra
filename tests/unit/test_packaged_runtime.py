"""The login runtime ships with the package: ``pyproject.toml`` force-includes each file the
generator reads from ``tools/logincli`` (and the tools' ``go.mod``) at the path it reads the
installed copy from, so a git install declares a ``Login`` with commands like a checkout."""

from __future__ import annotations

import tomllib

import pytest

from tests.helpers import REPO_ROOT
from tundravm.declarative._login import PACKAGED_RUNTIME, RUNTIME_FILES

BUILD = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"]


@pytest.mark.parametrize(
    ("target", "name"), [(t, n) for t in ("wheel", "sdist") for n in RUNTIME_FILES]
)
def test_each_runtime_file_is_force_included_where_the_reader_finds_it(
    target: str, name: str
) -> None:
    included = BUILD["build"]["targets"][target]["force-include"]
    source = f"tools/{RUNTIME_FILES[name]}"
    assert (REPO_ROOT / source).is_file()
    installed = f"tundravm/{PACKAGED_RUNTIME}/{name}" if target == "wheel" else source
    assert included[source] == installed
