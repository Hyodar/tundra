"""The state builder: resolved declarations written straight into the compiler's ``RecipeState``.

:func:`~tundravm.declarative.lower.lower` decides how variants map onto
profiles (the default one, profiles that extend it, standalone ones);
:class:`StateBuilder` writes each profile's :class:`~tundravm.models.ProfileState`
from a variant's declarations, in declaration order. Hooks keep their order
within a phase, runtime-init steps their priorities, and the runtime tools
(keys, disks, secrets) are rendered by ``tundravm._modules`` and recorded per
profile for ``check()``.

Nothing here touches the network; ``Path`` contents are read here.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import stat
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

from tundravm._modules import DiskEncryption, DiskSpec, KeyGeneration, KeySpec, SecretDelivery
from tundravm._modules.base import TUNDRA_TOOLS, Module
from tundravm._modules.disk_encryption import (
    DISK_ENCRYPTION_BUILD_PACKAGES,
    DISK_ENCRYPTION_INIT_PRIORITY,
)
from tundravm._modules.key_generation import (
    KEY_GENERATION_BUILD_PACKAGES,
    KEY_GENERATION_INIT_PRIORITY,
)
from tundravm._modules.secret_delivery import (
    SECRET_DELIVERY_BUILD_PACKAGES,
    SECRET_DELIVERY_INIT_PRIORITY,
)
from tundravm._source import BuildRecipe, GitSource, HttpSource, ScriptBuild, SourceBuild
from tundravm._source import Install as SourceInstall
from tundravm.errors import ValidationError
from tundravm.models import (
    NETWORK_SETUP_UNIT,
    VALID_PHASES,
    CommandSpec,
    DebloatConfig,
    FileEntry,
    FileKind,
    GroupSpec,
    HookSpec,
    InitScriptEntry,
    OutputTarget,
    PartitionSpec,
    Phase,
    ProfileState,
    RecipeState,
    RepositorySpec,
    SecretSchema,
    SecretSpec,
    SecretTarget,
    ServiceSpec,
    TemplateEntry,
    UnitAction,
    UnitStateSpec,
    UserSpec,
    enable_unit,
    ships_unit,
    unit_name,
)
from tundravm.platforms import azure, gcp

from .model import (
    Build,
    Debloat,
    Declaration,
    Directory,
    Disk,
    File,
    Git,
    Group,
    Hook,
    Http,
    Init,
    Kernel,
    Key,
    Package,
    Partition,
    Repository,
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
)
from .resolve import CLOUD_TARGETS, _builtin_inits, has_init, order_hooks, order_inits

HISTORICAL = "nethermind-v1"
"""The dialect that reproduces the historical nethermind-tdx tree byte for byte."""
INIT_SERVICE = "runtime-init.service"
UNIT_DIRECTORY = "/usr/lib/systemd/system"
BUILD_SOURCES = ("Build", "BuildSources")
"""Per-variant ``Setting``: ``src[:dest]`` host directories mounted into the build."""
STRIP_IMAGE_VERSION = """sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release" """
"""The finalize hook that drops ``IMAGE_VERSION`` from os-release for reproducible attestation."""

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# systemd resource limits (``Limit<RESOURCE>=``), see systemd.exec(5).
_LIMIT_RESOURCES = frozenset(
    {
        "AS",
        "CORE",
        "CPU",
        "DATA",
        "FSIZE",
        "LOCKS",
        "MEMLOCK",
        "MSGQUEUE",
        "NICE",
        "NOFILE",
        "NPROC",
        "RSS",
        "RTPRIO",
        "RTTIME",
        "SIGPENDING",
        "STACK",
    }
)
_AZURE_UNIT = f"{UNIT_DIRECTORY}/azure-complete-provisioning.service"
_AZURE_POSTINST = (
    "mkosi-chroot systemctl enable azure-complete-provisioning.service",
    "mkosi-chroot mkdir -p /etc/systemd/system/minimal.target.wants",
    "mkosi-chroot ln -sf "
    "/usr/lib/systemd/system/azure-complete-provisioning.service "
    "/etc/systemd/system/minimal.target.wants/azure-complete-provisioning.service",
)


