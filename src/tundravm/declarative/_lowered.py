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

from tundravm._options import MkosiOptions
from tundravm._source import KernelSource, NamedSource, Resolver, SourceBuild, source_drift
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
from tundravm.models import Arch, InitScriptEntry, Kernel, ProfileState, RecipeState
from tundravm.policy import Policy

if TYPE_CHECKING:
    from tundravm._modules.base import Module
    from tundravm.backends.base import BuildBackend

LOCK_FILENAME = "tundravm.lock"


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
        """The active profiles' lockfile payload (:func:`recipe_payload`)."""
        return recipe_payload(self.state, self.active)

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
        """Source builds declared for *profile* (default: every active profile), by name."""
        builds: dict[str, SourceBuild] = {}
        for name in (profile,) if profile is not None else self.active:
            builds.update(self.state.effective_profile(name).source_builds)
        return dict(sorted(builds.items()))

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

    def lock_sources(self) -> dict[str, NamedSource]:
        """What ``lock`` pins and ``fetch`` checks out for the active profiles, by name.

        The source builds and, outside ``nethermind-v1`` (:attr:`fetches_sources`),
        the built kernels' sources (:meth:`kernel_source`).
        """
        builds = self.source_builds()
        sources: dict[str, NamedSource] = dict(builds)
        if not self.fetches_sources:
            return sources
        for profile in self.active:
            spec = self.kernel_source(profile)
            if spec is None:
                continue
            if spec.name in builds:
                raise ValidationError(
                    f"Source build {spec.name!r} takes the name of the kernel's source.",
                    hint=(
                        f"Rename the Build: {spec.name!r} names the kernel's lockfile pin "
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

    def pinned_state(self, pins: Mapping[str, LockedFetch]) -> RecipeState:
        """The state with every pinned source build's hook rendered at its pin from *pins*."""
        profiles: dict[str, ProfileState] = {}
        changed = False
        mounted = self.fetches_sources
        for name, profile in self.state.profiles.items():
            swaps = {
                spec.render(mounted=mounted): pinned
                for spec in profile.source_builds.values()
                if (pinned := spec.render(spec.pin_from(pins), mounted=mounted))
                != spec.render(mounted=mounted)
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
            elsewhere = unselected_sources(lock, payload)
            removed = [s for s in removed if s.removeprefix("sources.") not in elsewhere]
        return replace(
            drift,
            added=(*drift.added, *added),
            changed=(*drift.changed, *changed),
            removed=(*drift.removed, *removed),
            details={**drift.details, **details},
        )


def _init_scripts_payload(entries: Sequence[InitScriptEntry]) -> list[dict[str, object]]:
    return [
        {"priority": entry.priority, "sha256": hashlib.sha256(entry.script.encode()).hexdigest()}
        for entry in sorted(entries, key=lambda item: (item.priority, item.script))
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
        # The default profile's fragments are the top-level "init_scripts".
        own_init = state.ensure_profile(profile_name).init_scripts
        if profile_name != state.default_profile and own_init:
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
        "base": state.base,
        "arch": state.arch,
        "default_profile": state.default_profile,
        "init_scripts": _init_scripts_payload(
            state.ensure_profile(state.default_profile).init_scripts
        ),
        "profiles": profiles_data,
    }


__all__ = ["LOCK_FILENAME", "Lowered", "recipe_payload"]
