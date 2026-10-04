"""Internal lowering target; not a public API.

``tundravm.declarative.lower`` turns a declarative ``Recipe`` into an
:class:`Image` (every variant a profile) and the lifecycle compiles, locks and
bakes it. Nothing outside ``tundravm`` should build one by hand.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Self

from ._modules.base import Module
from ._modules.init import Init
from ._options import MkosiOptions
from ._source import Resolver, SourceBuild, source_drift
from .backends.base import BuildBackend
from .check import check as run_checks
from .compiler import (
    PHASE_ORDER,
    EmitConfig,
    MkosiEmission,
    emit_mkosi_tree,
)
from .errors import (
    LintError,
    LockfileError,
    PolicyError,
    ValidationError,
)
from .lockfile import (
    LockDrift,
    LockedFetch,
    Lockfile,
    compare_lock,
    read_lockfile,
    recipe_digest,
    unselected_sources,
)
from .models import (
    VALID_PHASES,
    Arch,
    ArtifactRef,
    BakeRequest,
    BakeResult,
    CommandSpec,
    CompileResult,
    DebloatConfig,
    FileEntry,
    GroupSpec,
    HookSpec,
    InitScriptEntry,
    Kernel,
    KillMode,
    OutputTarget,
    PartitionSpec,
    Phase,
    ProfileBuildResult,
    ProfileState,
    RecipeState,
    RepositorySpec,
    RestartPolicy,
    SecurityProfile,
    ServiceSpec,
    ServiceType,
    TemplateEntry,
    UnitAction,
    UnitStateSpec,
    UserSpec,
    unit_name,
)
from .observability import (
    Progress,
    Reporter,
    StructuredLogger,
    display_path,
    format_duration,
    format_size,
)
from .policy import Policy, ensure_bake_policy

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]*\$?")
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


class _Unset(Enum):
    TOKEN = 0


_UNSET: Final = _Unset.TOKEN
_DRIFT_MESSAGE_LINES = 15


def _read_source(path: Path) -> str | bytes:
    """Read *path* as UTF-8 text, or as raw bytes when it is not valid UTF-8."""
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data


def _normalize_limits(service: str, limits: Mapping[str, str | int] | None) -> dict[str, str]:
    """``{"NOFILE": 1048576}`` (or ``LimitNOFILE``) as ``{"NOFILE": "1048576"}``."""
    normalized: dict[str, str] = {}
    for key, value in (limits or {}).items():
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


def _walk_tree(root: Path, exclude: Sequence[str]) -> list[tuple[str, Path]]:
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


def _short_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _is_strip_hook(hook: HookSpec) -> bool:
    return hook.phase == "finalize" and "IMAGE_VERSION" in hook.command.argv[0]


def _init_scripts_payload(entries: Sequence[InitScriptEntry]) -> list[dict[str, object]]:
    return [
        {"priority": entry.priority, "sha256": hashlib.sha256(entry.script.encode()).hexdigest()}
        for entry in sorted(entries, key=lambda item: (item.priority, item.script))
    ]


def _validate_units(action: str, units: tuple[str, ...]) -> tuple[str, ...]:
    if not units:
        raise ValidationError(
            f"{action}() requires at least one unit.",
            hint="Name at least one systemd unit, e.g. 'ssh.service'.",
        )
    for unit in units:
        if not unit or not _UNIT_NAME.fullmatch(unit):
            raise ValidationError(
                f"Invalid unit name {unit!r} for {action}().",
                hint="Pass systemd unit names such as 'ssh.service' or 'ssh.socket'.",
            )
    return tuple(dict.fromkeys(units))


@dataclass(slots=True, kw_only=True)
class Image:
    """The lowered recipe: per-profile state the compiler emits as an mkosi tree.

    ``declarative.lower`` fills it through the declaration methods (``install``,
    ``file``, ``service``, ``apply`` for the internal modules, ...) inside
    ``profiles(name)`` blocks; ``compile()`` emits the tree and ``bake()`` runs
    the backend. mkosi-only knobs live in :class:`~tundravm._options.MkosiOptions`.
    """

    base: str = "debian/bookworm"
    arch: Arch = "x86_64"
    backend: BuildBackend | None = None
    build_dir: Path = field(default_factory=lambda: Path("build"))
    reproducible: bool = True
    policy: Policy = field(default_factory=Policy)
    kernel: Kernel | None = None
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    default_profile: str = "default"
    mkosi: MkosiOptions = field(default_factory=MkosiOptions)
    profile_mkosi: dict[str, MkosiOptions] = field(default_factory=dict)
    """Profiles whose mkosi options differ from ``mkosi``."""
    profile_kernels: dict[str, Kernel | None] = field(default_factory=dict)
    """Profiles whose kernel differs from ``kernel`` (``None``: no kernel)."""
    lock_file: Path | None = None
    """The lockfile to read instead of ``<build_dir>/tundravm.lock``."""
    logger: StructuredLogger = field(init=False, default_factory=StructuredLogger, repr=False)
    init: Init = field(init=False, default_factory=Init, repr=False)
    _state: RecipeState = field(init=False, repr=False)
    _active_profiles: tuple[str, ...] = field(init=False, repr=False)
    _modules: dict[str, list[Module]] = field(init=False, default_factory=dict, repr=False)
    _last_compile_digest: str | None = field(init=False, default=None, repr=False)
    _last_compile_path: Path | None = field(init=False, default=None, repr=False)
    _last_compile_emission: MkosiEmission | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self.build_dir = Path(self.build_dir)
        self._state = RecipeState.initialize(
            base=self.base,
            arch=self.arch,
            default_profile=self.default_profile,
        )
        self._active_profiles = (self.default_profile,)
        if self.reproducible:
            self.strip_image_version()

    @property
    def state(self) -> RecipeState:
        return self._state

    @property
    def profile_names(self) -> tuple[str, ...]:
        """Every profile declared so far, sorted."""
        return tuple(sorted(self._state.profiles))

    def apply(self, *modules: Module) -> Self:
        """Apply one or more modules to the active profiles, in order.

        Equivalent to ``module.apply(self)`` for each module, but chainable::

            img.apply(KeyGeneration(), DiskEncryption(), Tdxs())

        Each module records itself in ``applied_modules()``.
        """
        if not modules:
            raise ValidationError(
                "apply() requires at least one module.",
                hint="Pass at least one module instance.",
            )
        for module in modules:
            if not isinstance(module, Module):
                raise ValidationError(
                    f"{type(module).__name__} is not a module.",
                    hint="Subclass tundravm._modules.Module; see docs/module-authoring.md.",
                )
            module.apply(self)
        return self

    def applied_modules(
        self, profile: str | None = None, *, inherited: bool = False
    ) -> tuple[Module, ...]:
        """``Module`` instances applied to *profile* (default: the active profile), in order.

        With ``inherited=True`` the modules of the profile it extends come first.
        """
        selected = self._resolve_operation_profile(profile)
        own = tuple(self._modules.get(selected, ()))
        extends = self._state.ensure_profile(selected).extends
        if not inherited or extends is None:
            return own
        base = tuple(self._modules.get(extends, ()))
        return base + tuple(m for m in own if not any(m is b for b in base))

    def profile(self, name: str, *, extends: str | None | Literal[_Unset.TOKEN] = _UNSET) -> str:
        """Declare profile *name* (extending the default one unless *extends* says otherwise).

        Declarations go to it inside ``with img.profiles(name): ...``. Pass
        ``extends=None`` for a standalone profile. Returns the normalized name.
        """
        (selected,) = self._normalize_profile_names((name,))
        self._ensure_profile(selected, extends=extends)
        return selected

    @contextmanager
    def profiles(self, *names: str) -> Iterator[Self]:
        """Make *names* (declared with :meth:`profile` unless default) active inside the block."""
        selected = self._normalize_profile_names(names)
        previous_profiles = self._active_profiles
        for profile_name in selected:
            self._ensure_profile(profile_name, extends=_UNSET)
        self._active_profiles = selected
        try:
            yield self
        finally:
            self._active_profiles = previous_profiles

    def install(self, *packages: str) -> Self:
        if not packages:
            raise ValidationError(
                "install() requires at least one package.",
                hint="Declare packages with Package('curl').",
            )
        for package in packages:
            if not package:
                raise ValidationError(
                    "Package names must be non-empty.",
                    hint="Give every Package() a non-empty name.",
                )
        for profile in self._iter_active_profiles():
            profile.packages.update(packages)
        return self

    def build_packages(self, *packages: str) -> Self:
        """Declare packages required at build time (removed after build)."""
        if not packages:
            raise ValidationError(
                "build_packages() requires at least one package.",
                hint="Declare build-time packages with Package('golang', role='build').",
            )
        for package in packages:
            if not package:
                raise ValidationError(
                    "Package names must be non-empty.",
                    hint="Give every Package() a non-empty name.",
                )
        for profile in self._iter_active_profiles():
            profile.build_packages.update(packages)
        return self

    def mount_build_source(self, src: str, *, dest: str = "") -> Self:
        """Mount the host directory *src* into the build environment at *dest* (BuildSources)."""
        if not src:
            raise ValidationError(
                "mount_build_source() requires a non-empty src path.",
                hint="Pass the host directory to mount into the build.",
            )
        for profile in self._iter_active_profiles():
            profile.build_sources.append((src, dest))
        return self

    def build_from(self, spec: SourceBuild) -> Self:
        """Fetch, build and install *spec* in the build phase, pinned through the lockfile.

        Adds the build packages the source and recipe need and one cached build
        hook. Until :meth:`lock` pins the source, the hook clones the symbolic
        ref; once ``<build_dir>/tundravm.lock`` records a pin, ``compile()``
        emits a fetch of exactly that commit (or a sha256-checked download).
        """
        profiles = self._iter_active_profiles()
        for profile in profiles:
            if spec.name in profile.source_builds:
                raise ValidationError(
                    f"Source build {spec.name!r} is already declared.",
                    hint="Give each source build a unique name.",
                    context={"profile": profile.name, "build_from": spec.name},
                )
        if spec.packages:
            self.build_packages(*spec.packages)
        for profile in profiles:
            profile.source_builds[spec.name] = spec
        return self.shell(spec.render(), phase="build")

    def source_builds(self, *, profile: str | None = None) -> dict[str, SourceBuild]:
        """Source builds declared for *profile* (default: every active profile), by name."""
        names = (profile,) if profile is not None else self._active_profiles
        builds: dict[str, SourceBuild] = {}
        for name in names:
            builds.update(self._state.effective_profile(name).source_builds)
        return dict(sorted(builds.items()))

    def source_pins(self, path: str | Path | None = None) -> dict[str, LockedFetch]:
        """Source-build entries of the lockfile at *path* (default ``<build_dir>/tundravm.lock``).

        Empty when the lockfile does not exist.
        """
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        if not lock_path.exists():
            return {}
        lock = read_lockfile(lock_path)
        return {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}

    def unpinned_sources(self, path: str | Path | None = None) -> list[str]:
        """Names of active source builds the lockfile at *path* does not pin."""
        pins = self.source_pins(path)
        return [name for name, spec in self.source_builds().items() if spec.pin_from(pins) is None]

    def repository(
        self,
        url: str,
        *,
        name: str | None = None,
        suite: str | None = None,
        components: tuple[str, ...] | list[str] = (),
        keyring: str | None = None,
        priority: int = 100,
    ) -> Self:
        if not url:
            raise ValidationError(
                "repository() requires a non-empty URL.",
                hint="Give Repository() the archive base URL, e.g. 'https://deb.debian.org/debian'.",
            )
        repo_name = name or url.split("/")[-1] or url
        entry = RepositorySpec(
            name=repo_name,
            url=url,
            suite=suite,
            components=tuple(components),
            keyring=keyring,
            priority=priority,
        )
        for profile in self._iter_active_profiles():
            profile.repositories.append(entry)
        return self

    def file(
        self,
        dest: str,
        *,
        content: str | bytes | None = None,
        src: str | Path | None = None,
        mode: str = "0644",
    ) -> Self:
        """Place a file at *dest* in the image; *src* that is not UTF-8 is copied as bytes."""
        if not dest:
            raise ValidationError(
                "file() requires a destination path.",
                hint="Give File() an absolute path in the image, e.g. File('/etc/motd', 'hi\\n').",
            )
        if content is None and src is None:
            raise ValidationError(
                "file() requires content= or src=.",
                hint="Give File() inline content or a host Path to copy.",
            )
        if content is not None and src is not None:
            raise ValidationError(
                "file() accepts content= or src=, not both.",
                hint="Pass either inline content or a host file, not both.",
            )
        resolved_content = content if content is not None else _read_source(Path(src or ""))
        for profile in self._iter_active_profiles():
            profile.files.append(FileEntry(path=dest, content=resolved_content, mode=mode))
        return self

    def copy_tree(
        self,
        dest: str,
        *,
        src: str | Path,
        mode: str | None = None,
        exclude: Sequence[str] = (),
    ) -> Self:
        """Place every file under the host directory *src* at *dest* in the image.

        Files keep their relative paths. Each gets *mode*, or 0755 when executable on
        the host and 0644 otherwise. *exclude* holds fnmatch globs matched against the
        path relative to *src* (``*`` also matches ``/``); a matching directory is
        skipped whole. Symlinked files are copied, symlinked directories are not followed.
        """
        if not dest:
            raise ValidationError(
                "copy_tree() requires a destination path.",
                hint="Give Directory() an absolute path in the image, e.g. '/opt/app'.",
            )
        root = Path(src)
        if not root.is_dir():
            raise ValidationError(
                "copy_tree() src must be an existing directory.",
                hint="Paths are relative to the working directory; check src= exists.",
                context={"src": str(root)},
            )
        found = _walk_tree(root, exclude)
        if not found:
            raise ValidationError(
                "copy_tree() found no files to copy.",
                hint="Check src= and exclude=.",
                context={"src": str(root)},
            )
        prefix = dest.rstrip("/")
        for rel, host_path in sorted(found):
            file_mode = mode or ("0755" if host_path.stat().st_mode & 0o111 else "0644")
            self.file(f"{prefix}/{rel}", content=_read_source(host_path), mode=file_mode)
        return self

    def template(
        self,
        dest: str,
        *,
        src: str | Path | None = None,
        template: str | None = None,
        variables: Mapping[str, str | int | float] | None = None,
        mode: str = "0644",
    ) -> Self:
        if not dest:
            raise ValidationError(
                "template() requires a destination path.",
                hint="Give Template() the absolute path of the rendered file in the image.",
            )

        if src is not None and template is not None:
            raise ValidationError(
                "template() accepts src= or template=, not both.",
                hint="Pass either an inline template string or a host file, not both.",
            )
        if src is not None:
            template_content = Path(src).read_text(encoding="utf-8")
        elif template is not None:
            template_content = template
        else:
            raise ValidationError(
                "template() requires src= or template=.",
                hint="Give Template() an inline template string or a host Path.",
            )

        resolved_vars: dict[str, str] = {}
        if variables is not None:
            resolved_vars = {k: str(v) for k, v in sorted(variables.items())}

        try:
            rendered = template_content.format_map(resolved_vars)
        except KeyError as exc:
            raise ValidationError(
                "template() variables are missing required placeholders.",
                hint="Provide all placeholder keys used in the template string.",
                context={"path": dest, "missing_key": str(exc)},
            ) from exc
        entry = TemplateEntry(
            path=dest,
            template=template_content,
            variables=resolved_vars,
            rendered=rendered,
            mode=mode,
        )
        for profile in self._iter_active_profiles():
            profile.templates.append(entry)
        return self

    def group(self, name: str, *, system: bool = False, gid: int | None = None) -> Self:
        """Create group *name* in postinst (``groupadd``), before any user is created.

        Users join it with ``user(..., groups=(name,))``; ``check()`` reports users that
        list a group nobody declares (``user-group-undefined``).
        """
        if not name or not _GROUP_NAME.fullmatch(name):
            raise ValidationError(
                f"Invalid group name {name!r}.",
                hint="Use lowercase letters, digits, '_' and '-', starting with a letter or '_'.",
            )
        entry = GroupSpec(name=name, system=system, gid=gid)
        for profile in self._iter_active_profiles():
            if any(g.name == name for g in profile.groups):
                raise ValidationError(
                    f"Duplicate group name '{name}' in variant '{profile.name}'.",
                    hint="Group names must be unique within a profile.",
                    context={"group": name, "profile": profile.name},
                )
            profile.groups.append(entry)
        return self

    def user(
        self,
        name: str,
        *,
        system: bool = False,
        home: str | None = None,
        shell: str = "/usr/sbin/nologin",
        uid: int | None = None,
        gid: int | str | None = None,
        groups: tuple[str, ...] | list[str] = (),
    ) -> Self:
        """Create user *name* in postinst; *gid* is its primary group, by number or name."""
        if not name:
            raise ValidationError(
                "user() requires a non-empty user name.",
                hint="Give User() an account name, e.g. User('app').",
            )
        entry = UserSpec(
            name=name,
            system=system,
            home=home,
            shell=shell,
            uid=uid,
            gid=gid,
            groups=tuple(groups),
        )
        for profile in self._iter_active_profiles():
            existing_names = {u.name for u in profile.users}
            if name in existing_names:
                raise ValidationError(
                    f"Duplicate user name '{name}' in variant '{profile.name}'.",
                    hint="User names must be unique within a profile.",
                    context={"user": name, "profile": profile.name},
                )
            profile.users.append(entry)
        return self

    def service(
        self,
        name: str,
        *,
        command: tuple[str, ...] | list[str] | str,
        description: str | None = None,
        user: str | None = None,
        working_dir: str | None = None,
        env: Mapping[str, str] | None = None,
        env_file: str | None = None,
        exec_start_pre: Sequence[str] = (),
        after: tuple[str, ...] | list[str] = (),
        requires: tuple[str, ...] | list[str] = (),
        wants: tuple[str, ...] | list[str] = (),
        restart: RestartPolicy = "no",
        enabled: bool = True,
        extra_unit: Mapping[str, Mapping[str, str]] | None = None,
        security_profile: SecurityProfile = "default",
        group: str | None = None,
        wanted_by: str | None = None,
        type: ServiceType | None = None,
        limits: Mapping[str, str | int] | None = None,
        kill_mode: KillMode | None = None,
        timeout_stop: str | None = None,
        after_init: bool = True,
    ) -> Self:
        """Register a systemd service unit in the current profile(s).

        *env* becomes ``Environment=`` lines (sorted, quoted when needed), *env_file*
        ``EnvironmentFile=``, *working_dir* ``WorkingDirectory=``, each *exec_start_pre*
        command an ``ExecStartPre=`` line, and *description* ``Description=``
        (default: the service name). *group* sets ``Group=``, *type* ``Type=`` (default
        ``simple``), *wanted_by* ``WantedBy=`` (default ``minimal.target``), *limits*
        one ``Limit<RESOURCE>=`` line each (``{"NOFILE": 1048576}``, sorted),
        *kill_mode* ``KillMode=`` and *timeout_stop* ``TimeoutStopSec=``.

        To enable a unit that a package or ``file()`` already ships, use :meth:`enable`.
        """
        if not name:
            raise ValidationError(
                "service() requires a non-empty service name.",
                hint="Give Service() a name, e.g. Service('app', '/usr/bin/app').",
            )
        if not command:
            raise ValidationError(
                f"service() requires a non-empty command for '{name}'.",
                hint=f"To enable a unit a package or file() ships, use enable({name!r}).",
            )
        limit_data = _normalize_limits(name, limits)
        env_data = dict(env or {})
        for key, value in env_data.items():
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
        pre_commands = (exec_start_pre,) if isinstance(exec_start_pre, str) else exec_start_pre
        exec_argv: tuple[str, ...]
        exec_argv = tuple(shlex.split(command)) if isinstance(command, str) else tuple(command)
        entry = ServiceSpec(
            name=name,
            command=exec_argv,
            user=user,
            after=tuple(after),
            requires=tuple(requires),
            wants=tuple(dict.fromkeys(wants)),
            restart=restart,
            enabled=enabled,
            extra_unit=dict(extra_unit) if extra_unit else {},
            security_profile=security_profile,
            description=description or None,
            env=env_data,
            env_file=env_file or None,
            working_dir=working_dir or None,
            exec_start_pre=tuple(pre_commands),
            group=group or None,
            wanted_by=wanted_by or None,
            type=type,
            limits=limit_data,
            kill_mode=kill_mode,
            timeout_stop=timeout_stop or None,
            after_init=after_init,
        )
        for profile in self._iter_active_profiles():
            existing_names = {s.name for s in profile.services}
            if name in existing_names:
                raise ValidationError(
                    f"Duplicate service name '{name}' in variant '{profile.name}'.",
                    hint="Service names must be unique within a profile.",
                    context={"service": name, "profile": profile.name},
                )
            profile.services.append(entry)
        return self

    def enable(self, *units: str) -> Self:
        """Enable units that a package or ``file()`` ships (``systemctl enable``).

        Each unit is enabled in postinst together with the units of ``service()``, in
        declaration order, and linked into ``minimal.target.wants``. ``foo`` means
        ``foo.service``. Enabling a unit twice, or one a ``service()`` already enables,
        is a no-op.
        """
        for unit in _validate_units("enable", units):
            for profile in self._iter_active_profiles():
                key = unit_name(unit)
                index = next(
                    (i for i, s in enumerate(profile.services) if unit_name(s.name) == key),
                    None,
                )
                if index is None:
                    profile.services.append(ServiceSpec(name=unit, enabled=True))
                elif not profile.services[index].enabled:
                    profile.services[index] = replace(profile.services[index], enabled=True)
        return self

    def disable(self, *units: str) -> Self:
        """``systemctl disable`` installed units, after every postinst hook ran."""
        return self._unit_state("disable", units)

    def mask(self, *units: str) -> Self:
        """``systemctl mask`` installed units, after every postinst hook and ``disable()``."""
        return self._unit_state("mask", units)

    def _unit_state(self, action: UnitAction, units: tuple[str, ...]) -> Self:
        names = [unit_name(unit) for unit in _validate_units(action, units)]
        for profile in self._iter_active_profiles():
            for name in names:
                spec = UnitStateSpec(action=action, unit=name)
                if spec not in profile.unit_states:
                    profile.unit_states.append(spec)
        return self

    def partition(self, name: str, *, size: str, mount_at: str, fs: str = "ext4") -> Self:
        if not name:
            raise ValidationError(
                "partition() requires a non-empty name.",
                hint="Give Partition() a name, e.g. Partition('data', size='10G', mount='/data').",
            )
        if not size or not mount_at:
            raise ValidationError(
                "partition() requires both size and mount_at values.",
                hint="Give Partition() a size (e.g. '10G') and a mount point (e.g. '/data').",
            )
        entry = PartitionSpec(name=name, size=size, mount_at=mount_at, fs=fs)
        for profile in self._iter_active_profiles():
            profile.partitions.append(entry)
        return self

    def targets(self, *targets: OutputTarget) -> Self:
        """Set the artifacts the active profiles bake (``qemu``, ``azure``, ``gcp``)."""
        if not targets:
            raise ValidationError(
                "targets() requires at least one target.",
                hint="Set Variant(target=...) to 'qemu', 'azure' or 'gcp'.",
            )
        deduped = tuple(dict.fromkeys(targets))
        for profile in self._iter_active_profiles():
            profile.output_targets = deduped
            profile.output_targets_explicit = True
        return self

    def debloat(
        self,
        *,
        enabled: bool = True,
        paths_remove: tuple[str, ...] | None = None,
        paths_skip: tuple[str, ...] | list[str] = (),
        extra_remove_paths: tuple[str, ...] | list[str] = (),
        paths_skip_for_profiles: dict[str, tuple[str, ...]] | None = None,
        systemd_minimize: bool = True,
        systemd_units_keep: tuple[str, ...] | None = None,
        extra_keep_units: tuple[str, ...] | list[str] = (),
        systemd_bins_keep: tuple[str, ...] | None = None,
    ) -> Self:
        """Configure image debloating — removal of unnecessary files and systemd units."""
        _defaults = DebloatConfig()
        if not enabled:
            config = DebloatConfig(enabled=False)
        else:
            profile_skips: tuple[tuple[str, tuple[str, ...]], ...] = ()
            if paths_skip_for_profiles:
                profile_skips = tuple((k, v) for k, v in sorted(paths_skip_for_profiles.items()))
            config = DebloatConfig(
                enabled=True,
                paths_remove=paths_remove or _defaults.paths_remove,
                paths_skip=tuple(paths_skip),
                extra_remove_paths=tuple(extra_remove_paths),
                paths_skip_for_profiles=profile_skips,
                systemd_minimize=systemd_minimize,
                systemd_units_keep=systemd_units_keep or _defaults.systemd_units_keep,
                extra_keep_units=tuple(extra_keep_units),
                systemd_bins_keep=systemd_bins_keep or _defaults.systemd_bins_keep,
            )

        for profile in self._iter_active_profiles():
            profile.debloat = config
            profile.debloat_explicit = True
        return self

    def explain_debloat(self, *, profile: str | None = None) -> dict[str, object]:
        selected_profile = self._resolve_operation_profile(profile)
        config = self._state.effective_profile(selected_profile).debloat
        return {
            "profile": selected_profile,
            "enabled": config.enabled,
            "paths_remove": list(config.effective_paths_remove),
            "paths_skip": list(config.paths_skip),
            "systemd_minimize": config.systemd_minimize,
            "systemd_units_keep": list(config.effective_units_keep),
            "systemd_bins_keep": list(config.systemd_bins_keep),
        }

    def skeleton(self, dest: str, *, content: str | bytes, mode: str = "0644") -> Self:
        """Place a file in ``mkosi.skeleton/``: in the image before the package manager runs."""
        if not dest:
            raise ValidationError(
                "skeleton() requires a destination path.",
                hint="Give File(..., stage='skeleton') an absolute path in the image.",
            )
        for profile in self._iter_active_profiles():
            profile.skeleton_files.append(FileEntry(path=dest, content=content, mode=mode))
        return self

    def strip_image_version(self, *, enabled: bool = True) -> Self:
        """Strip IMAGE_VERSION from /etc/os-release for reproducible attestation."""
        if not enabled:
            # Remove any existing finalize hooks that match the strip command
            for profile in self._iter_active_profiles():
                if "finalize" in profile.phases:
                    profile.phases["finalize"] = [
                        cmd
                        for cmd in profile.phases["finalize"]
                        if "IMAGE_VERSION" not in cmd.argv[0]
                    ]
                    profile.hooks = [
                        h
                        for h in profile.hooks
                        if not (h.phase == "finalize" and "IMAGE_VERSION" in h.command.argv[0])
                    ]
            return self
        script = """sed -i '/^IMAGE_VERSION=/d' "$BUILDROOT/usr/lib/os-release" """
        return self.shell(script, phase="finalize")

    def runtime_init(self, script: str, *, priority: int = 100) -> Self:
        """Append a bash fragment to the active profiles' runtime-init script.

        Fragments are ordered by *priority* (lower runs first) when Init
        generates ``/usr/bin/runtime-init``. Profiles that extend the default
        profile run its fragments too; standalone profiles only their own.
        Modules use this to register their binary invocations into the boot
        sequence.
        """
        if not script:
            raise ValidationError(
                "runtime_init() requires non-empty script content.",
                hint="Give Init() the shell script runtime-init runs at boot.",
            )
        entry = InitScriptEntry(script=script, priority=priority)
        for profile in self._iter_active_profiles():
            profile.init_scripts.append(entry)
        return self

    def init_scripts(self, profile: str | None = None) -> tuple[InitScriptEntry, ...]:
        """Runtime-init fragments *profile* (default: the active one) runs, deduplicated.

        Registration order, the default profile's first for a profile that extends it.
        """
        selected = self._resolve_operation_profile(profile)
        entries = self._state.effective_profile(selected).init_scripts
        return tuple({(e.priority, e.script): e for e in entries}.values())

    def shell(
        self,
        command: str,
        *,
        phase: Phase,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> Self:
        """Run the shell *command* in build *phase* (``boot`` runs it at VM boot)."""
        if not command:
            raise ValidationError(
                "shell() requires a command.",
                hint="Give Hook() a non-empty script.",
            )
        if phase not in VALID_PHASES:
            raise ValidationError(
                f"Invalid phase {phase!r}.",
                hint=f"Expected one of: {', '.join(sorted(VALID_PHASES))}",
            )
        env_data = dict(env or {})
        for profile in self._iter_active_profiles():
            spec = CommandSpec(argv=(command,), env=dict(env_data), cwd=cwd)
            profile.phases.setdefault(phase, []).append(spec)
            profile.hooks.append(HookSpec(phase=phase, command=spec))
        return self

    def lock_status(
        self,
        path: str | Path | None = None,
        *,
        resolver: Resolver | None = None,
        profiles: Sequence[str] | None = None,
    ) -> LockDrift:
        """Compare the lockfile at *path* with the current recipe, section by section.

        Reads ``<build_dir>/tundravm.lock`` by default and never writes. The
        returned :class:`~tundravm.lockfile.LockDrift` lists changed, added and
        removed sections; ``render()`` prints them (``~ variants.default.packages:
        +htop``) or ``lock is up to date``. A lockfile written before section
        digests existed reports every section as added. Source builds report as
        ``+ sources.<name>: source <name> is not pinned`` or ``~ sources.<name>: <old>
        -> <new>`` (pinned for another ref, or, with *resolver*, the ref has moved);
        without *resolver* nothing touches the network. Raises
        :class:`LockfileError` when the lockfile is missing or unreadable.

        *profiles* that leave out some declared profile are checked against
        their own sections only (see :func:`~tundravm.lockfile.compare_lock`).
        """
        lock_path = self._normalize_path(path, fallback=self._default_lock_path())
        lock = read_lockfile(lock_path)
        with self._operation_scope(profiles) as names:
            partial = not set(self._state.profiles) <= set(names)
            return self._lock_drift(lock, resolver=resolver, partial=partial)

    def _lock_drift(self, lock: Lockfile, *, resolver: Resolver | None, partial: bool) -> LockDrift:
        """Section and source drift of the active profiles against *lock*."""
        payload = self._recipe_payload(profile_names=self._active_profiles)
        drift = compare_lock(lock, payload, partial=partial)
        pins = {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}
        added, changed, removed, details = source_drift(
            self.source_builds(), pins, resolver=resolver
        )
        if partial:
            elsewhere = unselected_sources(lock, payload)
            removed = [s for s in removed if s.removeprefix("sources.") not in elsewhere]
        return replace(
            drift,
            added=(*drift.added, *added),
            changed=(*drift.changed, *changed),
            removed=(*drift.removed, *removed),
            details={**drift.details, **details},
        )

    def compile(
        self,
        path: str | Path,
        *,
        force: bool = False,
        profiles: Sequence[str] | None = None,
    ) -> CompileResult:
        """Emit the mkosi tree for *profiles* (default: the active ones) to *path*."""
        with self._operation_scope(profiles):
            return self._compile(self._normalize_path(path), force=force)

    def _compile(self, destination: Path, *, force: bool) -> CompileResult:
        # Runtime-init is generated into a scratch copy so compiling never changes what
        # later steps (lint, lock, a second compile) see.
        declared = self._state
        self._state = copy.deepcopy(declared)
        try:
            return self._emit(destination, force=force)
        finally:
            self._state = declared

    def _emit(self, destination: Path, *, force: bool) -> CompileResult:
        self._apply_init()
        digest = recipe_digest(self._recipe_payload(profile_names=self._active_profiles))
        pins = self.source_pins()
        self._enforce_source_policy(pins)
        config = self._emit_config()
        # The mkosi options and kernel shape the tree without entering the digest.
        compile_key = self._compile_key(digest, pins) + ":" + _short_sha(repr(config))
        if (
            not force
            and self._last_compile_digest == compile_key
            and self._last_compile_path == destination
            and destination.exists()
        ):
            return CompileResult(
                path=destination,
                profiles=self._active_profiles,
                digest=digest,
            )
        self._last_compile_emission = emit_mkosi_tree(
            recipe=self._pinned_state(pins),
            destination=destination,
            profile_names=self._active_profiles,
            base=self.base,
            config=config,
        )
        self._last_compile_digest = compile_key
        self._last_compile_path = destination
        return CompileResult(
            path=destination,
            profiles=self._active_profiles,
            digest=digest,
        )

    def bake(
        self,
        output_dir: str | Path | None = None,
        *,
        frozen: bool = False,
        force: bool = False,
        reporter: Reporter | None = None,
        profiles: Sequence[str] | None = None,
    ) -> BakeResult:
        """Compile, build, and package *profiles* (default: the active ones) via the backend.

        *reporter* receives progress events: lint/lock/compile phases, per-profile
        prepare and build phases with durations, every backend output line,
        artifacts with sizes, the report path, and a final ``done``. Without a
        reporter, backend output only surfaces in a failing backend's error.
        """
        with self._operation_scope(profiles):
            return self._bake(output_dir, frozen=frozen, force=force, reporter=reporter)

    def _bake(
        self,
        output_dir: str | Path | None,
        *,
        frozen: bool,
        force: bool,
        reporter: Reporter | None,
    ) -> BakeResult:
        progress = Progress(reporter)
        active = self._active_profiles
        scope = active[0] if len(active) == 1 else None
        with (
            self.logger.attached(progress.reporter, started_at=progress.started),
            progress.guard(profile=scope),
        ):
            ensure_bake_policy(policy=self.policy, frozen=frozen)
            with progress.phase("lint", "lint", profile=scope):
                diagnostics = run_checks(self)
                for diagnostic in diagnostics:
                    if diagnostic.level in ("warning", "error"):
                        progress.emit(
                            "warning",
                            diagnostic.profile,
                            f"{diagnostic.code}: {diagnostic.message}",
                            level=diagnostic.level,
                            code=diagnostic.code,
                        )
                errors = [d for d in diagnostics if d.level == "error"]
                if errors:
                    raise LintError(
                        f"Recipe has {len(errors)} error-level diagnostics.",
                        hint="Run `tundravm lint RECIPE` to see them.",
                        context={"codes": ", ".join(d.code for d in errors[:3])},
                    )
            if frozen:
                with progress.phase("lock", "verify lockfile", profile=scope):
                    self._assert_frozen_lock(profile_names=self._active_profiles)
            destination = self._normalize_path(output_dir, fallback=self.build_dir)
            destination.mkdir(parents=True, exist_ok=True)
            recipe_lock_digest = recipe_digest(
                self._recipe_payload(profile_names=self._active_profiles),
            )
            lock_digest = self._compute_lock_digest(recipe_lock_digest)

            # Compile the mkosi tree (skips if unchanged)
            emission_root = destination / "mkosi"
            with progress.phase("compile", "compile", profile=scope):
                self.compile(emission_root, force=force)
            emission = self._last_compile_emission
            assert emission is not None

            # Validate backend
            if self.backend is None:
                raise ValidationError(
                    "No build backend configured.",
                    hint=(
                        "Pass --backend (lima, nix or local), or bind "
                        "`backend = LimaMkosiBackend()` in the recipe file."
                    ),
                )
            backend = self.backend

            profiles_result: dict[str, ProfileBuildResult] = {}
            for profile_name in self._sorted_active_profile_names():
                profile_started = progress.elapsed()
                profile = self._state.effective_profile(profile_name)
                profile_dir = destination / profile_name
                profile_dir.mkdir(parents=True, exist_ok=True)

                self.logger.log(
                    operation="bake_profile_start",
                    profile=profile_name,
                    phase="build",
                    module="image",
                    builder=backend.name,
                    message=f"Starting variant bake via {backend.name} backend.",
                )

                # Build via the real backend, streaming its output to the reporter
                request = BakeRequest(
                    profile=profile_name,
                    build_dir=destination,
                    emit_dir=emission_root,
                    output_targets=profile.output_targets,
                    on_output=None if reporter is None else progress.output(profile_name),
                )

                with progress.phase("prepare", f"prepare {backend.name}", profile=profile_name):
                    backend.prepare(request)
                try:
                    with progress.phase("build", f"build via {backend.name}", profile=profile_name):
                        backend_result = backend.execute(request)
                finally:
                    backend.cleanup(request)

                # Merge backend artifacts into profile result
                profile_result = backend_result.profiles.get(
                    profile_name, ProfileBuildResult(profile=profile_name)
                )

                # If the backend didn't find typed artifacts for all targets,
                # check if the output files exist with expected names
                for target in profile.output_targets:
                    if target not in profile_result.artifacts:
                        artifact_path = profile_dir / self._artifact_filename(target)
                        if artifact_path.exists():
                            profile_result.artifacts[target] = ArtifactRef(
                                target=target,
                                path=artifact_path,
                            )

                # Hash artifacts (chunked; images can be several GiB)
                artifact_digests: dict[str, str] = {}
                for target, artifact in sorted(profile_result.artifacts.items()):
                    path = Path(artifact.path)
                    if not path.exists():
                        continue
                    with path.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                    artifact_digests[target] = digest
                    profile_result.artifacts[target] = replace(artifact, digest=digest)
                    size = path.stat().st_size
                    progress.emit(
                        "artifact",
                        profile_name,
                        f"artifact {target} {display_path(path)} ({format_size(size)})",
                        role="image",
                        target=target,
                        path=str(path),
                        size_bytes=str(size),
                        sha256=digest,
                    )

                # Generate build report
                script_checksums = self._script_checksums(
                    emission.script_paths.get(profile_name, {})
                )
                profile_logs = self.logger.records_for_profile(profile_name)

                report_path = profile_dir / "report.json"
                report_payload = {
                    "profile": profile_name,
                    "lock_digest": lock_digest,
                    "backend": backend.name,
                    "debloat": self.explain_debloat(profile=profile_name),
                    "artifact_digests": artifact_digests,
                    "emitted_scripts": script_checksums,
                    "artifacts": {
                        target: str(artifact.path)
                        for target, artifact in profile_result.artifacts.items()
                    },
                    "logs": profile_logs,
                }
                report_path.write_text(
                    json.dumps(report_payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                progress.emit(
                    "artifact",
                    profile_name,
                    f"report {display_path(report_path)}",
                    role="report",
                    path=str(report_path),
                )
                profile_result.report_path = report_path
                profile_result.duration_s = progress.elapsed() - profile_started
                profiles_result[profile_name] = profile_result

                self.logger.log(
                    operation="bake_profile_complete",
                    profile=profile_name,
                    phase="build",
                    module="image",
                    builder=backend.name,
                    message="Completed variant bake.",
                )

            total = progress.elapsed()
            bake_result = BakeResult(
                profiles=profiles_result,
                lock_digest=lock_digest,
                backend=backend.name,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                duration_s=total,
            )
            bake_result.save(destination)
            noun = "variant" if len(profiles_result) == 1 else "variants"
            progress.emit(
                "done",
                scope,
                f"baked {len(profiles_result)} {noun} in {format_duration(total)}",
                status="ok",
                duration_s=f"{total:.3f}",
            )
        return bake_result

    def mkosi_for(self, profile: str) -> MkosiOptions:
        """The mkosi options profile *profile* is emitted with."""
        return self.profile_mkosi.get(profile, self.mkosi)

    def kernel_for(self, profile: str) -> Kernel | None:
        """The kernel profile *profile* builds."""
        return self.profile_kernels.get(profile, self.kernel)

    def _emit_config(self) -> EmitConfig:
        """Build an EmitConfig from the Image's settings and ``self.mkosi``.

        Profiles with their own options or kernel get their own configuration.
        """
        own = sorted(set(self.profile_mkosi) | set(self.profile_kernels))
        return replace(
            self._emit_config_for(self.mkosi, self.kernel),
            profiles={
                name: self._emit_config_for(self.mkosi_for(name), self.kernel_for(name))
                for name in own
            },
        )

    def _emit_config_for(self, options: MkosiOptions, kernel: Kernel | None) -> EmitConfig:
        emit_kwargs: dict[str, object] = {
            "base": self.base,
            "arch": self.arch,
            "reproducible": self.reproducible,
            "kernel": kernel,
            "mirror": self.mirror,
            "tools_tree_mirror": self.tools_tree_mirror,
            "with_network": options.with_network,
            "clean_package_metadata": options.clean_package_metadata,
            "manifest_format": options.manifest_format,
            "compress_output": options.compress_output,
            "output_directory": options.output_directory,
            "sandbox_trees": options.sandbox_trees,
            "package_cache_directory": options.package_cache_directory,
            "init_script": options.init_script,
            "generate_version_script": options.generate_version_script,
            "generate_cloud_postoutput": options.generate_cloud_postoutput,
            "emit_mode": options.emit_mode,
            "environment": dict(options.environment) or None,
            "environment_passthrough": options.environment_passthrough,
            "settings": options.settings,
            "bootable": options.bootable,
        }
        if options.seed is not None:
            emit_kwargs["seed"] = options.seed
        return EmitConfig(**emit_kwargs)  # type: ignore[arg-type]

    def _artifact_filename(self, target: OutputTarget) -> str:
        mapping: dict[OutputTarget, str] = {
            "qemu": "disk.qcow2",
            "azure": "disk.vhd",
            "gcp": "disk.raw.tar.gz",
        }
        return mapping[target]

    def _normalize_path(self, path: str | Path | None, *, fallback: Path | None = None) -> Path:
        if path is None:
            if fallback is None:
                raise ValidationError(
                    "A path value is required.",
                    hint="Pass the output directory, e.g. 'build/mkosi'.",
                )
            return fallback
        return Path(path)

    def _normalize_profile_names(self, names: tuple[str, ...]) -> tuple[str, ...]:
        if not names:
            raise ValidationError(
                "At least one profile name is required.",
                hint="Name at least one variant, e.g. 'default'.",
            )
        normalized: list[str] = []
        for name in names:
            if not name:
                raise ValidationError(
                    "Profile names must be non-empty.",
                    hint="Use a declared variant name, e.g. 'default'.",
                )
            if name not in normalized:
                normalized.append(name)
        return tuple(normalized)

    @contextmanager
    def _operation_scope(self, profiles: Sequence[str] | None) -> Iterator[tuple[str, ...]]:
        """Run an operation on *profiles*, or on the active selection when ``None``."""
        if profiles is None:
            yield self._active_profiles
            return
        names = self._normalize_profile_names(
            (profiles,) if isinstance(profiles, str) else tuple(profiles)
        )
        unknown = [name for name in names if name not in self._state.profiles]
        if unknown:
            raise ValidationError(
                f"Unknown variant(s): {', '.join(unknown)}.",
                hint=f"Declared variants: {', '.join(self.profile_names)}",
            )
        previous = self._active_profiles
        self._active_profiles = names
        try:
            yield names
        finally:
            self._active_profiles = previous

    def _sorted_active_profile_names(self) -> list[str]:
        return sorted(self._active_profiles)

    def _default_lock_path(self) -> Path:
        return self.lock_file if self.lock_file is not None else self.build_dir / "tundravm.lock"

    def _compute_lock_digest(self, fallback_digest: str) -> str:
        lock_path = self._default_lock_path()
        if not lock_path.exists():
            return fallback_digest
        return hashlib.sha256(lock_path.read_bytes()).hexdigest()

    def _resolve_operation_profile(self, profile: str | None) -> str:
        if profile is not None:
            return profile
        if len(self._active_profiles) == 1:
            return self._active_profiles[0]
        raise ValidationError(
            "Operation requires an explicit profile when multiple profiles are active.",
            hint="Pass profile='name' to the operation.",
            context={"operation": "resolve_profile"},
        )

    def _apply_init(self) -> None:
        """Apply Init: generate runtime-init files and inject deps into services.

        Runs on every active profile and on the default profile they extend. An
        extending profile only gets its own runtime-init files when it adds init
        scripts; otherwise it inherits the default profile's.
        """
        if self.init is None:
            return
        default_name = self._state.default_profile
        names = list(self._active_profiles)
        if any(self._ensure_profile(n).extends is not None for n in names):
            names.insert(0, default_name)
        targets = [self._ensure_profile(n) for n in dict.fromkeys(names)]
        generators: list[ProfileState] = []
        for profile in targets:
            if profile.extends is not None and not profile.init_scripts:
                continue  # inherits the default profile's runtime-init files
            merged = self.init_scripts(profile.name)
            if merged:
                self.init.apply(profile, scripts=merged)
                generators.append(profile)
        init_svc = self.init.service_name
        # Inject After/Requires runtime-init.service into the services of every
        # profile that runs runtime-init
        for profile in targets:
            if not self.init_scripts(profile.name):
                continue
            patched: list[ServiceSpec] = []
            for svc in profile.services:
                if svc.name == init_svc or svc.name.endswith(".target") or not svc.after_init:
                    patched.append(svc)
                    continue
                after = svc.after if init_svc in svc.after else (init_svc, *svc.after)
                requires = svc.requires if init_svc in svc.requires else (init_svc, *svc.requires)
                patched.append(replace(svc, after=after, requires=requires))
            profile.services = patched
        # Register runtime-init for enablement (systemctl enable + minimal.target.wants)
        # in each generating profile that does not have it yet. enable() appends to every
        # active profile, so scope it to one profile at a time to stay idempotent
        # across compiles with different profile selections.
        missing = [
            profile.name
            for profile in generators
            if not any(s.name == init_svc for s in profile.services)
        ]
        for profile_name in missing:
            with self.profiles(profile_name):
                self.enable(init_svc)

    def _record_module(self, module: Module) -> None:
        for profile_name in self._active_profiles:
            applied = self._modules.setdefault(profile_name, [])
            if not any(existing is module for existing in applied):
                applied.append(module)

    def _iter_active_profiles(self) -> list[ProfileState]:
        profiles: list[ProfileState] = []
        for profile_name in self._active_profiles:
            profiles.append(self._ensure_profile(profile_name))
        return profiles

    def _ensure_profile(
        self, name: str, *, extends: str | None | Literal[_Unset.TOKEN] = _UNSET
    ) -> ProfileState:
        if extends is not _UNSET:
            self._state.set_extends(name, extends)
            self._sync_strip_hook(name)
        return self._state.ensure_profile(name)

    def _sync_strip_hook(self, name: str) -> None:
        """Give a standalone profile the default profile's IMAGE_VERSION strip hook.

        Extending profiles inherit it through the merge; a standalone one would
        otherwise silently lose it.
        """
        default = self._state.ensure_profile(self._state.default_profile)
        profile = self._state.ensure_profile(name)
        if profile is default:
            return
        strip = next((h for h in default.hooks if _is_strip_hook(h)), None)
        own = [h for h in profile.hooks if _is_strip_hook(h)]
        if profile.extends is None and strip is not None and not own:
            profile.phases.setdefault("finalize", []).append(strip.command)
            profile.hooks.append(strip)
        elif profile.extends is not None and strip is not None and strip in own:
            profile.hooks.remove(strip)
            profile.phases["finalize"].remove(strip.command)

    def _recipe_payload(self, *, profile_names: tuple[str, ...]) -> dict[str, object]:
        profiles_data: dict[str, dict[str, object]] = {}
        for profile_name in sorted(profile_names):
            profile = self._state.effective_profile(profile_name)
            phases = {
                phase: [
                    {
                        "argv": list(command.argv),
                        "env": dict(command.env),
                        "cwd": command.cwd,
                    }
                    for command in commands
                ]
                for phase, commands in sorted(profile.phases.items())
            }
            repositories = [
                {
                    "name": repository.name,
                    "url": repository.url,
                    "suite": repository.suite,
                    "components": list(repository.components),
                    "keyring": repository.keyring,
                    "priority": repository.priority,
                }
                for repository in sorted(
                    profile.repositories,
                    key=lambda item: (item.priority, item.name, item.url),
                )
            ]
            files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.data).hexdigest(),
                }
                for file_entry in sorted(profile.files, key=lambda item: item.path)
            ]
            templates = [
                {
                    "path": tmpl.path,
                    "mode": tmpl.mode,
                    "sha256": hashlib.sha256(tmpl.rendered.encode()).hexdigest(),
                    "variables": dict(sorted(tmpl.variables.items())),
                }
                for tmpl in sorted(profile.templates, key=lambda item: item.path)
            ]
            users = [
                {
                    "name": u.name,
                    "system": u.system,
                    "home": u.home,
                    "uid": u.uid,
                    "gid": u.gid,
                    "shell": u.shell,
                    "groups": list(u.groups),
                }
                for u in sorted(profile.users, key=lambda item: item.name)
            ]
            services = [
                {
                    "name": svc.name,
                    "command": list(svc.command),
                    "user": svc.user,
                    "after": list(svc.after),
                    "requires": list(svc.requires),
                    "wants": list(svc.wants),
                    "restart": svc.restart,
                    "enabled": svc.enabled,
                    "security_profile": svc.security_profile,
                    **svc.extras(),
                }
                for svc in sorted(profile.services, key=lambda item: item.name)
            ]
            partitions = [
                {
                    "name": partition.name,
                    "size": partition.size,
                    "mount": partition.mount_at,
                    "fs": partition.fs,
                }
                for partition in sorted(profile.partitions, key=lambda item: item.name)
            ]
            # "after_phase" is no longer declarable; the key stays so digests do not move.
            hooks = [
                {"phase": hook.phase, "after_phase": None, "argv": list(hook.command.argv)}
                for hook in sorted(
                    profile.hooks,
                    key=lambda item: (PHASE_ORDER.index(item.phase), item.command.argv),
                )
            ]
            secrets = [
                {
                    "name": secret.name,
                    "required": secret.required,
                    "schema": None
                    if secret.schema is None
                    else {
                        "kind": secret.schema.kind,
                        "min_length": secret.schema.min_length,
                        "max_length": secret.schema.max_length,
                        "pattern": secret.schema.pattern,
                        "enum": list(secret.schema.enum),
                    },
                    "targets": [
                        {
                            "kind": t.kind,
                            "location": t.location,
                            "mode": t.mode,
                            "scope": t.scope,
                        }
                        for t in secret.targets
                    ],
                }
                for secret in sorted(profile.secrets, key=lambda item: item.name)
            ]
            skeleton_files = [
                {
                    "path": file_entry.path,
                    "mode": file_entry.mode,
                    "sha256": hashlib.sha256(file_entry.data).hexdigest(),
                }
                for file_entry in sorted(profile.skeleton_files, key=lambda item: item.path)
            ]
            # The default profile's payload carries no "extends" key so default-only
            # recipes keep their digests.
            inheritance: dict[str, object] = (
                {} if profile_name == self._state.default_profile else {"extends": profile.extends}
            )
            # Groups and unit states are keyed only when declared so older recipes keep
            # their digests.
            declared: dict[str, object] = {}
            if profile.groups:
                declared["groups"] = [
                    {"name": g.name, "system": g.system, "gid": g.gid}
                    for g in sorted(profile.groups, key=lambda item: item.name)
                ]
            # The default profile's fragments are the top-level "init_scripts".
            own_init = self._state.ensure_profile(profile_name).init_scripts
            if profile_name != self._state.default_profile and own_init:
                declared["init_scripts"] = _init_scripts_payload(own_init)
            if profile.source_builds:
                declared["source_builds"] = {
                    name: spec.to_payload() for name, spec in sorted(profile.source_builds.items())
                }
            if profile.unit_states:
                declared["unit_states"] = {
                    action: [s.unit for s in profile.unit_states if s.action == action]
                    for action in sorted({s.action for s in profile.unit_states})
                }
            profiles_data[profile_name] = {
                **inheritance,
                **declared,
                "packages": sorted(profile.packages),
                "build_packages": sorted(profile.build_packages),
                "build_sources": profile.build_sources,
                "output_targets": list(profile.output_targets),
                "phases": phases,
                "repositories": repositories,
                "files": files,
                "skeleton_files": skeleton_files,
                "templates": templates,
                "users": users,
                "services": services,
                "partitions": partitions,
                "hooks": hooks,
                "secrets": secrets,
                "debloat": {
                    "enabled": profile.debloat.enabled,
                    "paths_remove": list(profile.debloat.effective_paths_remove),
                    "systemd_minimize": profile.debloat.systemd_minimize,
                },
            }

        return {
            "base": self._state.base,
            "arch": self._state.arch,
            "default_profile": self._state.default_profile,
            "init_scripts": _init_scripts_payload(
                self._state.ensure_profile(self._state.default_profile).init_scripts
            ),
            "profiles": profiles_data,
        }

    def _script_checksums(self, scripts: dict[Phase, Path]) -> dict[str, str]:
        checksums: dict[str, str] = {}
        for phase, path in sorted(scripts.items(), key=lambda item: PHASE_ORDER.index(item[0])):
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            checksums[f"{phase}:{path.name}"] = checksum
        return checksums

    def _compile_key(self, digest: str, pins: Mapping[str, LockedFetch]) -> str:
        used = {
            name: pin
            for name, spec in self.source_builds().items()
            if (pin := spec.pin_from(pins)) is not None
        }
        if not used:
            return digest
        return digest + ":" + hashlib.sha256(json.dumps(used, sort_keys=True).encode()).hexdigest()

    def _enforce_source_policy(self, pins: Mapping[str, LockedFetch]) -> None:
        """Refuse unpinned source builds when ``policy.mutable_ref_policy`` is ``"error"``.

        Under ``"warn"`` compile stays silent: the ``source-unpinned`` check and
        explain's ``pinned=-`` report it, and frozen bakes refuse it.
        """
        if self.policy.mutable_ref_policy != "error":
            return
        unpinned = [spec for spec in self.source_builds().values() if spec.pin_from(pins) is None]
        if not unpinned:
            return
        names = ", ".join(f"{spec.name}@{spec.source.requested}" for spec in unpinned)
        raise PolicyError(
            f"Unpinned source builds are not allowed by policy: {names}.",
            hint="Run `tundravm lock RECIPE` to pin them, or relax mutable_ref_policy.",
            context={"operation": "compile", "sources": names},
        )

    def _pinned_state(self, pins: Mapping[str, LockedFetch]) -> RecipeState:
        """The recipe state with every pinned source build's hook rendered at its pin."""
        profiles: dict[str, ProfileState] = {}
        changed = False
        for name, profile in self._state.profiles.items():
            swaps = {
                spec.render(): pinned
                for spec in profile.source_builds.values()
                if (pinned := spec.render(spec.pin_from(pins))) != spec.render()
            }
            if not swaps:
                profiles[name] = profile
                continue
            changed = True
            commands = {
                id(hook.command): replace(hook.command, argv=(swaps[hook.command.argv[0]],))
                for hook in profile.hooks
                if hook.command.argv and hook.command.argv[0] in swaps
            }
            profiles[name] = replace(
                profile,
                phases={
                    phase: [commands.get(id(cmd), cmd) for cmd in cmds]
                    for phase, cmds in profile.phases.items()
                },
                hooks=[
                    replace(hook, command=commands[id(hook.command)])
                    if id(hook.command) in commands
                    else hook
                    for hook in profile.hooks
                ],
            )
        return replace(self._state, profiles=profiles) if changed else self._state

    def _assert_frozen_lock(self, *, profile_names: tuple[str, ...]) -> None:
        lock_path = self._default_lock_path()
        lock = read_lockfile(lock_path)
        current_recipe = self._recipe_payload(profile_names=profile_names)
        # A lock written for more variants than this bake selects covers it when
        # every section of the selected variants and the recipe-wide ones matches.
        drift = compare_lock(lock, current_recipe, partial=True)
        if lock.recipe_digest == recipe_digest(current_recipe) or drift.is_clean:
            self._assert_sources_pinned(lock_path)
            return
        lines = drift.render().splitlines()
        if len(lines) > _DRIFT_MESSAGE_LINES:
            hidden = len(lines) - _DRIFT_MESSAGE_LINES
            lines = [
                *lines[:_DRIFT_MESSAGE_LINES],
                f"... {hidden} more; run `tundravm lock RECIPE --check` for the full list",
            ]
        sections = drift.sections
        changed = ", ".join(sections[:_DRIFT_MESSAGE_LINES])
        if len(sections) > _DRIFT_MESSAGE_LINES:
            changed += f", ... ({len(sections) - _DRIFT_MESSAGE_LINES} more)"
        raise LockfileError(
            "Frozen bake lockfile is stale for current recipe state:\n"
            + "\n".join(f"  {line}" for line in lines),
            hint="Run tundravm lock RECIPE to accept these changes, or revert them.",
            context={"lock": str(lock_path), "changed": changed},
        )

    def _assert_sources_pinned(self, lock_path: Path) -> None:
        unpinned = self.unpinned_sources(lock_path)
        if unpinned:
            raise LockfileError(
                f"Frozen bake requires every source build to be pinned; unpinned: "
                f"{', '.join(unpinned)}.",
                hint="run tundravm lock RECIPE to pin them.",
                context={"lock": str(lock_path), "sources": ", ".join(unpinned)},
            )
