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
with content becomes an ``Image``: the payload of a tree tundravm wrote
(``mkosi.images/payload/``), else the image with ``Packages=`` (not mkosi's own
``default-initrd``), else the main one. The report after the source gives every setting
the tree sets one of four verdicts: ``kept`` (the tree the image lowers to resolves it to
the same value), ``mapped`` (a public slot carries it, named), ``defaulted`` (tundravm's
opinion replaced it, named) or ``refused`` (nothing carries it, and why). The verdicts are
measured, not assumed: the converted image is lowered and its tree resolved by mkosi too,
so ``kept`` means the same resolved value, and nothing is dropped without a line.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

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
"""The script settings and the ``Mkosi(scripts=)`` stage each becomes."""

Kind = Literal["kept", "mapped", "defaulted", "refused"]
KINDS: tuple[Kind, ...] = ("mapped", "kept", "defaulted", "refused")
"""The verdicts of the mkosi report, in the order it prints them."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """One line of the mkosi report: what became of *setting* (its *value* shown short).

    *reason* is the slot a ``mapped`` setting went to, the opinion that replaced a
    ``defaulted`` one, or why a ``refused`` one has no slot; a ``kept`` setting needs none.
    """

    kind: Kind
    setting: str
    value: str = ""
    reason: str = ""

    @property
    def line(self) -> str:
        shown = f"{self.setting}={self.value}" if self.value else self.setting
        if self.kind == "mapped":
            return f"# mapped: {shown} -> {self.reason}"
        return f"# {self.kind}: {shown}" + (f": {self.reason}" if self.reason else "")

    def as_json(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "setting": self.setting,
            "value": self.value,
            "reason": self.reason,
        }


@dataclass(slots=True)
class Converted:
    """The source a conversion printed and what it says about the input.

    ``--from mkosi`` fills *report* (one :class:`Verdict` per setting) and *notes*;
    ``--from systemd`` fills *notes* and *unsupported*.
    """

    source: str
    unsupported: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    report: list[Verdict] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "report": [verdict.as_json() for verdict in self.report],
            "notes": list(self.notes),
            "unsupported": list(self.unsupported),
        }


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
        call = (
            _packaged(service, imports) if service.package else _call(service, imports, lists=True)
        )
        entries.append(f"{call},")
    services = "".join(f"        {entry}\n" for entry in entries)
    source = (
        f'"""{name}: converted from {path.name} by `tundravm convert --from systemd`."""\n\n'
        f"{_imports(imports)}\n\n"
        f"image = Image(\n    {_quoted(name)},\n    services=[\n{services}    ],\n)\n"
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
            if current == default or _source(current, set()) == _source(default, set()):
                continue
        text = _source(current, imports, lists=lists)
        args.append(text if required and not item.kw_only else f"{item.name}={text}")
    return f"{type(value).__name__}({', '.join(args)})"


def _packaged(service: Any, imports: set[str]) -> str:
    """A ``Service.packaged`` as that call: the unit, then what differs from its defaults."""
    from .declarative.systemd import Hardening

    imports.add("Service")
    args = [_quoted(service.unit_name)]
    if service.package != service.stem:
        args.append(f"package={_quoted(service.package)}")
    if service.hardening != Hardening.preset("static"):
        args.append(f"hardening={_source(service.hardening, imports)}")
    if service.unit is not None:
        args.append(f"unit={_call(service.unit, imports)}")
    return f"Service.packaged({', '.join(args)})"


def _source(value: object, imports: set[str], *, lists: bool = False) -> str:
    from .declarative.systemd import Hardening, ceiling

    preset = ceiling(value) if isinstance(value, Hardening) else None
    if preset is not None:
        return _quoted(preset[0])
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
    return _quoted(value) if isinstance(value, str) else repr(value)


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
        return _wrapped(source)
    return done.stdout if done.returncode == 0 and done.stdout else _wrapped(source)


WIDTH = 100
"""The line length ``new`` sets for ruff, which a converted file keeps without ruff too."""


def _wrapped(source: str) -> str:
    """*source* with each code line over :data:`WIDTH` broken at its outer brackets, as
    ``ruff format`` would: where ruff is not installed (a ``uv tool install``)."""
    lines: list[str] = []
    for line in source.splitlines():
        stripped = line.lstrip()
        if len(line) <= WIDTH or stripped.startswith("#"):
            lines.append(line)
            continue
        tail = "," if stripped.endswith(",") else ""
        broken = _broken(stripped.removesuffix(tail), len(line) - len(stripped))
        broken[-1] += tail
        lines.extend(broken)
    return "\n".join(lines) + "\n"


def _broken(expr: str, indent: int) -> list[str]:
    """*expr* at *indent*: one line when it fits, else its outer call, list or dict with its
    items on one line inside the brackets, or one per line (a trailing comma after each when
    there are several) when they do not fit either."""
    pad = " " * indent
    split = None if indent + len(expr) <= WIDTH else _outer_brackets(expr)
    if split is None:
        return [pad + expr]
    head, items, close = split
    inner = ", ".join(items)
    if indent + 4 + len(inner) <= WIDTH:
        return [pad + head, f"{pad}    {inner}", pad + close]
    lines = [pad + head]
    for item in items:
        lines.extend(_broken(item, indent + 4))
        lines[-1] += "," if len(items) > 1 else ""
    return [*lines, pad + close]


def _outer_brackets(expr: str) -> tuple[str, list[str], str] | None:
    """``(head through the first outer bracket, the items, the closing bracket)`` when that
    bracket closes at the end of *expr*, else ``None``."""
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(expr).readline))
    except (tokenize.TokenError, SyntaxError):
        return None
    depth, start, cuts = 0, -1, list[int]()
    for token in tokens:
        col = token.start[1]
        if token.type != tokenize.OP or token.start[0] != 1:
            continue
        if token.string in "([{":
            start = col if depth == 0 and start < 0 else start
            depth += 1
        elif token.string in ")]}":
            depth -= 1
            if depth == 0 and start >= 0:
                if col != len(expr) - 1:
                    return None
                bounds = zip([start, *cuts], [*cuts, col], strict=True)
                items = [expr[a + 1 : b].strip() for a, b in bounds]
                return expr[: start + 1], [item for item in items if item], expr[col]
        elif token.string == "," and depth == 1 and start >= 0:
            cuts.append(col)
    return None


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


# ── mkosi: what becomes of each setting ──────────────────────────────────

PAYLOAD = "payload"
"""The subimage holding the OS in a tree tundravm wrote (``mkosi.images/payload/``)."""

GENERATED = frozenset(
    {
        "ExtraTrees",
        "SkeletonTrees",
        *SCRIPTS,
        "Bootloader",
        "BuildPackages",
        "Initrds",
        "KernelCommandLine",
        "KernelModules",
    }
)
"""What a payload tundravm wrote holds because its Image file generated it."""

DEFAULT_INITRD = "default-initrd"
"""The initrd image mkosi adds to every bootable tree that names none."""

CLOUD_WRAPPERS = ("azure", "gcp")
"""The subimages a tree tundravm wrote wraps the payload in, one per cloud target."""

_VM = "a `mkosi vm` setting; tundravm run boots the build itself"
_BOOT = "every image boots as a measured UKI, the payload itself, and installs no bootloader"
_INITRD = (
    "the UKI's initrd is the OS (RootFS.initramfs()) or mkosi's default initrd "
    "(RootFS.verity()); packages= go in the image"
)
_MEASURED = (
    "tundravm measures the UKI and predicts its registers (measurements.json); build --sign "
    "signs the manifest, and no Secure Boot or verity key signs anything"
)
_BUILD_DIR = (
    "tundravm build runs mkosi in its own build directory, with the caches it manages, and "
    "never incrementally"
)
_TOOLS = "the tools tree is the image's Debian from the locked snapshot, with mkosi's defaults"
_HOST = "nothing of the host enters the build: the lock names every source and the snapshot"
_LOGIN = (
    "a production image has no console login and no root password; dev=DevAccess(password=) "
    "adds one to the dev profile"
)
_OUTPUTS = (
    "the tree's images are payload (the UKI), qemu (its disk) and one per cloud target, "
    "named and written by tundravm"
)
_SNAPSHOT = (
    "the build reads snapshot.debian.org at the locked snapshot (Debian(snapshot=)), every "
    "archive signed and checked"
)
_ENVIRONMENT = (
    "the build environment comes from the lock; a value here would change what was measured "
    "without changing the lock"
)
_SCRIPTS = (
    "Mkosi(scripts=) runs prepare, build, postinst, finalize and postoutput scripts; sources "
    "are fetched on the host and the build directory is tundravm's to clean"
)
_BUILDS = "a Build, GoBinary or CargoBinary declares the source it reads and the packages it needs"
_DEBS = "a directory of .deb files; Debian(repositories=) adds a signed archive instead"
_SIZED = (
    "the disk is sized to its partitions: a 128 MiB ESP, and with RootFS.verity() the root "
    "and its hash"
)
_ISO = "an ISO output; tundravm builds a UKI inside a disk"
_OCI = "an OCI output; tundravm builds a UKI inside a disk"
_WHOLE = "a base tree is another image; declare the whole image here"
_TARGETS = (
    "a subimage; tundravm's tree holds the payload, its QEMU disk and one wrapper per cloud target"
)
_FIRMWARE = "firmware files have no slot; Kernel.default() keeps the curated modules"


def _group(names: str, reason: str) -> dict[str, str]:
    return dict.fromkeys(names.split(), reason)


DEFAULTED: dict[str, str] = {
    **_group("Mirror RepositoryKeyCheck RepositoryKeyFetch", _SNAPSHOT),
    **_group(
        "ManifestFormat Output OutputExtension CompressOutput CompressLevel OutputMode "
        "SplitArtifacts",
        _OUTPUTS,
    ),
    "RepartDirectories": "tundravm writes the partitions: the ESP, and with RootFS.verity() "
    "the root and its hash",
    "Seed": "tundravm fixes the seed, so the partition UUIDs reproduce",
    "OutputDirectory": _BUILD_DIR,
    "MinimumVersion": "the tree requires mkosi 27.1, which tundravm pins",
    "CleanPackageMetadata": "package manager metadata is removed from every image",
    "SourceDateEpoch": "every file is dated 0, so a build reproduces",
    **_group(
        "Bootloader BiosBootloader ShimBootloader UnifiedKernelImages UnifiedKernelImageFormat "
        "UnifiedKernelImageProfiles Splash Devicetrees MicrocodeHost MakeInitrd",
        _BOOT,
    ),
    **_group(
        "Initrds InitrdProfiles InitrdPackages InitrdVolatilePackages KernelInitrdModules "
        "KernelModulesInitrd KernelModulesInitrdInclude KernelModulesInitrdExclude "
        "KernelModulesInitrdIncludeHost",
        _INITRD,
    ),
    "KernelModulesExclude": "the curated module set stays whole; Kernel(modules=) adds to it",
    "MachineId": "/etc/machine-id is uninitialized in the image: a fresh id per boot",
    **_group("Autologin RootPassword RootShell", _LOGIN),
    "Ssh": "SshAccess (tundravm.components) opens SSH to named keys, DevAccess in the dev "
    "profile; nothing listens otherwise",
    **_group(
        "SecureBoot SecureBootAutoEnroll SecureBootKey SecureBootKeySource SecureBootCertificate "
        "SecureBootCertificateSource SecureBootSignTool VerityKey VerityKeySource "
        "VerityCertificate VerityCertificateSource SignExpectedPcr SignExpectedPcrKey "
        "SignExpectedPcrKeySource SignExpectedPcrCertificate SignExpectedPcrCertificateSource "
        "SignInitrdPCRs Passphrase Checksum Sign Key OpenPGPTool",
        _MEASURED,
    ),
    "Verity": "RootFS.initramfs() has no verity; RootFS.verity() hashes the root and measures "
    "the hash",
    **_group(
        "ToolsTree ToolsTreeDistribution ToolsTreeRelease ToolsTreeProfiles ToolsTreeMirror "
        "ToolsTreeSnapshot ToolsTreeRepositories ToolsTreeSandboxTrees ToolsTreePackages "
        "ToolsTreePackageDirectories ToolsTreeCertificates",
        _TOOLS,
    ),
    **_group(
        "Incremental CacheOnly WorkspaceDirectory CacheDirectory CacheKey PackageCacheDirectory "
        "BuildDirectory BuildKey UseSubvolumes RepartOffline History BuildSourcesEphemeral",
        _BUILD_DIR,
    ),
    "WithNetwork": "the build runs offline; a Git or Http source is fetched on the host, from "
    "the lock",
    "MakeScriptsExecutable": "tundravm writes every script executable",
    "Firmware": "the QEMU disk boots through OVMF; tundravm run picks the firmware",
}
"""Settings tundravm decides for every image: the opinion that replaces a differing value."""

REFUSED: dict[str, str] = {
    "Architecture": "tundravm builds x86-64, the one architecture TDX runs",
    "Distribution": "tundravm builds Debian only",
    "LocalMirror": _HOST,
    "Format": "tundravm builds the payload as a UKI inside a disk, no other format",
    **_group("OutputSize SectorSize", _SIZED),
    "ImageVersion": "the build id is the manifest's digest; an Image has no version",
    **_group("OciLabels OciAnnotations", _OCI),
    **_group("Overlay BaseTrees", _WHOLE),
    **_group("ElTorito ElToritoSystem ElToritoVolume ElToritoPublisher", _ISO),
    **_group("CleanScripts SyncScripts", _SCRIPTS),
    "ConfigureScripts": "a configure script rewrites the configuration after `mkosi summary` "
    "resolved it, so the build would not be what the manifest records",
    **_group("BuildPackages BuildSources", _BUILDS),
    **_group("PackageDirectories VolatilePackageDirectories", _DEBS),
    "Bootable": "a non-bootable image; every image tundravm builds boots as a UKI",
    "KernelModulesIncludeHost": _HOST,
    **_group("FirmwareInclude FirmwareExclude FirmwareFiles", _FIRMWARE),
    "Dependencies": _TARGETS,
    **_group("Environment EnvironmentFiles", _ENVIRONMENT),
    **_group(
        "PassEnvironment ExtraSearchPaths ProxyUrl ProxyExclude ProxyPeerCertificate "
        "ProxyClientCertificate ProxyClientKey ForeignUIDRange DelegateRanges",
        _HOST,
    ),
    **_group(
        "NSpawnSettings Ephemeral RuntimeTrees RuntimeSize RuntimeNetwork RuntimeBuildSources "
        "BindUser VirtualMachineMonitor ForwardJournal SysupdateDirectory Console MaxMem KVM CXL "
        "VSock VSockCID TPM Removable DiskType FirmwareVariables Linux Drives QemuArgs Register "
        "StorageTargetMode",
        _VM,
    ),
    "RAM": f"{_VM} (run --memory)",
    "CPUs": f"{_VM} (run --cpus)",
    "Credentials": "secrets= are delivered after attestation; nothing is passed at boot",
    **_group("SshKey SshCertificate", "run ssh takes --identity; SshAccess names the keys"),
    "Machine": "tundravm run names instances <image>-<n>",
    "KernelCommandLineExtra": "Kernel(cmdline=) measures its parameters; nothing is added at boot",
}
"""Settings no slot carries: why, and what to do instead."""

VERBATIM = frozenset(
    "WithRecommends WithDocs RemovePackages RemoveFiles Locale LocaleMessages Keymap Timezone "
    "Hostname SELinuxRelabel WithTests".split()
)
"""Content settings the image carries as they are, through ``Mkosi(settings=)``."""

MAPPED = frozenset(
    {
        *SCRIPTS,
        *(
            "Include Distribution Release Snapshot Repositories ImageId Packages VolatilePackages "
            "ExtraTrees SkeletonTrees Verity SandboxTrees KernelCommandLine KernelModules "
            "KernelModulesInclude Profiles"
        ).split(),
    }
)
"""Settings the converter turns into a public slot (some only for some values)."""

DERIVED = frozenset(
    {
        "Files",
        "Image",
        "Machine",
        "Output",
        "OutputExtension",
        "Dependencies",
        "Initrds",
        "BuildSubdirectory",
    }
)
"""Resolved values mkosi derives from the tree's place or shape, reported only when written."""

