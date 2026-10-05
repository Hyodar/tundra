"""The lowered recipe: the compiler's state and what compiling, locking and baking it needs.

:func:`~tundravm.declarative.lower` builds a :class:`Lowered`; ``_compile``
emits it as an mkosi tree and ``_bake`` builds it. It is a value: an operation
on some variants, another lockfile or another build directory runs on a copy
(:meth:`Lowered.select`, :func:`dataclasses.replace`). Not a public API.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from tundravm._modules.init import runtime_init_order
from tundravm._options import MkosiOptions
from tundravm._source import (
    DebFile,
    KernelSource,
    NamedSource,
    Resolver,
    SourceBuild,
    fingerprint,
    source_drift,
    source_section,
)
from tundravm.compiler import PHASE_ORDER
from tundravm.errors import ValidationError
from tundravm.lockfile import (
    LockDrift,
    LockedFetch,
    Lockfile,
    compare_lock,
    read_lockfile,
    recipe_digest,
    unselected_sources,
)
from tundravm.models import (
    Arch,
    FileEntry,
    InitScriptEntry,
    Kernel,
    ProfileState,
    RecipeState,
)
from tundravm.policy import Policy

if TYPE_CHECKING:
    from tundravm._modules.base import Module
    from tundravm.backends.base import BuildBackend

LOCK_FILENAME = "tundravm.lock"
EFI_STUB_SOURCE = "efi-stub"
"""The lockfile name of the package an ``EfiStub`` installs (``efi-stub-<variant>`` where a
variant's differs from the default variant's)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Lowered:
    """A recipe lowered onto the compiler: per-profile state, mkosi options, kernels and tools.

    Recipe-wide mkosi options and the kernel apply to every profile but those in
    :attr:`profile_mkosi` and :attr:`profile_kernels`. Operations run on
    :attr:`active`, the :meth:`select`-ed profiles.
    """

    state: RecipeState
    modules: Mapping[str, Sequence[Module]] = field(default_factory=dict, repr=False)
    """The runtime tools configured on each profile, which the checks inspect."""
    mkosi: MkosiOptions = field(default_factory=MkosiOptions)
    profile_mkosi: Mapping[str, MkosiOptions] = field(default_factory=dict)
    """Profiles whose mkosi options differ from ``mkosi``."""
    kernel: Kernel | None = None
    profile_kernels: Mapping[str, Kernel | None] = field(default_factory=dict)
    """Profiles whose kernel differs from ``kernel`` (``None``: no kernel)."""
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    snapshot: str | None = None
    epoch: int | None = None
    """``Recipe.epoch``: ``SOURCE_DATE_EPOCH``, ``None`` for a non-reproducible build."""
    reproducible: bool = True
    policy: Policy = field(default_factory=Policy)
    backend: BuildBackend | None = None
    build_dir: Path = Path("build")
    lock_file: Path | None = None
    """The lockfile to read instead of ``<build_dir>/tundravm.lock``."""
    fetched_pins: Mapping[str, LockedFetch] = field(default_factory=dict, repr=False)
    """Pins ``fetch`` resolved for sources the lockfile does not pin (unlocked bakes)."""
    selected: tuple[str, ...] = ()
    """The profiles operations run on; empty: the default profile."""
    local_sources: frozenset[str] = frozenset()
    """Source builds whose source differs between the recipe's variants (every variant,
    selected or not): each variant's is pinned on its own, as ``<variant>/<name>``."""
    deb_files: tuple[DebFile, ...] = ()
    """The packages the recipe's ``EfiStub`` hooks install, outside ``nethermind-v1``:
    sources :meth:`deb_file` names per profile."""
    secret_allowed: Mapping[str, frozenset[str]] = field(default_factory=dict, repr=False)
    """Per profile, what declares ``allow_secret=True``: file paths, ``Directory`` paths
    ending in ``/`` and service unit names. Outside the recipe digest."""

    @property
    def base(self) -> str:
        return self.state.base

    @property
    def arch(self) -> Arch:
        return self.state.arch

    @property
    def default_profile(self) -> str:
        return self.state.default_profile

    @property
    def profile_names(self) -> tuple[str, ...]:
        """Every lowered profile, sorted."""
        return tuple(sorted(self.state.profiles))

    @property
    def active(self) -> tuple[str, ...]:
        """The profiles operations run on."""
        return self.selected or (self.state.default_profile,)

    def select(self, profiles: Sequence[str] | None) -> Lowered:
        """This recipe operating on *profiles* (deduplicated); ``None`` keeps the selection."""
        if profiles is None:
            return self
        names = tuple(dict.fromkeys((profiles,) if isinstance(profiles, str) else profiles))
        if not names or not all(names):
            raise ValidationError(
                "Variant names must be non-empty, and at least one is required.",
                hint="Name a declared variant, e.g. 'default'.",
            )
        unknown = [name for name in names if name not in self.state.profiles]
        if unknown:
            raise ValidationError(
                f"Unknown variant(s): {', '.join(unknown)}.",
                hint=f"Declared variants: {', '.join(self.profile_names)}",
            )
        return replace(self, selected=names)

    def profile(self, profile: str | None = None) -> str:
        """*profile*, or the only active one."""
        if profile is not None:
            return profile
        if len(self.active) == 1:
            return self.active[0]
        raise ValidationError(
            "Operation requires an explicit profile when multiple profiles are active.",
            hint="Pass profile='name' to the operation.",
            context={"operation": "resolve_profile"},
        )

    def mkosi_for(self, profile: str) -> MkosiOptions:
        """The mkosi options *profile* is emitted with."""
        return self.profile_mkosi.get(profile, self.mkosi)

    def kernel_for(self, profile: str) -> Kernel | None:
        """The kernel *profile* builds."""
        return self.profile_kernels.get(profile, self.kernel)

    def applied_modules(
        self, profile: str | None = None, *, inherited: bool = False
    ) -> tuple[Module, ...]:
        """The runtime tools configured on *profile* (default: the active one), in order.

        With ``inherited=True`` those of the profile it extends come first.
        """
        selected = self.profile(profile)
        own = tuple(self.modules.get(selected, ()))
        extends = self.state.ensure_profile(selected).extends
        if not inherited or extends is None:
            return own
        base = tuple(self.modules.get(extends, ()))
        return base + tuple(m for m in own if not any(m is b for b in base))

    def init_scripts(self, profile: str | None = None) -> tuple[InitScriptEntry, ...]:
        """Runtime-init fragments *profile* (default: the active one) runs, deduplicated.

        Registration order, the default profile's first for a profile that extends it.
        """
        entries = self.state.effective_profile(self.profile(profile)).init_scripts
        return tuple({(e.priority, e.script): e for e in entries}.values())

    def explain_debloat(self, *, profile: str | None = None) -> dict[str, object]:
        selected = self.profile(profile)
        config = self.state.effective_profile(selected).debloat
        return {
            "profile": selected,
            "enabled": config.enabled,
            "paths_remove": list(config.effective_paths_remove),
            "paths_skip": list(config.paths_skip),
            "systemd_minimize": config.systemd_minimize,
            "systemd_units_keep": list(config.effective_units_keep),
            "systemd_bins_keep": list(config.systemd_bins_keep),
        }

    def payload(self) -> dict[str, object]:
        """The active profiles' lockfile payload.

        :func:`recipe_payload` of the state, plus what lies outside it: the
        ``distribution`` (base, arch, mirrors, snapshot, epoch), the ``compiler``
        (tundravm version, dialect, the default variant's mkosi options) and per
        variant its ``kernel`` (version, source, cmdline, tdx, sha256 of the config
        bytes) and, where they differ from the default variant's, its ``mkosi`` options.
        """
        from tundravm import __version__

        payload = recipe_payload(self.state, self.active)
        profiles = payload["profiles"]
        assert isinstance(profiles, dict)
        for name, entry in profiles.items():
            entry["kernel"] = kernel_payload(self.kernel_for(name))
            if name in self.profile_mkosi:
                entry["mkosi"] = mkosi_payload(self.profile_mkosi[name])
        return {
            "distribution": {
                "base": self.state.base,
                "arch": self.state.arch,
                "mirror": self.mirror,
                "tools_mirror": self.tools_tree_mirror,
                "snapshot": self.snapshot,
                "epoch": self.epoch,
            },
            "compiler": {
                "tundravm": __version__,
                "dialect": self.mkosi.dialect,
                "mkosi": mkosi_payload(self.mkosi),
            },
            **payload,
        }

    def digest(self) -> str:
        """The recipe digest of the active profiles, as the lockfile records it."""
        return recipe_digest(self.payload())

    # ── sources and pins ────────────────────────────────────────────────

    @property
    def fetches_sources(self) -> bool:
        """Whether source builds build host-fetched checkouts (every dialect but nethermind-v1).

        Their hooks, and the kernel build script, copy
        ``$SRCDIR/tundravm-sources/<name>-<pin[:12]>``, which ``tundravm fetch``
        writes to ``<build_dir>/.sources`` and the backend mounts; under
        ``nethermind-v1`` the hooks and the kernel script fetch in the build sandbox.
        """
        return self.mkosi.dialect != "nethermind-v1"

    def source_builds(self, *, profile: str | None = None) -> dict[str, SourceBuild]:
        """Source builds declared for *profile* (default: every active profile), by lock key.

        The key is the build's name, or ``<variant>/<name>`` for a build in
        :attr:`local_sources` (:meth:`keyed`).
        """
        builds: dict[str, SourceBuild] = {}
        for name in (profile,) if profile is not None else self.active:
            for build in self.state.effective_profile(name).source_builds.values():
                spec = self.keyed(name, build)
                builds[spec.key] = spec
        return dict(sorted(builds.items()))

    def source_key(self, profile: str, name: str) -> str:
        """The lockfile key of *profile*'s source build *name*.

        *name* itself, unless the build's source differs between variants: then
        ``<variant>/<name>``, the variant being the one whose hook builds it (the
        default variant for a build an extending variant inherits).
        """
        if name not in self.local_sources:
            return name
        own = self.state.ensure_profile(profile)
        if name not in own.source_builds and own.extends is not None:
            profile = own.extends
        return f"{profile}/{name}"

    def keyed(self, profile: str, build: SourceBuild) -> SourceBuild:
        """*build* (declared for *profile*) under its lockfile key (:meth:`source_key`)."""
        key = self.source_key(profile, build.name)
        return build if key == build.name else replace(build, lock_name=key)

    def kernel_source(self, profile: str) -> KernelSource | None:
        """The source of the kernel *profile* builds, under its lockfile name.

        ``None`` when the profile builds no kernel (none, or one without a
        config). The name is ``kernel``, or ``kernel-<profile>`` when the
        profile's kernel source differs from the default profile's.
        """
        kernel = self.kernel_for(profile)
        if kernel is None or not kernel.config_file:
            return None
        spec = KernelSource.of(kernel)
        if profile in self.profile_kernels and (
            self.kernel is None or KernelSource.of(self.kernel).source != spec.source
        ):
            return replace(spec, name=f"kernel-{profile}")
        return spec

    def deb_file(self, profile: str) -> DebFile | None:
        """The ``EfiStub`` package *profile*'s postinst hooks install, under its lockfile name.

        ``None`` when it installs none (or under ``nethermind-v1``). The name is
        ``efi-stub``, or ``efi-stub-<profile>`` when the profile's package differs
        from the default profile's.
        """
        found = self._deb_of(profile)
        if found is None:
            return None
        default = self._deb_of(self.default_profile)
        if found == default or (default is None and len(self.deb_files) == 1):
            return found
        return replace(found, name=f"{EFI_STUB_SOURCE}-{profile}")

    def _deb_of(self, profile: str) -> DebFile | None:
        if not self.deb_files:
            return None
        scripts = {
            hook.command.argv[0]
            for hook in self.state.effective_profile(profile).hooks
            if hook.command.argv
        }
        return next((deb for deb in self.deb_files if deb.script in scripts), None)

    def lock_sources(self) -> dict[str, NamedSource]:
        """What ``lock`` pins and ``fetch`` checks out for the active profiles, by name.

        The source builds and, outside ``nethermind-v1`` (:attr:`fetches_sources`),
        the built kernels' sources (:meth:`kernel_source`) and the ``EfiStub``
        packages (:meth:`deb_file`).
        """
        builds = self.source_builds()
        sources: dict[str, NamedSource] = dict(builds)
        if not self.fetches_sources:
            return sources
        for profile in self.active:
            for spec, what, owner in (
                (self.kernel_source(profile), "the kernel's source", "the kernel's"),
                (self.deb_file(profile), "the EfiStub package", "the EfiStub package's"),
            ):
                if spec is None:
                    continue
                if spec.name in builds:
                    raise ValidationError(
                        f"Source build {spec.name!r} takes the name of {what}.",
                        hint=(
                            f"Rename the Build: {spec.name!r} names {owner} lockfile pin "
                            "and checkout."
                        ),
                        context={"profile": profile},
                    )
                sources[spec.name] = spec
        return dict(sorted(sources.items()))

    @property
    def lock_path(self) -> Path:
        """The lockfile this recipe reads: :attr:`lock_file`, else ``<build_dir>/tundravm.lock``."""
        return self.lock_file if self.lock_file is not None else self.build_dir / LOCK_FILENAME

    def source_pins(self, path: Path | None = None) -> dict[str, LockedFetch]:
        """The named pins of the lockfile at *path* (default :attr:`lock_path`), if it exists."""
        lock_path = self.lock_path if path is None else Path(path)
        if not lock_path.exists():
            return {}
        lock = read_lockfile(lock_path)
        return {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}

    def build_pins(self) -> dict[str, LockedFetch]:
        """The lockfile's source pins, then :attr:`fetched_pins` for the sources it lacks."""
        return {**self.source_pins(), **self.fetched_pins}

    def unpinned_sources(self, path: Path | None = None) -> list[str]:
        """Names of the active :meth:`lock_sources` the lockfile at *path* does not pin."""
        pins = self.source_pins(path)
        return [name for name, spec in self.lock_sources().items() if spec.pin_from(pins) is None]

    def build_distribution(self, profile: str) -> dict[str, object]:
        """What *profile*'s source builds compile against, for their cache fingerprint.

        The base, architecture, mirror and snapshot, the variant's repositories
        and build packages, and its ``Build`` settings that shape the build
        sandbox (environment, sandbox trees and files, verbatim keys such as
        ``ToolsTree``), so a persistent ``BuildDirectory`` never restores a binary
        built by another toolchain. ``PackageCacheDirectory`` and ``WithNetwork``
        choose where packages come from, not which, and stay out.
        """
        effective = self.state.effective_profile(profile)
        options = self.mkosi_for(profile)
        return {
            "base": self.state.base,
            "arch": self.arch,
            "mirror": self.mirror,
            "snapshot": self.snapshot,
            "repositories": [
                {
                    "name": repository.name,
                    "url": repository.url,
                    "suite": repository.suite,
                    "components": list(repository.components),
                    "priority": repository.priority,
                }
                for repository in sorted(
                    effective.repositories, key=lambda item: (item.name, item.url)
                )
            ],
            "build_packages": sorted(effective.build_packages),
            "build_settings": {
                "environment": dict(sorted(options.environment.items())),
                "environment_passthrough": list(options.environment_passthrough or ()),
                "sandbox_trees": list(options.sandbox_trees),
                "sandbox_files": [
                    {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                    for path, text in options.sandbox_files
                ],
                "settings": [
                    [key, list(values)]
                    for section, key, values in options.settings
                    if section == "Build"
                ],
            },
        }

    def distribution_fingerprint(self, profile: str) -> str:
        """The :func:`~tundravm._source.fingerprint` of *profile*'s :meth:`build_distribution`."""
        return fingerprint(self.build_distribution(profile))

    def pinned_state(
        self, pins: Mapping[str, LockedFetch], *, variant: str | None = None
    ) -> RecipeState:
        """The state with every pinned source build's hook rendered at its pin from *pins*.

        A mounted hook's fingerprint takes the distribution of the profile that
        declares the build, or with *variant* that of *variant* for every
        profile, the builds *variant* inherits included: what its own tree runs.
        """
        profiles: dict[str, ProfileState] = {}
        changed = False
        mounted = self.fetches_sources
        shared = self.build_distribution(variant) if mounted and variant is not None else None
        for name, profile in self.state.profiles.items():
            distribution = shared
            if mounted and shared is None:
                distribution = self.build_distribution(name)
            swaps = {
                spec.render(mounted=mounted): pinned
                for spec in profile.source_builds.values()
                if (
                    pinned := spec.render(
                        self.keyed(name, spec).pin_from(pins),
                        mounted=mounted,
                        distribution=distribution,
                    )
                )
                != spec.render(mounted=mounted)
            }
            deb = self.deb_file(name)
            if deb is not None and (pinned := deb.render(deb.pin_from(pins))) != deb.script:
                swaps[deb.script] = pinned
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
        return replace(self.state, profiles=profiles) if changed else self.state

    # ── lock drift ──────────────────────────────────────────────────────

    def lock_status(
        self, path: Path | None = None, *, resolver: Resolver | None = None
    ) -> LockDrift:
        """Compare the lockfile at *path* (default :attr:`lock_path`) with the active profiles.

        Never writes. The returned :class:`~tundravm.lockfile.LockDrift` lists
        changed, added and removed sections; ``render()`` prints them (``~
        variants.default.packages: +htop``) or ``lock is up to date``. A lockfile
        written before section digests existed reports every section as added.
        Source builds report as ``+ sources.<name>: source <name> is not pinned`` or
        ``~ sources.<name>: <old> -> <new>`` (pinned for another ref, or, with
        *resolver*, the ref has moved); without *resolver* nothing touches the
        network. Raises :class:`LockfileError` when the lockfile is missing or
        unreadable. A selection that leaves out some profile is checked against
        its own sections only (see :func:`~tundravm.lockfile.compare_lock`).
        """
        lock = read_lockfile(self.lock_path if path is None else Path(path))
        partial = not set(self.state.profiles) <= set(self.active)
        return self.drift(lock, resolver=resolver, partial=partial)

    def drift(self, lock: Lockfile, *, resolver: Resolver | None, partial: bool) -> LockDrift:
        """Section and source drift of the active profiles against *lock*."""
        payload = self.payload()
        drift = compare_lock(lock, payload, partial=partial)
        pins = {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}
        added, changed, removed, details = source_drift(
            self.lock_sources(), pins, resolver=resolver
        )
        if partial:
            elsewhere = {source_section(key) for key in unselected_sources(lock, payload)}
            removed = [s for s in removed if s not in elsewhere]
        return replace(
            drift,
            added=(*drift.added, *added),
            changed=(*drift.changed, *changed),
            removed=(*drift.removed, *removed),
            details={**drift.details, **details},
        )


def kernel_payload(kernel: Kernel | None) -> dict[str, object] | None:
    """The lockfile section of the kernel a variant builds: ``None`` when it builds none.

    The config is hashed by its bytes; a config file that does not exist is
    recorded by its path (compiling it fails).
    """
    if kernel is None:
        return None
    config: dict[str, object] = {"config_sha256": None}
    if kernel.config_file:
        path = Path(kernel.config_file)
        if path.is_file():
            config["config_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            config["config_missing"] = str(kernel.config_file)
    return {
        "version": kernel.version,
        "source": KernelSource.of(kernel).source.to_payload(),
        "cmdline": kernel.cmdline,
        "tdx": kernel.tdx,
        **config,
    }


def mkosi_payload(options: MkosiOptions) -> dict[str, object]:
    """*options* that differ from the defaults, JSON-ready; sandbox files by sha256.

    The dialect is the ``compiler`` section's own key.
    """
    found: dict[str, object] = {}
    for name, value in options.non_defaults().items():
        if name == "dialect":
            continue
        if name == "sandbox_files":
            found[name] = [
                {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                for path, text in options.sandbox_files
            ]
            continue
        found[name] = _json_ready(value)
    return found


def _json_ready(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in sorted(value.items())}
    if isinstance(value, tuple | list):
        return [_json_ready(item) for item in value]
    return value


def _file_payload(entry: FileEntry) -> dict[str, object]:
    """A path in the image: its kind (file, symlink or directory), mode and content sha256."""
    return {
        "path": entry.path,
        "kind": entry.kind,
        "mode": entry.mode,
        "sha256": hashlib.sha256(entry.data).hexdigest(),
    }


def _init_scripts_payload(entries: Sequence[InitScriptEntry]) -> list[dict[str, object]]:
    """The runtime-init fragments in the order the emitted script runs them."""
    return [
        {"priority": entry.priority, "sha256": hashlib.sha256(entry.script.encode()).hexdigest()}
        for entry in runtime_init_order(entries)
    ]


def recipe_payload(state: RecipeState, profile_names: Sequence[str]) -> dict[str, object]:
    """The lockfile payload of *profile_names*: what ``recipe_digest`` and section digests hash."""
    profiles_data: dict[str, dict[str, object]] = {}
    for profile_name in sorted(profile_names):
        profile = state.effective_profile(profile_name)
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
                **({} if repository.in_image else {"in_image": False}),
            }
            for repository in sorted(
                profile.repositories,
                key=lambda item: (item.priority, item.name, item.url),
            )
        ]
        files = [_file_payload(entry) for entry in sorted(profile.files, key=lambda e: e.path)]
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
            _file_payload(entry) for entry in sorted(profile.skeleton_files, key=lambda e: e.path)
        ]
        # The default profile's payload carries no "extends" key so default-only
        # recipes keep their digests.
        inheritance: dict[str, object] = (
            {} if profile_name == state.default_profile else {"extends": profile.extends}
        )
        # Groups and unit states are keyed only when declared so older recipes keep
        # their digests.
        declared: dict[str, object] = {}
        if profile.groups:
            declared["groups"] = [
                {"name": g.name, "system": g.system, "gid": g.gid}
                for g in sorted(profile.groups, key=lambda item: item.name)
            ]
        # The default profile's fragments are the top-level "init_scripts"; a variant
        # adding its own records the merged sequence its runtime-init runs.
        own_init = state.ensure_profile(profile_name).init_scripts
        if profile_name != state.default_profile and own_init:
            declared["init_scripts"] = _init_scripts_payload(profile.init_scripts)
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
                "paths_skip": sorted(profile.debloat.paths_skip),
                "variant_paths": {
                    variant: list(paths)
                    for variant, paths in sorted(profile.debloat.profile_conditional_paths.items())
                },
                "systemd_minimize": profile.debloat.systemd_minimize,
                "systemd_units_keep": list(profile.debloat.effective_units_keep),
                "systemd_bins_keep": sorted(profile.debloat.systemd_bins_keep),
                "clean_var_dirs": list(profile.debloat.clean_var_dirs),
            },
        }

    return {
        "default_profile": state.default_profile,
        "init_scripts": _init_scripts_payload(
            state.ensure_profile(state.default_profile).init_scripts
        ),
        "profiles": profiles_data,
    }


__all__ = ["LOCK_FILENAME", "Lowered", "kernel_payload", "mkosi_payload", "recipe_payload"]
