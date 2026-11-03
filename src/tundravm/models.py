"""Core typed dataclasses for recipe state and build/deploy requests."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, get_args

from ._options import RootLayout
from .errors import ValidationError
from .toolchain import Toolchain

if TYPE_CHECKING:
    from ._source import SourceBuild

Arch = Literal["x86_64", "aarch64"]
OutputTarget = Literal["qemu", "azure", "gcp"]
FileKind = Literal["file", "symlink", "directory"]
SecurityProfile = Literal["strict", "default", "none"]
RestartPolicy = Literal["always", "on-failure", "on-abnormal", "no"]
ALL_STEPS = "*"
"""The ``ServiceSpec.needs`` entry for the whole runtime-init chain (``runtime-init.service``)."""
ServiceType = Literal["simple", "exec", "oneshot", "notify", "forking"]
KillMode = Literal["control-group", "mixed", "process", "none"]
UnitAction = Literal["disable", "mask"]
UnitKind = Literal["service", "socket", "timer", "mount"]
"""What a ``ServiceSpec`` renders: ``<name>.service`` (the default), a socket, timer or mount."""

MODULES_PATH = "/usr/lib/modules"
"""Where a kernel's loadable modules live; the default debloat list removes it."""

DEFAULT_DEBLOAT_PATHS_REMOVE = (
    "/etc/machine-id",
    "/etc/ssh/ssh_host_*_key*",
    "/usr/lib/modules",
    "/usr/lib/pcrlock.d",
    "/usr/lib/systemd/catalog",
    "/usr/lib/systemd/network",
    "/usr/lib/systemd/user",
    "/usr/lib/systemd/user-generators",
    "/usr/lib/tmpfiles.d",
    "/usr/lib/udev/hwdb.bin",
    "/usr/lib/udev/hwdb.d",
    "/usr/share/bash-completion",
    "/usr/share/bug",
    "/usr/share/debconf",
    "/usr/share/doc",
    "/usr/share/gcc",
    "/usr/share/gdb",
    "/usr/share/info",
    "/usr/share/initramfs-tools",
    "/usr/share/lintian",
    "/usr/share/locale",
    "/usr/share/man",
    "/usr/share/menu",
    "/usr/share/mime",
    "/usr/share/perl5/debconf",
    "/usr/share/polkit-1",
    "/usr/share/systemd",
    "/usr/share/zsh",
    "/etc/credstore",
    "/etc/systemd/network",
)

DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP = (
    "basic.target",
    "final.target",
    "local-fs-pre.target",
    "local-fs.target",
    "minimal.target",
    "network-online.target",
    "poweroff.target",
    "reboot.target",
    "shutdown.target",
    "slices.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald-dev-log.socket",
    "systemd-journald.service",
    "systemd-journald.socket",
    "systemd-poweroff.service",
    "systemd-reboot.service",
    "systemd-remount-fs.service",
    "systemd-sysctl.service",
    "timers.target",
    "umount.target",
)
"""Units of the ``systemd`` package debloat leaves unmasked. ``sockets.target`` and
``timers.target`` stay so ``WantedBy=sockets.target``/``timers.target`` of a generated
socket or timer works on its own, not only through the boot target's wants link.

The orderly poweroff and reboot stay: ``poweroff.target`` and ``reboot.target``, their
services and the ``shutdown``, ``umount`` and ``final`` targets those require, so a power
button or a cloud stop stops the services and unmounts the volumes before the power goes.
``halt``, ``kexec``, ``soft-reboot``, ``exit`` and ``ctrl-alt-del.target`` stay masked.

Boot-time tmpfiles are off: ``systemd-tmpfiles-setup.service`` is masked and the
``systemd-tmpfiles`` binary removed, so no ``tmpfiles.d`` line runs at boot. The image ships
its paths with their modes, the runtime-init step ``owners`` applies ``owner=``, and a
service's ``state=``/``runtime=`` make the directories it writes; a tmpfiles.d file would
only re-create at every boot what the measured image already says, from configuration the
debloat otherwise strips (lint ``debloat-removes-needed-unit``)."""

DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP = (
    "journalctl",
    "systemctl",
    "systemd",
    "systemd-tty-ask-password-agent",
)

Phase = Literal[
    "sync",
    "skeleton",
    "prepare",
    "build",
    "extra",
    "postinst",
    "finalize",
    "postoutput",
    "clean",
    "repart",
    "boot",
]
VALID_PHASES: frozenset[str] = frozenset(get_args(Phase))


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class RepositorySpec:
    name: str
    url: str
    suite: str | None = None
    components: tuple[str, ...] = ()
    keyring: str | None = None
    priority: int = 100
    in_image: bool = True
    sha256: str | None = None

    @property
    def release_url(self) -> str:
        """The signed ``InRelease`` file *sha256* pins."""
        return f"{self.url.rstrip('/')}/dists/{self.suite}/InRelease"

    @property
    def locked_variable(self) -> str:
        """:func:`inrelease_variable` of this repository."""
        return inrelease_variable(self.name)


def inrelease_variable(name: str) -> str:
    """The mkosi ``Environment=`` variable an offline build sets to the ``InRelease`` digest
    the lock recorded for repository *name*: its ``sync`` step compares that instead of
    fetching the file."""
    return "TUNDRAVM_INRELEASE_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A path in the image: file bytes, a symlink (``content`` is its target) or a directory."""

    path: str
    content: str | bytes
    mode: str = "0644"
    kind: FileKind = "file"

    @property
    def data(self) -> bytes:
        """File content as bytes (text is UTF-8 encoded)."""
        return self.content.encode() if isinstance(self.content, str) else self.content


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """A container's root: the ``mkosi.images/<name>/`` subimage the payload ships at
    :attr:`image` (``Container`` in :mod:`tundravm.components`)."""

    name: str
    packages: tuple[str, ...]
    files: tuple[FileEntry, ...] = ()
    directories: tuple[str, ...] = ()
    """Mount points the root must hold for the unit's bind mounts (it is read-only)."""

    @property
    def image(self) -> str:
        """Where the payload ships the root image."""
        return f"{CONTAINERS_PATH}/{self.name}.raw"


CONTAINERS_PATH = "/usr/lib/tundravm/containers"
"""Where the payload ships each container's root image, ``<name>.raw``."""


@dataclass(frozen=True, slots=True)
class TemplateEntry:
    path: str
    template: str
    variables: Mapping[str, str] = field(default_factory=dict)
    rendered: str = ""
    mode: str = "0644"


@dataclass(frozen=True, slots=True)
class UserSpec:
    name: str
    system: bool = False
    home: str | None = None
    shell: str = "/usr/sbin/nologin"
    uid: int | None = None
    gid: int | str | None = None
    groups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """A group created in postinst before any user (``groupadd``)."""

    name: str
    system: bool = False
    gid: int | None = None


