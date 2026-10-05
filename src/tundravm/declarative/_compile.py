"""Emit a :class:`~tundravm.declarative._lowered.Lowered` recipe as an mkosi tree (internal).

:func:`emit` generates runtime-init into a scratch copy of the state (so
compiling never changes what lint, lock or a second compile see), renders each
pinned source build's hook at its pin (per variant in the per-directory layout,
whose trees each run their inherited builds), and hands the state with one
``EmitConfig`` per profile to the emitter.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tundravm._modules.init import Init
from tundravm._options import MkosiOptions
from tundravm.compiler import EmitConfig, MkosiEmission, emit_mkosi_tree
from tundravm.errors import PolicyError, ValidationError
from tundravm.lockfile import LockedFetch
from tundravm.models import (
    NETWORK_SETUP_UNIT,
    CompileResult,
    Kernel,
    ProfileState,
    ServiceSpec,
    enable_unit,
    ships_unit,
)

from ._lowered import Lowered


def emit(lowered: Lowered, destination: Path) -> tuple[CompileResult, MkosiEmission]:
    """Emit the mkosi tree of *lowered*'s active profiles to *destination*.

    The result's digest is that of the state runtime-init was generated into.
    """
    pins = lowered.build_pins()
    _enforce_source_policy(lowered, pins)
    initialized = _with_init(lowered)
    config = _emit_config(lowered, pins)
    if config.emit_mode == "native_profiles":
        _require_shared_distribution(lowered)
        groups = [(initialized.pinned_state(pins), lowered.active)]
    else:
        # Each variant's directory runs its inherited builds too, keyed by its own distribution.
        groups = [
            (initialized.pinned_state(pins, variant=name), (name,)) for name in lowered.active
        ]
    emissions = [
        emit_mkosi_tree(
            recipe=state,
            destination=Path(destination),
            profile_names=names,
            base=lowered.base,
            config=config,
        )
        for state, names in groups
    ]
    emission = MkosiEmission(
        root=emissions[0].root,
        profile_paths={k: v for each in emissions for k, v in each.profile_paths.items()},
        script_paths={k: v for each in emissions for k, v in each.script_paths.items()},
    )
    digest = initialized.digest()
    return CompileResult(path=Path(destination), profiles=lowered.active, digest=digest), emission


def _require_shared_distribution(lowered: Lowered) -> None:
    """Refuse a native layout whose root builds run in a variant with another distribution.

    The root tree's kernel script and source build hooks run in every variant
    and are keyed by the default variant's :meth:`~Lowered.build_distribution`;
    a variant that builds against another distribution would restore, or cache
    under the default variant's key, a build from its own toolchain.
    """
    default = lowered.default_profile
    if not lowered.fetches_sources or not (
        lowered.kernel_source(default) is not None
        or lowered.state.effective_profile(default).source_builds
    ):
        return
    expected = lowered.distribution_fingerprint(default)
    for name in lowered.active:
        if name != default and lowered.distribution_fingerprint(name) != expected:
            raise ValidationError(
                f"native_profiles mode cannot build variant {name!r}: it builds against "
                f"another distribution than the default variant {default!r}, whose kernel "
                "and source builds the root tree runs (and caches) for every variant.",
                hint=(
                    "Give the variant the default variant's repositories, build packages "
                    "and Build settings, or use emit_mode='per_directory'."
                ),
                context={"profile": name, "operation": "emit_mkosi"},
            )


def _with_init(lowered: Lowered) -> Lowered:
    """A copy of *lowered* with runtime-init generated and wired into its services.

    Covers every active profile and the default profile they extend. An
    extending profile only gets its own runtime-init files when it adds init
    scripts; otherwise it inherits the default profile's.
    """
    copied = replace(lowered, state=copy.deepcopy(lowered.state))
    state = copied.state
    init = Init()
    names = list(copied.active)
    if any(state.ensure_profile(n).extends is not None for n in names):
        names.insert(0, state.default_profile)
    targets = [state.ensure_profile(n) for n in dict.fromkeys(names)]
    generators: list[ProfileState] = []
    for profile in targets:
        merged = copied.init_scripts(profile.name)
        if not merged:
            continue
        network_setup = _needs_network_setup(copied, profile.name)
        if profile.extends is not None and not profile.init_scripts:
            # inherits the default profile's runtime-init files and enablement,
            # unless its own network-setup.service changes the unit
            if network_setup != _needs_network_setup(copied, profile.extends):
                init.apply(profile, scripts=merged, network_setup=network_setup)
            continue
        init.apply(profile, scripts=merged, network_setup=network_setup)
        generators.append(profile)
    service = init.service_name
    for profile in targets:
        if not copied.init_scripts(profile.name):
            continue
        patched: list[ServiceSpec] = []
        for svc in profile.services:
            if svc.name == service or svc.name.endswith(".target") or not svc.after_init:
                patched.append(svc)
                continue
            after = svc.after if service in svc.after else (service, *svc.after)
            requires = svc.requires if service in svc.requires else (service, *svc.requires)
            patched.append(replace(svc, after=after, requires=requires))
        profile.services = patched
    # Enable runtime-init (systemctl enable + minimal.target.wants) where it is generated.
    for profile in generators:
        if not any(s.name == service for s in profile.services):
            enable_unit(profile, service)
    return copied


def _needs_network_setup(lowered: Lowered, profile: str) -> bool:
    """Whether runtime-init requires ``network-setup.service`` in *profile*.

    Always under ``nethermind-v1``; otherwise only when the profile declares
    that unit (else runtime-init waits for ``network-online.target``).
    """
    if lowered.mkosi_for(profile).dialect == "nethermind-v1":
        return True
    return ships_unit(lowered.state.effective_profile(profile), NETWORK_SETUP_UNIT)


def _enforce_source_policy(lowered: Lowered, pins: Mapping[str, LockedFetch]) -> None:
    """Refuse unpinned sources when ``policy.mutable_ref_policy`` is ``"error"``.

    Under ``"warn"`` compile stays silent: the ``source-unpinned`` check and
    inspect's ``pinned=-`` report it, and frozen bakes refuse it.
    """
    if lowered.policy.mutable_ref_policy != "error":
        return
    unpinned = [s for s in lowered.lock_sources().values() if s.pin_from(pins) is None]
    if not unpinned:
        return
    names = ", ".join(f"{spec.name}@{spec.source.requested}" for spec in unpinned)
    raise PolicyError(
        f"Unpinned source builds are not allowed by policy: {names}.",
        hint="Run `tundravm lock RECIPE` to pin them, or relax mutable_ref_policy.",
        context={"operation": "compile", "sources": names},
    )


def _emit_config(lowered: Lowered, pins: Mapping[str, LockedFetch]) -> EmitConfig:
    """The emitter's configuration: recipe-wide, plus one per profile with its own.

    Profiles with their own mkosi options or kernel get their own. Outside
    ``nethermind-v1`` each built kernel carries its pin from *pins*.
    """

    def config(profile: str) -> EmitConfig:
        return _emit_config_for(
            lowered,
            lowered.mkosi_for(profile),
            lowered.kernel_for(profile),
            _kernel_pin(lowered, profile, pins),
        )

    default = _kernel_pin(lowered, lowered.default_profile, pins)
    own = set(lowered.profile_mkosi) | set(lowered.profile_kernels)
    own.update(name for name in lowered.active if _kernel_pin(lowered, name, pins) != default)
    return replace(
        _emit_config_for(lowered, lowered.mkosi, lowered.kernel, default),
        profiles={name: config(name) for name in sorted(own)},
    )


def _kernel_pin(
    lowered: Lowered, profile: str, pins: Mapping[str, LockedFetch]
) -> dict[str, object]:
    """``EmitConfig`` fields for *profile*'s kernel: pin, checkout and distribution fingerprint."""
    spec = lowered.kernel_source(profile)
    pin = None if spec is None or not lowered.fetches_sources else spec.pin_from(pins)
    if spec is None or pin is None:
        return {}
    return {
        "kernel_pin": pin,
        "kernel_checkout": spec.pin_dir(pin),
        "kernel_distribution": lowered.distribution_fingerprint(profile),
    }