@dataclass(slots=True)
class StateBuilder:
    """Writes resolved declarations into :attr:`state`, one profile at a time.

    *historical* selects the ``nethermind-v1`` spelling: groups and users as
    postinst lines, source builds fetched in the build sandbox.
    :attr:`modules` holds the runtime-tool configurations applied to each
    profile, which ``check()`` inspects.
    """

    state: RecipeState
    historical: bool = False
    modules: dict[str, list[Module]] = field(default_factory=dict)

    # ── profiles ────────────────────────────────────────────────────────

    def strip_image_version(self, *, enabled: bool) -> None:
        """Add (or remove) the default profile's IMAGE_VERSION strip hook."""
        profile = self.state.ensure_profile(self.state.default_profile)
        if enabled:
            _shell(profile, STRIP_IMAGE_VERSION, phase="finalize")
            return
        if "finalize" in profile.phases:
            profile.phases["finalize"] = [
                cmd for cmd in profile.phases["finalize"] if "IMAGE_VERSION" not in cmd.argv[0]
            ]
            profile.hooks = [h for h in profile.hooks if not _is_strip_hook(h)]

    def extending(self, name: str) -> None:
        """Declare profile *name*, extending the default profile."""
        self.state.set_extends(name, self.state.default_profile)

    def standalone(self, name: str) -> None:
        """Declare profile *name* standalone; it gets the default profile's strip hook."""
        self.state.set_extends(name, None)
        default = self.state.ensure_profile(self.state.default_profile)
        profile = self.state.ensure_profile(name)
        strip = next((h for h in default.hooks if _is_strip_hook(h)), None)
        if strip is not None and not any(_is_strip_hook(h) for h in profile.hooks):
            profile.phases.setdefault("finalize", []).append(strip.command)
            profile.hooks.append(strip)

    def default_debloat(self, name: str) -> None:
        """Give standalone profile *name* the default debloat configuration."""
        _set_debloat(self.state.ensure_profile(name), _debloat_config())

    def targets(
        self, name: str, targets: Sequence[Target], *, inherited: Sequence[Target] | None
    ) -> None:
        """Set *targets* on profile *name* plus their platform integration, unless inherited.

        ``lower`` extends a cloud-targeted profile only with the same targets.
        """
        if inherited is not None and tuple(targets) == tuple(inherited):
            return
        assert inherited is None or not any(t in CLOUD_TARGETS for t in inherited)
        profile = self.state.ensure_profile(name)
        _set_targets(profile, targets)
        for target in targets:
            if target == "azure":
                self._azure(profile)
            elif target == "gcp":
                _gcp(profile)
        _set_targets(profile, targets)  # platform integrations narrow the targets to their own

    # ── declarations ────────────────────────────────────────────────────

    def declare(
        self,
        name: str,
        items: Sequence[Declaration],
        *,
        full: Sequence[Declaration],
        reemit: Sequence[Unit] = (),
        skip: Collection[Declaration] = (),
        scripts: Mapping[Declaration, str] | None = None,
    ) -> None:
        """Write *items* into profile *name*'s own state, in declaration order.

        *full* is the whole resolved variant: it decides runtime-init wiring.
        Under ``nethermind-v1`` groups and users are postinst lines at their
        declaration position, spelled as the historical tree spells them. Hooks
        in *skip* keep their place in the hook order but are not emitted; hooks
        in *scripts* run the script they map to instead of their own. *reemit*
        units are written again so they wait for runtime-init.
        """
        profile = self.state.ensure_profile(name)
        after_init = has_init(full)
        hooks = iter(order_hooks([item for item in items if isinstance(item, Hook)]))
        runtime_done = False
        for item in items:
            match item:
                case Package(role="runtime"):
                    profile.packages.add(item.name)
                case Package():
                    profile.build_packages.add(item.name)
                case File():
                    _file(profile, item)
                case Directory():
                    _directory(profile, item)
                case Group() if self.historical:
                    _shell(profile, groupadd_line(item), phase="postinst")
                case User() if self.historical:
                    _shell(profile, useradd_line(item), phase="postinst")
                case Group():
                    _group(profile, item)
                case User():
                    _user(profile, item)
                case Unit():
                    _unit(profile, item, after_init=after_init)
                case Service():
                    _service(profile, item)
                case Template():
                    _template(profile, item)
                case Setting() if (item.section, item.key) == BUILD_SOURCES:
                    for value in item.values:
                        source, _, dest = value.partition(":")
                        if not source:
                            raise ValidationError(
                                f"Setting Build.BuildSources: {value!r} names no host directory.",
                                hint="Write each value as 'src' or 'src:dest'.",
                            )
                        profile.build_sources.append((source, dest))
                case Hook():
                    hook = next(hooks)
                    if hook not in skip:
                        script = (scripts or {}).get(hook, hook.script)
                        _shell(profile, script, phase=hook.phase, env=dict(hook.env), cwd=hook.cwd)
                case Repository():
                    profile.repositories.append(_repository(item))
                case Partition():
                    profile.partitions.append(
                        PartitionSpec(
                            name=item.name, size=item.size, mount_at=item.mount, fs=item.filesystem
                        )
                    )
                case Debloat():
                    _set_debloat(profile, _debloat(item))
                case Key() | Disk() | Secrets():
                    if not runtime_done:
                        self._runtime_tools(name, profile, items)
                        runtime_done = True
                case Build():
                    self._build_from(profile, source_build(item, mark_unpinned=not self.historical))
                case Kernel(source=Http()) if self.historical:
                    profile.build_packages.add("curl")  # the build hook downloads the archive
                case Init() | Setting() | Kernel() | RuntimeTools():
                    pass  # inits register below; the rest is per-tree configuration
        for unit in reemit:
            _unit(profile, replace(unit, enabled=None, masked=None), after_init=True)
        inits = order_inits(
            [item for item in items if isinstance(item, Init)], builtins=_builtin_inits(full)
        )
        for init in inits:
            profile.init_scripts.append(InitScriptEntry(script=init.script, priority=init.priority))

    # ── source builds and runtime tools ─────────────────────────────────

    def _build_from(self, profile: ProfileState, spec: SourceBuild) -> None:
        """Record *spec* and its cached build hook (pinned through the lockfile at compile)."""
        if spec.name in profile.source_builds:
            raise ValidationError(
                f"Source build {spec.name!r} is already declared.",
                hint="Give each source build a unique name.",
                context={"profile": profile.name, "build_from": spec.name},
            )
        profile.build_packages.update(spec.packages)
        profile.source_builds[spec.name] = spec
        _shell(profile, spec.render(mounted=not self.historical), phase="build")

    def _runtime_tools(
        self, name: str, profile: ProfileState, items: Sequence[Declaration]
    ) -> None:
        """Configure key generation, disk setup and secret delivery for *items*, in that order."""
        tools = next((item for item in items if isinstance(item, RuntimeTools)), None)
        source = TUNDRA_TOOLS if tools is None else _git(tools.source)
        paths = tools or RuntimeTools(Git(TUNDRA_TOOLS.url, TUNDRA_TOOLS.ref))
        keys = [item for item in items if isinstance(item, Key)]
        disks = [item for item in items if isinstance(item, Disk)]
        secrets = [item for item in items if isinstance(item, Secrets)]
        secret_paths = _secret_paths(paths, secrets)
        applied = self.modules.setdefault(name, [])
        if keys:
            keygen = KeyGeneration(
                keys=tuple(_key_spec(k) for k in keys), config_path=paths.key_config, source=source
            )
            profile.build_packages.update(KEY_GENERATION_BUILD_PACKAGES)
            self._build_from(profile, keygen.source_spec())
            if any(spec.tpm_enabled() for spec in keygen.keys):
                profile.packages.add("tpm2-tools")
            _add_file(profile, keygen.config_path, keygen.render_config())
            _runtime_init(profile, keygen.init_script(), KEY_GENERATION_INIT_PRIORITY)
            applied.append(keygen)
        if disks:
            setup = DiskEncryption(
                disks=tuple(_disk_spec(d) for d in disks),
                config_path=paths.disk_config,
                source=source,
            )
            profile.build_packages.update(DISK_ENCRYPTION_BUILD_PACKAGES)
            self._build_from(profile, setup.source_spec())
            profile.packages.add("cryptsetup")
            _add_file(profile, setup.config_path, setup.render_config())
            _runtime_init(profile, setup.render_init_script(), DISK_ENCRYPTION_INIT_PRIORITY)
            applied.append(setup)
        for entry, (config_path, manifest_path) in zip(secrets, secret_paths, strict=True):
            delivery = SecretDelivery(
                secrets=tuple(_secret_spec(s) for s in entry.entries),
                host=entry.host,
                port=entry.port,
                ssh_dir=entry.ssh_directory,
                key_path=entry.ssh_key_path,
                store_at=None if entry.store is None else entry.store.name,
                config_path=config_path,
                manifest_path=manifest_path,
                source=source,
                env_suffix=None if len(secrets) == 1 else entry.name,
            )
            spec = delivery.source_spec()
            if spec.name not in profile.source_builds:  # a second delivery reuses the binary
                profile.build_packages.update(SECRET_DELIVERY_BUILD_PACKAGES)
                self._build_from(profile, spec)
            profile.secrets.extend(delivery.secrets)
            _add_file(profile, delivery.config_path, delivery.render_config())
            _add_file(profile, delivery.manifest_path, delivery.render_manifest())
            for path, dropin in delivery.service_dropins().items():
                _add_file(profile, path, dropin)
            _runtime_init(profile, delivery.init_script(), SECRET_DELIVERY_INIT_PRIORITY)
            applied.append(delivery)

    def _azure(self, profile: ProfileState) -> None:
        """The Azure integration: dmidecode, the provisioning script, its enabled unit."""
        profile.packages.add("dmidecode")
        _add_file(
            profile, "/usr/bin/azure-complete-provisioning", azure.AZURE_PROVISIONING_SCRIPT, "0755"
        )
        network_setup = self.historical or ships_unit(
            self.state.effective_profile(profile.name), NETWORK_SETUP_UNIT
        )
        unit = (
            azure.AZURE_PROVISIONING_SERVICE
            if network_setup
            else azure.AZURE_PROVISIONING_SERVICE_ONLINE
        )
        _add_file(profile, _AZURE_UNIT, unit)
        for line in _AZURE_POSTINST:
            _shell(profile, line, phase="postinst")
        _set_targets(profile, ("azure",))


