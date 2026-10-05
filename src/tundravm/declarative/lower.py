"""Lowering: a declarative :class:`Recipe` as the fluent :class:`~tundravm.Image`.

The lowered image holds the same ``RecipeState`` the equivalent fluent calls
build, so compile, lint, diff, lock, bake and explain work on it unchanged.

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

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from tundravm._image import Image, _read_source, _walk_tree
from tundravm._modules import (
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
)
from tundravm._modules.base import TUNDRA_TOOLS
from tundravm._options import MkosiOptions
from tundravm._source import BuildRecipe, GitSource, HttpSource, ScriptBuild, SourceBuild
from tundravm._source import Install as FluentInstall
from tundravm.compiler.emit_mkosi import PHASE_TO_MKOSI_KEY
from tundravm.errors import ValidationError
from tundravm.models import Kernel as FluentKernel
from tundravm.models import SecretSchema, SecretSpec, SecretTarget
from tundravm.platforms import AzurePlatform, GcpPlatform

from .model import (
    BASE_PARENT,
    Build,
    Debloat,
    Declaration,
    Directory,
    Disk,
    File,
    Fragment,
    Git,
    Group,
    Hook,
    Http,
    Init,
    Kernel,
    Key,
    Package,
    Partition,
    Recipe,
    Repository,
    Resolved,
    RuntimeTools,
    Secret,
    SecretEnv,
    Secrets,
    Service,
    Setting,
    Target,
    Template,
    Unit,
    User,
    Variant,
)
from .resolve import (
    CLOUD_TARGETS,
    DEFAULT_TARGET,
    _builtin_inits,
    describe,
    has_init,
    identity,
    order_hooks,
    order_inits,
    resolve,
)
from .utils import BACKPORTS_TREE, Backports, EfiStub

INIT_SERVICE = "runtime-init.service"
BACKPORTS_SOURCES = BACKPORTS_TREE.partition(":")[2]
"""Where :class:`Backports` sources go in the build sandbox (``current`` dialect)."""
BACKPORTS_PINS = "/etc/apt/preferences.d/debian-backports.pref"
"""Where :class:`Backports` pins go in the build sandbox (``current`` dialect)."""
UNIT_DIRECTORY = "/usr/lib/systemd/system"

_OVERRIDABLE = (File, User, Group, Partition, Repository, Debloat)
"""Types an extending profile can redeclare: the fluent merge replaces them by identity."""
HISTORICAL = "nethermind-v1"
"""The dialect that reproduces the historical nethermind-tdx tree byte for byte."""
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
_BUILD_SOURCES = ("Build", "BuildSources")
"""Per-variant ``Setting``: ``src[:dest]`` host directories mounted into the build."""
_LIST_SETTINGS = (("Build", "Environment"), ("Build", "SandboxTrees"), _BUILD_SOURCES)
_TRUE = frozenset({"true", "yes", "1"})


class _Standalone(Exception):
    """The extending-profile merge cannot express a variant; lower it standalone.

    The message says why, completing "it ...".
    """


_FALSE = frozenset({"false", "no", "0"})


def lower(recipe: Recipe, *, variants: Sequence[str] | None = None) -> Image:
    """Build the compiler's image *recipe* describes, with *variants* (default: all).

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
    extra: dict[str, Any] = {} if recipe.policy is None else {"policy": recipe.policy}
    img = Image(
        base=recipe.base,
        arch=recipe.arch,
        mirror=recipe.mirror,
        tools_tree_mirror=recipe.tools_mirror,
        snapshot=recipe.snapshot,
        reproducible=recipe.epoch is not None,
        default_profile=default.name,
        mkosi=options[default.name],
        kernel=kernels[default.name],
        **extra,
    )
    strip = recipe.mkosi.strip_os_release
    if strip is not None and strip != (recipe.epoch is not None):
        img.strip_image_version(enabled=strip)

    with img.profiles(default.name):
        _declare(img, base.items, full=base.items, dialect=dialect, skip=backports, scripts=scripts)
    _apply_target(img, default.name, base.targets, inherited=(DEFAULT_TARGET,))

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
                img.profile_mkosi[variant.name] = options[variant.name]
            if kernels[variant.name] != kernels[default.name]:
                img.profile_kernels[variant.name] = kernels[variant.name]
            img.profile(variant.name, extends=None)
            with img.profiles(variant.name):
                _declare(
                    img,
                    child.items,
                    full=child.items,
                    dialect=dialect,
                    skip=backports,
                    scripts=scripts,
                )
                if not any(isinstance(i, Debloat) for i in child.items) and any(
                    isinstance(i, Debloat) for i in base.items
                ):
                    img.debloat()  # standalone profiles otherwise fall back to the default's
            _apply_target(img, variant.name, child.targets, inherited=None)
            continue
        img.profile(variant.name, extends=default.name)
        with img.profiles(variant.name):
            _declare(
                img,
                own,
                full=child.items,
                reemit=reemit,
                dialect=dialect,
                skip=backports,
                scripts=scripts,
            )
        _apply_target(img, variant.name, child.targets, inherited=base.targets)
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
    return img


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
        or (isinstance(item, Setting) and (item.section, item.key) != _BUILD_SOURCES)
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

    A fluent profile that extends another only adds, or replaces the types its
    merge overrides by identity; anything else raises.
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


