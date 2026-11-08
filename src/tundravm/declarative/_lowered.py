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
    DotnetBuild,
    HttpSource,
    KernelFile,
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
    LockedToolchain,
    Lockfile,
    compare_lock,
    image_digest,
    read_lockfile,
    unselected_sources,
)
from tundravm.models import (
    Arch,
    FileEntry,
    InitScriptEntry,
    Kernel,
    OutputTarget,
    ProfileState,
    RecipeState,
    RepositorySpec,
)
from tundravm.policy import Policy
from tundravm.toolchain import ToolsTree, locked_version

if TYPE_CHECKING:
    from tundravm._modules.base import Module
    from tundravm._modules.probe import Probe
    from tundravm.backends.base import BuildBackend

LOCK_FILENAME = "tundravm.lock"
EFI_STUB_SOURCE = "efi-stub"
"""The lockfile name of the package an ``EfiStub`` installs (``efi-stub-<variant>`` where a
variant's differs from the default variant's)."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Lowered:
    """A recipe lowered onto the compiler: per-profile state, mkosi options, kernel and tools.

    Every variant is lowered, whatever is selected. The default profile is the payload
    every other builds on: a profile extends it, adding content (an overlay,
    :attr:`overlays`) or only the outputs it selects (:attr:`targets`), or is lowered whole
    as a standalone profile (an overlay too) when it extends another, removes or replaces.
    The emitter writes what every profile shares once and each overlay's own beyond it.
    The mkosi options and the kernel are the payload's, so every profile's. Operations
    run on :attr:`active`, the :meth:`select`-ed profiles.
    """

    state: RecipeState
    modules: Mapping[str, Sequence[Module]] = field(default_factory=dict, repr=False)
    """The runtime tools configured on each profile, which the checks inspect."""
    image_id: str = "image"
    """The tree's ``ImageId=``: the recipe name."""
    mkosi: MkosiOptions = field(default_factory=MkosiOptions)
    kernel: Kernel | None = None
    targets: Mapping[str, tuple[OutputTarget, ...]] = field(default_factory=dict)
    """Each lowered profile's outputs (``qemu``, ``azure``, ``gcp``); the payload carries the
    glue of every target of the recipe."""
    overlays: tuple[str, ...] = ()
    """The profiles emitted as ``payload/mkosi.profiles/<name>/`` (their content differs
    from the default profile's)."""
    overlay_sandbox_files: Mapping[str, tuple[tuple[str, str], ...]] = field(
        default_factory=dict, repr=False
    )
    """Per overlay, the build-sandbox files only it adds (a variant's ``Backports``)."""
    concerns: Mapping[tuple[str, str], str] = field(default_factory=dict, repr=False)
    """The payload's packages by ``(name, role)``: the concern whose ``mkosi.conf.d`` file
    lists each (absent: the payload's own ``mkosi.conf``)."""
    mirror: str | None = None
    tools_tree_mirror: str | None = None
    snapshot: str | None = None
    epoch: int | None = None
    """``Platform.epoch``: ``SOURCE_DATE_EPOCH``, ``None`` for a non-reproducible build."""
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
    """Source builds whose source differs between the recipe's variants (every variant,
    selected or not): each variant's is pinned on its own, as ``<variant>/<name>``."""
    deb_files: tuple[DebFile, ...] = ()
    """The packages the recipe's ``EfiStub`` hooks install: sources :meth:`deb_file` names
    per profile."""
    secret_allowed: Mapping[str, frozenset[str]] = field(default_factory=dict, repr=False)
    """Per profile, what declares ``allow_secret=True``: file paths, ``FileTree`` paths
    ending in ``/`` and service unit names. Outside the recipe digest."""
    unreleased: frozenset[str] = frozenset()
    """Profiles of variants marked ``Variant(release=False)``: their payload carries
    ``"release": false`` (an eligible variant's carries nothing, so its digest is unchanged)."""
    probes: Mapping[str, Probe] = field(default_factory=dict, repr=False)
    """The boot probe each development profile carries (``tundravm._modules.probe``); a
    production profile has none."""

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
                "Profile names must be non-empty, and at least one is required.",
                hint="Name a declared profile, e.g. 'production'.",
            )
        unknown = [name for name in names if name not in self.state.profiles]
        if unknown:
            raise ValidationError(
                f"Unknown profile(s): {', '.join(unknown)}.",
                hint=f"Declared profiles: {', '.join(self.profile_names)}",
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

    @property
    def tools_tree(self) -> ToolsTree:
        """The tools tree the tree pins (``ToolsTreeDistribution=``, ``ToolsTreeRelease=``,
        ``ToolsTreeSnapshot=``): what ``mkosi summary --json`` resolves as ``Tools``."""
        distribution, _, release = self.state.base.partition("/")
        return ToolsTree(distribution=distribution, release=release, snapshot=self.snapshot)

    def clouds(self, profile: str) -> tuple[str, ...]:
        """The cloud wrappers *profile* builds beside the QEMU disk (its outputs but qemu)."""
        return tuple(t for t in self.targets.get(profile, ()) if t != "qemu")

    def mkosi_profiles(self, profile: str) -> tuple[str, ...]:
        """The mkosi profiles *profile* is built with: itself when it is an overlay."""
        return (profile,) if profile in self.overlays else ()

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
        return tuple({(e.name, e.script): e for e in entries}.values())

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
        (tundravm version, the mkosi options) and per variant its ``kernel`` (version,
        source, cmdline, tdx, sha256 of the config bytes) and its ``outputs``.
        """
        payload = recipe_payload(self.state, self.active)
        profiles = payload["profiles"]
        assert isinstance(profiles, dict)
        for name, entry in profiles.items():
            entry["kernel"] = kernel_payload(self.kernel)
            entry["outputs"] = list(self.targets.get(name, ("qemu",)))
            if name in self.unreleased:
                entry["release"] = False
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
                "tundravm": locked_version(),
                "mkosi": mkosi_payload(self.mkosi),
            },
            **payload,
        }

    def digest(self) -> str:
        """The recipe digest of the active profiles, as the lockfile records it."""
        return image_digest(self.payload())

    # ── sources and pins ────────────────────────────────────────────────

    def source_builds(self, *, profile: str | None = None) -> dict[str, SourceBuild]:
        """Source builds declared for *profile* (default: every active profile), by lock key.

        The key is the build's name: every variant builds the payload's builds, and a
        variant that would replace one is refused.
        """
        builds: dict[str, SourceBuild] = {}
        for name in (profile,) if profile is not None else self.active:
            for build in self.state.effective_profile(name).source_builds.values():
                spec = self.keyed(name, build)
                builds[spec.key] = spec
        return dict(sorted(builds.items()))

    def keyed(self, profile: str, build: SourceBuild) -> SourceBuild:
        """*build* (declared for *profile*) under its lockfile key: its name."""
        del profile
        return build

    def kernel_source(self, profile: str) -> KernelSource | None:
        """The source of the kernel *profile* builds, under its lockfile name.

        ``None`` when the recipe builds no kernel (none, or one without a config). Every
        profile builds the payload's kernel, named ``kernel``.
        """
        del profile
        if self.kernel is None or not self.kernel.built:
            return None
        return KernelSource.of(self.kernel)

    def kernel_files(self, profile: str) -> tuple[KernelFile, ...]:
        """The downloads *profile*'s composed kernel reads, under their lockfile names.

        Its Debian config package (``kernel-config``) and http patches
        (``kernel-patch-<name>``); ``-<profile>`` is appended to a file that differs
        from the default profile's.
        """
        default = {} if profile == self.default_profile else self._kernel_files(None)
        return tuple(
            spec
            if default.get(spec.name) in (None, spec)
            else replace(spec, name=f"{spec.name}-{profile}")
            for spec in self._kernel_files(profile).values()
        )

    def _kernel_files(self, profile: str | None) -> dict[str, KernelFile]:
        del profile
        kernel = self.kernel
        if kernel is None:
            return {}
        found: dict[str, KernelFile] = {}
        if kernel.base_deb is not None:
            source = HttpSource(
                kernel.base_deb,
                sha256=kernel.base_deb_sha256,
                mirrors=kernel.base_deb_mirrors,
                missing=kernel.base_deb_missing,
            )
            found["kernel-config"] = KernelFile("kernel-config", source)
        for patch in kernel.patches:
            if patch.url is not None:
                name = f"kernel-patch-{patch.name}"
                found[name] = KernelFile(name, HttpSource(patch.url, sha256=patch.sha256))
        return found

    def deb_file(self, profile: str) -> DebFile | None:
        """The ``EfiStub`` package *profile*'s postinst hooks install, under its lockfile name.

        ``None`` when it installs none. The name is
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

        The source builds, the built kernel's source (:meth:`kernel_source`) and the ``EfiStub``
        packages (:meth:`deb_file`).
        """
        builds = self.source_builds()
        sources: dict[str, NamedSource] = dict(builds)
        for profile in self.active:
            files = [
                (spec, f"the kernel's {spec.name}", "a kernel file's")
                for spec in self.kernel_files(profile)
            ]
            for spec, what, owner in (
                (self.kernel_source(profile), "the kernel's source", "the kernel's"),
                (self.deb_file(profile), "the EFI stub package", "the EFI stub package's"),
                *files,
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

    def pinned_repositories(self) -> dict[str, RepositorySpec]:
        """The digest-pinned apt repositories of the active profiles, by name: what ``lock``
        records the ``InRelease`` of."""
        return {
            repository.name: repository
            for profile in self.active
            for repository in self.state.effective_profile(profile).repositories
            if repository.sha256 is not None
        }

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

    def sdk_pins(self, path: Path | None = None) -> dict[str, LockedToolchain]:
        """The .NET SDK pins (``toolchains``) of the lockfile at *path* (default
        :attr:`lock_path`), by source build, if it exists."""
        lock_path = self.lock_path if path is None else Path(path)
        if not lock_path.exists():
            return {}
        return {item.name: item for item in read_lockfile(lock_path).toolchains}

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
        built by another toolchain. ``WithNetwork`` chooses where packages come
        from, not which, and stays out.
        """
        effective = self.state.effective_profile(profile)
        options = self.mkosi
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
        self, pins: Mapping[str, LockedFetch], sdks: Mapping[str, LockedToolchain] | None = None
    ) -> RecipeState:
        """The state with every pinned source build's hook rendered at its pin from *pins*.

        A hook's fingerprint takes the distribution of the profile that declares the build
        and, for a Dotnet build, the SDK version *sdks* (default :meth:`sdk_pins`) pins.
        """
        if sdks is None:
            sdks = self.sdk_pins()
        profiles: dict[str, ProfileState] = {}
        changed = False
        for name, profile in self.state.profiles.items():
            distribution = self.build_distribution(name)
            swaps = {
                spec.render(): pinned
                for spec in profile.source_builds.values()
                if (
                    pinned := spec.render(
                        self.keyed(name, spec).pin_from(pins),
                        distribution=distribution,
                        sdk=self._sdk_version(self.keyed(name, spec), sdks),
                    )
                )
                != spec.render()
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
        profiles.default.packages: +htop``) or ``lock is up to date``. A lockfile
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

    @staticmethod
    def _sdk_version(spec: SourceBuild, sdks: Mapping[str, LockedToolchain]) -> str | None:
        """The SDK version *sdks* pins for the Dotnet build *spec* at its key, if any."""
        pinned = sdks.get(spec.key) if isinstance(spec.build, DotnetBuild) else None
        return None if pinned is None else pinned.version

    def drift(self, lock: Lockfile, *, resolver: Resolver | None, partial: bool) -> LockDrift:
        """Section, source and toolchain drift of the active profiles against *lock*.

        A Dotnet build whose SDK the lock does not pin, or pinned for another source pin,
        drifts as ``toolchains.<name>``; so does a pin for a build that is gone.
        """
        payload = self.payload()
        drift = compare_lock(lock, payload, partial=partial)
        pins = {fetch.name: fetch for fetch in lock.fetches if fetch.name is not None}
        sources = self.lock_sources()
        added, changed, removed, details = source_drift(sources, pins, resolver=resolver)
        unselected = unselected_sources(lock, payload) if partial else frozenset()
        if partial:
            elsewhere = {source_section(key) for key in unselected}
            removed = [s for s in removed if s not in elsewhere]
        sdks = {item.name: item for item in lock.toolchains}
        for name, source in sources.items():
            section = f"toolchains.{name}"
            if not isinstance(source, SourceBuild) or not isinstance(source.build, DotnetBuild):
                continue
            sdk, pin = sdks.pop(name, None), pins.get(name)
            if sdk is None:
                added.append(section)
                details[section] = f"the .NET SDK of source build {name} is not pinned"
            elif pin is not None and sdk.source != pin.digest:
                changed.append(section)
                details[section] = f".NET SDK {sdk.version} was pinned for {sdk.source[:7]}"
        removed.extend(f"toolchains.{name}" for name in sorted(sdks) if name not in unselected)
        declared = self.pinned_repositories()
        recorded = {item.name: item for item in lock.repositories}
        for name in sorted(declared.keys() | recorded.keys()):
            section, spec, item = f"repositories.{name}", declared.get(name), recorded.get(name)
            if item is None:
                added.append(section)
                details[section] = f"repository {name}'s InRelease is not locked"
            elif spec is None:
                if not partial:
                    removed.append(section)
            elif (spec.url, spec.suite, spec.sha256) != (item.url, item.suite, item.sha256):
                changed.append(section)
                details[section] = f"InRelease {item.sha256[:7]} -> {(spec.sha256 or '')[:7]}"
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
        **_composed_payload(kernel),
    }