_MAIN_IMAGE = "main"
_CONTENT_DIRECTORIES = frozenset(
    {
        "mkosi.extra",
        "mkosi.extra.common",
        "mkosi.skeleton",
        "mkosi.sandbox",
        "mkosi.repart",
        "mkosi.tools.conf",
        "mkosi.output",
        "mkosi.cache",
        "mkosi.builddir",
        "mkosi.workspace",
        "mkosi.tools",
        "mkosi.pkgmngr",
    }
)
"""Directories of a tree that hold no configuration files."""
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
UNIT_DIRECTORIES = ("usr/lib/systemd/system", "etc/systemd/system")
"""Where an extra tree's unit files are, which become ``Service`` values."""
_SOURCES = "etc/apt/sources.list.d"
_DATED = "https://snapshot.debian.org/archive/"
_DEBIAN_ARCHIVE = "https://snapshot.debian.org/archive/debian"
_DEBIAN_KEYRING = "/usr/share/keyrings/debian-archive-keyring.gpg"
_SHOWN_ITEMS = 6


def executable_inputs(tree: Path) -> list[Path]:
    """The files mkosi would execute while parsing *tree*."""
    found = []
    for name in EXECUTABLE_INPUTS:
        for path in sorted(tree.rglob(name)):
            if path.is_file() and os.access(path, os.X_OK):
                found.append(path)
    return found


