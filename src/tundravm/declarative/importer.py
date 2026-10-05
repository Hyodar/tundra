"""``import_tree``: an existing mkosi tree as a readable recipe module (``tundravm import``).

The importer reverse-maps what the compiler writes: ``mkosi.conf`` becomes the
recipe-wide fields, ``Package`` and ``Setting`` declarations; the synthetic
postinst and finalize lines become ``User``, ``Group``, ``Service``/``Unit``
enablement and ``Debloat``; the generated runtime-init, kernel build, cloud
glue, backports sources and EFI stub become ``Init``, ``Kernel``, variant
targets, ``Backports`` and ``EfiStub``. What has no first-class declaration is
kept verbatim (``File``, ``Unit``, ``Hook``, ``Setting``); what cannot be kept
at all is reported in :attr:`Imported.notes`. The first variant directory is
``Recipe.common``; every other one is a :class:`Variant` holding what differs
from it.
"""

from __future__ import annotations

import keyword
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal

from tundravm._modules.init import Init as InitGenerator
from tundravm.backends.base import is_mkosi_state
from tundravm.compiler.emit_mkosi import (
    ARCH_TO_MKOSI,
    AZURE_POSTOUTPUT_SCRIPT,
    DEFAULT_SEED,
    GCP_POSTOUTPUT_SCRIPT,
    HISTORICAL_OUTPUT_NAME,
    MINIMAL_TARGET_UNIT,
    MKOSI_VERSION_SCRIPT,
    PHASE_TO_MKOSI_KEY,
    DeterministicMkosiEmitter,
    _groupadd_command,
    _render_kernel_build_script,
    _systemd_unit_content,
    _useradd_command,
    cloud_postoutput_script,
)
from tundravm.errors import ValidationError
from tundravm.models import DebloatConfig, ProfileState
from tundravm.platforms import azure, gcp

from .lower import _kernel
from .model import (
    EMPTY_FRAGMENT,
    Debloat,
    Declaration,
    File,
    Fragment,
    Git,
    Group,
    Hook,
    Http,
    Init,
    Kernel,
    Mkosi,
    Package,
    Phase,
    Recipe,
    Service,
    Setting,
    Unit,
    User,
    Variant,
)
from .resolve import identity
from .state import HISTORICAL, INIT_SERVICE, STRIP_IMAGE_VERSION, groupadd_line, useradd_line
from .state import _debloat as debloat_config
from .state import _group as state_group
from .state import _service as state_service
from .state import _user as state_user
from .utils import BACKPORTS_TREE, Backports, Composite, EfiStub

Dialect = Literal["current", "nethermind-v1"]
Item = Declaration | Fragment

LARGE_FILE = 64 * 1024
"""Files above this size (or not UTF-8 text) are imported from the tree, not inlined."""
PREAMBLE = "#!/usr/bin/env bash\nset -euo pipefail\n\n"
"""The first lines of every phase script the compiler generates."""
UNIT_DIR = "usr/lib/systemd/system"
DEFAULT_KERNEL_REPO = "https://github.com/gregkh/linux"
_WANTS_DIR = 'mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"'
_AZURE_POSTINST = (
    "mkosi-chroot systemctl enable azure-complete-provisioning.service",
    "mkosi-chroot mkdir -p /etc/systemd/system/minimal.target.wants",
    "mkosi-chroot ln -sf "
    "/usr/lib/systemd/system/azure-complete-provisioning.service "
    "/etc/systemd/system/minimal.target.wants/azure-complete-provisioning.service",
)
_AZURE_FILES = {
    "usr/bin/azure-complete-provisioning": (azure.AZURE_PROVISIONING_SCRIPT,),
    f"{UNIT_DIR}/azure-complete-provisioning.service": (
        azure.AZURE_PROVISIONING_SERVICE,
        azure.AZURE_PROVISIONING_SERVICE_ONLINE,
    ),
}
_GCP_FILES = {
    "etc/hosts": (gcp.GCP_HOSTS,),
    "etc/resolv.conf": (gcp.GCP_RESOLV_CONF,),
    "usr/lib/udev/rules.d/65-gce-disk-naming.rules": (gcp.GCE_DISK_NAMING_RULES,),
    "usr/lib/udev/google_nvme_id": (gcp.GOOGLE_NVME_ID,),
}
_TARGET_PACKAGES = {"azure": "dmidecode", "gcp": "udev"}
_SCRIPT_PHASES: dict[str, Phase] = {key: phase for phase, key in PHASE_TO_MKOSI_KEY.items()}
_NATIVE_SCRIPTS: dict[str, Phase] = {
    "mkosi.sync": "sync",
    "mkosi.prepare": "prepare",
    "mkosi.build": "build",
    "mkosi.postinst": "postinst",
    "mkosi.finalize": "finalize",
    "mkosi.postoutput": "postoutput",
    "mkosi.clean": "clean",
}
_SKIPPED = ("mkosi.sandbox", "mkosi.cache", "mkosi.tools", "mkosi.builddir", "mkosi.output")
_VERBATIM = (File, Hook, Setting)
_ORDERED = (Hook, Service, User, Group, Init, Fragment)


