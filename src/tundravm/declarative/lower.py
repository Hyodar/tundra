"""Lowering: a declarative :class:`Recipe` as the compiler's ``RecipeState``.

``lower`` maps variants onto compiler profiles and has
:class:`~tundravm.declarative.state.StateBuilder` write each profile's
declarations; the result, with the recipe-wide mkosi options and kernels, is
the internal :class:`~tundravm.declarative._lowered.Lowered`, which compile,
lint, diff, lock, bake and inspect work on.

Variants map onto compiler profiles. The default variant (the one named
``default``, else the first root variant) is the default profile and receives
every declaration it resolves to. A variant whose parent is ``base`` or the
default variant becomes a profile that extends the default one and declares
only what it adds (or replaces, for types the profile merge overrides by
identity). Every other variant (parentless, chained onto another variant, with
its own settings or kernel, or removing/replacing what the extending merge
cannot express) is lowered standalone from its own resolved declarations, with
its own mkosi options and kernel.

Lowering is deterministic and never touches the network; ``Path`` contents
are read here.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any
from urllib.parse import urlparse

from tundravm._options import MkosiOptions
from tundravm.compiler.emit_mkosi import PHASE_TO_MKOSI_KEY
from tundravm.errors import ValidationError
from tundravm.models import Kernel as FluentKernel
from tundravm.models import RecipeState

from ._lowered import Lowered
from .model import (
    BASE_PARENT,
    Debloat,
    Declaration,
    Disk,
    Fragment,
    Group,
    Hook,
    Http,
    Kernel,
    Key,
    Partition,
    Recipe,
    Repository,
    Resolved,
    RuntimeTools,
    Secrets,
    Setting,
    Unit,
    User,
    Variant,
)
from .model import File as FileDeclaration
from .resolve import CLOUD_TARGETS, DEFAULT_TARGET, describe, has_init, identity, resolve
from .state import (
    BUILD_SOURCES,
    HISTORICAL,
    INIT_SERVICE,
    StateBuilder,
    groupadd_line,
    inject_after_init,
    useradd_line,
)
from .utils import BACKPORTS_TREE, Backports, EfiStub

BACKPORTS_SOURCES = BACKPORTS_TREE.partition(":")[2]
"""Where :class:`Backports` sources go in the build sandbox (``current`` dialect)."""
BACKPORTS_PINS = "/etc/apt/preferences.d/debian-backports.pref"
"""Where :class:`Backports` pins go in the build sandbox (``current`` dialect)."""
_OVERRIDABLE = (FileDeclaration, User, Group, Partition, Repository, Debloat)
"""Types an extending profile can redeclare: the profile merge replaces them by identity."""
_RUNTIME = (Key, Disk, Secrets, RuntimeTools)

_SINGLE_SETTINGS: dict[tuple[str, str], str] = {
    ("Output", "Seed"): "seed",
    ("Output", "OutputDirectory"): "output_directory",
    ("Output", "CompressOutput"): "compress_output",
    ("Output", "ManifestFormat"): "manifest_format",
    ("Build", "PackageCacheDirectory"): "package_cache_directory",
}
_BOOL_SETTINGS: dict[tuple[str, str], str] = {
    ("Build", "WithNetwork"): "with_network",
    ("Content", "CleanPackageMetadata"): "clean_package_metadata",
}
_COMPILER_KEYS: dict[str, str] = {
    "Distribution": "Recipe.base",
    "Release": "Recipe.base",
    "Architecture": "Recipe.arch",
    "Mirror": "Recipe.mirror",
    "ToolsTreeMirror": "Recipe.tools_mirror",
    "SourceDateEpoch": "Recipe.epoch",
    "Format": "Variant targets (the compiler always builds a UKI)",
    "ImageId": "the variant name",
    "Packages": "Package(name)",
    "BuildPackages": "Package(name, role='build')",
    "Bootable": "Kernel(...) or Package('linux-image-amd64'); only Bootable=no is accepted",
    "KernelCommandLine": "Kernel(cmdline=...)",
    "ExtraTrees": "File/Directory/Template(stage='extra')",
    "SkeletonTrees": "File/Directory/Template(stage='skeleton')",
    **{key: "Hook(name, phase, script)" for key in PHASE_TO_MKOSI_KEY.values()},
}
"""mkosi keys the compiler writes itself, and the declaration that sets each."""
_BOOTABLE = ("Content", "Bootable")
"""``Setting("Content", "Bootable", ("no",))``: a non-bootable image; any other value is the
compiler's to write."""
_LIST_SETTINGS = (("Build", "Environment"), ("Build", "SandboxTrees"), BUILD_SOURCES)
_TRUE = frozenset({"true", "yes", "1"})