@dataclass(slots=True)
class _Tree:
    """An mkosi tree as mkosi resolved it: the content image, the profiles, the files read."""

    root: Path
    main: dict[str, Any]
    content: dict[str, Any]
    others: list[tuple[str, str]]
    written: dict[str, list[str]]
    """Each canonical setting written in a conf file of the tree, by the files writing it."""
    unknown: dict[str, list[str]]
    """Each key no mkosi 27.1 setting answers to, by the files writing it."""
    includes: list[tuple[str, str]]
    """``(file, value)`` of each ``Include=``."""
    matches: dict[str, list[str]]
    """The ``[Match]`` lines of each conf file."""
    read: set[str]
    """Every conf file of the tree mkosi read, for the default selection or a profile."""
    profiles: dict[str, dict[str, Any]]
    """Each profile directory's content image, resolved with that profile selected."""
    distribution_defaulted: bool = False
    """Whether the tree names no distribution, so mkosi would build the host's."""

    @property
    def tundravm(self) -> bool:
        """Whether tundravm wrote the tree (its OS is the ``payload`` subimage)."""
        return (self.root / "mkosi.images" / PAYLOAD).is_dir()


def _ini(text: str) -> Iterator[tuple[str, str, str]]:
    """``(section, key, value)`` of each setting *text* writes, as mkosi reads an INI file:
    ``#`` starts a comment anywhere, an indented line continues the value above it."""
    section = key = ""
    value: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0]
        if not line.strip():
            continue
        if section and key and value is not None and line[0].isspace():
            value = f"{value}\n{line.strip()}"
            continue
        if section and key and value is not None:
            yield section, key, value
            key, value = "", None
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        key, delimiter, value = line.partition("=")
        if not section or not delimiter:
            key, value = "", None
            continue
        key, value = key.strip().lstrip("@"), value.strip()
    if section and key and value is not None:
        yield section, key, value