@dataclass(frozen=True, slots=True)
class Imported:
    """What :func:`import_tree` reads from a tree.

    ``recipe_source`` is the generated module (it binds ``recipe``), ``recipe``
    that module executed, ``notes`` what was skipped or could not be mapped,
    and ``coverage`` how many declarations are first-class (``declared``) and how
    many keep tree text as is (``verbatim``: ``File``, ``Unit`` files, ``Hook``,
    ``Setting``).
    """

    recipe_source: str
    recipe: Recipe
    notes: tuple[str, ...] = ()
    coverage: Mapping[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        """The fields as JSON values (``recipe`` as its name and variant names)."""
        return {
            "recipe_source": self.recipe_source,
            "recipe": {
                "name": self.recipe.name,
                "variants": [variant.name for variant in self.recipe.variants],
            },
            "notes": list(self.notes),
            "coverage": dict(self.coverage),
        }


def import_tree(
    tree: Path,
    *,
    name: str | None = None,
    variants: Sequence[str] | None = None,
    dialect: Dialect | None = None,
    out: Path | None = None,
) -> Imported:
    """Read the mkosi tree at *tree* as a recipe module.

    *tree* is one variant directory (it holds ``mkosi.conf``) or a directory of
    them as ``compile`` writes them; *variants* picks some of those (the first
    becomes ``Recipe.common``, default: ``default`` first, then by name). *name*
    is the recipe name (default: the directory name). *dialect* is the emission
    dialect the tree was written with (default: guessed). *out* is where the
    module will be written: ruff formats it with that file's configuration.
    """
    root = Path(tree)
    if not root.is_dir():
        raise ValidationError(
            f"Cannot import {root}: it is not a directory.",
            hint="Pass an mkosi tree: a directory with mkosi.conf, or one holding variant "
            "directories that do.",
            context={"tree": str(root)},
        )
    found_dirs = _variant_dirs(root)
    if not found_dirs:
        raise ValidationError(
            f"Cannot import {root}: it holds no mkosi.conf.",
            hint="Pass a directory with mkosi.conf, or one whose subdirectories have one.",
            context={"tree": str(root)},
        )
    if variants:
        known = {d.name: d for d in found_dirs}
        unknown = [v for v in variants if v not in known]
        if unknown:
            raise ValidationError(
                f"Unknown variant directory: {', '.join(unknown)}.",
                hint=f"Variant directories in {root}: {', '.join(sorted(known))}.",
            )
        found_dirs = [known[v] for v in dict.fromkeys(variants)]
    single = (root / "mkosi.conf").is_file()
    recipe_name = name or _recipe_name(root)
    chosen = dialect or _guess_dialect(found_dirs)
    paths = _PathPool()
    readers = [
        _VariantReader(directory, variant=_dir_variant(directory, single), dialect=chosen)
        for directory in found_dirs
    ]
    read = [reader.read(paths) for reader in readers]
    notes: list[str] = []
    if not dialect and chosen == HISTORICAL:
        notes.append("dialect nethermind-v1 guessed from the tree (pass --dialect to override)")
    if single:
        notes.append(f"single variant directory imported as variant {read[0].name!r}")
    for found in read:
        notes.extend(f"{found.name}: {note}" for note in found.notes)
    recipe_value, merge_notes = _assemble(recipe_name, read, chosen, root)
    notes.extend(merge_notes)
    source = _module_source(recipe_value, tree=root, out=out)
    formatted, format_note = _ruff_format(source, out)
    if format_note:
        notes.append(format_note)
    namespace: dict[str, Any] = {"__name__": "tundravm_imported"}
    if out is not None:
        namespace["__file__"] = str(Path(out).resolve())
    exec(compile(formatted, str(out or "<imported recipe>"), "exec"), namespace)
    recipe = namespace["recipe"]
    notes.extend(_round_trip_notes(recipe, root, [f.name for f in read], single=single))
    return Imported(
        recipe_source=formatted,
        recipe=recipe,
        notes=tuple(notes),
        coverage=_coverage(recipe),
    )


# ── tree layout ─────────────────────────────────────────────────────────


def _variant_dirs(root: Path) -> list[Path]:
    if (root / "mkosi.conf").is_file():
        return [root]
    found = sorted(child for child in root.iterdir() if (child / "mkosi.conf").is_file())
    return sorted(found, key=lambda d: (d.name != "default", d.name))


def _recipe_name(root: Path) -> str:
    resolved = root.resolve()
    if resolved.name in ("mkosi", "") or (resolved / "mkosi.conf").is_file():
        return resolved.parent.name or "imported"
    return resolved.name


def _dir_variant(directory: Path, single: bool) -> str:
    if not single:
        return directory.name
    for line in _read_text(directory / "mkosi.conf").splitlines():
        if line.startswith("ImageId="):
            return line.partition("=")[2].strip() or "default"
    return "default"


def _guess_dialect(dirs: Sequence[Path]) -> Dialect:
    """``nethermind-v1`` when the tree spells something only that dialect writes."""
    for directory in dirs:
        conf = _read_text(directory / "mkosi.conf")
        if "/archive/" in conf:
            return "nethermind-v1"
        for script in ("azure-postoutput.sh", "gcp-postoutput.sh"):
            text = _read_text(directory / "scripts" / script)
            if HISTORICAL_OUTPUT_NAME in text:
                return "nethermind-v1"
        build = _read_text(directory / "scripts" / "04-build.sh")
        if 'KERNEL_CACHE="${BUILDDIR}/' in build or "$BUILDDIR/debian-backports" in _read_text(
            directory / "scripts" / "01-sync.sh"
        ):
            return "nethermind-v1"
    return "current"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


class _PathPool:
    """Tree files a recipe reads by ``Path``: equal contents share the first path."""

    def __init__(self) -> None:
        self._by_content: dict[bytes, Path] = {}

    def path(self, host: Path) -> Path:
        data = host.read_bytes()
        return self._by_content.setdefault(data, host.resolve())


# ── mkosi.conf ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Line:
    section: str
    key: str
    value: str
    """Text after ``=`` with continuation lines (``"\\n    pkg"``) kept as written."""
    comment: bool = False


def _parse_conf(text: str) -> list[_Line]:
    lines: list[_Line] = []
    section = ""
    for raw in text.split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        if raw[:1].isspace() and lines and not lines[-1].comment:
            lines[-1].value += "\n" + raw
        elif stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif stripped.startswith("#"):
            lines.append(_Line(section, "#", stripped[1:].strip(), comment=True))
        else:
            key, _, value = raw.partition("=")
            lines.append(_Line(section, key.strip(), value))
    return lines


def _words(value: str) -> list[str]:
    return [word for word in re.split(r"[\s,]+", value) if word]


# ── one variant directory ───────────────────────────────────────────────


@dataclass(slots=True)
class _Found:
    """One variant directory reverse-mapped: its declarations and recipe-wide fields."""

    name: str
    items: list[Item]
    targets: tuple[str, ...]
    wide: dict[str, Any]
    mkosi: dict[str, Any]
    notes: list[str]


@dataclass
class _VariantReader:
    """Reads one variant directory; each step consumes the tree paths it maps."""

    root: Path
    variant: str
    dialect: Dialect
    notes: list[str] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)
    wide: dict[str, Any] = field(default_factory=dict)
    mkosi: dict[str, Any] = field(default_factory=dict)
    environment: list[str] = field(default_factory=list)
    kernel_conf: dict[str, Any] = field(default_factory=dict)
    extra_settings: list[Setting] = field(default_factory=list)
    skeleton_paths: list[str] = field(default_factory=list)
    sync_text: tuple[str, str, str] | None = None
    finalize_commands: list[str] | None = None

    @property
    def historical(self) -> bool:
        return self.dialect == HISTORICAL

    def text(self, rel: str) -> str | None:
        path = self.root / rel
        if not path.is_file() or path.is_symlink():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None

    def take(self, rel: str) -> str | None:
        found = self.text(rel)
        if found is not None:
            self.consumed.add(rel)
        return found

    def read(self, paths: _PathPool) -> _Found:
        conf = self._conf()
        packages, build_packages, settings, scripts = self._conf_items(conf)
        extra = self._tree_files("mkosi.extra")
        skeleton = self._tree_files("mkosi.skeleton")
        targets = self._targets(extra, scripts)
        kernel, build_hooks = self._kernel(conf, scripts, paths)
        composites = self._backports(settings, scripts)
        postinst = self._postinst(scripts)
        debloat = self._debloat(postinst, scripts)
        if debloat is None or debloat.minimize_systemd:
            if skeleton.get("etc/systemd/system/minimal.target") == MINIMAL_TARGET_UNIT:
                if debloat is None or debloat.enabled:
                    del skeleton["etc/systemd/system/minimal.target"]
        init_script = skeleton.pop("init", None)
        if isinstance(init_script, str):
            self.mkosi["init_script"] = init_script
        if self.text("../mkosi.version") == MKOSI_VERSION_SCRIPT:
            self.mkosi["version_script"] = True
        init, units = self._units(extra, postinst)
        files = [
            *self._files(skeleton, "mkosi.skeleton", "skeleton", paths),
            *self._files(extra, "mkosi.extra", "extra", paths),
        ]
        hooks = self._hooks(scripts, postinst, composites)
        items: list[Item] = [*packages, *build_packages]
        if self.sync_text is None:
            items.extend(c for c in composites if isinstance(c, Backports))
        items.extend(settings)
        items.extend(self.extra_settings)
        if kernel is not None:
            items.append(kernel)
        items.extend(postinst.accounts)
        items.extend(files)
        items.extend(units)
        if init is not None:
            items.append(init)
        items.extend(build_hooks)
        items.extend(hooks)
        if debloat is not None and debloat != Debloat():
            items.append(debloat)
        self._leftovers()
        return _Found(self.variant, items, targets, self.wide, self.mkosi, self.notes)

    # ── mkosi.conf ──

    def _conf(self) -> list[_Line]:
        text = self.take("mkosi.conf") or ""
        dropins = sorted((self.root / "mkosi.conf.d").glob("*.conf"))
        for path in dropins:
            text += "\n" + (self.take(path.relative_to(self.root).as_posix()) or "")
        return _parse_conf(text)

    def _conf_items(
        self, conf: list[_Line]
    ) -> tuple[list[Package], list[Package], list[Setting], dict[Phase, list[str]]]:
        values: dict[str, list[str]] = {}
        for line in conf:
            values.setdefault(line.key, []).append(line.value.strip())

        def one(key: str) -> str | None:
            found = values.get(key)
            return found[-1] if found else None

        distribution, release = one("Distribution") or "debian", one("Release")
        self.wide["base"] = f"{distribution}/{release}" if release else distribution
        arch = one("Architecture")
        if arch is not None:
            reverse = {v: k for k, v in ARCH_TO_MKOSI.items()}
            if arch in reverse:
                self.wide["arch"] = reverse[arch]
            else:
                self.notes.append(f"Architecture={arch} has no Recipe.arch; using x86_64")
        self.wide["mirror"] = one("Mirror")
        self.wide["tools_mirror"] = one("ToolsTreeMirror")
        self.wide["snapshot"] = one("Snapshot")
        epoch = one("SourceDateEpoch")
        self.wide["epoch"] = int(epoch) if epoch is not None and epoch.isdigit() else None
        fmt = one("Format") or "uki"
        bootable = one("Bootable")
        packages = [
            Package(n) for line in conf if line.key == "Packages" for n in _words(line.value)
        ]
        build_packages = [
            Package(n, role="build")
            for line in conf
            if line.key == "BuildPackages"
            for n in _words(line.value)
        ]
        scripts: dict[Phase, list[str]] = {}
        settings: dict[tuple[str, str], list[str]] = {}
        kernel_keys = {"Bootable", "KernelCommandLine"}
        handled = {
            "Distribution",
            "Release",
            "Architecture",
            "Mirror",
            "ToolsTreeMirror",
            "Snapshot",
            "SourceDateEpoch",
            "Format",
            "Packages",
            "BuildPackages",
            *kernel_keys,
        }
        environment: list[str] = []
        for line in conf:
            key, value = line.key, line.value.strip()
            if line.comment or key in handled:
                continue
            if key in _SCRIPT_PHASES and line.section in ("Content", "Scripts", ""):
                scripts.setdefault(_SCRIPT_PHASES[key], []).extend(_words(value))
                continue
            if key == "ImageId":
                if value != self.variant:
                    self.notes.append(f"ImageId={value} differs from the variant name; dropped")
                continue
            if key in ("ExtraTrees", "SkeletonTrees"):
                expected = "mkosi.extra" if key == "ExtraTrees" else "mkosi.skeleton"
                if value != expected:
                    self.notes.append(
                        f"{key}={value} is not mapped (the compiler writes {expected})"
                    )
                continue
            if key == "Seed" and value == DEFAULT_SEED and self.wide["epoch"] is not None:
                continue
            if (key, value) == ("ManifestFormat", "json"):
                continue
            if (key, value.lower()) in (("WithNetwork", "true"), ("CleanPackageMetadata", "true")):
                continue
            if key == "Environment":
                environment.append(value)
                continue
            section = "Build" if key == "BuildSources" else line.section
            settings.setdefault((section, key), []).append(
                line.value if "\n" in line.value else value
            )
        sde = f"SOURCE_DATE_EPOCH={epoch}" if epoch is not None else None
        if sde is not None and sde in environment:
            environment.remove(sde)
        self.environment = environment
        if fmt == "disk" and bootable == "no":
            settings[("Content", "Bootable")] = ["no"]
        elif fmt != "uki":
            self.notes.append(f"Format={fmt} is not mapped (the compiler builds a UKI)")
        self.kernel_conf = {
            "cmdline": one("KernelCommandLine"),
            "bootable": bootable,
            "version": next(
                (
                    line.value.partition("=")[2]
                    for line in conf
                    if line.comment and line.value.startswith("KernelVersion=")
                ),
                None,
            ),
            "tdx": any(line.comment and "TDX-enabled kernel" in line.value for line in conf),
        }
        result = [Setting(section, key, tuple(vals)) for (section, key), vals in settings.items()]
        return packages, build_packages, result, scripts

    # ── kernel ──

    def _kernel(
        self, conf: list[_Line], scripts: dict[Phase, list[str]], paths: _PathPool
    ) -> tuple[Kernel | None, list[Hook]]:
        info = self.kernel_conf
        build = scripts.get("build", [])
        text = self.text(build[0]) if len(build) == 1 else None
        config = self.root / "kernel" / "kernel.config"
        built = text is not None and "KERNEL_CACHE=" in text and config.is_file()
        if info["version"] is None and info["cmdline"] is None and not built:
            if info["bootable"] == "yes":
                self.notes.append("Bootable=yes without a kernel is not mapped")
            self._finish_environment()
            return None, []
        version = info["version"]
        if text is not None:
            match = re.search(r'^KERNEL_VERSION="([^"]*)"', text, re.M)
            version = match.group(1) if match else version
        version = version or "unknown"
        common: dict[str, Any] = {"cmdline": info["cmdline"] or "", "tdx": info["tdx"]}
        if not built:
            self._finish_environment()
            return Kernel(version, source=Git(DEFAULT_KERNEL_REPO, f"v{version}"), **common), []
        assert text is not None
        config_path = paths.path(config)
        for source in self._kernel_sources(text, version):
            kernel = Kernel(version, source=source, config=config_path, **common)
            rendered = _render_kernel_build_script(_kernel(kernel), self.dialect).rstrip()
            if text == rendered + "\n":
                body = ""
            elif text.startswith(rendered + "\n\n"):
                body = text[len(rendered) + 2 :].rstrip("\n")
            else:
                continue
            self.consumed.update((build[0], "kernel/kernel.config"))
            scripts.pop("build")
            for name in ("KERNEL_IMAGE", "KERNEL_VERSION"):
                if name in self.environment:
                    self.environment.remove(name)
            self._finish_environment()
            return kernel, [Hook("build", "build", body)] if body.strip() else []
        self.notes.append("kernel build script not recognized; kernel settings are not mapped")
        self._finish_environment()
        return None, []

    def _kernel_sources(self, text: str, version: str) -> list[Git | Http]:
        found: list[Git | Http] = []
        tagged = re.search(r'--branch "v\$\{KERNEL_VERSION\}" \\\n\s+(\S+) ', text)
        if tagged:
            found.append(Git(tagged.group(1), f"v{version}"))
        branch = re.search(r"git clone --depth 1[^\n]*--branch (\S+) \\\n\s+(\S+) ", text)
        if branch:
            submodules = "--recurse-submodules" in branch.group(0)
            found.append(
                Git(
                    shlex.split(branch.group(2))[0],
                    shlex.split(branch.group(1))[0],
                    submodules=submodules,
                )
            )
        commit = re.search(r"fetch --depth 1 (\S+) ([0-9a-f]{40})", text)
        if commit:
            found.append(Git(shlex.split(commit.group(1))[0], commit.group(2)))
        archive = re.search(r"curl -fsSL (\S+) -o", text)
        digest = re.search(r'echo "([0-9a-f]{64})  ', text)
        if archive:
            sha = digest.group(1) if digest else None
            found.append(Http(shlex.split(archive.group(1))[0], sha256=sha))
        found.append(Git(DEFAULT_KERNEL_REPO, f"v{version}"))
        return found

    def _finish_environment(self) -> None:
        if self.environment:
            self.extra_settings.append(Setting("Build", "Environment", tuple(self.environment)))

    # ── cloud targets ──

    def _targets(
        self, extra: dict[str, str | bytes | None], scripts: dict[Phase, list[str]]
    ) -> tuple[str, ...]:
        targets: list[str] = []
        for target, files, base in (
            ("azure", _AZURE_FILES, AZURE_POSTOUTPUT_SCRIPT),
            ("gcp", _GCP_FILES, GCP_POSTOUTPUT_SCRIPT),
        ):
            if not all(extra.get(path) in texts for path, texts in files.items()):
                continue
            targets.append(target)
            for path in files:
                del extra[path]
            script = f"scripts/{target}-postoutput.sh"
            text = self.text(script)
            if text == cloud_postoutput_script(base, self.dialect):
                self.consumed.add(script)
                listed = scripts.get("postoutput", [])
                if script in listed:
                    listed.remove(script)
            elif text is None:
                self.mkosi["cloud_postoutput"] = False
            else:
                self.notes.append(f"{script} differs from the {target} conversion script")
        return tuple(targets) or ("qemu",)

    # ── backports and the EFI stub ──

    def _backports(self, settings: list[Setting], scripts: dict[Phase, list[str]]) -> list[Item]:
        release = str(self.wide["base"]).partition("/")[2]
        if self.historical:
            sync = scripts.get("sync", [])
            text = self.text(sync[0]) if len(sync) == 1 else None
            tree = Setting("Build", "SandboxTrees", (BACKPORTS_TREE,))
            if text is None or tree not in settings:
                return []
            for candidate in _backports_candidates(text):
                hook = _composite_hook(candidate)
                if hook.script in text:
                    settings.remove(tree)
                    self.sync_text = (sync[0], text, hook.script)
                    return [candidate]
            return []
        sources = "mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources"
        pins = "mkosi.sandbox/etc/apt/preferences.d/debian-backports.pref"
        text, pref = self.text(sources), self.text(pins)
        if text is None:
            return []
        for candidate in _backports_candidates(text):
            try:
                rendered = candidate.render_sources(
                    mirror=self.wide["mirror"], release=release, snapshot=self.wide["snapshot"]
                )
            except ValidationError:
                continue
            if rendered == text and pref == candidate.render_preferences(release=release):
                self.consumed.update((sources, pins))
                return [candidate]
        return []

    # ── postinst ──

    def _postinst(self, scripts: dict[Phase, list[str]]) -> _Postinst:
        rels = scripts.pop("postinst", [])
        parsed = _Postinst()
        if len(rels) != 1:
            parsed.extra = rels
            return parsed
        text = self.text(rels[0])
        if text is None or not text.startswith(PREAMBLE):
            parsed.extra = rels
            return parsed
        self.consumed.add(rels[0])
        lines = text[len(PREAMBLE) :].removesuffix("\n").split("\n") if text != PREAMBLE else []
        lines = parsed.take_minimize(lines)
        lines = parsed.take_synthetic(lines, historical=self.historical)
        lines = parsed.take_unit_states(lines)
        parsed.commands = lines
        return parsed

    # ── finalize and debloat ──

    def _debloat(self, postinst: _Postinst, scripts: dict[Phase, list[str]]) -> Debloat | None:
        rels = scripts.get("finalize", [])
        text = self.text(rels[0]) if len(rels) == 1 else None
        self.finalize_commands = None
        if text is None or not text.startswith(PREAMBLE):
            return Debloat(enabled=False) if postinst.minimize is None else None
        lines = text[len(PREAMBLE) :].removesuffix("\n").split("\n") if text != PREAMBLE else []
        marker = "# User-defined finalize commands"
        if marker in lines:
            cut = lines.index(marker)
            synthetic, commands = lines[: max(cut - 1, 0)], lines[cut + 1 :]
        else:
            synthetic, commands = lines, []
        candidate = _debloat_from(synthetic, postinst.minimize)
        if candidate is None:
            if synthetic and synthetic != [""]:
                self.notes.append(f"{rels[0]}: finalize lines not recognized; kept as a hook")
                self.finalize_commands = [line for line in lines if line != marker]
                scripts.pop("finalize")
                self.consumed.add(rels[0])
                return Debloat(enabled=False)
            candidate = Debloat(enabled=False)
        scripts.pop("finalize")
        self.consumed.add(rels[0])
        strip = STRIP_IMAGE_VERSION
        stripped = bool(commands) and commands[0] == strip
        if stripped:
            commands = commands[1:]
        if stripped != (self.wide["epoch"] is not None):
            self.mkosi["strip_os_release"] = stripped
        self.finalize_commands = commands
        return candidate

    # ── units and runtime-init ──

    def _units(
        self, extra: dict[str, str | bytes | None], postinst: _Postinst
    ) -> tuple[Init | None, list[Unit | Service]]:
        shipped: dict[str, str] = {}
        for path in sorted(extra):
            content = extra[path]
            name = path.rpartition("/")[2]
            if (
                path.startswith(UNIT_DIR + "/")
                and "/" not in path[len(UNIT_DIR) + 1 :]
                and isinstance(content, str)
                and not self._mode(f"mkosi.extra/{path}") & 0o111
            ):
                shipped[name] = content
                del extra[path]
        enabled = list(postinst.enabled)
        init = self._init(extra, shipped, enabled)
        result: list[Unit | Service] = []
        disabled, masked = postinst.disabled, postinst.masked
        for unit in enabled:
            content = shipped.pop(unit, None)
            service = _service_of(unit, content, init=init is not None) if content else None
            if service is not None and unit not in masked:
                result.append(service)
                continue
            result.append(
                Unit(unit, content, enabled=True, masked=True if unit in masked else None)
            )
        for unit in sorted(shipped):
            result.append(
                Unit(
                    unit,
                    shipped[unit],
                    enabled=False if unit in disabled else None,
                    masked=True if unit in masked else None,
                )
            )
        listed = {u.name for u in result}
        for unit in [*disabled, *masked]:
            if unit not in listed:
                listed.add(unit)
                result.append(
                    Unit(
                        unit,
                        enabled=False if unit in disabled else None,
                        masked=True if unit in masked else None,
                    )
                )
        return init, result

    def _init(
        self, extra: dict[str, str | bytes | None], shipped: dict[str, str], enabled: list[str]
    ) -> Init | None:
        script = extra.get("usr/bin/runtime-init")
        unit = shipped.get(INIT_SERVICE)
        header = "#!/bin/bash\nset -euo pipefail\n\n"
        if not isinstance(script, str) or unit is None or INIT_SERVICE not in enabled:
            return None
        if enabled[-1] != INIT_SERVICE or not script.startswith(header):
            return None
        network = self.historical or any(
            path.rpartition("/")[2] == "network-setup.service"
            for path in (*shipped, *self.skeleton_paths)
        )
        if unit != InitGenerator()._render_service_unit(network_setup=network):
            return None
        if not self._mode("mkosi.extra/usr/bin/runtime-init") & 0o111:
            return None
        body = script[len(header) :]
        if not body.strip():
            return None
        del extra["usr/bin/runtime-init"]
        del shipped[INIT_SERVICE]
        enabled.remove(INIT_SERVICE)
        return Init("runtime-init", body)

    # ── files ──

    def _tree_files(self, top: str) -> dict[str, str | bytes | None]:
        """``rel -> text`` (``bytes`` when large or binary, ``None`` for links and dirs)."""
        base = self.root / top
        found: dict[str, str | bytes | None] = {}
        if not base.is_dir():
            return found
        for current, dirnames, filenames in os.walk(base):
            here = Path(current)
            if here != base and not dirnames and not filenames:
                found[here.relative_to(base).as_posix()] = None
            for name in sorted([*filenames, *(d for d in dirnames if (here / d).is_symlink())]):
                path = here / name
                rel = path.relative_to(base).as_posix()
                if path.is_symlink():
                    found[rel] = None
                    continue
                data = path.read_bytes()
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    found[rel] = data
                    continue
                found[rel] = data if len(data) > LARGE_FILE else text
        if top == "mkosi.skeleton":
            self.skeleton_paths = list(found)
        self.consumed.update(f"{top}/{rel}" for rel in found)
        return found

    def _mode(self, rel: str) -> int:
        """*rel*'s permission bits, without the group write bit a ``002`` umask adds."""
        mode = (self.root / rel).lstat().st_mode & 0o7777
        return mode & ~0o020 if mode in (0o664, 0o775) else mode

    def _files(
        self,
        found: dict[str, str | bytes | None],
        top: str,
        stage: Literal["skeleton", "extra"],
        paths: _PathPool,
    ) -> list[File]:
        files: list[File] = []
        for rel in sorted(found):
            content = found[rel]
            host = self.root / top / rel
            if content is None:
                kind = "symlink" if host.is_symlink() else "empty directory"
                self.notes.append(f"{top}/{rel}: {kind} is not imported")
                continue
            mode = self._mode(f"{top}/{rel}")
            value: str | Path = content if isinstance(content, str) else paths.path(host)
            if not isinstance(content, str):
                self.notes.append(f"{top}/{rel}: binary or over 64 KiB; read from the tree")
            files.append(File(f"/{rel}", value, mode=mode, stage=stage))
        return files

    # ── hooks ──

    def _hooks(
        self, scripts: dict[Phase, list[str]], postinst: _Postinst, composites: list[Item]
    ) -> list[Item]:
        items: list[Item] = []
        sync = self.sync_text
        for phase, rels in scripts.items():
            for rel in rels:
                if sync is not None and rel == sync[0]:
                    self.consumed.add(rel)
                    before, _, after = _body(sync[1])[0].partition(sync[2])
                    items.extend(_segments("sync", before.removesuffix("\n").split("\n")))
                    items.extend(c for c in composites if isinstance(c, Backports))
                    items.extend(_segments("sync", after.removeprefix("\n").split("\n"), 2))
                    continue
                text = self.text(rel)
                if text is None:
                    if (
                        not (self.root / rel).exists()
                        and rel.startswith("scripts/")
                        and phase == "postoutput"
                    ):
                        continue
                    self.notes.append(f"{rel}: not a text script; not imported")
                    continue
                self.consumed.add(rel)
                body, exact = _body(text)
                if not exact:
                    self.notes.append(f"{rel}: not in the compiler's script format; body kept")
                if body.strip():
                    items.extend(_segments(phase, body.split("\n")))
        for name, phase in _NATIVE_SCRIPTS.items():
            text = self.take(name)
            if text is not None:
                body, _ = _body(text)
                self.notes.append(f"{name}: imported as a {phase} hook (scripts/ in the tree)")
                if body.strip():
                    items.extend(_segments(phase, body.split("\n")))
        commands = postinst.commands
        commands = _drop_block(commands, list(_AZURE_POSTINST))
        items.extend(self._postinst_items(commands))
        finalize = self.finalize_commands
        if finalize:
            items.extend(_segments("finalize", finalize))
        return items

    def _postinst_items(self, lines: list[str]) -> list[Item]:
        """The postinst commands as hooks, with the EFI stub and accounts recognized in place."""
        stub = _efi_stub_in(lines, historical=self.historical)
        blocks: list[tuple[int, int, Item]] = []
        if stub is not None:
            blocks.append(stub)
        if self.historical:
            for index, line in enumerate(lines):
                account = _account(line)
                if account is not None and not any(s <= index < e for s, e, _ in blocks):
                    blocks.append((index, index + 1, account))
        blocks.sort(key=lambda block: block[0])
        items: list[Item] = []
        start = 0
        part = 1
        for begin, end, item in blocks:
            segment = _segments("postinst", lines[start:begin], part)
            part += len(segment)
            items.extend(segment)
            items.append(item)
            start = end
        items.extend(_segments("postinst", lines[start:], part))
        return items

    def _leftovers(self) -> None:
        for current, dirnames, filenames in os.walk(self.root):
            here = Path(current)
            rel_dir = here.relative_to(self.root).as_posix()
            if rel_dir == ".":
                dirnames[:] = [d for d in dirnames if d not in ("mkosi.extra", "mkosi.skeleton")]
            for name in sorted(filenames):
                rel = (here / name).relative_to(self.root).as_posix()
                if rel in self.consumed or is_mkosi_state(rel):
                    continue
                top = rel.split("/", 1)[0]
                if top in _SKIPPED or name.endswith(".sources"):
                    self.notes.append(f"{rel}: skipped ({top} is build state, not recipe input)")
                else:
                    self.notes.append(f"{rel}: not mapped")