def _declare(
    img: Image,
    items: Sequence[Declaration],
    *,
    full: Sequence[Declaration],
    reemit: Sequence[Unit] = (),
    dialect: str = "current",
    skip: Collection[Declaration] = (),
    scripts: Mapping[Declaration, str] | None = None,
) -> None:
    """Issue the fluent calls for *items* on the active profile.

    *full* is the whole resolved variant: it decides runtime-init wiring.
    Under the ``nethermind-v1`` dialect groups and users are postinst lines
    at their declaration position, spelled as the historical tree spells them.
    Hooks in *skip* keep their place in the hook order but are not emitted;
    hooks in *scripts* run the script they map to instead of their own.
    """
    historical = dialect == HISTORICAL
    after_init = has_init(full)
    hooks = iter(order_hooks([item for item in items if isinstance(item, Hook)]))
    runtime_done = False
    for item in items:
        match item:
            case Package(role="runtime"):
                img.install(item.name)
            case Package():
                img.build_packages(item.name)
            case File():
                _file(img, item)
            case Directory():
                _directory(img, item)
            case Group() if historical:
                img.shell(groupadd_line(item), phase="postinst")
            case User() if historical:
                img.shell(useradd_line(item), phase="postinst")
            case Group():
                img.group(item.name, system=item.system, gid=item.gid)
            case User():
                img.user(
                    item.name,
                    system=item.system,
                    home=item.home,
                    shell=item.shell,
                    uid=item.uid,
                    gid=item.primary_group,
                    groups=item.groups,
                )
            case Unit():
                _unit(img, item, after_init=after_init)
            case Service():
                _service(img, item)
            case Template():
                _template(img, item)
            case Setting() if (item.section, item.key) == _BUILD_SOURCES:
                for value in item.values:
                    source, _, dest = value.partition(":")
                    img.mount_build_source(source, dest=dest)
            case Hook():
                hook = next(hooks)
                if hook not in skip:
                    script = (scripts or {}).get(hook, hook.script)
                    img.shell(script, phase=hook.phase, env=dict(hook.env), cwd=hook.cwd)
            case Repository():
                img.repository(
                    item.url,
                    name=item.name,
                    suite=item.suite,
                    components=item.components,
                    keyring=item.keyring,
                    priority=item.priority,
                    in_image=item.in_image,
                )
            case Partition():
                img.partition(item.name, size=item.size, mount_at=item.mount, fs=item.filesystem)
            case Debloat():
                img.debloat(
                    enabled=item.enabled,
                    paths_remove=item.remove,
                    paths_skip=item.keep_paths,
                    extra_remove_paths=item.extra_remove,
                    paths_skip_for_profiles=dict(item.keep_paths_by_variant) or None,
                    systemd_minimize=item.minimize_systemd,
                    systemd_units_keep=item.keep_units,
                    extra_keep_units=item.keep_units_extra,
                    systemd_bins_keep=item.keep_binaries,
                )
            case Key() | Disk() | Secrets():
                if not runtime_done:
                    _runtime_tools(img, items)
                    runtime_done = True
            case Build():
                img.build_from(_source_build(item, mark_unpinned=not historical))
            case Kernel(source=Http()) if historical:
                img.build_packages("curl")  # the build hook downloads the archive
            case Init() | Setting() | Kernel() | RuntimeTools():
                pass  # inits register below; the rest is per-tree configuration
    for unit in reemit:
        _unit(img, replace(unit, enabled=None, masked=None), after_init=True)
    inits = order_inits(
        [item for item in items if isinstance(item, Init)], builtins=_builtin_inits(full)
    )
    for init in inits:
        img.runtime_init(init.script, priority=init.priority)