@dataclass(frozen=True, slots=True)
class UnitStateSpec:
    """A ``systemctl disable|mask`` of an already-installed unit."""

    action: UnitAction
    unit: str


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    name: str
    command: tuple[str, ...] = ()
    user: str | None = None
    after: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    restart: RestartPolicy = "no"
    enabled: bool = True
    extra_unit: Mapping[str, Mapping[str, str | tuple[str, ...]]] = field(default_factory=dict)
    security_profile: SecurityProfile = "default"
    description: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    env_file: str | None = None
    working_dir: str | None = None
    exec_start_pre: tuple[str, ...] = ()
    group: str | None = None
    wanted_by: str | None = None
    type: ServiceType | None = None
    limits: Mapping[str, str] = field(default_factory=dict)
    kill_mode: KillMode | None = None
    timeout_stop: str | None = None
    needs: tuple[str, ...] = (ALL_STEPS,)
    """The runtime-init steps the unit waits for; ``ALL_STEPS`` is the whole chain."""
    kind: UnitKind = "service"
    before: tuple[str, ...] = ()
    part_of: tuple[str, ...] = ()
    binds_to: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    remain_after_exit: bool = False
    exec_start_post: tuple[str, ...] = ()
    exec_reload: str | None = None
    dynamic_user: bool = False
    groups: tuple[str, ...] = ()
    env_files: tuple[str, ...] = ()
    """``EnvironmentFile=`` lines after ``env_file``."""
    credentials: Mapping[str, str] = field(default_factory=dict)
    """``LoadCredential=<name>:<path>``, by name."""
    state: str | None = None
    logs: str | None = None
    runtime: str | None = None
    cache: str | None = None
    write_paths: tuple[str, ...] = ()
    restart_sec: str | None = None
    """``None``: ``RestartSec=5`` when ``restart`` is set (the historical line); ``""``: none."""
    timeout_start: str | None = None
    hardening: tuple[tuple[str, str], ...] | None = None
    """Rendered ``Hardening`` directives; ``None`` falls back to ``security_profile``."""
    mounts_for: tuple[str, ...] = ()
    """``RequiresMountsFor=`` paths: ``write_paths`` on a declared mount point."""
    conditions: tuple[tuple[str, str], ...] = ()
    """``Condition*=`` lines, in declaration order (``conditions=`` on Service/Task)."""

    def extras(self) -> dict[str, object]:
        """Optional unit fields that are set, keyed by name (empty for a plain service)."""
        optional: dict[str, object] = {
            "description": self.description,
            "env": dict(sorted(self.env.items())),
            "env_file": self.env_file,
            "exec_start_pre": list(self.exec_start_pre),
            "group": self.group,
            "kill_mode": self.kill_mode,
            "limits": dict(sorted(self.limits.items())),
            "timeout_stop": self.timeout_stop,
            "type": self.type,
            "wanted_by": self.wanted_by,
            "working_dir": self.working_dir,
            "runs_before_init": not self.needs,
            "needs": list(self.needs) if self.needs != (ALL_STEPS,) else None,
            "kind": self.kind if self.kind != "service" else None,
            "before": list(self.before),
            "part_of": list(self.part_of),
            "binds_to": list(self.binds_to),
            "conflicts": list(self.conflicts),
            "remain_after_exit": self.remain_after_exit,
            "exec_start_post": list(self.exec_start_post),
            "exec_reload": self.exec_reload,
            "dynamic_user": self.dynamic_user,
            "groups": list(self.groups),
            "env_files": list(self.env_files),
            "credentials": dict(sorted(self.credentials.items())),
            "state": self.state,
            "logs": self.logs,
            "runtime": self.runtime,
            "cache": self.cache,
            "write_paths": list(self.write_paths),
            "mounts_for": list(self.mounts_for),
            "conditions": [f"{key}={value}" for key, value in self.conditions],
            "timeout_start": self.timeout_start,
            "extra": {
                section: {k: v if isinstance(v, str) else list(v) for k, v in sorted(keys.items())}
                for section, keys in sorted(self.extra_unit.items())
            },
        }
        kept = {key: value for key, value in optional.items() if value}
        if self.restart_sec is not None:
            kept["restart_sec"] = self.restart_sec
        if self.hardening is not None:
            kept["hardening"] = [list(entry) for entry in self.hardening]
        return kept


@dataclass(frozen=True, slots=True)
class PartitionSpec:
    name: str
    size: str
    mount_at: str
    fs: str = "ext4"


@dataclass(frozen=True, slots=True)
class HookSpec:
    phase: Phase
    command: CommandSpec


@dataclass(frozen=True, slots=True)
class SecretSchema:
    kind: Literal["string", "json"] = "string"
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SecretTarget:
    """Where a secret is delivered: a file, or an environment variable.

    An env target with *service* (a unit name, ``app.service``) is that
    service's; without one, with ``scope="global"``, it is the global environment.
    """

    kind: Literal["file", "env"]
    location: str
    mode: str = "0400"
    scope: Literal["service", "global"] = "service"
    owner: str | None = None
    service: str | None = None

    @classmethod
    def file(
        cls,
        path: str,
        *,
        mode: str = "0400",
        owner: str | None = None,
    ) -> SecretTarget:
        return cls(kind="file", location=path, mode=mode, scope="service", owner=owner)

    @classmethod
    def env(
        cls,
        name: str,
        *,
        scope: Literal["service", "global"] = "service",
        service: str | None = None,
    ) -> SecretTarget:
        return cls(kind="env", location=name, mode="0400", scope=scope, service=service)


