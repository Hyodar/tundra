"""Helpers shared by several test modules: repository paths, the in-process CLI, conf parsing."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from tundravm._source import GitSource, HttpSource, checkout_identity
from tundravm.cli import main
from tundravm.models import BakeRequest

REPO_ROOT = Path(__file__).resolve().parent.parent
SURGE_EXAMPLE = REPO_ROOT / "examples" / "surge-tdx-prover"
EXAMPLE_RECIPES = tuple(
    path for path in sorted((REPO_ROOT / "examples").glob("*.py")) if path.name != "__init__.py"
)
"""The top-level example recipe files (the surge flagship lives in its own directory)."""


def source_dir(
    name: str,
    pin: str,
    url: str,
    *,
    http: bool = False,
    subdir: str | None = None,
    submodules: bool = False,
) -> str:
    """The ``.sources`` directory of *name*'s checkout of *pin* from *url* (``pin_dir``)."""
    source = (
        HttpSource(url) if http else GitSource(url, "ref", subdir=subdir, submodules=submodules)
    )
    return f"{name}-{pin[:12]}-{checkout_identity(source)[:8]}"


def run_main(*argv: str) -> tuple[int, str]:
    """Run ``tundravm *argv`` in-process; return the exit code and stdout."""
    out = io.StringIO()
    code = main(list(argv), stdout=out)
    return code, out.getvalue()


def write_recipe_file(
    tmp_path: Path, source: str, monkeypatch: pytest.MonkeyPatch | None = None
) -> Path:
    """Write *source* to ``tmp_path / "recipe.py"``.

    With *monkeypatch*, the test also runs from *tmp_path*, so the CLI's ``build/`` is
    ``tmp_path / "build"``.
    """
    if monkeypatch is not None:
        monkeypatch.chdir(tmp_path)
    path = tmp_path / "recipe.py"
    path.write_text(source, encoding="utf-8")
    return path


def conf_list(conf: str, key: str) -> list[str]:
    """The values of a multi-line ``key=`` list in an mkosi.conf."""
    lines = conf.splitlines()
    start = lines.index(f"{key}=") + 1 if f"{key}=" in lines else len(lines)
    values: list[str] = []
    for line in lines[start:]:
        if not line.startswith("    "):
            break
        values.append(line.strip())
    return values


def bake_request(tmp_path: Path) -> BakeRequest:
    """A ``default`` bake request with its build and emit dirs under *tmp_path*."""
    return BakeRequest(profile="default", build_dir=tmp_path / "build", emit_dir=tmp_path / "emit")