class _Standalone(Exception):
    """The extending-profile merge cannot express a variant; lower it standalone.

    The message says why, completing "it ...".
    """


_FALSE = frozenset({"false", "no", "0"})


def lower(recipe: Recipe, *, variants: Sequence[str] | None = None) -> Lowered:
    """Lower *recipe* onto the compiler, with *variants* (default: all).

    The default variant is always lowered: the other profiles build on it.
    Variants whose settings or kernel differ from the default variant's get
    their own. Raises ``ValidationError`` for an unresolvable recipe, for
    declarations the compiler cannot express, and for variants the native
    layout cannot emit.
    """
    default = _default_variant(recipe)
    selected = _selected(recipe, variants, default)
    resolved = {variant.name: resolve(recipe, variant=variant.name) for variant in selected}
    base = resolved[default.name]
    dialect = recipe.mkosi.dialect
    if dialect != HISTORICAL:
        _check_mirrors(recipe)
    backports = {} if dialect == HISTORICAL else _backports_sources(recipe)
    scripts = {} if dialect == HISTORICAL else _efi_stub_scripts(recipe)
    options = {
        name: _mkosi_options(
            recipe,
            [i for i in r.items if isinstance(i, Setting)],
            sandbox_files=tuple(file for i in r.items if i in backports for file in backports[i]),
        )
        for name, r in resolved.items()
    }
    kernels = {name: _variant_kernel(r.items) for name, r in resolved.items()}
    builder = StateBuilder(
        RecipeState.initialize(base=recipe.base, arch=recipe.arch, default_profile=default.name),
        historical=dialect == HISTORICAL,
    )
    reproducible = recipe.epoch is not None
    strip = recipe.mkosi.strip_os_release
    if reproducible:
        builder.strip_image_version(enabled=True)
    if strip is not None and strip != reproducible:
        builder.strip_image_version(enabled=strip)

    builder.declare(default.name, base.items, full=base.items, skip=backports, scripts=scripts)
    builder.targets(default.name, base.targets, inherited=(DEFAULT_TARGET,))

    profile_mkosi: dict[str, MkosiOptions] = {}
    profile_kernels: dict[str, FluentKernel | None] = {}
    rejected: list[tuple[str, str]] = []
    for variant in selected[1:]:
        child = resolved[variant.name]
        try:
            if variant.parent not in (BASE_PARENT, default.name):
                raise _Standalone(
                    "is parentless"
                    if variant.parent is None
                    else f"is chained onto variant {variant.parent!r}"
                )
            if any(t in CLOUD_TARGETS for t in base.targets) and child.targets != base.targets:
                raise _Standalone(
                    f"targets {', '.join(child.targets)} without the default variant's "
                    f"{', '.join(base.targets)} integration"
                )
            _same_configuration(base, child)
            own, reemit = _overlay(base, child, dialect=dialect)
        except _Standalone as why:
            if recipe.mkosi.layout == "native":
                rejected.append((variant.name, str(why)))
                continue
            if options[variant.name] != options[default.name]:
                profile_mkosi[variant.name] = options[variant.name]
            if kernels[variant.name] != kernels[default.name]:
                profile_kernels[variant.name] = kernels[variant.name]
            builder.standalone(variant.name)
            builder.declare(
                variant.name, child.items, full=child.items, skip=backports, scripts=scripts
            )
            if not any(isinstance(i, Debloat) for i in child.items) and any(
                isinstance(i, Debloat) for i in base.items
            ):
                builder.default_debloat(variant.name)  # else it falls back to the default's
            builder.targets(variant.name, child.targets, inherited=None)
            continue
        builder.extending(variant.name)
        builder.declare(
            variant.name, own, full=child.items, reemit=reemit, skip=backports, scripts=scripts
        )
        builder.targets(variant.name, child.targets, inherited=base.targets)
    if rejected:
        plural = "s" if len(rejected) > 1 else ""
        listed = ", ".join(f"{name!r} (it {why})" for name, why in rejected)
        raise ValidationError(
            f"Mkosi(layout='native') cannot emit variant{plural} {listed}: mkosi "
            f"applies the root mkosi.conf (the default variant {default.name!r}) to every "
            "profile, so a native profile can only add to it.",
            hint=f"Use Mkosi(layout='directories'), or make each a pure addition to "
            f"{default.name!r}: parent {BASE_PARENT!r} or {default.name!r}, nothing removed, "
            f"{default.name!r}'s settings and kernel, and replace= limited to File, User, "
            "Group, Partition, Repository and Debloat.",
            context={"variants": ", ".join(name for name, _ in rejected)},
        )
    extra: dict[str, Any] = {} if recipe.policy is None else {"policy": recipe.policy}
    return Lowered(
        state=builder.state,
        modules=builder.modules,
        mirror=recipe.mirror,
        tools_tree_mirror=recipe.tools_mirror,
        snapshot=recipe.snapshot,
        reproducible=reproducible,
        mkosi=options[default.name],
        kernel=kernels[default.name],
        profile_mkosi=profile_mkosi,
        profile_kernels=profile_kernels,
        **extra,
    )