@dataclass(frozen=True, slots=True)
class SecretSpec:
    name: str
    required: bool = True
    schema: SecretSchema | None = None
    targets: tuple[SecretTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class DebloatConfig:
    enabled: bool = True
    paths_remove: tuple[str, ...] = DEFAULT_DEBLOAT_PATHS_REMOVE
    paths_skip: tuple[str, ...] = ()
    extra_remove_paths: tuple[str, ...] = ()
    paths_skip_for_profiles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    systemd_minimize: bool = True
    systemd_units_keep: tuple[str, ...] = DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP
    extra_keep_units: tuple[str, ...] = ()
    systemd_bins_keep: tuple[str, ...] = DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP
    clean_var_dirs: tuple[str, ...] = ("/var/log", "/var/cache")
    keep_modules: bool | None = None
    """``Debloat(keep_modules=...)``: ``True`` keeps ``/usr/lib/modules``, ``False`` removes it
    knowingly (no ``debloat-removes-modules``), ``None`` follows the removal list."""
    declared: bool = False
    """Whether a ``Debloat`` declaration set this (else it is the implicit default)."""

    @property
    def effective_paths_remove(self) -> tuple[str, ...]:
        """Paths to remove = default + extra - skipped - profile-conditional."""
        skip_set = set(self.paths_skip)
        # Also exclude paths that are conditionally skipped for profiles
        for _profile, paths in self.paths_skip_for_profiles:
            skip_set.update(paths)
        combined = list(self.paths_remove) + list(self.extra_remove_paths)
        return tuple(sorted(set(p for p in combined if p not in skip_set)))

    @property
    def profile_conditional_paths(self) -> dict[str, tuple[str, ...]]:
        """Paths that should only be removed when a specific profile is NOT active."""
        result: dict[str, list[str]] = {}
        all_paths = set(self.paths_remove) | set(self.extra_remove_paths)
        for profile_name, paths in self.paths_skip_for_profiles:
            for p in paths:
                if p in all_paths:
                    result.setdefault(profile_name, []).append(p)
        return {k: tuple(sorted(v)) for k, v in result.items()}

    @property
    def effective_units_keep(self) -> tuple[str, ...]:
        """Units to keep = default + extra."""
        return tuple(sorted(set(self.systemd_units_keep) | set(self.extra_keep_units)))


@dataclass(frozen=True, slots=True)
class Kernel:
    """The kernel the build phase compiles when ``config_file`` is set.

    The source is the git repository ``source_repo`` at ``source_ref`` (``None``:
    tag ``v<version>``), optionally a ``source_subdir`` of it with submodules, or
    the tarball ``source_archive`` checked against ``source_sha256`` instead.
    """

    version: str | None = None
    config_file: str | Path | None = None
    cmdline: str | None = None
    tdx: bool = False
    source_repo: str = "https://github.com/gregkh/linux"
    source_ref: str | None = None
    source_subdir: str | None = None
    source_submodules: bool = False
    source_archive: str | None = None
    source_sha256: str | None = None
    base_file: str | Path | None = None
    """A local base config: the start of the composed config (``config_file`` is the whole one)."""
    base_deb: str | None = None
    """The Debian ``linux-config`` package whose *base_member* is the base config."""
    base_deb_mirrors: tuple[str, ...] = ()
    """Where *base_deb* is read when its pool lacks it: the snapshot's security pool."""
    base_deb_missing: str | None = None
    """Why no pool serving *base_deb* means the snapshot lacks that version, naming it."""
    base_deb_sha256: str | None = None
    base_member: str | None = None
    options: tuple[tuple[str, str], ...] = ()
    """Requested ``CONFIG_*`` values (``n`` for off), merged onto the base and checked after
    ``olddefconfig``."""
    patches: tuple[KernelPatchSpec, ...] = ()
    modules: bool = False
    """Build and install loadable modules; ``False`` requests ``CONFIG_MODULES=n``."""
    hardening: bool | None = None
    """Whether the options carry Tundra's hardening fragment: ``False`` for
    ``Kernel.build(hardening=False)``, which ``kernel-unhardened`` reports; ``None`` when the
    internal model did not say."""

    @property
    def composed(self) -> bool:
        """Whether the config is composed from a base (with options and patches)."""
        return self.base_file is not None or self.base_deb is not None

    @property
    def built(self) -> bool:
        """Whether the build phase compiles this kernel: a whole config or a base."""
        return bool(self.config_file) or self.composed


@dataclass(frozen=True, slots=True)
class KernelPatchSpec:
    """A kernel patch applied with ``patch -p1``: a local *path*, or *url* checked by sha256."""

    name: str
    path: str | Path | None = None
    url: str | None = None
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class InitScriptEntry:
    """A runtime-init step's bash: *name* is its step, *after*/*before* its graph edges."""

    script: str
    name: str
    after: tuple[str, ...] = ()
    before: tuple[str, ...] = ()


@dataclass(slots=True)
class ProfileState:
    """One profile's own declarations.

    ``extends`` names the profile this one builds on top of: ``None`` for the
    default profile and for standalone profiles, the default profile's name for
    every other profile unless it opted out. Read the merged result through
    :meth:`RecipeState.effective_profile`.
    """

    name: str
    extends: str | None = None
    packages: set[str] = field(default_factory=set)
    build_packages: set[str] = field(default_factory=set)
    build_sources: list[tuple[str, str]] = field(default_factory=list)
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    output_targets_explicit: bool = False
    phases: dict[Phase, list[CommandSpec]] = field(default_factory=dict)
    repositories: list[RepositorySpec] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    skeleton_files: list[FileEntry] = field(default_factory=list)
    templates: list[TemplateEntry] = field(default_factory=list)
    groups: list[GroupSpec] = field(default_factory=list)
    users: list[UserSpec] = field(default_factory=list)
    services: list[ServiceSpec] = field(default_factory=list)
    unit_states: list[UnitStateSpec] = field(default_factory=list)
    partitions: list[PartitionSpec] = field(default_factory=list)
    hooks: list[HookSpec] = field(default_factory=list)
    secrets: list[SecretSpec] = field(default_factory=list)
    init_scripts: list[InitScriptEntry] = field(default_factory=list)
    debloat: DebloatConfig = field(default_factory=DebloatConfig)
    debloat_explicit: bool = False
    source_builds: dict[str, SourceBuild] = field(default_factory=dict)
    keep_bins: set[str] = field(default_factory=set)
    """Binaries of the ``systemd`` package this profile keeps beyond its debloat's list
    (``systemd-analyze`` for a probe that scores hardening)."""
    containers: list[ContainerSpec] = field(default_factory=list)


@dataclass(slots=True)
class RecipeState:
    base: str
    arch: Arch
    default_profile: str
    profiles: dict[str, ProfileState]

    @classmethod
    def initialize(cls, *, base: str, arch: Arch, default_profile: str) -> RecipeState:
        default = ProfileState(name=default_profile)
        return cls(
            base=base,
            arch=arch,
            default_profile=default_profile,
            profiles={default_profile: default},
        )

    def ensure_profile(self, name: str) -> ProfileState:
        """Return *name*'s own state, creating it (extending the default profile) if new."""
        if name not in self.profiles:
            extends = None if name == self.default_profile else self.default_profile
            self.profiles[name] = ProfileState(name=name, extends=extends)
        return self.profiles[name]

    def set_extends(self, name: str, extends: str | None) -> None:
        """Make *name* extend *extends* (``None``: standalone); only the default can be extended."""
        self._validate_extends(name, extends)
        self.ensure_profile(name).extends = extends

    def effective_profile(self, name: str) -> ProfileState:
        """What *name* builds: its own declarations merged over the profile it extends.

        The default profile's effective view is its own state (the same object).
        Every other profile gets a new :class:`ProfileState`; standalone profiles
        (``extends=None``) only take output targets and debloat from the default
        profile when they did not set them.
        """
        own = self.ensure_profile(name)
        if name == self.default_profile:
            return own
        self._validate_extends(name, own.extends)
        default = self.ensure_profile(self.default_profile)
        if own.extends is None:
            return replace(
                own,
                output_targets=_fallback_targets(default, own),
                debloat=_fallback_debloat(default, own),
            )
        return merge_profiles(default, own)

    def _validate_extends(self, name: str, extends: str | None) -> None:
        if name == self.default_profile and extends is not None:
            raise ValidationError(
                f"The default profile {name!r} cannot extend another profile.",
                hint="Drop extends= on the default profile; other profiles extend it.",
                context={"profile": name, "extends": extends},
            )
        if extends is not None and extends != self.default_profile:
            raise ValidationError(
                f"Profile {name!r} can only extend the default profile "
                f"{self.default_profile!r}, not {extends!r}.",
                hint="Pass extends=None for a standalone profile.",
                context={"profile": name, "extends": extends},
            )


def merge_profiles(base: ProfileState, own: ProfileState) -> ProfileState:
    """*own* layered over *base* as a new :class:`ProfileState`; see ``effective_profile``.

    Sets are unioned; keyed lists keep *base*'s entries that *own* does not
    redeclare, then append *own*'s; ordered lists (hooks, phases) run *base*'s
    first.
    """
    extra_paths = {_norm_path(f.path) for f in own.files}
    extra_paths.update(_norm_path(t.path) for t in own.templates)
    phases: dict[Phase, list[CommandSpec]] = {
        phase: list(commands) for phase, commands in base.phases.items()
    }
    for phase, commands in own.phases.items():
        phases.setdefault(phase, []).extend(commands)
    return ProfileState(
        name=own.name,
        extends=own.extends,
        packages=base.packages | own.packages,
        build_packages=base.build_packages | own.build_packages,
        build_sources=list(dict.fromkeys((*base.build_sources, *own.build_sources))),
        output_targets=_fallback_targets(base, own),
        output_targets_explicit=own.output_targets_explicit,
        phases=phases,
        repositories=_override(base.repositories, own.repositories, lambda r: r.name),
        files=_override(base.files, own.files, lambda f: _norm_path(f.path), extra_paths),
        skeleton_files=_override(
            base.skeleton_files, own.skeleton_files, lambda f: _norm_path(f.path)
        ),
        templates=_override(
            base.templates, own.templates, lambda t: _norm_path(t.path), extra_paths
        ),
        groups=_override(base.groups, own.groups, lambda g: g.name),
        users=_override(base.users, own.users, lambda u: u.name),
        services=_override(base.services, own.services, lambda s: unit_name(s.name)),
        unit_states=list(dict.fromkeys((*base.unit_states, *own.unit_states))),
        partitions=_override(base.partitions, own.partitions, lambda p: p.name),
        hooks=[*base.hooks, *own.hooks],
        source_builds={**base.source_builds, **own.source_builds},
        secrets=_override(base.secrets, own.secrets, lambda s: s.name),
        init_scripts=list(
            {(e.name, e.script): e for e in (*base.init_scripts, *own.init_scripts)}.values()
        ),
        debloat=_fallback_debloat(base, own),
        debloat_explicit=own.debloat_explicit,
        keep_bins=base.keep_bins | own.keep_bins,
        containers=_override(base.containers, own.containers, lambda c: c.name),
    )


def unit_name(name: str) -> str:
    """Systemd unit name for a service name (``foo`` -> ``foo.service``)."""
    return name if "." in name else f"{name}.service"


NETWORK_SETUP_UNIT = "network-setup.service"
"""The unit runtime-init requires when a profile ships it (else ``network-online.target``)."""


def enable_unit(profile: ProfileState, unit: str) -> None:
    """Enable *unit* in *profile*: postinst ``systemctl enable`` plus a ``minimal.target`` link.

    A unit *profile* already lists as a service is switched on in place.
    """
    key = unit_name(unit)
    index = next((i for i, s in enumerate(profile.services) if unit_name(s.name) == key), None)
    if index is None:
        profile.services.append(ServiceSpec(name=unit, enabled=True))
    elif not profile.services[index].enabled:
        profile.services[index] = replace(profile.services[index], enabled=True)


def declared_paths(profile: ProfileState) -> set[str]:
    """The normalized paths of the files, templates and skeleton files *profile* places."""
    paths = {_norm_path(f.path) for f in profile.files}
    paths.update(_norm_path(t.path) for t in profile.templates)
    paths.update(_norm_path(f.path) for f in profile.skeleton_files)
    return paths


def debloat_units_keep(profile: ProfileState) -> tuple[str, ...]:
    """The units *profile*'s systemd debloat leaves unmasked: its keep list, plus the packaged
    ``systemd-*`` units and targets the profile enables (an enabled instance keeps its
    template), since masking one would undo the enable. Debloat masks only the ``systemd``
    package's units, so other packages' units need no entry."""
    shipped = {PurePosixPath(f.path).name for f in (*profile.files, *profile.skeleton_files)}
    keep = set(profile.debloat.effective_units_keep)
    for svc in profile.services:
        if not svc.enabled or svc.command:
            continue
        unit = unit_name(svc.name)
        if "@" in unit and "@." not in unit:
            keep.add(unit.partition("@")[0] + "@.service")
        elif unit not in shipped and (unit.startswith("systemd-") or unit.endswith(".target")):
            keep.add(unit)
    return tuple(sorted(keep))


def ships_unit(profile: ProfileState, unit: str) -> bool:
    """Whether *profile* (an effective profile) ships, generates or enables *unit*."""
    files = (*profile.files, *profile.skeleton_files)
    return any(PurePosixPath(f.path).name == unit for f in files) or any(
        unit_name(s.name) == unit for s in profile.services
    )


def _norm_path(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/"))


def _override[T](
    base: Iterable[T],
    own: Iterable[T],
    key: Callable[[T], Hashable],
    taken: set[str] | None = None,
) -> list[T]:
    own_list = list(own)
    replaced: set[Hashable] = set(taken) if taken is not None else {key(item) for item in own_list}
    return [item for item in base if key(item) not in replaced] + own_list


def _fallback_targets(base: ProfileState, own: ProfileState) -> tuple[OutputTarget, ...]:
    return own.output_targets if own.output_targets_explicit else base.output_targets


def _fallback_debloat(base: ProfileState, own: ProfileState) -> DebloatConfig:
    return own.debloat if own.debloat_explicit else base.debloat


@dataclass(frozen=True, slots=True)
class BakeRequest:
    """One profile build. ``on_output`` receives each backend output line as it
    arrives; when it is ``None`` a failing backend puts the output tail in its
    error message instead. ``on_notice`` receives what the backend decided on the
    user's behalf (e.g. running mkosi with sudo), as ``(level, message)`` with level
    ``info`` or ``warning``."""

    profile: str
    build_dir: Path
    emit_dir: Path
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    on_output: Callable[[str], None] | None = field(default=None, compare=False, repr=False)
    on_notice: Callable[[str, str], None] | None = field(default=None, compare=False, repr=False)
    sources_dir: Path | None = None
    """The host-fetched checkouts (``<build_dir>/.sources``) the backend mounts at
    ``$SRCDIR/tundravm-sources``; ``None`` when the build fetches nothing on the host."""
    mkosi_args: tuple[str, ...] = ()
    """The mkosi options that select this variant of the tree (``declarative.mkosi_argv``:
    ``--profile=``, ``--dependency=``); backends pass them before the verb."""
    toolchain: Toolchain | None = None
    """The lockfile's toolchain (mkosi, the tools tree); ``None`` without a lockfile."""
    root: RootLayout = "initramfs"
    """The tree's root layout, which with ``uki_max_size`` sized its ESP
    (``emit_mkosi.esp_bytes``)."""
    uki_max_size: str | None = None
    """``Mkosi(uki_max_size=)``: the initramfs UKI's ceiling."""
    cache_key: str | None = None
    """What makes the build incremental: the key tundravm appends to mkosi's ``CacheKey=``
    (:func:`~tundravm.declarative._bake.incremental_key`), under which mkosi keeps the base
    tree after package install; ``None`` builds from scratch, as every production build does."""

    def notice(self, level: str, message: str) -> None:
        """Pass *message* to ``on_notice``, if set."""
        if self.on_notice is not None:
            self.on_notice(level, message)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    target: OutputTarget
    path: Path
    digest: str | None = None
    manifest_digest: str | None = None
    """The sha256 of mkosi's package manifest beside the artifact at bake time; ``None``
    when there was none."""
    boot_payload: Path | None = None
    """The UKI the disk artifact boots, when the backend found it beside the disk; ``None``
    for a UKI artifact itself or when it could not be determined."""
    boot_payload_digest: str | None = None
    """The sha256 of ``boot_payload`` at bake time."""


CacheState = Literal["hit", "saved", "none"]
"""What one image of the tree did with mkosi's incremental cache: reused its cached base
tree (``hit``), built and stored it (``saved``), or neither (``none``: mkosi wrote no cache
entry, as a stand-in does)."""


@dataclass(frozen=True, slots=True)
class CacheUse:
    """How an incremental build used mkosi's cache: the *directory* it lives in, the *key*
    tundravm appended to mkosi's ``CacheKey=`` and, per image of the tree (``payload``,
    ``main``, a cloud wrapper), its :data:`CacheState`."""

    directory: Path
    key: str
    images: Mapping[str, CacheState]


@dataclass(slots=True)
class ProfileBuildResult:
    profile: str
    artifacts: dict[OutputTarget, ArtifactRef] = field(default_factory=dict)
    duration_s: float | None = field(default=None, compare=False)
    cache: CacheUse | None = field(default=None, compare=False)
    """The incremental cache's use, when the request carried a ``cache_key`` and the backend
    can see mkosi's cache directory; ``None`` otherwise."""


@dataclass(frozen=True, slots=True)
class CompileResult:
    """Result of compile(): the tree's path, the compiled profiles and the recipe digest."""

    path: Path
    profiles: tuple[str, ...]
    digest: str

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)


@dataclass(slots=True)
class BakeResult:
    profiles: dict[str, ProfileBuildResult] = field(default_factory=dict)
    lock_digest: str | None = None
    backend: str | None = None
    duration_s: float | None = field(default=None, compare=False)
    lock_sha256: str | None = None
    """The sha256 of the lockfile bytes the bake read; ``None`` when it read none."""


@dataclass(frozen=True, slots=True)
class DeployRequest:
    profile: str
    target: OutputTarget
    artifact_path: Path
    parameters: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeployResult:
    target: OutputTarget
    deployment_id: str
    endpoint: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


__all__ = [
    "ALL_STEPS",
    "Arch",
    "ArtifactRef",
    "BakeRequest",
    "BakeResult",
    "CONTAINERS_PATH",
    "CommandSpec",
    "ContainerSpec",
    "CompileResult",
    "DebloatConfig",
    "DEFAULT_DEBLOAT_PATHS_REMOVE",
    "DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP",
    "DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP",
    "DeployRequest",
    "DeployResult",
    "FileEntry",
    "FileKind",
    "GroupSpec",
    "HookSpec",
    "Kernel",
    "KernelPatchSpec",
    "KillMode",
    "OutputTarget",
    "PartitionSpec",
    "Phase",
    "VALID_PHASES",
    "ProfileBuildResult",
    "ProfileState",
    "RepositorySpec",
    "debloat_units_keep",
    "declared_paths",
    "merge_profiles",
    "unit_name",
    "RecipeState",
    "RestartPolicy",
    "SecretSchema",
    "SecretSpec",
    "SecretTarget",
    "SecurityProfile",
    "ServiceSpec",
    "ServiceType",
    "TemplateEntry",
    "UnitAction",
    "UnitStateSpec",
    "UserSpec",
]
