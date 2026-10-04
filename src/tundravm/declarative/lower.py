"""Lowering: a declarative :class:`Recipe` as the fluent :class:`~tundravm.Image`.

The lowered image holds the same ``RecipeState`` the equivalent fluent calls
build, so compile, lint, diff, lock, bake and explain work on it unchanged.

Variants map onto compiler profiles. The default variant (the one named
``default``, else the first root variant) is the default profile and receives
every declaration it resolves to. A variant whose parent is ``base`` or the
default variant becomes a profile that extends the default one and declares
only what it adds (or replaces, for types the profile merge overrides by
identity). Every other variant (parentless, chained onto another variant, or
removing/replacing what the extending merge cannot express) is lowered
standalone from its own resolved declarations.

Lowering is deterministic and never touches the network; ``Path`` contents
are read here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

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
    Git,
    Group,
    Hook,
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

INIT_SERVICE = "runtime-init.service"
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
_BUILD_SOURCES = ("Build", "BuildSources")
"""Per-variant ``Setting``: ``src[:dest]`` host directories mounted into the build."""
_TRUE = frozenset({"true", "yes", "1"})


class _Standalone(Exception):
    """The extending-profile merge cannot express a variant; lower it standalone."""


_FALSE = frozenset({"false", "no", "0"})


def lower(recipe: Recipe, *, variants: Sequence[str] | None = None) -> Image:
    """Build the compiler's image *recipe* describes, with *variants* (default: all).

    The default variant is always lowered: the other profiles build on it.
    Raises ``ValidationError`` for an unresolvable recipe and for variants that
    change recipe-wide declarations (settings other than ``BuildSources``, the
    kernel).
    """
    default = _default_variant(recipe)
    selected = _selected(recipe, variants, default)
    resolved = {variant.name: resolve(recipe, variant=variant.name) for variant in selected}
    base = resolved[default.name]

    recipe_wide = [item for item in base.items if _recipe_wide(item)]
    for variant in selected[1:]:
        _check_recipe_wide(resolved[variant.name], recipe_wide)
    kernel = next((item for item in recipe_wide if isinstance(item, Kernel)), None)
    extra: dict[str, Any] = {} if recipe.policy is None else {"policy": recipe.policy}
    img = Image(
        base=recipe.base,
        arch=recipe.arch,
        mirror=recipe.mirror,
        tools_tree_mirror=recipe.tools_mirror,
        reproducible=recipe.epoch is not None,
        default_profile=default.name,
        mkosi=_mkosi_options(recipe, [s for s in recipe_wide if isinstance(s, Setting)]),
        kernel=None if kernel is None else _kernel(kernel),
        **extra,
    )
    strip = recipe.mkosi.strip_os_release
    if strip is not None and strip != (recipe.epoch is not None):
        img.strip_image_version(enabled=strip)

    dialect = recipe.mkosi.dialect
    with img.profiles(default.name):
        _declare(img, base.items, full=base.items, dialect=dialect)
    _apply_target(img, default.name, base.targets, inherited=(DEFAULT_TARGET,))

    for variant in selected[1:]:
        child = resolved[variant.name]
        try:
            if variant.parent not in (BASE_PARENT, default.name):
                raise _Standalone
            own, reemit = _overlay(base, child, dialect=dialect)
        except _Standalone:
            if recipe.mkosi.layout == "native":
                raise ValidationError(
                    f"Variant {variant.name!r} is not a pure overlay of the default variant "
                    f"{default.name!r} (it is parentless, chained, or removes or replaces "
                    "declarations), and the native layout builds every variant on top of the "
                    "default one.",
                    hint="Use Mkosi(layout='directories') for this recipe.",
                    context={"variant": variant.name},
                ) from None
            img.profile(variant.name, extends=None)
            with img.profiles(variant.name):
                _declare(img, child.items, full=child.items, dialect=dialect)
                if not any(isinstance(i, Debloat) for i in child.items) and any(
                    isinstance(i, Debloat) for i in base.items
                ):
                    img.debloat()  # standalone profiles otherwise fall back to the default's
            _apply_target(img, variant.name, child.targets, inherited=None)
            continue
        img.profile(variant.name, extends=default.name)
        with img.profiles(variant.name):
            _declare(img, own, full=child.items, reemit=reemit, dialect=dialect)
        _apply_target(img, variant.name, child.targets, inherited=base.targets)
    return img


def _recipe_wide(item: Declaration) -> bool:
    if isinstance(item, Setting):
        return (item.section, item.key) != _BUILD_SOURCES
    return isinstance(item, Kernel)


def _default_variant(recipe: Recipe) -> Variant:
    named = [v for v in recipe.variants if v.name == "default"]
    roots = [v for v in recipe.variants if v.parent in (BASE_PARENT, None)]
    variant = named[0] if named else (roots[0] if roots else recipe.variants[0])
    if variant.parent not in (BASE_PARENT, None):
        raise ValidationError(
            f"The default variant {variant.name!r} must have parent {BASE_PARENT!r} or None.",
            context={"variant": variant.name},
        )
    return variant


def _selected(recipe: Recipe, names: Sequence[str] | None, default: Variant) -> list[Variant]:
    if names is None:
        chosen = list(recipe.variants)
    else:
        chosen = [recipe.variant(name) for name in dict.fromkeys(names)]
    return [default, *(v for v in chosen if v.name != default.name)]


def _check_recipe_wide(child: Resolved, recipe_wide: Sequence[Declaration]) -> None:
    known = {identity(item): item for item in recipe_wide}
    present = {identity(item) for item in child.items if _recipe_wide(item)}
    missing = [describe(key) for key in known if key not in present]
    if missing:
        raise ValidationError(
            f"Variant {child.variant!r} removes {', '.join(missing)}; settings and the kernel "
            "are recipe-wide.",
            hint="Declare them in Recipe.common (or the default variant).",
            context={"variant": child.variant},
        )
    for item in child.items:
        if _recipe_wide(item) and known.get(identity(item)) != item:
            raise ValidationError(
                f"Variant {child.variant!r} declares its own {describe(identity(item))}; "
                "settings and the kernel are recipe-wide.",
                hint="Declare them in Recipe.common (or the default variant).",
                context={"variant": child.variant},
            )


def _overlay(
    base: Resolved, child: Resolved, *, dialect: str = "current"
) -> tuple[list[Declaration], list[Unit]]:
    """What *child* declares on top of *base* (its extended profile), plus units to re-emit.

    A fluent profile that extends another only adds, or replaces the types its
    merge overrides by identity; anything else raises.
    """
    inherited = {identity(item): item for item in base.items}
    present = {identity(item) for item in child.items}
    if any(key not in present for key in inherited):
        raise _Standalone  # the merge only adds
    own: list[Declaration] = []
    for item in child.items:
        previous = inherited.get(identity(item))
        if previous is None:
            own.append(item)
        elif previous != item:
            if not isinstance(item, _OVERRIDABLE) or (
                dialect == HISTORICAL and isinstance(item, (Group, User))
            ):
                raise _Standalone  # the merge cannot replace this type
            own.append(item)
    if any(isinstance(item, _RUNTIME) for item in own) and any(
        isinstance(item, (Key, Disk, Secrets)) for item in base.items
    ):
        raise _Standalone  # runtime tools are configured once per profile
    if any(isinstance(item, Setting) for item in own):
        raise _Standalone  # build-source mounts merge per profile
    if any(isinstance(item, Debloat) and item.keep_paths_by_variant for item in own):
        raise _Standalone
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
) -> None:
    """Issue the fluent calls for *items* on the active profile.

    *full* is the whole resolved variant: it decides runtime-init wiring.
    Under the ``nethermind-v1`` dialect groups and users are postinst lines
    at their declaration position, spelled as the historical tree spells them.
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
                img.shell(hook.script, phase=hook.phase, env=dict(hook.env), cwd=hook.cwd)
            case Repository():
                img.repository(
                    item.url,
                    name=item.name,
                    suite=item.suite,
                    components=item.components,
                    keyring=item.keyring,
                    priority=item.priority,
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
            case Init() | Setting() | Kernel() | RuntimeTools():
                pass  # inits register below; the rest is recipe-wide or configuration
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
    """Set *targets* on profile *name* plus their platform integration, unless inherited."""
    if inherited is not None and tuple(targets) == tuple(inherited):
        return
    if inherited is not None and any(t in CLOUD_TARGETS for t in inherited):
        raise ValidationError(
            f"Variant {name!r} targets {', '.join(targets)} but extends a "
            f"{', '.join(inherited)} profile.",
            context={"variant": name},
        )
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
                raise ValidationError(f"Unit {unit.name}: {content} is not UTF-8 text.")
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
            raise ValidationError(f"Template {item.path}: {source} is not UTF-8 text.")
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
    if len(secrets) > 1:
        raise ValidationError(
            f"Only one Secrets declaration per variant lowers; got "
            f"{', '.join(s.name for s in secrets)}.",
        )
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
    for entry in secrets:
        img.apply(
            SecretDelivery(
                secrets=tuple(_secret_spec(s) for s in entry.entries),
                host=entry.host,
                port=entry.port,
                ssh_dir=entry.ssh_directory,
                key_path=entry.ssh_key_path,
                store_at=None if entry.store is None else entry.store.name,
                config_path=paths.secret_config,
                manifest_path=paths.secret_manifest,
                source=source,
            )
        )


def _kernel(kernel: Kernel) -> FluentKernel:
    source = kernel.source
    if (
        not isinstance(source, Git)
        or source.ref != f"v{kernel.version}"
        or source.subdir is not None
        or source.submodules
    ):
        raise ValidationError(
            f"Kernel {kernel.version}: the compiler clones tag v{kernel.version} of a git "
            f"repository; {source!r} does not lower yet.",
            hint=f"Use Git(url, 'v{kernel.version}').",
        )
    return FluentKernel(
        version=kernel.version,
        config_file=kernel.config,
        cmdline=kernel.cmdline or None,
        tdx=kernel.tdx,
        source_repo=source.url,
    )


def _setting_bool(setting: Setting, value: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValidationError(f"Setting {setting.section}.{setting.key}: {value!r} is not a boolean.")


def _mkosi_options(recipe: Recipe, settings: Sequence[Setting]) -> MkosiOptions:
    """``MkosiOptions`` for the recipe-wide fields and *settings*."""
    config = recipe.mkosi
    changes: dict[str, Any] = {
        "emit_mode": "native_profiles" if config.layout == "native" else "per_directory",
        "init_script": config.init_script,
        "generate_version_script": config.version_script,
        "generate_cloud_postoutput": config.cloud_postoutput,
    }
    environment: dict[str, str] = {}
    passthrough: list[str] = []
    if recipe.epoch:
        environment["SOURCE_DATE_EPOCH"] = str(recipe.epoch)
    for setting in settings:
        key = (setting.section, setting.key)
        if key == ("Build", "Environment"):
            for value in setting.values:
                name, sep, assigned = value.partition("=")
                if sep:
                    environment[name] = assigned
                else:
                    passthrough.append(name)
            continue
        if key == ("Build", "SandboxTrees"):
            changes["sandbox_trees"] = setting.values
            continue
        field = _SINGLE_SETTINGS.get(key) or _BOOL_SETTINGS.get(key)
        if field is None:
            known = sorted(
                f"{s}.{k}"
                for s, k in (*_SINGLE_SETTINGS, *_BOOL_SETTINGS, ("Build", "Environment"))
            )
            raise ValidationError(
                f"Setting {setting.section}.{setting.key} has no compiler mapping.",
                hint=f"Supported: {', '.join(known)}, Build.SandboxTrees, Build.BuildSources. "
                "Mirrors, the epoch, the layout and the generated helper files are Recipe "
                "and Mkosi fields.",
            )
        if len(setting.values) != 1:
            raise ValidationError(
                f"Setting {setting.section}.{setting.key} takes exactly one value.",
            )
        (value,) = setting.values
        changes[field] = _setting_bool(setting, value) if key in _BOOL_SETTINGS else value
    if environment:
        changes["environment"] = environment
    if passthrough:
        changes["environment_passthrough"] = tuple(passthrough)
    return MkosiOptions(**changes)


__all__ = [
    "HISTORICAL",
    "INIT_SERVICE",
    "groupadd_line",
    "inject_after_init",
    "lower",
    "useradd_line",
]