def _check_mirrors(recipe: Recipe) -> None:
    """Reject a ``Recipe.mirror``/``tools_mirror`` that names an archive, not a mirror root.

    mkosi appends ``<distribution>`` (or ``archive/<distribution>/<snapshot>``) to
    ``Mirror=``, so a URL that already ends there resolves to nothing.
    """
    distribution = recipe.base.partition("/")[0]
    for what, url in (("mirror", recipe.mirror), ("tools_mirror", recipe.tools_mirror)):
        if url is None:
            continue
        path = urlparse(url).path.rstrip("/")
        if path.endswith(f"/{distribution}") or "/archive/" in f"{path}/":
            raise ValidationError(
                f"Recipe.{what} {url!r} names an archive; mkosi appends "
                f"'{distribution}' (or 'archive/{distribution}/<snapshot>') to it.",
                hint="Pass the mirror root, e.g. 'https://deb.debian.org', and pin a "
                "snapshot with Recipe(snapshot='20251113T083151Z') (mirror defaults to "
                "https://snapshot.debian.org then).",
                context={"field": f"Recipe.{what}"},
            )


def _fragments(recipe: Recipe) -> Iterator[Fragment]:
    """Every fragment of *recipe*, nested ones included, in declaration order."""

    def walk(fragment: Fragment) -> Iterator[Fragment]:
        yield fragment
        for item in fragment.items:
            if isinstance(item, Fragment):
                yield from walk(item)

    yield from walk(recipe.common)
    for variant in recipe.variants:
        yield from walk(variant.add)


def _backports_sources(recipe: Recipe) -> dict[Declaration, tuple[tuple[str, str], ...]]:
    """The sync hook of each :class:`Backports` in *recipe*, mapped to its sandbox files.

    The ``current`` dialect writes those sources and pins into ``mkosi.sandbox`` in
    place of the hook: mkosi 26 reads ``SandboxTrees=`` before any script runs and
    gives sync scripts no ``$BUILDDIR``.
    """
    release = recipe.base.partition("/")[2]
    found: dict[Declaration, tuple[tuple[str, str], ...]] = {}
    for fragment in _fragments(recipe):
        if isinstance(fragment, Backports):
            hook = next(item for item in fragment.items if isinstance(item, Hook))
            sources = fragment.render_sources(
                mirror=recipe.mirror, release=release, snapshot=recipe.snapshot
            )
            pins = fragment.render_preferences(release=release)
            found[hook] = ((BACKPORTS_SOURCES, sources), (BACKPORTS_PINS, pins))
    return found