def _conf_files(root: Path) -> list[Path]:
    """Every configuration file of the tree, in the order mkosi reads them."""
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if _CONTENT_DIRECTORIES.intersection(parts):
            continue
        if path.name == "mkosi.conf" or (
            path.suffix == ".conf" and path.parent.name == "mkosi.conf.d"
        ):
            found.append(path)
    return found


def _resolve(root: Path, allow_scripts: bool) -> _Tree:
    """*root* as mkosi resolves it, plus what its files write."""
    from . import _mkosi

    parsed: dict[Path, list[tuple[str, str, str]]] = {}
    matches: dict[str, list[str]] = {}
    for file in _conf_files(root):
        shown = file.relative_to(root).as_posix()
        parsed[file] = []
        for section, key, value in _ini(file.read_text(encoding="utf-8", errors="replace")):
            if section in ("Match", "TriggerMatch"):
                matches.setdefault(shown, []).append(f"{key}={value}".replace("\n", " "))
            else:
                parsed[file].append((section, key, value))
    argv: list[str] = []
    summary = _mkosi.summary(root, extra_argv=argv)
    distribution_defaulted = False
    names_distribution = any(
        key == "Distribution" and str(file) in _files(summary.images)
        for file, items in parsed.items()
        for _, key, _ in items
    )
    if summary.main.get("Distribution") != "debian" and not names_distribution:
        argv = ["--distribution=debian"]
        summary = _mkosi.summary(root, extra_argv=argv)
        distribution_defaulted = True
    images = list(summary.images)
    tundravm = (root / "mkosi.images" / PAYLOAD).is_dir()
    content = _content(images, summary.main, tundravm=tundravm)
    others = [
        (i.get("Image") or _MAIN_IMAGE, str(i.get("Format"))) for i in images if i is not content
    ]
    if tundravm:
        # A cloud wrapper no profile selects is in the tree but not in mkosi's summary.
        listed = {name for name, _ in others} | {content.get("Image")}
        found = sorted(d.name for d in (root / "mkosi.images").iterdir() if d.is_dir())
        others += [(name, "disk") for name in found if name not in listed]
    read = _files(images)
    read_default = set(read)
    profiles: dict[str, dict[str, Any]] = {}
    selected = set(summary.main.get("Profiles") or ())
    for directory in sorted((root / "mkosi.profiles").glob("*/")) if not tundravm else ():
        name = directory.name
        if name in selected or not _NAME.fullmatch(name):
            continue
        chosen = _mkosi.summary(root, extra_argv=[*argv, f"--profile={name}"])
        read.update(_files(chosen.images))
        profiles[name] = _content(list(chosen.images), chosen.main, tundravm=False)
    written: dict[str, list[str]] = {}
    unknown: dict[str, list[str]] = {}
    includes: list[tuple[str, str]] = []
    for file, items in parsed.items():
        if str(file) not in read_default:
            continue
        shown = file.relative_to(root).as_posix()
        for section, key, value in items:
            if section == "Include" and key == "Include":
                includes.extend((shown, item) for item in value.split())
                continue
            setting = _mkosi.setting(key)
            if setting is None:
                unknown.setdefault(key, []).append(shown)
            else:
                written.setdefault(setting.name, []).append(shown)
    return _Tree(
        root=root,
        main=summary.main,
        content=content,
        others=others,
        written=written,
        unknown=unknown,
        includes=includes,
        matches=matches,
        read=read,
        profiles=profiles,
        distribution_defaulted=distribution_defaulted,
    )


def _files(images: Sequence[Mapping[str, Any]]) -> set[str]:
    """Every configuration file mkosi read for *images*."""
    return {str(path) for image in images for path in image.get("Files") or ()}


def _content(
    images: list[dict[str, Any]], main: dict[str, Any], *, tundravm: bool
) -> dict[str, Any]:
    """The image with the OS: the payload of a tree tundravm wrote, else the image with
    packages (not mkosi's own initrd), else the main one."""
    own = [i for i in images if i.get("Image") != DEFAULT_INITRD]
    if tundravm:
        return next((i for i in own if i.get("Image") == PAYLOAD), main)
    return next((i for i in own if i.get("Packages")), main)


def from_mkosi(tree: Path, *, allow_scripts: bool = False) -> Converted:
    """The mkosi tree at *tree* as an ``Image`` file, with the report of every setting."""
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
    resolved = _resolve(root, allow_scripts)
    writer = _Writer(resolved)
    source = writer.source()
    report = writer.report
    notes = writer.notes
    lowered = _lowered(source, root)
    report.extend(_classified(resolved, writer.handled, lowered))
    report.extend(_unread(resolved))
    report.sort(key=lambda verdict: KINDS.index(verdict.kind))
    if scripts:
        notes.append(f"ran while parsing: {', '.join(p.name for p in scripts)}")
    return Converted(_formatted(source), notes=notes, report=report)


@functools.cache
def _defaults() -> dict[str, Any]:
    """Every setting's value in an empty Debian tree (what mkosi chooses when nothing is
    said), with the tree's own path as ``<tree>``."""
    from . import _mkosi

    with tempfile.TemporaryDirectory(prefix="tundravm-convert-") as scratch:
        (Path(scratch) / "mkosi.conf").write_text("[Distribution]\nDistribution=debian\n")
        main = _mkosi.summary(Path(scratch)).main
        return {key: _normal(value, Path(scratch)) for key, value in main.items()}


def _normal(value: Any, root: Path) -> Any:
    """*value* with *root* spelt ``<tree>`` (and its parent ``<parent>``), so two trees'
    resolved values compare."""
    if isinstance(value, str):
        value = value.replace(str(root), "<tree>")
        return value.replace(str(root.parent), "<parent>") if root.parent != root else value
    if isinstance(value, list):
        return [_normal(item, root) for item in value]
    if isinstance(value, dict):
        return {key: _normal(item, root) for key, item in value.items()}
    return value