# ── postinst ────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Postinst:
    """The postinst script split into what the compiler generates and the hook commands."""

    accounts: list[Group | User] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    masked: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    minimize: tuple[tuple[str, ...], tuple[str, ...]] | None = None
    extra: list[str] = field(default_factory=list)

    def take_minimize(self, lines: list[str]) -> list[str]:
        marker = "# Debloat: remove unwanted systemd binaries"
        if marker not in lines:
            return lines
        index = lines.index(marker)
        if index == 0 or lines[index - 1] != "":
            return lines
        lists = [
            tuple(re.findall(r'"([^"]*)"', line))
            for line in lines[index:]
            if line.startswith(("systemd_bin_whitelist=(", "systemd_svc_whitelist=("))
        ]
        if len(lists) != 2:
            return lines
        bins, units = lists
        if lines[index - 1 :] != _minimize_lines(bins, units):
            return lines
        self.minimize = (bins, units)
        return lines[: index - 1]

    def take_synthetic(self, lines: list[str], *, historical: bool) -> list[str]:
        start = 0
        accounts: list[Group | User] = []
        while not historical and start < len(lines):
            account = _parse_account(lines[start])
            if account is None or _synthetic_line(account) != lines[start]:
                break
            accounts.append(account)
            start += 1
        enables: list[str] = []
        end = start
        while end < len(lines) and re.fullmatch(r"mkosi-chroot systemctl enable \S+", lines[end]):
            enables.append(lines[end].split()[3])
            end += 1
        if enables:
            expected = [_WANTS_DIR] + [
                f'ln -sf "/etc/systemd/system/{unit}" '
                '"$BUILDROOT/etc/systemd/system/minimal.target.wants/"'
                for unit in enables
            ]
            if lines[end : end + len(expected)] == expected:
                self.enabled = enables
                end += len(expected)
            else:
                end = start
        self.accounts = accounts
        return lines[end:]

    def take_unit_states(self, lines: list[str]) -> list[str]:
        rest = list(lines)
        masked: list[str] = []
        disabled: list[str] = []
        if rest and rest[-1].startswith("mkosi-chroot systemctl mask "):
            masked = rest.pop().split()[3:]
        if rest and rest[-1].startswith("mkosi-chroot systemctl disable "):
            disabled = rest.pop().split()[3:]
        if not _states_reproduce(self.enabled, disabled, masked):
            return lines
        self.disabled, self.masked = disabled, masked
        return rest