def _efi_stub_scripts(recipe: Recipe) -> dict[Declaration, str]:
    """The postinst hook of each :class:`EfiStub` in *recipe*, mapped to its ``current`` script."""
    return {
        next(item for item in fragment.items if isinstance(item, Hook)): fragment.render_script()
        for fragment in _fragments(recipe)
        if isinstance(fragment, EfiStub)
    }


def _wide(items: Sequence[Declaration]) -> dict[tuple[str, ...], Declaration]:
    """The settings (but ``BuildSources``) and the kernel: the per-tree configuration."""
    return {
        identity(item): item
        for item in items
        if isinstance(item, Kernel)
        or (isinstance(item, Setting) and (item.section, item.key) != BUILD_SOURCES)
    }


def _same_configuration(base: Resolved, child: Resolved) -> None:
    """Raise ``_Standalone`` when *child*'s settings or kernel differ from *base*'s."""
    mine, theirs = _wide(child.items), _wide(base.items)
    changed = sorted(key for key in mine.keys() | theirs.keys() if mine.get(key) != theirs.get(key))
    if changed:
        raise _Standalone(f"has its own {', '.join(describe(key) for key in changed)}")


def _default_variant(recipe: Recipe) -> Variant:
    """The variant called ``default``, else the first root variant.

    It may itself be chained onto another variant: it is lowered from its
    resolved declarations either way.
    """
    named = [v for v in recipe.variants if v.name == "default"]
    roots = [v for v in recipe.variants if v.parent in (BASE_PARENT, None)]
    return named[0] if named else (roots[0] if roots else recipe.variants[0])


def _selected(recipe: Recipe, names: Sequence[str] | None, default: Variant) -> list[Variant]:
    if names is None:
        chosen = list(recipe.variants)
    else:
        chosen = [recipe.variant(name) for name in dict.fromkeys(names)]
    return [default, *(v for v in chosen if v.name != default.name)]


def _overlay(
    base: Resolved, child: Resolved, *, dialect: str = "current"
) -> tuple[list[Declaration], list[Unit]]:
    """What *child* declares on top of *base* (its extended profile), plus units to re-emit.

    A profile that extends another only adds, or replaces the types its merge
    overrides by identity; anything else raises.
    """
    inherited = {identity(item): item for item in base.items}
    present = {identity(item) for item in child.items}
    missing = [describe(key) for key in inherited if key not in present]
    if missing:
        raise _Standalone(f"removes {', '.join(missing)}")  # the merge only adds
    own: list[Declaration] = []
    for item in child.items:
        previous = inherited.get(identity(item))
        if previous is None:
            own.append(item)
        elif previous != item:
            if not isinstance(item, _OVERRIDABLE) or (
                dialect == HISTORICAL and isinstance(item, (Group, User))
            ):
                raise _Standalone(f"replaces {describe(identity(item))}")
            own.append(item)
    if any(isinstance(item, _RUNTIME) for item in own) and any(
        isinstance(item, (Key, Disk, Secrets)) for item in base.items
    ):
        # runtime tools are configured once per profile
        raise _Standalone("changes the keys, disks or secrets the default variant configures")
    if any(isinstance(item, Setting) for item in own):
        raise _Standalone("has its own Build.BuildSources")  # mounts merge per profile
    if any(isinstance(item, Debloat) and item.keep_paths_by_variant for item in own):
        raise _Standalone("declares per-variant debloat paths")
    reemit: list[Unit] = []
    if has_init(child.items) and not has_init(base.items):
        reemit = [
            item
            for item in child.items
            if isinstance(item, Unit)
            and item.after_init
            and item.content is not None
            and identity(item) in inherited
        ]
    return own, reemit


def _variant_kernel(items: Sequence[Declaration]) -> FluentKernel | None:
    kernel = next((item for item in items if isinstance(item, Kernel)), None)
    return None if kernel is None else _kernel(kernel)