def _apply_target(
    img: Image, name: str, targets: Sequence[Target], *, inherited: Sequence[Target] | None
) -> None:
    """Set *targets* on profile *name* plus their platform integration, unless inherited.

    ``lower`` extends a cloud-targeted profile only with the same targets.
    """
    if inherited is not None and tuple(targets) == tuple(inherited):
        return
    assert inherited is None or not any(t in CLOUD_TARGETS for t in inherited)
    with img.profiles(name):
        img.targets(*targets)
        for target in targets:
            if target == "azure":
                img.apply(AzurePlatform())
            elif target == "gcp":
                img.apply(GcpPlatform())
        img.targets(*targets)  # platform integrations narrow the targets to their own


def _mode(mode: int) -> str:
    return f"{mode:04o}"


def _read(path: Path, *, owner: str) -> str | bytes:
    try:
        return _read_source(path)
    except OSError as exc:
        raise ValidationError(
            f"{owner}: cannot read {path}: {exc.strerror or exc}.",
            hint="Relative paths resolve against the working directory; check the file exists.",
            context={"path": str(path)},
        ) from exc


def _file(img: Image, item: File) -> None:
    content = item.content
    data = _read(content, owner=f"File {item.path}") if isinstance(content, Path) else content
    if item.stage == "skeleton":
        img.skeleton(item.path, content=data, mode=_mode(item.mode))
    else:
        img.file(item.path, content=data, mode=_mode(item.mode))


def _directory(img: Image, item: Directory) -> None:
    mode = None if item.mode is None else _mode(item.mode)
    if not item.source.is_dir():
        raise ValidationError(
            f"Directory {item.path}: source {item.source} is not an existing directory.",
            hint="Relative paths resolve against the working directory; check source= exists.",
            context={"path": item.path, "source": str(item.source)},
        )
    if item.stage == "extra":
        img.copy_tree(item.path, src=item.source, mode=mode, exclude=item.exclude)
        return
    found = _walk_tree(item.source, item.exclude)
    if not found:
        raise ValidationError(
            f"Directory {item.path}: no files to copy from {item.source}.",
            hint="Check source= and exclude=.",
        )
    prefix = item.path.rstrip("/")
    for rel, host in sorted(found):
        file_mode = mode or ("0755" if host.stat().st_mode & 0o111 else "0644")
        img.skeleton(f"{prefix}/{rel}", content=_read(host, owner="Directory"), mode=file_mode)


def _unit(img: Image, unit: Unit, *, after_init: bool) -> None:
    if unit.content is not None:
        content = unit.content
        if isinstance(content, Path):
            raw = _read(content, owner=f"Unit {unit.name}")
            if isinstance(raw, bytes):
                raise ValidationError(
                    f"Unit {unit.name}: {content} is not UTF-8 text.",
                    hint="Save the unit file as UTF-8; systemd units are text.",
                )
            content = raw
        if unit.after_init and after_init:
            content = inject_after_init(content)
        img.file(f"{UNIT_DIRECTORY}/{unit.name}", content=content)
    if unit.enabled is True:
        img.enable(unit.name)
    elif unit.enabled is False:
        img.disable(unit.name)
    if unit.masked:
        img.mask(unit.name)


def _service(img: Image, service: Service) -> None:
    img.service(
        service.name,
        command=service.exec_start,
        description=service.description,
        user=service.user,
        group=service.group,
        working_dir=service.working_dir,
        env=dict(service.env) or None,
        env_file=service.env_file,
        exec_start_pre=service.exec_start_pre,
        after=service.after,
        requires=service.requires,
        wants=service.wants,
        wanted_by=service.wanted_by,
        type=service.type,
        restart=service.restart,
        limits=dict(service.limits) or None,
        kill_mode=service.kill_mode,
        timeout_stop=service.timeout_stop,
        security_profile=service.security,
        after_init=service.after_init,
    )


def _template(img: Image, item: Template) -> None:
    source = item.template
    if isinstance(source, Path):
        raw = _read(source, owner=f"Template {item.path}")
        if isinstance(raw, bytes):
            raise ValidationError(
                f"Template {item.path}: {source} is not UTF-8 text.",
                hint="Save the template as UTF-8, or ship binary content with File().",
            )
        source = raw
    variables = dict(item.variables)
    if item.stage == "extra":
        img.template(item.path, template=source, variables=variables, mode=_mode(item.mode))
        return
    try:
        rendered = source.format_map({k: str(v) for k, v in variables.items()})
    except KeyError as exc:
        raise ValidationError(
            f"Template {item.path}: no value for placeholder {exc}.",
            hint="Add it to variables=, or write a literal brace as '{{' or '}}'.",
            context={"path": item.path},
        ) from exc
    img.skeleton(item.path, content=rendered, mode=_mode(item.mode))