def _states_reproduce(enabled: list[str], disabled: list[str], masked: list[str]) -> bool:
    """Whether :meth:`_VariantReader._units`' declaration order gives these state lines back."""
    if set(enabled) & set(disabled) or len(set(masked)) != len(masked):
        return False
    order = [*enabled, *(u for u in disabled if u not in enabled)]
    order += [u for u in masked if u not in order]
    return [u for u in order if u in masked] == masked


def _minimize_lines(bins: Sequence[str], units: Sequence[str]) -> list[str]:
    config = DebloatConfig(systemd_bins_keep=tuple(bins), systemd_units_keep=tuple(units))
    text = DeterministicMkosiEmitter()._render_postinst_script(
        [], ProfileState(name="import", debloat=config)
    )
    return text[len(PREAMBLE) :].removesuffix("\n").split("\n")


def _parse_account(line: str) -> Group | User | None:
    """The ``groupadd``/``useradd`` line *line* as a declaration, unchecked."""
    try:
        words = shlex.split(line)
    except ValueError:
        return None
    if len(words) < 3 or words[0] != "mkosi-chroot" or words[1] not in ("groupadd", "useradd"):
        return None
    options: dict[str, str] = {}
    flags: set[str] = set()
    rest = words[2:-1]
    index = 0
    while index < len(rest):
        word = rest[index]
        if word in ("--system", "--create-home"):
            flags.add(word)
            index += 1
        elif word in ("--gid", "--home-dir", "--shell", "--uid", "--groups") and index + 1 < len(
            rest
        ):
            options[word] = rest[index + 1]
            index += 2
        else:
            return None
    name = words[-1]
    try:
        if words[1] == "groupadd":
            gid = options.get("--gid")
            if set(options) - {"--gid"}:
                return None
            return Group(name, system="--system" in flags, gid=int(gid) if gid else None)
        gid_text = options.get("--gid")
        primary: str | int | None = int(gid_text) if gid_text and gid_text.isdigit() else gid_text
        uid = options.get("--uid")
        return User(
            name,
            system="--system" in flags,
            home=options.get("--home-dir"),
            shell=options.get("--shell", "/usr/sbin/nologin"),
            uid=int(uid) if uid else None,
            primary_group=primary,
            groups=tuple(g for g in options.get("--groups", "").split(",") if g),
        )
    except (ValidationError, ValueError):
        return None