def _kernel(kernel: Kernel) -> FluentKernel:
    """The compiler's kernel: tag ``v<version>`` stays ``source_ref=None``."""
    common: dict[str, Any] = {
        "version": kernel.version,
        "config_file": kernel.config,
        "cmdline": kernel.cmdline or None,
        "tdx": kernel.tdx,
    }
    source = kernel.source
    if isinstance(source, Http):
        if source.sha256 is None:
            raise ValidationError(
                f"Kernel {kernel.version}: an http kernel source needs sha256=; "
                f"Http({source.url!r}) has none.",
                hint="Pass Http(url, sha256=...), or use a git source: Git(url, ref).",
            )
        return FluentKernel(**common, source_archive=source.url, source_sha256=source.sha256)
    return FluentKernel(
        **common,
        source_repo=source.url,
        source_ref=None if source.ref == f"v{kernel.version}" else source.ref,
        source_subdir=source.subdir,
        source_submodules=source.submodules,
    )


def _setting_bool(setting: Setting, value: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValidationError(
        f"Setting {setting.section}.{setting.key}: {value!r} is not a boolean.",
        hint=f"Use one of: {', '.join(sorted(_TRUE | _FALSE))}",
    )


def _mkosi_options(
    recipe: Recipe,
    settings: Sequence[Setting],
    *,
    sandbox_files: tuple[tuple[str, str], ...] = (),
) -> MkosiOptions:
    """``MkosiOptions`` for the recipe-wide fields and one variant's *settings*.

    Settings without a compiler field are written verbatim, unless the compiler
    writes that key itself. *sandbox_files* replace the ``nethermind-v1``
    backports sandbox tree.
    """
    config = recipe.mkosi
    changes: dict[str, Any] = {
        "emit_mode": "native_profiles" if config.layout == "native" else "per_directory",
        "init_script": config.init_script,
        "generate_version_script": config.version_script,
        "generate_cloud_postoutput": config.cloud_postoutput,
        "dialect": config.dialect,
    }
    if sandbox_files:
        changes["sandbox_files"] = sandbox_files
    environment: dict[str, str] = {}
    passthrough: list[str] = []
    verbatim: list[tuple[str, str, tuple[str, ...]]] = []
    if recipe.epoch:
        environment["SOURCE_DATE_EPOCH"] = str(recipe.epoch)
    for setting in settings:
        key = (setting.section, setting.key)
        if key == BUILD_SOURCES:
            continue  # per-profile mounts, declared by _declare
        if key == ("Build", "Environment"):
            for value in setting.values:
                name, sep, assigned = value.partition("=")
                if sep:
                    environment[name] = assigned
                else:
                    passthrough.append(name)
            continue
        if key == ("Build", "SandboxTrees"):
            changes["sandbox_trees"] = tuple(
                v for v in setting.values if not (sandbox_files and v == BACKPORTS_TREE)
            )
            continue
        if key == _BOOTABLE and len(setting.values) == 1 and setting.values[0].lower() in _FALSE:
            changes["bootable"] = False
            continue
        field = _SINGLE_SETTINGS.get(key) or _BOOL_SETTINGS.get(key)
        if field is None:
            mapped = [
                f"Setting({s!r}, {k!r}, ...)"
                for s, k in (*_SINGLE_SETTINGS, *_BOOL_SETTINGS, *_LIST_SETTINGS)
                if k == setting.key
            ]
            if mapped or setting.key in _COMPILER_KEYS:
                alternative = mapped[0] if mapped else _COMPILER_KEYS[setting.key]
                raise ValidationError(
                    f"Setting {setting.section}.{setting.key}: the compiler writes "
                    f"{setting.key}= itself.",
                    hint=f"Declare it with {alternative}.",
                    context={"setting": f"{setting.section}.{setting.key}"},
                )
            verbatim.append((setting.section, setting.key, setting.values or ("",)))
            continue
        if len(setting.values) != 1:
            raise ValidationError(
                f"Setting {setting.section}.{setting.key} takes exactly one value.",
                hint=f"Pass one value: Setting({setting.section!r}, {setting.key!r}, (VALUE,)).",
            )
        (value,) = setting.values
        changes[field] = _setting_bool(setting, value) if key in _BOOL_SETTINGS else value
    if environment:
        changes["environment"] = environment
    if passthrough:
        changes["environment_passthrough"] = tuple(passthrough)
    if verbatim:
        changes["settings"] = tuple(verbatim)
    return MkosiOptions(**changes)


__all__ = [
    "HISTORICAL",
    "INIT_SERVICE",
    "groupadd_line",
    "inject_after_init",
    "lower",
    "useradd_line",
]
