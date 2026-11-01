"""Helpers shared by several test modules: repository paths, the in-process CLI, conf parsing."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tundravm._modules import execution
from tundravm._source import GitSource, HttpSource, Resolver, checkout_identity
from tundravm.cli import main
from tundravm.declarative import Lock, Recipe, lower
from tundravm.declarative.lifecycle import FetchedSource, fetch_image, variant_names
from tundravm.models import BakeRequest, FileEntry
from tundravm.templates import render_app_files, render_recipe_template

REPO_ROOT = Path(__file__).resolve().parent.parent
SURGE_EXAMPLE = REPO_ROOT / "examples" / "surge-tdx-prover"
EXAMPLE_RECIPES = tuple(
    path for path in sorted((REPO_ROOT / "examples").glob("*.py")) if path.name != "__init__.py"
)
"""The top-level example recipe files (the surge flagship lives in its own directory)."""
GATE_FILES = frozenset({execution.GATE_SCRIPT, execution.GATE_DROPIN})
"""What the execution gate adds to every lowered profile (``tundravm._modules.execution``)."""


def own_files(files: list[FileEntry]) -> list[FileEntry]:
    """*files* without the execution gate's, which every profile ships."""
    return [entry for entry in files if entry.path not in GATE_FILES]


def fetch(
    recipe: Recipe,
    *,
    lock: Lock | None,
    build: Path,
    variants: tuple[str, ...] | None = None,
    resolver: Resolver | None = None,
) -> tuple[FetchedSource, ...]:
    """Check *recipe*'s sources out under ``build/.sources``: a bake's fetch step alone."""
    names = variant_names(recipe, variants)
    return fetch_image(
        lower(recipe, variants=names), names, locked=lock, build=build, resolver=resolver
    )


def fake_ovmf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An OVMF code image and variable store under *tmp_path* that the QEMU deploy adapter
    finds instead of the host's (a test that deploys without TDX runs where none is installed)."""
    code, store = tmp_path / "ovmf" / "OVMF_CODE.fd", tmp_path / "ovmf" / "OVMF_VARS.fd"
    code.parent.mkdir(parents=True, exist_ok=True)
    code.write_bytes(b"code")
    store.write_bytes(b"vars")
    monkeypatch.setattr("tundravm.deploy.qemu.OVMF_CODE_PATHS", (str(code),))
    monkeypatch.setattr("tundravm.deploy.qemu.OVMF_VARS_PATHS", (str(store),))
    return code, store


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


def write_template(
    path: Path, template: str, *, title: str = "node", backend: str | None = None
) -> Path:
    """Write the image file of *template* at *path* and the program files ``new`` writes
    beside it (the ``service`` template's ``app/``); return *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    source = render_recipe_template(
        title=title, filename=path.name, release="trixie", template=template, backend=backend
    )
    path.write_text(source, encoding="utf-8")
    for rel, text in render_app_files(title=title, template=template).items():
        (path.parent / rel).parent.mkdir(parents=True, exist_ok=True)
        (path.parent / rel).write_text(text, encoding="utf-8")
    return path


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
    """The values of every multi-line ``key=`` list in *conf*, in order (an mkosi.conf, or
    several joined, as ``CompiledTree.conf`` returns the payload's)."""
    values: list[str] = []
    collecting = False
    for line in conf.splitlines():
        if line == f"{key}=":
            collecting = True
        elif collecting and line.startswith("    "):
            values.append(line.strip())
        else:
            collecting = False
    return values


def bake_request(tmp_path: Path) -> BakeRequest:
    """A ``default`` bake request with its build and emit dirs under *tmp_path*."""
    return BakeRequest(profile="default", build_dir=tmp_path / "build", emit_dir=tmp_path / "emit")


def on_platform(recipe: Recipe, **changes: Any) -> Recipe:
    """*recipe* with its platform's fields *changes* replaced (``snapshot=``, ``mirror=``, ...)."""
    return replace(recipe, platform=replace(recipe.platform, **changes))