def _synthetic_line(account: Group | User) -> str:
    """The line the ``current`` dialect's synthetic postinst block writes for *account*."""
    profile = ProfileState(name="import")
    if isinstance(account, Group):
        state_group(profile, account)
        return _groupadd_command(profile.groups[0])
    state_user(profile, account)
    return _useradd_command(profile.users[0])


def _account(line: str) -> Group | User | None:
    """A ``nethermind-v1`` postinst line that is exactly a declared group or user."""
    account = _parse_account(line)
    if account is None:
        return None
    rendered = groupadd_line(account) if isinstance(account, Group) else useradd_line(account)
    return account if rendered == line else None


def _drop_block(lines: list[str], block: list[str]) -> list[str]:
    for start in range(len(lines) - len(block), -1, -1):
        if lines[start : start + len(block)] == block:
            return lines[:start] + lines[start + len(block) :]
    return lines


def _efi_stub_in(lines: list[str], *, historical: bool) -> tuple[int, int, Item] | None:
    for index, line in enumerate(lines[:-1]):
        url = re.fullmatch(r'EFI_SNAPSHOT_URL="([^"]+)"', line)
        version = re.fullmatch(r'EFI_PACKAGE_VERSION="([^"]+)"', lines[index + 1])
        if not url or not version:
            continue
        root = "https://snapshot.debian.org/archive/debian/"
        snapshot = url.group(1)
        if snapshot.startswith(root) and "/" not in snapshot[len(root) :]:
            snapshot = snapshot[len(root) :]
        try:
            stub = EfiStub(snapshot=snapshot, version=version.group(1))
        except ValidationError:
            return None
        script = _composite_hook(stub).script if historical else stub.render_script()
        block = script.split("\n")
        if lines[index : index + len(block)] == block:
            return index, index + len(block), stub
    return None


