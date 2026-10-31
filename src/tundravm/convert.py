"""``convert``: an existing mkosi tree or systemd unit files as tundravm source.

``--from systemd`` reads unit files (a file, or every unit and ``NAME.d/*.conf`` drop-in of
a directory) and prints the typed declarations they convert to, pairing ``x.socket`` and
``x.timer`` with ``x.service``; ``exact`` keeps a declaration only when it renders the file
byte for byte, ``semantic`` when it renders the same directives. A unit no typed shape
renders is kept as text, and what blocked it is listed.

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
import textwrap
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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


def from_systemd(path: Path, *, mode: str = "exact") -> Converted:
    """Unit files at *path* as typed declarations (``convert --from systemd``)."""
    from ._units.parse import convert
    from .declarative._writer import declarations_source

    texts: dict[str, str] = {}
    for name, file in _unit_files(path).items():
        try:
            texts[name] = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValidationError(
                f"cannot read {file}: {error}", hint="Pass a readable UTF-8 unit file."
            ) from None
    conversions = convert(texts, mode=mode, init=True)  # type: ignore[arg-type]
    lines: list[str] = []
    unsupported: list[str] = []
    for conversion in conversions:
        units = " + ".join(conversion.units)
        if conversion.typed:
            kind = type(conversion.declaration).__name__
            moved = f"; extra= holds {', '.join(conversion.extra)}" if conversion.extra else ""
            lines.append(f"# {units}: {kind} ({mode}){moved}")
        else:
            blocked = "; ".join(conversion.blockers)
            lines.append(f"# {units}: kept as text, blocked by {blocked}")
            unsupported.append(f"{units}: {blocked}")
    source = "\n".join(lines) + "\n" + declarations_source([c.declaration for c in conversions])
    return Converted(source, unsupported)


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