def inject_after_init(text: str, service: str = INIT_SERVICE) -> str:
    """*text* with *service* first in its ``[Unit]`` ``After=`` and ``Requires=``.

    Existing directives get *service* prepended (what modules render through
    ``resolve_after``); missing ones are added after the last ``[Unit]`` line,
    ``After=`` before ``Requires=``.
    """
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == "[Unit]"), None)
    if start is None:
        return f"[Unit]\nAfter={service}\nRequires={service}\n\n{text}"
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )

    def find(directive: str) -> int | None:
        prefix = f"{directive}="
        return next((i for i in range(start + 1, end) if lines[i].startswith(prefix)), None)

    after, requires = find("After"), find("Requires")
    for index, directive in ((after, "After"), (requires, "Requires")):
        if index is None:
            continue
        values = lines[index][len(directive) + 1 :].split()
        if service not in values:
            lines[index] = f"{directive}={' '.join((service, *values))}"
    if after is None and requires is None:
        last = max(
            (i for i in range(start + 1, end) if lines[i].strip()),
            default=start,
        )
        lines[last + 1 : last + 1] = [f"After={service}", f"Requires={service}"]
    elif requires is None:
        assert after is not None
        lines.insert(after + 1, f"Requires={service}")
    elif after is None:
        lines.insert(requires, f"After={service}")
    return "\n".join(lines)


def _git(source: Git) -> GitSource:
    return GitSource(source.url, source.ref, subdir=source.subdir, submodules=source.submodules)


def groupadd_line(group: Group) -> str:
    """The ``nethermind-v1`` postinst line that creates *group*."""
    gid = "" if group.gid is None else f" --gid {group.gid}"
    return f"mkosi-chroot groupadd{' --system' if group.system else ''}{gid} {group.name}"


def useradd_line(user: User) -> str:
    """The ``nethermind-v1`` postinst line that creates *user* (no ``--create-home``)."""
    parts = ["mkosi-chroot useradd"]
    if user.system:
        parts.append("--system")
    if user.home is not None:
        parts.extend(("--home-dir", user.home))
    parts.extend(("--shell", user.shell))
    if user.uid is not None:
        parts.extend(("--uid", str(user.uid)))
    if user.primary_group is not None:
        parts.extend(("--gid", str(user.primary_group)))
    if user.groups:
        parts.extend(("--groups", ",".join(user.groups)))
    parts.append(user.name)
    return " ".join(parts)


def _source_build(build: Build, *, mark_unpinned: bool = True) -> SourceBuild:
    source: GitSource | HttpSource
    if isinstance(build.source, Git):
        source = _git(build.source)
    else:
        source = HttpSource(build.source.url, sha256=build.source.sha256)
    steps = tuple(
        FluentInstall.tree(step.source, step.destination)
        if step.directory
        else FluentInstall(
            "file", step.destination, step.source, None if step.mode is None else _mode(step.mode)
        )
        for step in build.install
    )
    recipe: BuildRecipe
    if build.recipe is not None:
        recipe = build.recipe
    else:
        assert build.script is not None  # Build allows exactly one of script and recipe
        recipe = ScriptBuild(
            script=build.script,
            output=build.install[0].source,
            packages=build.packages,
            env=dict(build.env),
        )
    return SourceBuild(
        name=build.name,
        source=source,
        build=recipe,
        install=steps,
        cache_key=build.cache_key,
        mark_unpinned=mark_unpinned,
    )


def _key_spec(key: Key) -> KeySpec:
    return KeySpec(
        key.name,
        strategy=key.strategy,
        output=key.output,
        size=key.size,
        pipe_path=key.pipe,
        persist_in_tpm=key.persist_in_tpm,
    )


def _disk_spec(disk: Disk) -> DiskSpec:
    key = disk.key
    common = {
        "device": disk.device,
        "mapper_name": disk.mapper,
        "mount_at": disk.mount,
        "format_policy": disk.format,
        "dirs": disk.directories,
    }
    if isinstance(key, Key) and key.output is None:
        return DiskSpec(disk.name, key_name=key.name, **common)  # type: ignore[arg-type]
    if isinstance(key, Key):
        return DiskSpec(disk.name, key=_key_spec(key), **common)  # type: ignore[arg-type]
    if isinstance(key, Path):
        return DiskSpec(disk.name, key_path=str(key), **common)  # type: ignore[arg-type]
    return DiskSpec(disk.name, **common)  # type: ignore[arg-type]