def _composite_hook(composite: Composite) -> Hook:
    return next(item for item in composite.items if isinstance(item, Hook))


def _backports_candidates(text: str) -> list[Backports]:
    def first(pattern: str) -> str | None:
        match = re.search(pattern, text, re.M)
        return match.group(1) if match else None

    archives = dict.fromkeys([None, first(r"^URIs: (\S+)$"), first(r'^MIRROR="([^"]*)"$')])
    releases = dict.fromkeys(
        [None, first(r"^Suites: (\S+)-backports$"), first(r'^RELEASE="([^"]*)"$')]
    )
    found: list[Backports] = []
    for archive in archives:
        for release in releases:
            if archive is not None and "$" in archive:
                continue
            if release is not None and "$" in release:
                continue
            candidate = Backports(archive_url=archive, release=release)
            if candidate not in found:
                found.append(candidate)
    return found


def _body(text: str) -> tuple[str, bool]:
    """A phase script's commands without the compiler's preamble; whether it was exact."""
    if text.startswith(PREAMBLE) and text.endswith("\n"):
        return text[len(PREAMBLE) : -1], True
    lines = text.split("\n")
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    return "\n".join(lines).strip("\n"), False


def _segments(phase: Phase, lines: Sequence[str], part: int = 1) -> list[Hook]:
    text = "\n".join(lines)
    if not text.strip():
        return []
    return [Hook(phase if part == 1 else f"{phase}-{part}", phase, text)]


# ── units ───────────────────────────────────────────────────────────────


_STRICT = (
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "NoNewPrivileges=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelTunables=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "MemoryDenyWriteExecute=yes",
)


def _service_of(unit: str, content: str, *, init: bool) -> Service | None:
    """The :class:`Service` that renders exactly *content*, if there is one."""
    if not unit.endswith(".service"):
        return None
    section = ""
    entries: list[tuple[str, str, str]] = []
    for line in content.split("\n"):
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line:
            key, sep, value = line.partition("=")
            if not sep:
                return None
            entries.append((section, key, value))
    lines = {f"{key}={value}" for _, key, value in entries}
    strict = all(line in lines for line in _STRICT)
    kwargs: dict[str, Any] = {"security": "strict"} if strict else {}
    env: list[tuple[str, str]] = []
    limits: list[tuple[str, str]] = []
    pre: list[str] = []
    exec_start: str | None = None
    name = unit.removesuffix(".service")
    for section, key, value in entries:
        if strict and f"{key}={value}" in _STRICT:
            continue
        match section, key:
            case "Unit", "Description":
                kwargs["description"] = None if value == name else value
            case "Unit", "After" | "Requires" | "Wants":
                kwargs[key.lower()] = tuple(value.split())
            case "Service", "Type":
                kwargs["type"] = None if value == "simple" else value
            case "Service", "ExecStartPre":
                pre.append(value)
            case "Service", "ExecStart":
                exec_start = value
            case "Service", "User" | "Group":
                kwargs[key.lower()] = value
            case "Service", "WorkingDirectory":
                kwargs["working_dir"] = value
            case "Service", "EnvironmentFile":
                kwargs["env_file"] = value
            case "Service", "Environment":
                if value.startswith('"') and value.endswith('"') and len(value) > 1:
                    value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                env_key, _, env_value = value.partition("=")
                env.append((env_key, env_value))
            case "Service", "Restart":
                kwargs["restart"] = value
            case "Service", "RestartSec":
                pass
            case "Service", "KillMode":
                kwargs["kill_mode"] = value
            case "Service", "TimeoutStopSec":
                kwargs["timeout_stop"] = value
            case "Service", _ if key.startswith("Limit"):
                limits.append((key.removeprefix("Limit"), value))
            case "Install", "WantedBy":
                kwargs["wanted_by"] = None if value == "minimal.target" else value
            case _:
                return None
    if exec_start is None:
        return None
    after, requires = kwargs.get("after", ()), kwargs.get("requires", ())
    injected = init and after[:1] == (INIT_SERVICE,) and requires[:1] == (INIT_SERVICE,)
    if injected:
        kwargs["after"], kwargs["requires"] = after[1:], requires[1:]
    elif init:
        kwargs["after_init"] = False
    try:
        service = Service(
            name,
            exec_start,
            env=tuple(env),
            limits=tuple(limits),
            exec_start_pre=tuple(pre),
            **kwargs,
        )
        profile = ProfileState(name="import")
        state_service(profile, service)
    except (ValidationError, TypeError):
        return None
    spec = profile.services[0]
    if init and spec.after_init:
        spec = _with_init_service(spec)
    return service if _systemd_unit_content(spec) == content else None