def _emit_config_for(
    lowered: Lowered, options: MkosiOptions, kernel: Kernel | None, pinned: Mapping[str, object]
) -> EmitConfig:
    emit_kwargs: dict[str, object] = {
        "base": lowered.base,
        "arch": lowered.arch,
        "reproducible": lowered.reproducible,
        "kernel": kernel,
        "mirror": lowered.mirror,
        "tools_tree_mirror": lowered.tools_tree_mirror,
        "snapshot": lowered.snapshot,
        "with_network": options.with_network,
        "clean_package_metadata": options.clean_package_metadata,
        "manifest_format": options.manifest_format,
        "compress_output": options.compress_output,
        "output_directory": options.output_directory,
        "sandbox_trees": options.sandbox_trees,
        "sandbox_files": options.sandbox_files,
        "package_cache_directory": options.package_cache_directory,
        "init_script": options.init_script,
        "generate_version_script": options.generate_version_script,
        "generate_cloud_postoutput": options.generate_cloud_postoutput,
        "emit_mode": options.emit_mode,
        "environment": dict(options.environment) or None,
        "environment_passthrough": options.environment_passthrough,
        "settings": options.settings,
        "bootable": options.bootable,
        "dialect": options.dialect,
    }
    if options.seed is not None:
        emit_kwargs["seed"] = options.seed
    emit_kwargs.update(pinned)
    return EmitConfig(**emit_kwargs)  # type: ignore[arg-type]


__all__ = ["emit"]
