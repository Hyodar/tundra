"""Core typed dataclasses for recipe state and build/deploy requests."""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from .errors import StateError, ValidationError

if TYPE_CHECKING:
    from .source import SourceBuild

Arch = Literal["x86_64", "aarch64"]
OutputTarget = Literal["qemu", "azure", "gcp"]
SecurityProfile = Literal["strict", "default", "none"]
RestartPolicy = Literal["always", "on-failure", "no"]
ServiceType = Literal["simple", "exec", "oneshot", "notify", "forking"]
KillMode = Literal["control-group", "mixed", "process", "none"]
UnitAction = Literal["disable", "mask"]

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
    "local-fs-pre.target",
    "local-fs.target",
    "minimal.target",
    "network-online.target",
    "slices.target",
    "sockets.target",
    "sysinit.target",
    "systemd-journald-dev-log.socket",
    "systemd-journald.service",
    "systemd-journald.socket",
    "systemd-remount-fs.service",
    "systemd-sysctl.service",
)

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


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    content: str | bytes
    mode: str = "0644"

    @property
    def data(self) -> bytes:
        """File content as bytes (text is UTF-8 encoded)."""
        return self.content.encode() if isinstance(self.content, str) else self.content


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
    gid: int | None = None
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
    extra_unit: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
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
        }
        return {key: value for key, value in optional.items() if value}


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
    kind: Literal["file", "env"]
    location: str
    mode: str = "0400"
    scope: Literal["service", "global"] = "service"
    owner: str | None = None

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
    def env(cls, name: str, *, scope: Literal["service", "global"] = "service") -> SecretTarget:
        return cls(kind="env", location=name, mode="0400", scope=scope)


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
    version: str | None = None
    config_file: str | Path | None = None
    cmdline: str | None = None
    tdx: bool = False
    source_repo: str = "https://github.com/gregkh/linux"

    @classmethod
    def generic(cls, version: str, *, cmdline: str | None = None) -> Kernel:
        return cls(version=version, cmdline=cmdline)

    @classmethod
    def from_config(cls, config_file: str) -> Kernel:
        return cls(config_file=config_file)

    @classmethod
    def tdx_kernel(
        cls,
        version: str,
        *,
        cmdline: str | None = None,
        config_file: str | Path | None = None,
        source_repo: str = "https://github.com/gregkh/linux",
    ) -> Kernel:
        return cls(
            version=version,
            tdx=True,
            cmdline=cmdline,
            config_file=config_file,
            source_repo=source_repo,
        )


@dataclass(frozen=True, slots=True)
class InitScriptEntry:
    """A fragment of bash to include in the runtime-init script."""

    script: str
    priority: int = 100


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
            {(e.priority, e.script): e for e in (*base.init_scripts, *own.init_scripts)}.values()
        ),
        debloat=_fallback_debloat(base, own),
        debloat_explicit=own.debloat_explicit,
    )