def _with_init_service(spec: Any) -> Any:
    from dataclasses import replace

    after = spec.after if INIT_SERVICE in spec.after else (INIT_SERVICE, *spec.after)
    requires = spec.requires if INIT_SERVICE in spec.requires else (INIT_SERVICE, *spec.requires)
    return replace(spec, after=after, requires=requires)


# ── debloat ─────────────────────────────────────────────────────────────


def _debloat_from(
    lines: list[str], minimize: tuple[tuple[str, ...], tuple[str, ...]] | None
) -> Debloat | None:
    """The :class:`Debloat` whose finalize lines are *lines*, if there is one."""
    if not lines or lines[0] != "# Debloat: clean files in var directories":
        return None
    removed: list[str] = []
    conditional: dict[str, list[str]] = {}
    profile: str | None = None
    for line in lines:
        rm = re.fullmatch(r'(\s*)rm -rf "\$BUILDROOT(/[^"]*)"', line)
        cond = re.fullmatch(r'if \[\[ ! "\$\{PROFILES:-\}" == \*"([^"]+)"\* \]\]; then', line)
        if cond:
            profile = cond.group(1)
        elif line == "fi":
            profile = None
        elif rm and profile is not None and rm.group(1):
            conditional.setdefault(profile, []).append(rm.group(2))
        elif rm and not rm.group(1):
            removed.append(rm.group(2))
    defaults = DebloatConfig()
    default_paths = set(defaults.paths_remove)
    kept_by_variant = {path for paths in conditional.values() for path in paths}
    kwargs: dict[str, Any] = {
        "extra_remove": tuple(sorted((set(removed) | kept_by_variant) - default_paths)),
        "keep_paths": tuple(sorted(default_paths - set(removed) - kept_by_variant)),
        "keep_paths_by_variant": tuple(
            (name, tuple(paths)) for name, paths in sorted(conditional.items())
        ),
        "minimize_systemd": minimize is not None,
    }
    if minimize is not None:
        bins, units = minimize
        if sorted(bins) != sorted(defaults.systemd_bins_keep):
            kwargs["keep_binaries"] = tuple(bins)
        default_units = set(defaults.systemd_units_keep)
        if set(units) > default_units:
            kwargs["keep_units_extra"] = tuple(sorted(set(units) - default_units))
        elif set(units) != default_units:
            kwargs["keep_units"] = tuple(units)
    try:
        candidate = Debloat(**kwargs)
    except ValidationError:
        return None
    config = debloat_config(candidate)
    emitted = DeterministicMkosiEmitter()._synthetic_finalize_lines(
        ProfileState(name="import", debloat=config)
    )
    if emitted != lines:
        return None
    if minimize is not None and _minimize_lines(
        config.systemd_bins_keep, config.effective_units_keep
    ) != _minimize_lines(*minimize):
        return None
    return candidate


# ── the recipe ──────────────────────────────────────────────────────────


def _key(item: Item) -> tuple[Any, ...]:
    if isinstance(item, Fragment):
        return ("Fragment", type(item).__name__, item.name)
    return identity(item)


def _ordered(item: Item) -> bool:
    if isinstance(item, Unit):
        return item.enabled is not None or item.masked is not None
    return isinstance(item, _ORDERED)


def _assemble(
    name: str, found: list[_Found], dialect: Dialect, root: Path
) -> tuple[Recipe, list[str]]:
    first = found[0]
    notes: list[str] = []
    for other in found[1:]:
        for key, value in first.wide.items():
            if other.wide.get(key) != value:
                notes.append(
                    f"{other.name}: {key} {other.wide.get(key)!r} differs from "
                    f"{first.name}'s {value!r}; the recipe uses {first.name}'s"
                )
        for key in sorted(set(first.mkosi) | set(other.mkosi)):
            if first.mkosi.get(key) != other.mkosi.get(key):
                notes.append(f"{other.name}: Mkosi.{key} differs from {first.name}'s")
    common = _without_target_packages(first.items, first.targets, ())
    variants = [Variant(first.name, **_target_args(first.targets))]
    for other in found[1:]:
        items = _without_target_packages(other.items, other.targets, common)
        variants.append(_overlay_variant(other, items, common))
    wide = first.wide
    recipe = Recipe(
        name=name,
        common=Fragment("common", items=tuple(common)),
        variants=tuple(variants),
        base=wide["base"],
        arch=wide.get("arch", "x86_64"),
        mirror=wide["mirror"],
        tools_mirror=wide["tools_mirror"],
        epoch=wide["epoch"],
        snapshot=wide["snapshot"],
        mkosi=Mkosi(dialect=dialect, **first.mkosi),
    )
    return recipe, notes


def _target_args(targets: tuple[str, ...]) -> dict[str, Any]:
    return {"target": targets[0]} if len(targets) == 1 else {"targets": targets}


def _without_target_packages(
    items: list[Item], targets: tuple[str, ...], common: Sequence[Item]
) -> list[Item]:
    implicit = {Package(_TARGET_PACKAGES[t]) for t in targets if t in _TARGET_PACKAGES}
    return [i for i in items if not (i in implicit and i not in common)]


def _overlay_variant(found: _Found, items: list[Item], common: list[Item]) -> Variant:
    inherited = {_key(item): item for item in common}
    split: list[Item] = []
    for item in items:
        previous = inherited.get(_key(item))
        if (
            isinstance(item, Hook)
            and isinstance(previous, Hook)
            and item.script.startswith(previous.script + "\n")
        ):
            split.append(previous)
            rest = item.script[len(previous.script) + 1 :]
            if rest.strip():
                split.append(Hook(f"{item.name}-{found.name}", item.phase, rest))
            continue
        split.append(item)
    own = {_key(item): item for item in split}
    add = [item for item in split if _key(item) not in inherited]
    replaced = {
        key: item for key, item in own.items() if key in inherited and inherited[key] != item
    }
    removed = [item for key, item in inherited.items() if key not in own]
    targets = _target_args(found.targets)
    fragments = [i for i in (*replaced.values(), *removed) if isinstance(i, Fragment)]
    resolved = [replaced.get(k, i) for k, i in inherited.items() if k in own] + add
    if fragments or [_key(i) for i in resolved if _ordered(i)] != [
        _key(i) for i in split if _ordered(i)
    ]:
        return Variant(
            found.name, parent=None, add=Fragment(found.name, items=tuple(split)), **targets
        )
    return Variant(
        found.name,
        add=Fragment(found.name, items=tuple(add)) if add else EMPTY_FRAGMENT,
        replace=tuple(i for i in replaced.values() if not isinstance(i, Fragment)),
        remove=tuple(i for i in removed if not isinstance(i, Fragment)),
        **targets,
    )