def _secret_spec(secret: Secret) -> SecretSpec:
    schema = secret.schema
    targets = tuple(
        SecretTarget.env(target.name, scope="global" if target.service is None else "service")
        if isinstance(target, SecretEnv)
        else SecretTarget.file(target.path, mode=_mode(target.mode), owner=target.owner)
        for target in secret.targets
    )
    return SecretSpec(
        name=secret.name,
        required=secret.required,
        schema=None
        if schema is None
        else SecretSchema(
            kind=schema.kind,
            min_length=schema.min_length,
            max_length=schema.max_length,
            pattern=schema.pattern,
            enum=schema.enum,
        ),
        targets=targets,
    )


def _runtime_tools(img: Image, items: Sequence[Declaration]) -> None:
    """Apply KeyGeneration, DiskEncryption and SecretDelivery for *items*, in that order."""
    tools = next((item for item in items if isinstance(item, RuntimeTools)), None)
    source = TUNDRA_TOOLS if tools is None else _git(tools.source)
    paths = tools or RuntimeTools(Git(TUNDRA_TOOLS.url, TUNDRA_TOOLS.ref))
    keys = [item for item in items if isinstance(item, Key)]
    disks = [item for item in items if isinstance(item, Disk)]
    secrets = [item for item in items if isinstance(item, Secrets)]
    secret_paths = _secret_paths(paths, secrets)
    if keys:
        img.apply(
            KeyGeneration(
                keys=tuple(_key_spec(k) for k in keys),
                config_path=paths.key_config,
                source=source,
            )
        )
    if disks:
        img.apply(
            DiskEncryption(
                disks=tuple(_disk_spec(d) for d in disks),
                config_path=paths.disk_config,
                source=source,
            )
        )
    for entry, (config_path, manifest_path) in zip(secrets, secret_paths, strict=True):
        img.apply(
            SecretDelivery(
                secrets=tuple(_secret_spec(s) for s in entry.entries),
                host=entry.host,
                port=entry.port,
                ssh_dir=entry.ssh_directory,
                key_path=entry.ssh_key_path,
                store_at=None if entry.store is None else entry.store.name,
                config_path=config_path,
                manifest_path=manifest_path,
                source=source,
            )
        )


def _secret_paths(tools: RuntimeTools, secrets: Sequence[Secrets]) -> list[tuple[str, str]]:
    """Config and manifest path of each of *secrets*.

    One ``Secrets`` uses the ``RuntimeTools`` paths; several get ``-<name>``
    appended to their stems (``/etc/tdx/secrets-api.yaml``). Raises when two
    paths coincide, or a file target is delivered by two of them.
    """
    if len(secrets) == 1:
        return [(tools.secret_config, tools.secret_manifest)]
    result = [
        (_suffixed(tools.secret_config, entry.name), _suffixed(tools.secret_manifest, entry.name))
        for entry in secrets
    ]
    owners: dict[str, str] = {tools.key_config: "RuntimeTools.key_config"}
    owners.setdefault(tools.disk_config, "RuntimeTools.disk_config")
    for entry, written in zip(secrets, result, strict=True):
        for path in written:
            if path in owners:
                raise ValidationError(
                    f"Secrets({entry.name}) writes {path}, which {owners[path]} also writes.",
                    hint="Rename one of them, or move the RuntimeTools paths apart.",
                    context={"path": path},
                )
            owners[path] = f"Secrets({entry.name})"
    delivered: dict[str, str] = {}
    for entry in secrets:
        for secret in entry.entries:
            for target in secret.targets:
                if isinstance(target, SecretEnv):
                    continue
                if delivered.setdefault(target.path, entry.name) != entry.name:
                    raise ValidationError(
                        f"Secrets({entry.name}) and Secrets({delivered[target.path]}) both "
                        f"deliver a secret to {target.path}.",
                        hint="Give each delivered file its own path.",
                        context={"path": target.path},
                    )
    return result


def _suffixed(path: str, name: str) -> str:
    """*path* with ``-<name>`` appended to its stem."""
    pure = PurePosixPath(path)
    return str(pure.with_name(f"{pure.stem}-{name}{pure.suffix}"))


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
                f"Kernel {kernel.version}: Http({source.url!r}) needs sha256=; the kernel "
                "archive is not a lockfile source, so its digest is pinned in the recipe.",
                hint="Pass Http(url, sha256=...), or use Git(url, ref).",
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
        if key == _BUILD_SOURCES:
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