def unit_name(name: str) -> str:
    """Systemd unit name for a service name (``foo`` -> ``foo.service``)."""
    return name if "." in name else f"{name}.service"


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
    error message instead."""

    profile: str
    build_dir: Path
    emit_dir: Path
    output_targets: tuple[OutputTarget, ...] = ("qemu",)
    on_output: Callable[[str], None] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    target: OutputTarget
    path: Path
    digest: str | None = None


@dataclass(slots=True)
class ProfileBuildResult:
    profile: str
    artifacts: dict[OutputTarget, ArtifactRef] = field(default_factory=dict)
    report_path: Path | None = None
    duration_s: float | None = field(default=None, compare=False)


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


BAKE_RESULT_FILENAME = "bake-result.json"
BAKE_RESULT_SCHEMA_VERSION = 1


@dataclass(slots=True)
class BakeResult:
    profiles: dict[str, ProfileBuildResult] = field(default_factory=dict)
    lock_digest: str | None = None
    backend: str | None = None
    created_at: str | None = None
    duration_s: float | None = field(default=None, compare=False)

    def artifact_for(self, *, profile: str, target: OutputTarget) -> ArtifactRef | None:
        profile_result = self.profiles.get(profile)
        if profile_result is None:
            return None
        return profile_result.artifacts.get(target)

    def save(self, build_dir: str | Path) -> Path:
        """Write ``<build_dir>/bake-result.json``; paths inside build_dir are stored relative."""
        base = Path(build_dir)
        base.mkdir(parents=True, exist_ok=True)
        profiles: dict[str, object] = {}
        for name, result in sorted(self.profiles.items()):
            artifacts = sorted(result.artifacts.items())
            entry: dict[str, object] = {
                "artifacts": {target: _portable_path(ref.path, base) for target, ref in artifacts},
                "report_path": (
                    None if result.report_path is None else _portable_path(result.report_path, base)
                ),
            }
            digests = {target: ref.digest for target, ref in artifacts if ref.digest is not None}
            if digests:
                entry["artifact_digests"] = digests
            profiles[name] = entry
        payload = {
            "schema_version": BAKE_RESULT_SCHEMA_VERSION,
            "created_at": self.created_at,
            "backend": self.backend,
            "lock_digest": self.lock_digest,
            "profiles": profiles,
        }
        path = base / BAKE_RESULT_FILENAME
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, build_dir: str | Path) -> BakeResult:
        """Read ``<build_dir>/bake-result.json`` written by :meth:`save`."""
        base = Path(build_dir)
        path = base / BAKE_RESULT_FILENAME
        context = {"path": str(path)}
        if not path.is_file():
            raise StateError(
                "No bake result found.",
                hint="Run bake() / tundravm bake first.",
                context=context,
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls._from_payload(payload, base)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise StateError(
                f"Unreadable bake result: {exc}",
                hint="Run bake() / tundravm bake again to regenerate it.",
                context=context,
            ) from exc

    @classmethod
    def _from_payload(cls, payload: Any, base: Path) -> BakeResult:
        if payload.get("schema_version") != BAKE_RESULT_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version {payload.get('schema_version')!r}")
        profiles: dict[str, ProfileBuildResult] = {}
        for name, entry in payload["profiles"].items():
            digests: dict[str, str] = entry.get("artifact_digests", {})
            artifacts: dict[OutputTarget, ArtifactRef] = {}
            for target, raw_path in entry["artifacts"].items():
                if target not in get_args(OutputTarget):
                    raise ValueError(f"unknown output target {target!r}")
                typed = cast(OutputTarget, target)
                artifacts[typed] = ArtifactRef(
                    target=typed,
                    path=_resolve_path(raw_path, base),
                    digest=digests.get(target),
                )
            report = entry.get("report_path")
            profiles[str(name)] = ProfileBuildResult(
                profile=str(name),
                artifacts=artifacts,
                report_path=None if report is None else _resolve_path(report, base),
            )
        return cls(
            profiles=profiles,
            lock_digest=payload.get("lock_digest"),
            backend=payload.get("backend"),
            created_at=payload.get("created_at"),
        )


def _portable_path(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(Path(path).resolve())


def _resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base / path


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
    "Arch",
    "ArtifactRef",
    "BakeRequest",
    "BAKE_RESULT_FILENAME",
    "BakeResult",
    "CommandSpec",
    "CompileResult",
    "DebloatConfig",
    "DEFAULT_DEBLOAT_PATHS_REMOVE",
    "DEFAULT_DEBLOAT_SYSTEMD_BINS_KEEP",
    "DEFAULT_DEBLOAT_SYSTEMD_UNITS_KEEP",
    "DeployRequest",
    "DeployResult",
    "FileEntry",
    "GroupSpec",
    "HookSpec",
    "Kernel",
    "KillMode",
    "OutputTarget",
    "PartitionSpec",
    "Phase",
    "VALID_PHASES",
    "ProfileBuildResult",
    "ProfileState",
    "RepositorySpec",
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