def _file_sha256(path: str | Path) -> str | None:
    """*path*'s sha256, ``None`` when it does not exist (compiling it fails)."""
    found = Path(path)
    return hashlib.sha256(found.read_bytes()).hexdigest() if found.is_file() else None


def _composed_payload(kernel: Kernel) -> dict[str, object]:
    """A composed kernel's base, requested options, patches and module mode; else nothing.

    Downloads are recorded by url (their sha256 is the ``fetches`` entry), local
    files by their sha256.
    """
    if not kernel.composed:
        return {}
    if kernel.base_deb is not None:
        base: dict[str, object] = {"deb": kernel.base_deb, "member": kernel.base_member}
    else:
        assert kernel.base_file is not None
        base = {"sha256": _file_sha256(kernel.base_file)}
    patches: list[dict[str, object]] = [
        {"name": patch.name, "url": patch.url}
        if patch.url is not None
        else {"name": patch.name, "sha256": _file_sha256(patch.path or "")}
        for patch in kernel.patches
    ]
    return {
        "base": base,
        "options": dict(kernel.options),
        "patches": patches,
        "modules": kernel.modules,
    }


def mkosi_payload(options: MkosiOptions) -> dict[str, object]:
    """*options* that differ from the defaults, JSON-ready; sandbox files by sha256."""
    found: dict[str, object] = {}
    for name, value in options.non_defaults().items():
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
    """The runtime-init fragments in step order (the graph's topological order), by step."""
    return [
        {"step": entry.name, "sha256": hashlib.sha256(entry.script.encode()).hexdigest()}
        for entry in runtime_init_order(entries)
    ]


def recipe_payload(state: RecipeState, profile_names: Sequence[str]) -> dict[str, object]:
    """The lockfile payload of *profile_names*: what ``image_digest`` and section digests hash."""
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
        if profile.containers:
            declared["containers"] = [
                {
                    "name": c.name,
                    "packages": list(c.packages),
                    "files": [_file_payload(entry) for entry in c.files],
                    "directories": list(c.directories),
                }
                for c in sorted(profile.containers, key=lambda item: item.name)
            ]
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
