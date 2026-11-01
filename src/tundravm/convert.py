"""``convert``: an existing mkosi tree or systemd unit files as tundravm source.

``--from systemd`` reads unit files (a file, or every unit and ``NAME.d/*.conf`` drop-in of
a directory) and prints an ``Image`` file with one ``Service`` per service unit, its
drop-ins and the ``x.timer`` or ``x.socket`` that starts it folded in
(:mod:`tundravm._units.image`): where the unit differs from what the image implies a note
says so, and what no ``Image`` slot holds is listed as unsupported.

``--from mkosi`` resolves the tree with mkosi itself (the adapter's ``summary``, i.e.
``parse_config``): includes, ``mkosi.conf.d``, profiles and subimages are mkosi's to
read, not ours. Parsing runs a tree's executable inputs (an executable ``mkosi.version``,
``mkosi.configure``), so a tree with one is refused unless ``--allow-scripts``. The image
with content (the one with ``Packages=``, else the main image) becomes an ``Image``:
``Distribution=debian`` and ``Release=`` as ``base=``, ``Packages=`` as ``packages=``,
``ExtraTrees=`` and ``SkeletonTrees=`` as ``Tree(...)``, the scripts as ``Mkosi(scripts=)``;
every other setting that differs from mkosi's default is reported as unsupported, with its
value, and nothing is silently dropped.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError

EXECUTABLE_INPUTS = ("mkosi.version", "mkosi.configure")
"""Files mkosi executes while it parses a tree, when they are executable."""

SCRIPTS = {
    "PrepareScripts": "prepare",
    "BuildScripts": "build",
    "PostInstallationScripts": "postinst",
    "FinalizeScripts": "finalize",
    "PostOutputScripts": "postoutput",
}

MAPPED = frozenset(
    {
        "Distribution",
        "Release",
        "Packages",
        "ExtraTrees",
        "SkeletonTrees",
        "ImageId",
        "Image",
        *SCRIPTS,
    }
)
"""What becomes part of the ``Image``."""

IMPLIED = frozenset(
    {
        "Architecture",
        "Format",
        "Output",
        "Bootable",
        "Seed",
        "SourceDateEpoch",
        "Snapshot",
        "ToolsTree",
        "ToolsTreeDistribution",
        "ToolsTreeRelease",
        "ToolsTreeSnapshot",
        "MinimumVersion",
        "Dependencies",
        "OutputDirectory",
        "History",
        "ManifestFormat",
        "KernelModules",
        "WithNetwork",
        "CleanPackageMetadata",
        "Files",
        "Profiles",
        "RepartDirectories",
        "SplitArtifacts",
        "Include",
        "Mirror",
        "MachineId",
        "BuildSources",
        "Machine",
        "OutputExtension",
        "ProxyExclude",
        "CompressOutput",
    }
)
"""Settings tundravm decides itself for every image (the tree it emits sets them)."""


@dataclass(slots=True)
class Converted:
    """The source a conversion printed and what it could not carry over."""

    source: str
    unsupported: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ── systemd ──────────────────────────────────────────────────────────────

UNIT_SUFFIXES = (".service", ".socket", ".timer", ".mount", ".path", ".target")


def _unit_files(path: Path) -> dict[str, Path]:
    if path.is_file():
        name = f"{path.parent.name}/{path.name}" if path.parent.name.endswith(".d") else path.name
        return {name: path}
    found: dict[str, Path] = {}
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        if item.suffix in UNIT_SUFFIXES:
            found[item.name] = item
        elif item.suffix == ".conf" and item.parent.name.endswith(".d"):
            found[f"{item.parent.name}/{item.name}"] = item
    if not found:
        raise ValidationError(
            f"{path} holds no unit file.",
            hint="Pass a unit file or a directory of them (x.service, x.socket, "
            "x.service.d/*.conf).",
        )
    return found


def from_systemd(path: Path) -> Converted:
    """Unit files at *path* as an ``Image`` file of ``Service`` values (``convert --from
    systemd``); see :mod:`tundravm._units.image`."""
    from ._image import Image
    from ._units.image import image_source

    texts: dict[str, str] = {}
    for name, file in _unit_files(path).items():
        try:
            texts[name] = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValidationError(
                f"cannot read {file}: {error}", hint="Pass a readable UTF-8 unit file."
            ) from None
    found = image_source(texts)
    name = (path.stem if path.is_file() else path.resolve().name) or "image"
    Image(name, services=[service for _, service, _ in found.services])
    imports = {"Image"}
    entries: list[str] = []
    for units, service, description in found.services:
        entries.append(f"# {units}" + (f": {description}" if description else ""))
        entries.append(f"{_call(service, imports, lists=True)},")
    services = "".join(f"        {entry}\n" for entry in entries)
    source = (
        f'"""{name}: converted from {path.name} by `tundravm convert --from systemd`."""\n\n'
        f"{_imports(imports)}\n\n"
        f"image = Image(\n    {name!r},\n    services=[\n{services}    ],\n)\n"
    )
    return Converted(_formatted(source), found.unsupported, found.notes)


def _call(value: Any, imports: set[str], *, lists: bool = False) -> str:
    """*value* (a dataclass) as its constructor call: the required fields, then each other
    field that differs from its default; *lists* writes a tuple field as a list."""
    imports.add(type(value).__name__)
    args: list[str] = []
    for item in fields(value):
        if not item.init:
            continue
        current = getattr(value, item.name)
        required = item.default is MISSING and item.default_factory is MISSING
        if not required:
            default = item.default if item.default is not MISSING else item.default_factory()  # type: ignore[misc]
            if current == default:
                continue
        text = _source(current, imports, lists=lists)
        args.append(text if required and not item.kw_only else f"{item.name}={text}")
    return f"{type(value).__name__}({', '.join(args)})"


def _source(value: object, imports: set[str], *, lists: bool = False) -> str:
    if isinstance(value, tuple):
        parts = [_source(item, imports) for item in value]
        if lists:
            return "[" + ", ".join(parts) + "]"
        return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
    if isinstance(value, Mapping):
        pairs = (f"{_source(k, imports)}: {_source(v, imports)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    if is_dataclass(value) and not isinstance(value, type):
        return _call(value, imports)
    return repr(value)


def _formatted(source: str) -> str:
    """*source* as ``ruff format`` writes it, or as is when ruff cannot run."""
    try:
        done = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--stdin-filename", "image.py", "-"],
            input=source,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return source
    return done.stdout if done.returncode == 0 and done.stdout else source


def _imports(names: Iterable[str]) -> str:
    """One ``from <namespace> import ...`` line per public namespace *names* live in."""
    import importlib

    from ._reference import MODULES

    homes: dict[str, list[str]] = {}
    for name in names:
        home = next(m for m in MODULES if name in importlib.import_module(m).__all__)
        homes.setdefault(home, []).append(name)
    return "\n".join(
        f"from {module} import {', '.join(sorted(homes[module]))}"
        for module in MODULES
        if module in homes
    )


# ── mkosi ────────────────────────────────────────────────────────────────


def executable_inputs(tree: Path) -> list[Path]:
    """The files mkosi would execute while parsing *tree*."""
    found = []
    for name in EXECUTABLE_INPUTS:
        for path in sorted(tree.rglob(name)):
            if path.is_file() and os.access(path, os.X_OK):
                found.append(path)
    return found


def from_mkosi(tree: Path, *, allow_scripts: bool = False) -> Converted:
    """The mkosi tree at *tree* as an ``Image`` file."""
    root = tree.resolve()
    if not (root / "mkosi.conf").is_file():
        raise ValidationError(
            f"{tree} has no mkosi.conf.",
            hint="Pass the directory mkosi builds from (the one holding mkosi.conf).",
        )
    scripts = executable_inputs(root)
    if scripts and not allow_scripts:
        names = ", ".join(str(p.relative_to(root)) for p in scripts)
        raise ValidationError(
            f"Parsing {tree} runs {names}.",
            hint="mkosi executes these while it reads the tree; pass --allow-scripts if you "
            "trust the tree.",
        )
    from . import _mkosi

    summary = _mkosi.summary(root)
    images = list(summary.images)
    content = next((i for i in images if i.get("Packages")), summary.main)
    converted = _image_source(content, root)
    for image in images:
        if image is content:
            continue
        name = image.get("Image") or "main"
        fmt = image.get("Format")
        if name == "main" and fmt == "disk":
            converted.notes.append(
                "main image (disk): the QEMU disk tundravm builds for every image"
            )
        elif name in ("azure", "gcp"):
            converted.notes.append(f"subimage {name}: targets=(..., {name!r})")
        else:
            converted.unsupported.append(f"subimage {name} (Format={fmt}): no Image slot")
    if scripts:
        converted.notes.append(f"ran while parsing: {', '.join(p.name for p in scripts)}")
    return converted


def _defaults() -> dict[str, Any]:
    """Every setting's value in an empty tree (what mkosi chooses when nothing is said)."""
    import tempfile

    from . import _mkosi

    with tempfile.TemporaryDirectory(prefix="tundravm-convert-") as scratch:
        (Path(scratch) / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\n")
        return dict(_mkosi.summary(Path(scratch)).main)


def _image_source(config: Mapping[str, Any], root: Path) -> Converted:
    defaults = _defaults()
    name = str(config.get("ImageId") or config.get("Image") or root.name)
    unsupported: list[str] = []
    imports = ["Image"]
    args: list[str] = [repr(name)]
    distribution, release = config.get("Distribution"), config.get("Release")
    if distribution == "debian":
        imports.append("Debian")
        args.append(f"base=Debian({release!r})")
    else:
        unsupported.append(f"Distribution={distribution}: tundravm builds Debian only")
    packages = list(config.get("Packages") or ())
    if packages:
        wrapped = textwrap.wrap(", ".join(repr(p) for p in packages), width=84)
        args.append("packages=[\n        " + "\n        ".join(wrapped) + ",\n    ]")
    trees = []
    for key in ("ExtraTrees", "SkeletonTrees"):
        for entry in config.get(key) or ():
            tree = Path(str(entry.get("Source")))
            if not tree.exists():
                continue
            trees.append(f"Tree({_relative(tree, root)!r}, {str(entry.get('Target') or '/')!r})")
            if key == "SkeletonTrees":
                unsupported.append(
                    f"SkeletonTrees={_relative(tree, root)}: installed as files, after the packages"
                )
    if trees:
        imports.append("Tree")
        args.append("files=[\n        " + ",\n        ".join(trees) + ",\n    ]")
    scripts: list[str] = []
    for key, stage in SCRIPTS.items():
        for script in config.get(key) or ():
            path = Path(str(script))
            if path.is_file():
                scripts.append(f"({stage!r}, (HERE / {_relative(path, root)!r}).read_text())")
    if scripts:
        imports.append("Mkosi")
        args.append("mkosi=Mkosi(scripts=[\n        " + ",\n        ".join(scripts) + ",\n    ])")
    for key, value in sorted(config.items()):
        if key in MAPPED or key in IMPLIED or value == defaults.get(key):
            continue
        unsupported.append(f"{key}={_shown(value, root)}")
    body = ",\n    ".join(args)
    header = "from pathlib import Path\n\n" if scripts else ""
    source = (
        f'"""{name}: converted from {root.name} by `tundravm convert --from mkosi`."""\n\n'
        f"{header}from tundravm import {', '.join(sorted(imports))}\n\n"
        + ("HERE = Path(__file__).parent\n\n" if scripts else "")
        + f"image = Image(\n    {body},\n)\n"
    )
    return Converted(source, unsupported)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _shown(value: object, root: Path) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text.replace(str(root) + "/", "")


def render(converted: Converted) -> Iterable[str]:
    """The lines ``convert`` prints after the source: notes and unsupported semantics."""
    for note in converted.notes:
        yield f"# note: {note}"
    for item in converted.unsupported:
        yield f"# unsupported: {item}"


__all__ = [
    "EXECUTABLE_INPUTS",
    "Converted",
    "executable_inputs",
    "from_mkosi",
    "from_systemd",
    "render",
]