def _coverage(recipe: Recipe) -> dict[str, int]:
    counts = {"declared": 0, "verbatim": 0}

    def count(items: Iterable[Item]) -> None:
        for item in items:
            if isinstance(item, Composite):
                counts["declared"] += 1
            elif isinstance(item, Fragment):
                count(item.items)
            elif isinstance(item, _VERBATIM) or (isinstance(item, Unit) and item.content):
                counts["verbatim"] += 1
            else:
                counts["declared"] += 1

    count(recipe.common.items)
    for variant in recipe.variants:
        count(variant.add.items)
        count(variant.replace)
    return counts


# ── source generation ───────────────────────────────────────────────────


@dataclass
class _Writer:
    """Renders declaration values as Python source, hoisting long text into constants."""

    tree: Path
    constants: dict[str, str] = field(default_factory=dict)
    """Text -> constant name."""
    names: set[str] = field(default_factory=set)
    imports: set[str] = field(default_factory=set)
    utils: set[str] = field(default_factory=set)
    paths: bool = False
    tree_paths: bool = False

    def constant(self, text: str, hint: str) -> str:
        if text in self.constants:
            return self.constants[text]
        base = re.sub(r"[^A-Za-z0-9]+", "_", hint).strip("_").upper() or "TEXT"
        if base[0].isdigit() or keyword.iskeyword(base.lower()):
            base = f"_{base}"
        name, index = base, 2
        while name in self.names or name in ("TREE", "Path"):
            name, index = f"{base}_{index}", index + 1
        self.names.add(name)
        self.constants[text] = name
        return name

    def value(self, value: object, *, hint: str = "", field_name: str = "") -> str:
        if isinstance(value, str):
            if value.count("\n") >= 2 or ("\n" in value and len(value) > 60):
                return self.constant(value, hint or field_name)
            return repr(value)
        if isinstance(value, bool) or value is None:
            return repr(value)
        if isinstance(value, int):
            return oct(value) if field_name == "mode" else repr(value)
        if isinstance(value, Path):
            try:
                rel = value.relative_to(self.tree.resolve())
            except ValueError:
                self.paths = True
                return f"Path({str(value)!r})"
            self.tree_paths = True
            return f"TREE / {rel.as_posix()!r}"
        if isinstance(value, tuple):
            parts = [self.value(v, hint=hint, field_name=field_name) for v in value]
            return "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
        if is_dataclass(value) and not isinstance(value, type):
            return self.call(value)
        raise TypeError(f"cannot render {value!r}")

    def call(self, obj: Any) -> str:
        cls = type(obj)
        if isinstance(obj, Composite):
            self.utils.add(cls.__name__)
        else:
            self.imports.add(cls.__name__)
        hint = _hint(obj)
        args: list[str] = []
        positional = True
        for item in fields(obj):
            if not item.init:
                continue
            value = getattr(obj, item.name)
            if item.default is MISSING and item.default_factory is MISSING:
                text = self.value(value, hint=hint, field_name=item.name)
                args.append(text if positional and not item.kw_only else f"{item.name}={text}")
                continue
            positional = False
            default = item.default if item.default is not MISSING else item.default_factory()  # type: ignore[misc]
            if value == default and type(value) is type(default):
                continue
            args.append(f"{item.name}={self.value(value, hint=hint, field_name=item.name)}")
        return f"{cls.__name__}({', '.join(args)})"


def _hint(obj: object) -> str:
    match obj:
        case File(path=path):
            return path.rpartition("/")[2] or path
        case Unit(name=name) | Service(name=name):
            return name
        case Hook(name=name):
            return f"{name}_script"
        case Init(name=name):
            return name
        case Mkosi():
            return "init_script"
    return type(obj).__name__


def _module_source(recipe: Recipe, *, tree: Path, out: Path | None) -> str:
    writer = _Writer(tree=tree)
    module = Path(out).name if out is not None else "RECIPE"
    body = "recipe = " + writer.value(recipe) + "\n"
    lines = [
        f'"""{recipe.name} image recipe, imported from an mkosi tree by `tundravm import`.',
        "",
        f"Lint:     tundravm lint {module}",
        f"Compile:  tundravm compile {module} --out mkosi",
        '"""',
        "",
    ]
    if writer.paths or writer.tree_paths:
        lines.append("from pathlib import Path")
        lines.append("")
    lines.append(f"from tundravm import {', '.join(sorted(writer.imports))}")
    if writer.utils:
        lines.append(f"from tundravm.declarative.utils import {', '.join(sorted(writer.utils))}")
    lines.append("")
    if writer.tree_paths:
        lines.append(_tree_binding(tree, out))
        lines.append('"""The imported tree: files the recipe reads by path live here."""')
        lines.append("")
    for text, name in writer.constants.items():
        lines.append(f"{name} = {_block(text)}")
        lines.append("")
    lines.append(body)
    return "\n".join(lines)


def _tree_binding(tree: Path, out: Path | None) -> str:
    """``TREE = ...``: relative to the module when it is written to *out*, else absolute."""
    if out is None:
        return f"TREE = Path({str(tree.resolve())!r})"
    rel = Path(os.path.relpath(tree.resolve(), Path(out).resolve().parent)).as_posix()
    if rel == ".":
        return "TREE = Path(__file__).resolve().parent"
    return f"TREE = Path(__file__).resolve().parent / {rel!r}"


def _block(text: str) -> str:
    if "\r" in text or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return repr(text)
    body = text.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    return '"""\\\n' + body + '"""'


def _ruff_format(source: str, out: Path | None) -> tuple[str, str | None]:
    """*source* formatted by ruff, or as is with a note when ruff is not available."""
    commands: list[list[str]] = [[sys.executable, "-m", "ruff"]]
    found = shutil.which("ruff")
    if found:
        commands.append([found])
    filename = str(out) if out is not None else "recipe.py"
    for command in commands:
        try:
            done = subprocess.run(
                [*command, "format", "--stdin-filename", filename, "-"],
                input=source,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and done.stdout:
            return done.stdout, None
    return source, "ruff is not available: the module is not formatted (run `ruff format` on it)"


# ── round trip ──────────────────────────────────────────────────────────


def _round_trip_notes(
    recipe: Recipe, root: Path, names: Sequence[str], *, single: bool
) -> list[str]:
    """Compile *recipe* and name the tree paths it does not give back."""
    from tundravm.errors import TdxError

    from .lifecycle import _digest_mode, read_tree
    from .lifecycle import compile as compile_recipe

    try:
        compiled = compile_recipe(recipe, variants=list(names))
    except (TdxError, OSError) as exc:
        return [f"round trip: the recipe does not compile: {exc}"]
    expected = read_tree(root)
    got = {entry.path: entry for entry in compiled.entries}
    want = {}
    for entry in expected.entries:
        path = f"{names[0]}/{entry.path}" if single else entry.path
        if single or path.split("/", 1)[0] in names:
            want[path] = entry
    differ = sorted(
        path
        for path in want.keys() | got.keys()
        if _entry_view(want.get(path), _digest_mode) != _entry_view(got.get(path), _digest_mode)
    )
    if not differ:
        return ["round trip: the recipe compiles back to this tree"]
    shown = ", ".join(differ[:10]) + (f" and {len(differ) - 10} more" if len(differ) > 10 else "")
    return [f"round trip: {len(differ)} path(s) compile differently: {shown}"]


def _entry_view(entry: Any, mode: Callable[[Any], int]) -> tuple[Any, ...] | None:
    """What a tree diff compares: bytes, link target and, for files, the exec bit."""
    if entry is None:
        return None
    return (entry.content, entry.symlink, mode(entry))


__all__ = ["Imported", "import_tree"]