def _gcp(profile: ProfileState) -> None:
    """The GCP integration: udev, metadata DNS, disk naming rules and their helper."""
    profile.packages.add("udev")
    _add_file(profile, "/etc/hosts", gcp.GCP_HOSTS)
    _add_file(profile, "/etc/resolv.conf", gcp.GCP_RESOLV_CONF)
    _add_file(profile, "/usr/lib/udev/rules.d/65-gce-disk-naming.rules", gcp.GCE_DISK_NAMING_RULES)
    _add_file(profile, "/usr/lib/udev/google_nvme_id", gcp.GOOGLE_NVME_ID, "0755")
    _set_targets(profile, ("gcp",))


# ── ProfileState writes ──────────────────────────────────────────────────


def _is_strip_hook(hook: HookSpec) -> bool:
    return hook.phase == "finalize" and "IMAGE_VERSION" in hook.command.argv[0]


def _shell(
    profile: ProfileState,
    command: str,
    *,
    phase: Phase,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> None:
    """Append *command* to *profile*'s *phase* scripts (``boot`` runs it at VM boot)."""
    if phase not in VALID_PHASES:
        raise ValidationError(
            f"Invalid phase {phase!r}.",
            hint=f"Expected one of: {', '.join(sorted(VALID_PHASES))}",
        )
    spec = CommandSpec(argv=(command,), env=dict(env or {}), cwd=cwd)
    profile.phases.setdefault(phase, []).append(spec)
    profile.hooks.append(HookSpec(phase=phase, command=spec))


def _runtime_init(profile: ProfileState, script: str, priority: int) -> None:
    profile.init_scripts.append(InitScriptEntry(script=script, priority=priority))


def _add_file(profile: ProfileState, path: str, content: str | bytes, mode: str = "0644") -> None:
    profile.files.append(FileEntry(path=path, content=content, mode=mode))


def _set_targets(profile: ProfileState, targets: Sequence[OutputTarget]) -> None:
    profile.output_targets = tuple(dict.fromkeys(targets))
    profile.output_targets_explicit = True


def _set_debloat(profile: ProfileState, config: DebloatConfig) -> None:
    profile.debloat = config
    profile.debloat_explicit = True


def _debloat_config(
    *,
    paths_remove: tuple[str, ...] | None = None,
    paths_skip: tuple[str, ...] = (),
    extra_remove_paths: tuple[str, ...] = (),
    paths_skip_for_profiles: Mapping[str, tuple[str, ...]] | None = None,
    systemd_minimize: bool = True,
    systemd_units_keep: tuple[str, ...] | None = None,
    extra_keep_units: tuple[str, ...] = (),
    systemd_bins_keep: tuple[str, ...] | None = None,
) -> DebloatConfig:
    """An enabled ``DebloatConfig``; unset (or empty) lists keep the defaults."""
    defaults = DebloatConfig()
    return DebloatConfig(
        enabled=True,
        paths_remove=paths_remove or defaults.paths_remove,
        paths_skip=tuple(paths_skip),
        extra_remove_paths=tuple(extra_remove_paths),
        paths_skip_for_profiles=tuple(sorted((paths_skip_for_profiles or {}).items())),
        systemd_minimize=systemd_minimize,
        systemd_units_keep=systemd_units_keep or defaults.systemd_units_keep,
        extra_keep_units=tuple(extra_keep_units),
        systemd_bins_keep=systemd_bins_keep or defaults.systemd_bins_keep,
    )


def _debloat(item: Debloat) -> DebloatConfig:
    if not item.enabled:
        return DebloatConfig(enabled=False)
    return _debloat_config(
        paths_remove=item.remove,
        paths_skip=item.keep_paths,
        extra_remove_paths=item.extra_remove,
        paths_skip_for_profiles=dict(item.keep_paths_by_variant),
        systemd_minimize=item.minimize_systemd,
        systemd_units_keep=item.keep_units,
        extra_keep_units=item.keep_units_extra,
        systemd_bins_keep=item.keep_binaries,
    )


def _repository(item: Repository) -> RepositorySpec:
    return RepositorySpec(
        name=item.name,
        url=item.url,
        suite=item.suite,
        components=tuple(item.components),
        keyring=item.keyring,
        priority=item.priority,
        in_image=item.in_image,
    )


def _group(profile: ProfileState, group: Group) -> None:
    if any(g.name == group.name for g in profile.groups):
        raise ValidationError(
            f"Duplicate group name '{group.name}' in variant '{profile.name}'.",
            hint="Group names must be unique within a profile.",
            context={"group": group.name, "profile": profile.name},
        )
    profile.groups.append(GroupSpec(name=group.name, system=group.system, gid=group.gid))


def _user(profile: ProfileState, user: User) -> None:
    if any(u.name == user.name for u in profile.users):
        raise ValidationError(
            f"Duplicate user name '{user.name}' in variant '{profile.name}'.",
            hint="User names must be unique within a profile.",
            context={"user": user.name, "profile": profile.name},
        )
    profile.users.append(
        UserSpec(
            name=user.name,
            system=user.system,
            home=user.home,
            shell=user.shell,
            uid=user.uid,
            gid=user.primary_group,
            groups=tuple(user.groups),
        )
    )


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


# ── files ────────────────────────────────────────────────────────────────


def read_source(path: Path) -> str | bytes:
    """Read *path* as UTF-8 text, or as raw bytes when it is not valid UTF-8."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _read(path: Path, *, owner: str) -> str | bytes:
    try:
        return read_source(path)
    except OSError as exc:
        raise ValidationError(
            f"{owner}: cannot read {path}: {exc.strerror or exc}.",
            hint="Relative paths resolve against the working directory; check the file exists.",
            context={"path": str(path)},
        ) from exc


def _read_text(path: Path, *, owner: str, hint: str) -> str:
    raw = _read(path, owner=owner)
    if isinstance(raw, bytes):
        raise ValidationError(f"{owner}: {path} is not UTF-8 text.", hint=hint)
    return raw


def _mode(mode: int) -> str:
    return f"{mode:04o}"


def walk_tree(root: Path, exclude: Sequence[str]) -> list[tuple[str, Path]]:
    """``(relative posix path, host path)`` of every file under *root* not *exclude*d.

    *exclude* holds fnmatch globs matched against the relative path (``*`` also
    matches ``/``); a matching directory is skipped whole. Symlinked directories
    are not followed.
    """
    patterns = (exclude,) if isinstance(exclude, str) else tuple(exclude)

    def excluded(rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)

    found: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(root):
        base = Path(current).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not excluded((base / d).as_posix()))
        for filename in sorted(filenames):
            rel = (base / filename).as_posix()
            if not excluded(rel) and (Path(current) / filename).is_file():
                found.append((rel, Path(current) / filename))
    return found


def _file(profile: ProfileState, item: File) -> None:
    content = item.content
    data = _read(content, owner=f"File {item.path}") if isinstance(content, Path) else content
    entries = profile.skeleton_files if item.stage == "skeleton" else profile.files
    entries.append(FileEntry(path=item.path, content=data, mode=_mode(item.mode)))


def walk_directory(
    root: Path, exclude: Sequence[str], *, follow: bool
) -> list[tuple[str, Path, FileKind]]:
    """``(relative posix path, host path, kind)`` of what ``Directory`` imports from *root*.

    Files, directories empty on the host, and (unless *follow*) symlinks, which
    are not descended into. *follow* reads through symlinks and descends into
    linked directories, each real directory once. *exclude* is as for
    :func:`walk_tree`.
    """
    patterns = tuple(exclude)

    def excluded(rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)

    found: list[tuple[str, Path, FileKind]] = []
    seen: set[Path] = set()
    for current, dirnames, filenames in os.walk(root, followlinks=follow):
        here = Path(current)
        if follow:
            real = here.resolve()
            if real in seen:
                dirnames[:] = []
                continue
            seen.add(real)
        base = here.relative_to(root)
        if base != Path(".") and not dirnames and not filenames:
            found.append((base.as_posix(), here, "directory"))
        kept = sorted(d for d in dirnames if not excluded((base / d).as_posix()))
        links = [] if follow else [d for d in kept if (here / d).is_symlink()]
        dirnames[:] = [d for d in kept if d not in links]
        for name in sorted(
            {*links, *(f for f in filenames if not excluded((base / f).as_posix()))}
        ):
            path = here / name
            rel = (base / name).as_posix()
            if not follow and path.is_symlink():
                found.append((rel, path, "symlink"))
            elif path.is_file():
                found.append((rel, path, "file"))
            elif path.is_symlink():
                raise ValidationError(
                    f"Directory: symlink {path} points to nothing to follow.",
                    hint="Fix the link, exclude it, or pass symlinks='preserve' to ship it as is.",
                    context={"path": str(path)},
                )
    return sorted(found)


def _directory(profile: ProfileState, item: Directory) -> None:
    """What lies under ``item.source``, at ``item.path`` (see :class:`Directory`).

    Without *mode* each file and empty directory keeps its permission bits;
    with it every file gets *mode* and every empty directory 0755.
    """
    if not item.source.is_dir():
        raise ValidationError(
            f"Directory {item.path}: source {item.source} is not an existing directory.",
            hint="Relative paths resolve against the working directory; check source= exists.",
            context={"path": item.path, "source": str(item.source)},
        )
    found = walk_directory(item.source, item.exclude, follow=item.symlinks == "follow")
    if not found:
        raise ValidationError(
            f"Directory {item.path}: no files to copy from {item.source}.",
            hint="Check source= and exclude=.",
        )
    entries = profile.skeleton_files if item.stage == "skeleton" else profile.files
    prefix = item.path.rstrip("/")
    for rel, host, kind in found:
        path = f"{prefix}/{rel}"
        if kind == "symlink":
            entries.append(FileEntry(path, os.readlink(host), "0777", kind="symlink"))
            continue
        kept = _mode(stat.S_IMODE(host.stat().st_mode))
        if kind == "directory":
            mode = kept if item.mode is None else "0755"
            entries.append(FileEntry(path, b"", mode, kind="directory"))
            continue
        mode = kept if item.mode is None else _mode(item.mode)
        entries.append(FileEntry(path, _read(host, owner="Directory"), mode))


def _template(profile: ProfileState, item: Template) -> None:
    source = item.template
    if isinstance(source, Path):
        source = _read_text(
            source,
            owner=f"Template {item.path}",
            hint="Save the template as UTF-8, or ship binary content with File().",
        )
    variables = {k: str(v) for k, v in sorted(item.variables)}
    try:
        rendered = source.format_map(variables)
    except KeyError as exc:
        raise ValidationError(
            f"Template {item.path}: no value for placeholder {exc}.",
            hint="Add it to variables=, or write a literal brace as '{{' or '}}'.",
            context={"path": item.path},
        ) from exc
    mode = _mode(item.mode)
    if item.stage == "extra":
        profile.templates.append(
            TemplateEntry(
                path=item.path,
                template=source,
                variables=variables,
                rendered=rendered,
                mode=mode,
            )
        )
    else:
        profile.skeleton_files.append(FileEntry(path=item.path, content=rendered, mode=mode))


# ── units and services ───────────────────────────────────────────────────


def _unit(profile: ProfileState, unit: Unit, *, after_init: bool) -> None:
    if unit.content is not None:
        content = unit.content
        if isinstance(content, Path):
            content = _read_text(
                content,
                owner=f"Unit {unit.name}",
                hint="Save the unit file as UTF-8; systemd units are text.",
            )
        if unit.after_init and after_init:
            content = inject_after_init(content)
        _add_file(profile, f"{UNIT_DIRECTORY}/{unit.name}", content)
    if unit.enabled is True:
        enable_unit(profile, unit.name)
    elif unit.enabled is False:
        _unit_state(profile, "disable", unit.name)
    if unit.masked:
        _unit_state(profile, "mask", unit.name)


def _unit_state(profile: ProfileState, action: UnitAction, unit: str) -> None:
    spec = UnitStateSpec(action=action, unit=unit_name(unit))
    if spec not in profile.unit_states:
        profile.unit_states.append(spec)


def _service(profile: ProfileState, service: Service) -> None:
    name = service.name
    limits = _limits(name, service.limits)
    for key, value in service.env:
        if not _ENV_KEY.fullmatch(key):
            raise ValidationError(
                f"Invalid environment variable name {key!r} for service '{name}'.",
                hint="Use letters, digits and underscores, not starting with a digit.",
            )
        if "\n" in value:
            raise ValidationError(
                f"Environment value for {key!r} in service '{name}' contains a newline.",
                hint="Use env_file= for multi-line values.",
            )
    if any(s.name == name for s in profile.services):
        raise ValidationError(
            f"Duplicate service name '{name}' in variant '{profile.name}'.",
            hint="Service names must be unique within a profile.",
            context={"service": name, "profile": profile.name},
        )
    command, pre = service.exec_start, service.exec_start_pre
    profile.services.append(
        ServiceSpec(
            name=name,
            command=tuple(shlex.split(command)) if isinstance(command, str) else tuple(command),
            user=service.user,
            after=tuple(service.after),
            requires=tuple(service.requires),
            wants=tuple(dict.fromkeys(service.wants)),
            restart=service.restart,
            enabled=True,
            extra_unit={},
            security_profile=service.security,
            description=service.description or None,
            env=dict(service.env),
            env_file=service.env_file or None,
            working_dir=service.working_dir or None,
            exec_start_pre=(pre,) if isinstance(pre, str) else tuple(pre),
            group=service.group or None,
            wanted_by=service.wanted_by or None,
            type=service.type,
            limits=limits,
            kill_mode=service.kill_mode,
            timeout_stop=service.timeout_stop or None,
            after_init=service.after_init,
        )
    )


def _limits(service: str, limits: Sequence[tuple[str, str | int]]) -> dict[str, str]:
    """``(("NOFILE", 1048576),)`` (or ``LimitNOFILE``) as ``{"NOFILE": "1048576"}``, sorted."""
    normalized: dict[str, str] = {}
    for key, value in limits:
        resource = key.removeprefix("Limit").upper()
        if resource not in _LIMIT_RESOURCES:
            raise ValidationError(
                f"Unknown resource limit {key!r} for service '{service}'.",
                hint=f"Expected one of: {', '.join(sorted(_LIMIT_RESOURCES))}.",
            )
        text = str(value)
        if not text or any(ch.isspace() for ch in text):
            raise ValidationError(
                f"Invalid value {value!r} for limit {key!r} in service '{service}'.",
                hint="Use a number, 'infinity', or soft:hard.",
            )
        normalized[resource] = text
    return dict(sorted(normalized.items()))


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


# ── source builds and runtime-tool specs ─────────────────────────────────


def _git(source: Git) -> GitSource:
    return GitSource(source.url, source.ref, subdir=source.subdir, submodules=source.submodules)


def source_build(build: Build, *, mark_unpinned: bool = True) -> SourceBuild:
    """The compiler's source build for *build*."""
    source: GitSource | HttpSource
    if isinstance(build.source, Git):
        source = _git(build.source)
    else:
        source = HttpSource(build.source.url, sha256=build.source.sha256)
    steps = tuple(
        SourceInstall.tree(step.source, step.destination)
        if step.directory
        else SourceInstall(
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
        SecretTarget.env(
            target.name,
            scope="global" if target.unit is None else "service",
            service=target.unit,
        )
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


__all__ = [
    "HISTORICAL",
    "INIT_SERVICE",
    "StateBuilder",
    "groupadd_line",
    "inject_after_init",
    "read_source",
    "source_build",
    "useradd_line",
    "walk_directory",
    "walk_tree",
]