def _lowered(source: str, root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The payload and main image of the tree *source* lowers to, as mkosi resolves them,
    normalised: the evidence a ``kept`` verdict rests on."""
    from . import _mkosi
    from ._image import compile as compile_image

    namespace: dict[str, Any] = {"__file__": str(root / "image.py"), "__name__": "image"}
    exec(source, namespace)
    tree = compile_image(namespace["image"])
    with tempfile.TemporaryDirectory(prefix="tundravm-convert-") as scratch:
        tree.write(Path(scratch))
        summary = _mkosi.summary(Path(scratch))
        payload = next(i for i in summary.images if i.get("Image") == PAYLOAD)
        return _normal(payload, Path(scratch)), _normal(summary.main, Path(scratch))


def _classified(
    resolved: _Tree, handled: set[str], lowered: tuple[dict[str, Any], dict[str, Any]]
) -> list[Verdict]:
    """A verdict for every setting the tree sets that the writer did not map: ``kept`` when
    the lowered tree resolves it to the same value, else the table's opinion or refusal."""
    from .compiler.emit_mkosi import MAIN_IMAGE_SETTINGS

    defaults = _defaults()
    payload, main = lowered
    root = resolved.root
    config = {
        key: _normal(resolved.main[key] if key in MAIN_IMAGE_SETTINGS else value, root)
        for key, value in resolved.content.items()
    }
    verdicts: list[Verdict] = []
    for key in sorted(config):
        value = config[key]
        if key in handled or key in DERIVED and key not in resolved.written:
            continue
        if value == defaults.get(key) and (key not in resolved.written or not value):
            continue
        shown = "(set)" if key in _SECRETS else _shown(value)
        ours = main.get(key) if key in MAIN_IMAGE_SETTINGS else payload.get(key)
        built = _BUILT.get((key, str(value)))
        if value == ours or built is not None:
            verdicts.append(Verdict("kept", key, shown, built or DEFAULTED.get(key, "")))
        elif key in DEFAULTED:
            verdicts.append(Verdict("defaulted", key, shown, DEFAULTED[key]))
        elif key in REFUSED:
            verdicts.append(Verdict("refused", key, shown, REFUSED[key]))
        else:
            verdicts.append(Verdict("refused", key, shown, "no Image slot holds it"))
    for key, files in sorted(resolved.unknown.items()):
        verdicts.append(
            Verdict(
                "refused",
                key,
                "",
                f"mkosi 27.1 knows no such setting ({', '.join(files)})",
            )
        )
    return verdicts


_SECRETS = frozenset({"RootPassword", "Passphrase"})
"""Settings whose value the report never prints."""

_BUILT: dict[tuple[str, str], str] = {
    ("Format", "disk"): "the main image is the disk around the UKI payload",
    ("Format", "uki"): "the payload is the UKI",
    ("Bootable", "enabled"): "the payload is a UKI, bootable by construction",
    ("Bootable", "auto"): "the payload is a UKI, bootable by construction",
}
"""Values the lowered tree carries in another image than the one compared, so they are
kept, with the image that carries them."""


def _unread(resolved: _Tree) -> list[Verdict]:
    """A ``refused`` line per conf file mkosi read for no selection, with its ``[Match]``."""
    verdicts: list[Verdict] = []
    generated = (
        [f"mkosi.images/{cloud}" for cloud in CLOUD_WRAPPERS] + [f"{PAYLOAD}/mkosi.profiles"]
        if resolved.tundravm
        else []
    )
    for file in _conf_files(resolved.root):
        if str(file) in resolved.read:
            continue
        shown = file.relative_to(resolved.root).as_posix()
        if any(shown.startswith(prefix + "/") for prefix in generated):
            continue
        match = " ".join(resolved.matches.get(shown, ())) or "no [Match]"
        verdicts.append(Verdict("refused", shown, "", f"mkosi did not read it ({match})"))
    for directory in sorted((resolved.root / PAYLOAD / "mkosi.profiles").glob("*/")):
        verdicts.append(
            Verdict(
                "refused",
                f"{PAYLOAD}/mkosi.profiles/{directory.name}",
                "",
                "generated by the Image file that wrote this tree (its profiles=); convert "
                "that file's image, not its tree",
            )
        )
    return verdicts


def _shown(value: Any) -> str:
    """*value* short enough for a report line."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        if set(value) == {"Source", "Target"}:
            source = _shown(value["Source"])
            return f"{source}:{value['Target']}" if value["Target"] else source
        if "Name" in value:
            return f"{value['Name']}={_shown(value.get('Value') or value.get('Path'))}"
        return " ".join(f"{k}={_shown(v)}" for k, v in value.items())
    if isinstance(value, list):
        items = [_shown(item) for item in value]
        if len(items) > _SHOWN_ITEMS:
            items = [*items[:_SHOWN_ITEMS], f"... (+{len(items) - _SHOWN_ITEMS})"]
        return " ".join(items)
    text = str(value).replace("<tree>/", "").replace("<tree>", ".")
    return text.replace("<parent>/", "../")


def _verbatim(value: Any) -> str | None:
    """*value* as the one line ``Mkosi(settings=)`` takes; ``None`` when it has none."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        if any(not isinstance(item, str) or not item or item.split() != [item] for item in value):
            return None
        return " ".join(value)
    if isinstance(value, int | float):
        return str(value)
    return value if isinstance(value, str) and "\n" not in value else None


class _Writer:
    """Writes the ``Image`` source of a resolved tree and the ``mapped`` verdicts."""

    def __init__(self, resolved: _Tree) -> None:
        self.resolved = resolved
        self.root = resolved.root
        self.config = resolved.content
        self.written = resolved.written
        self.report: list[Verdict] = []
        self.notes: list[str] = []
        self.handled: set[str] = set()
        """Settings the source carries (mapped) or refused, so classification skips them."""
        self.imports: set[str] = {"Image"}
        self.services: list[str] = []
        self.here = False

    def mapped(self, setting: str, value: Any, slot: str) -> None:
        self.handled.add(setting)
        self.report.append(Verdict("mapped", setting, _shown(_normal(value, self.root)), slot))

    def refused(self, setting: str, value: Any, reason: str) -> None:
        self.handled.add(setting)
        self.report.append(Verdict("refused", setting, _shown(_normal(value, self.root)), reason))

    def relative(self, path: Path) -> str:
        self.here = True
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def source(self) -> str:
        config = self.config
        resolved = self.resolved
        name = str(resolved.main.get("ImageId") or self.root.name)
        if resolved.main.get("ImageId"):
            self.mapped("ImageId", name, f"Image({_quoted(name)})")
        args: list[str] = [_quoted(name)]
        for file, value in resolved.includes:
            self.report.append(
                Verdict(
                    "kept",
                    "Include",
                    value,
                    f"resolved by mkosi (from {file}); its settings are listed as this tree's",
                )
            )
        self.handled.add("Include")
        base = self.base()
        if base:
            args.append(f"base={base}")
        verity = str(config.get("Verity") or "") == "hash"
        if verity:
            self.imports.add("RootFS")
            self.mapped("Verity", "hash", "rootfs=RootFS.verity()")
            args.append("rootfs=RootFS.verity()")
        elif str(config.get("Verity") or "") in ("signed", "defer"):
            self.refused(
                "Verity",
                config["Verity"],
                "a signed or deferred verity root; RootFS.verity() puts the root hash on the "
                "measured command line",
            )
        wrappers = [cloud for cloud in CLOUD_WRAPPERS if cloud in {n for n, _ in resolved.others}]
        targets = ("qemu", *wrappers) if resolved.tundravm and wrappers else ()
        packages = self.packages(verity=verity, targets=targets)
        if packages:
            args.append("packages=[" + ", ".join(_quoted(p) for p in packages) + "]")
        files = self.files()
        if files:
            args.append("files=[" + ", ".join(files) + "]")
        kernel = self.kernel()
        if kernel:
            args.append(f"kernel={kernel}")
        if targets:
            args.append(f"targets=({', '.join(_quoted(target) for target in targets)})")
        profiles = self.profiles()
        if profiles:
            args.append("profiles={" + ", ".join(profiles) + "}")
        mkosi = self.mkosi()
        if mkosi:
            args.append(f"mkosi={mkosi}")
        self.subimages()
        if self.services:
            args.append("services=[" + ", ".join(self.services) + "]")
        header = "from pathlib import Path\n\n" if self.here else ""
        return (
            f'"""{name}: converted from {self.root.name} by `tundravm convert --from mkosi`."""'
            f"\n\n{header}{_imports(self.imports)}\n\n"
            + ("HERE = Path(__file__).parent\n\n" if self.here else "")
            + f"image = Image({', '.join(args)})\n"
        )

    def base(self) -> str:
        """``base=Debian(...)``: the release, a snapshot, the extra components as a
        repository, and the signed, dated apt sources of a sandbox tree."""
        config, written = self.config, self.written
        distribution = config.get("Distribution")
        if self.resolved.distribution_defaulted:
            self.report.append(
                Verdict(
                    "defaulted",
                    "Distribution",
                    "",
                    "the tree names none, so mkosi would build the host's; tundravm builds "
                    "Debian, and the tree was resolved for it",
                )
            )
            self.handled.add("Distribution")
        elif distribution == "debian":
            self.mapped("Distribution", distribution, "base=Debian(...)")
        else:
            self.refused("Distribution", distribution, REFUSED["Distribution"])
            return ""
        parts: list[str] = []
        release = str(config.get("Release") or "")
        if "Release" in written:
            self.mapped("Release", release, f"base=Debian({_quoted(release)})")
        else:
            self.report.append(
                Verdict(
                    "defaulted",
                    "Release",
                    "",
                    f"the tree names none, so mkosi would build {release}; tundravm builds "
                    "trixie, the tested baseline",
                )
            )
            self.handled.add("Release")
            release = "trixie"
        parts.append(_quoted(release))
        snapshot = config.get("Snapshot")
        if snapshot:
            self.mapped("Snapshot", snapshot, f"Debian(snapshot={_quoted(str(snapshot))})")
            parts.append(f"snapshot={_quoted(str(snapshot))}")
        repositories: list[str] = []
        components = [str(c) for c in config.get("Repositories") or ()]
        if components:
            call = (
                f"AptRepository({_quoted(_DEBIAN_ARCHIVE)}, {_quoted(release)}, "
                f"[{', '.join(_quoted(c) for c in dict.fromkeys(components))}], "
                f"key={_quoted(_DEBIAN_KEYRING)})"
            )
            self.mapped(
                "Repositories",
                components,
                f"Debian(repositories=[{call}]) (the {release} suite; updates and security "
                "keep main)",
            )
            repositories.append(call)
        repositories.extend(self.sandbox_sources())
        if repositories:
            self.imports.add("AptRepository")
            parts.append("repositories=[" + ", ".join(repositories) + "]")
        self.imports.add("Debian")
        return f"Debian({', '.join(parts)})"

    def sandbox_sources(self) -> list[str]:
        """Each signed, dated deb822 source of a ``SandboxTrees=`` tree as an
        ``AptRepository`` call; every other source, and any other file, is refused."""
        from ._image._values import PACKAGE_KEYRINGS

        found: list[str] = []
        for entry in self.config.get("SandboxTrees") or ():
            tree = Path(str(entry.get("Source")))
            shown = self.relative(tree)
            if str(entry.get("Target") or "/") != "/" or not tree.is_dir():
                self.refused("SandboxTrees", shown, "not a tree at /")
                continue
            used: set[Path] = set()
            for file in sorted((tree / _SOURCES).glob("*.sources")):
                used.add(file)
                for stanza in file.read_text(encoding="utf-8").split("\n\n"):
                    fields_ = dict(
                        (key.strip(), value.strip())
                        for key, _, value in (line.partition(":") for line in stanza.splitlines())
                        if value and not key.startswith((" ", "#"))
                    )
                    if not fields_:
                        continue
                    where = f"{self.relative(file)}: {fields_.get('URIs', '?')}"
                    url, suite = fields_.get("URIs", ""), fields_.get("Suites", "")
                    signed = fields_.get("Signed-By", "")
                    keyring = tree / signed.lstrip("/")
                    if not signed or fields_.get("Trusted", "").lower() == "yes":
                        self.refused(
                            "SandboxTrees", where, "unsigned, and an AptRepository takes key="
                        )
                        continue
                    if signed.startswith(PACKAGE_KEYRINGS):
                        key = _quoted(signed)
                    elif keyring.is_file():
                        used.add(keyring)
                        key = f"HERE / {_quoted(self.relative(keyring))}"
                    else:
                        self.refused(
                            "SandboxTrees", where, f"Signed-By={signed} is not in the tree"
                        )
                        continue
                    if not url.startswith(_DATED) or url.count("/") < 6:
                        self.refused(
                            "SandboxTrees",
                            where,
                            "not dated; AptRepository(..., sha256=) takes the digest `curl -fsSL "
                            f"{url.rstrip('/')}/dists/{suite}/InRelease | sha256sum` prints",
                        )
                        continue
                    components = fields_.get("Components", "main").split()
                    parts = [_quoted(url.rstrip("/")), _quoted(suite)]
                    if components != ["main"]:
                        parts.append(f"[{', '.join(map(_quoted, components))}]")
                    call = f"AptRepository({', '.join(parts)}, key={key})"
                    self.mapped("SandboxTrees", where, f"Debian(repositories=[{call}])")
                    found.append(call)
            for file in sorted(path for path in tree.rglob("*") if path.is_file()):
                if file not in used:
                    self.refused(
                        "SandboxTrees",
                        f"{shown}/{file.relative_to(tree).as_posix()}",
                        "only apt sources and their keyrings have a slot (Debian(repositories=))",
                    )
        return found

    def packages(self, *, verity: bool, targets: tuple[str, ...]) -> list[str]:
        """``packages=``: ``Packages=`` and ``VolatilePackages=`` less what every image
        installs anyway."""
        config = self.config
        release = str(config.get("Release") or "") if "Release" in self.written else None
        implied = implied_packages(release, verity=verity, targets=targets)
        listed = [str(p) for p in config.get("Packages") or ()]
        volatile = [str(p) for p in config.get("VolatilePackages") or ()]
        if volatile:
            self.mapped("VolatilePackages", volatile, "packages= (installed like Packages=)")
        packages = sorted(p for p in {*listed, *volatile} if p not in implied)
        dropped = sorted(set(listed) & implied)
        if listed:
            slot = "packages="
            if dropped:
                slot += f" (every image installs {', '.join(dropped)}: left out)"
            self.mapped("Packages", listed, slot)
        return packages

    def files(self) -> list[str]:
        """``files=``: a ``Tree`` per extra or skeleton tree, a ``File.copy`` per file."""
        if self.resolved.tundravm:
            for key in sorted(GENERATED):
                if self.config.get(key) != _defaults().get(key):
                    self.refused(key, self.config.get(key), self.generated())
            return []
        found: list[str] = []
        targets: dict[str, str] = {}
        for key in ("ExtraTrees", "SkeletonTrees"):
            for entry in self.config.get(key) or ():
                source = Path(str(entry.get("Source")))
                target = str(entry.get("Target") or "/")
                shown = f"{self.relative(source)}:{target}"
                if not source.exists():
                    self.refused(key, shown, "not in the tree (an output of another image?)")
                    continue
                if source.is_file():
                    call = f"File.copy(HERE / {_quoted(self.relative(source))}, {_quoted(target)})"
                    self.imports.add("File")
                    self.mapped(key, shown, f"files=[{call}]")
                    found.append(call)
                    continue
                if target in targets:
                    self.refused(
                        key,
                        shown,
                        f"a second tree at {target} ({targets[target]} is the first); an Image "
                        "holds one Tree per target: merge them",
                    )
                    continue
                targets[target] = shown
                excluded = self.units(key, source, target)
                exclude = (
                    ", exclude=[" + ", ".join(_quoted(path) for path in excluded) + "]"
                    if excluded
                    else ""
                )
                call = f"Tree(HERE / {_quoted(self.relative(source))}, {_quoted(target)}{exclude})"
                self.imports.add("Tree")
                caveat = (
                    " (installed after the packages, as an extra tree)"
                    if key == "SkeletonTrees"
                    else ""
                )
                self.mapped(key, shown, f"files=[{call}]{caveat}")
                found.append(call)
        return found

    def units(self, key: str, source: Path, target: str) -> list[str]:
        """The unit files under *source*'s systemd directories as ``Service`` values
        (:mod:`tundravm._units.image`, as ``convert --from systemd`` writes them), and the
        paths to leave out of the ``Tree``: each unit converted, and the links enabling it."""
        from ._units.image import image_source

        if target != "/":
            return []
        texts: dict[str, Path] = {}
        for directory in UNIT_DIRECTORIES:
            base = source / directory
            for item in sorted(base.rglob("*")) if base.is_dir() else ():
                parts = item.relative_to(base).parts
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix in UNIT_SUFFIXES and len(parts) == 1:
                    texts[item.name] = item
                elif item.suffix == ".conf" and len(parts) == 2 and parts[0].endswith(".d"):
                    texts[f"{parts[0]}/{item.name}"] = item
        if not texts:
            return []
        found = image_source(
            {
                name: path.read_text(encoding="utf-8", errors="replace")
                for name, path in texts.items()
            }
        )
        converted: set[str] = set()
        for units, service, _ in found.services:
            names = units.split(" + ")
            converted.update(names)
            call = (
                _packaged(service, self.imports)
                if service.package
                else _call(service, self.imports, lists=True)
            )
            self.services.append(call)
            shown = " + ".join(self.relative(texts[name]) for name in names if name in texts)
            self.mapped(
                key, shown, f"services=[{call.split('(', 1)[0]}(...)] (left out of the Tree)"
            )
        for note in found.notes:
            self.notes.append(f"{_shown(self.relative(source))}: {note}")
        for why in found.unsupported:
            self.report.append(Verdict("refused", key, _shown(self.relative(source)), why))
        excluded = [texts[name].relative_to(source).as_posix() for name in sorted(converted)]
        for directory in UNIT_DIRECTORIES:
            base = source / directory
            for item in sorted(base.rglob("*")) if base.is_dir() else ():
                if item.is_symlink() and item.name in converted:
                    excluded.append(item.relative_to(source).as_posix())
        return excluded

    def generated(self) -> str:
        return (
            "generated by the Image file that wrote this tree (its services, components, "
            "files, kernel and builds); convert that file's image, not its tree"
        )

    def kernel(self) -> str:
        """``kernel=Kernel.default(...)``: plain module names and the command line parameters
        the image does not own."""
        from ._image import Kernel

        config = self.config
        if self.resolved.tundravm:
            return ""
        modules: list[str] = []
        for key in ("KernelModules", "KernelModulesInclude"):
            for item in config.get(key) or ():
                name = str(item)
                if name in ("default", "host") or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    self.refused(key, name, "a glob; Kernel(modules=) takes module names")
                elif name not in modules:
                    modules.append(name)
            if modules and config.get(key):
                self.mapped(
                    key,
                    config[key],
                    "kernel=Kernel.default(modules=[...]) (added to the curated set)",
                )
        cmdline: list[str] = []
        for item in config.get("KernelCommandLine") or ():
            parameter = str(item)
            try:
                Kernel.default(cmdline=[parameter])
            except ValidationError as error:
                self.refused(
                    "KernelCommandLine", parameter, f"{error.args[0]} {error.hint or ''}".strip()
                )
                continue
            cmdline.append(parameter)
        if cmdline:
            self.mapped("KernelCommandLine", cmdline, "kernel=Kernel.default(cmdline=[...])")
        if not modules and not cmdline:
            return ""
        self.imports.add("Kernel")
        parts: list[str] = []
        if modules:
            parts.append("modules=[" + ", ".join(_quoted(m) for m in modules) + "]")
        if cmdline:
            parts.append("cmdline=[" + ", ".join(_quoted(p) for p in cmdline) + "]")
        return f"Kernel.default({', '.join(parts)})"

    def profiles(self) -> list[str]:
        """``profiles=``: a ``Profile`` per ``mkosi.profiles/`` directory, adding the packages
        and trees it adds to the image; what else it changes is refused."""
        selected = [str(p) for p in self.resolved.main.get("Profiles") or ()]
        if selected:
            self.mapped("Profiles", selected, "the image itself (selected for every build)")
        found: list[str] = []
        base = {k: _normal(v, self.root) for k, v in self.config.items()}
        for name, config in self.resolved.profiles.items():
            setting = f"mkosi.profiles/{name}"
            adds: list[str] = []
            for key, value in sorted(config.items()):
                value = _normal(value, self.root)
                if value == base.get(key) or key in DERIVED or key == "Profiles":
                    continue
                if key in ("Packages", "VolatilePackages"):
                    removed = [p for p in base.get(key) or () if p not in value]
                    if removed:
                        self.report.append(
                            Verdict(
                                "refused",
                                f"{setting}: {key}",
                                _shown(removed),
                                "a Profile adds packages; service.ref.remove() drops a service",
                            )
                        )
                    adds.extend(_quoted(str(p)) for p in value if p not in (base.get(key) or ()))
                    continue
                if key in ("ExtraTrees", "SkeletonTrees"):
                    for entry in value:
                        if entry in (base.get(key) or ()):
                            continue
                        source = self.root / str(entry["Source"]).replace("<tree>/", "")
                        target = str(entry.get("Target") or "/")
                        if source.is_dir():
                            self.imports.add("Tree")
                            adds.append(
                                f"Tree(HERE / {_quoted(self.relative(source))}, {_quoted(target)})"
                            )
                        else:
                            self.report.append(
                                Verdict(
                                    "refused",
                                    f"{setting}: {key}",
                                    _shown(entry),
                                    "not a directory of the tree",
                                )
                            )
                    continue
                self.report.append(
                    Verdict(
                        "refused",
                        f"{setting}: {key}",
                        _shown(value),
                        "a Profile adds packages, files and services and edits services; "
                        f"{key} is the whole image's",
                    )
                )
            self.imports.add("Profile")
            call = "Profile(add=[" + ", ".join(adds) + "])" if adds else "Profile()"
            self.mapped(setting, "", f"profiles={{{_quoted(name)}: {call}}}")
            found.append(f"{_quoted(name)}: {call}")
        self.handled.add("Profiles")
        return found

    def mkosi(self) -> str:
        """``mkosi=Mkosi(settings=, scripts=)``: the verbatim settings and the scripts."""
        config = self.config
        settings: dict[str, str] = {}
        defaults = _defaults()
        for key in sorted(VERBATIM):
            value = config.get(key)
            if key not in config or _normal(value, self.root) == defaults.get(key):
                continue
            text = _verbatim(value)
            if text is None:
                self.refused(key, value, "not one line of text, which Mkosi(settings=) takes")
                continue
            settings[key] = text
            self.mapped(key, value, f"mkosi=Mkosi(settings={{{_quoted(key)}: {_quoted(text)}}})")
        scripts: list[str] = []
        for key, stage in SCRIPTS.items():
            for item in config.get(key) or ():
                path = Path(str(item))
                if self.resolved.tundravm:
                    continue
                if not path.is_file():
                    self.refused(key, self.relative(path), "not a file of the tree")
                    continue
                relative = _quoted(self.relative(path))
                scripts.append(f"({_quoted(stage)}, (HERE / {relative}).read_text())")
                self.mapped(
                    key, self.relative(path), f"mkosi=Mkosi(scripts=[({_quoted(stage)}, ...)])"
                )
        if not settings and not scripts:
            return ""
        self.imports.add("Mkosi")
        parts: list[str] = []
        if settings:
            pairs = ", ".join(f"{_quoted(k)}: {_quoted(v)}" for k, v in settings.items())
            parts.append(f"settings={{{pairs}}}")
        if scripts:
            parts.append("scripts=[" + ", ".join(scripts) + "]")
        return f"Mkosi({', '.join(parts)})"

    def subimages(self) -> None:
        """A note per image of tundravm's own layout, a refusal per other subimage."""
        for name, fmt in self.resolved.others:
            if name == _MAIN_IMAGE and fmt == "disk" and self.resolved.tundravm:
                self.notes.append(
                    "main image (disk): the QEMU disk tundravm builds for every image"
                )
            elif name == DEFAULT_INITRD:
                self.notes.append(f"{name}: mkosi's own initrd, which tundravm's UKI uses too")
            elif name in CLOUD_WRAPPERS and self.resolved.tundravm:
                self.notes.append(
                    f"subimage {name}: the {name} disk tundravm wraps the payload in, "
                    f"written as the {name!r} target"
                )
            else:
                self.report.append(
                    Verdict("refused", f"mkosi.images/{name}", f"Format={fmt}", _TARGETS)
                )


def implied_packages(
    release: str | None, *, verity: bool = False, targets: Sequence[str] = ()
) -> frozenset[str]:
    """The packages the compiler installs in an image of *release* (default: tundravm's)
    that lists none: the platform, the TEE baseline, the firewall and the glue of
    *targets*, plus the account tools every service's user needs."""
    from ._image import Debian, Image, RootFS, lower
    from .compiler.emit_mkosi import ACCOUNT_TOOLS_PACKAGE

    image = Image(
        "implied",
        base=Debian() if release is None else Debian(release),
        rootfs=RootFS.verity() if verity else RootFS.initramfs(),
        targets=tuple(targets) or ("qemu",),  # type: ignore[arg-type]
    )
    lowered = lower(image)
    profile = lowered.state.profiles[lowered.default_profile]
    return frozenset({*profile.packages, ACCOUNT_TOOLS_PACKAGE})


def _quoted(text: str) -> str:
    """*text* as a Python string literal in double quotes, as ``ruff format`` writes it."""
    return json.dumps(text, ensure_ascii=False)


def render(converted: Converted) -> Iterable[str]:
    """The lines ``convert`` prints after the source: the mkosi report, notes and what the
    systemd conversion could not carry."""
    for verdict in converted.report:
        yield verdict.line
    for note in converted.notes:
        yield f"# note: {note}"
    for item in converted.unsupported:
        yield f"# unsupported: {item}"


__all__ = [
    "DEFAULTED",
    "DERIVED",
    "EXECUTABLE_INPUTS",
    "KINDS",
    "MAPPED",
    "REFUSED",
    "VERBATIM",
    "Converted",
    "Verdict",
    "executable_inputs",
    "from_mkosi",
    "from_systemd",
    "implied_packages",
    "render",
]
