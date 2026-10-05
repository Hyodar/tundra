"""Emit a :class:`~tundravm.declarative._lowered.Lowered` recipe as an mkosi tree (internal).

:func:`emit` generates runtime-init into a scratch copy of the state (so
compiling never changes what lint, lock or a second compile see), renders each
pinned source build's hook at its pin, and hands the state with one
``EmitConfig`` per profile to the emitter.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tundravm._modules.init import Init
from tundravm._options import MkosiOptions
from tundravm._source import KernelSource
from tundravm.compiler import EmitConfig, MkosiEmission, emit_mkosi_tree
from tundravm.errors import PolicyError
from tundravm.lockfile import LockedFetch, recipe_digest
from tundravm.models import (
    NETWORK_SETUP_UNIT,
    CompileResult,
    Kernel,
    ProfileState,
    ServiceSpec,
    enable_unit,
    ships_unit,
)

from ._lowered import Lowered, recipe_payload


def emit(lowered: Lowered, destination: Path) -> tuple[CompileResult, MkosiEmission]:
    """Emit the mkosi tree of *lowered*'s active profiles to *destination*.

    The result's digest is that of the state runtime-init was generated into.
    """
    pins = lowered.build_pins()
    _enforce_source_policy(lowered, pins)
    initialized = _with_init(lowered)
    emission = emit_mkosi_tree(
        recipe=initialized.pinned_state(pins),
        destination=Path(destination),
        profile_names=lowered.active,
        base=lowered.base,
        config=_emit_config(lowered, pins),
    )
    digest = recipe_digest(recipe_payload(initialized.state, lowered.active))
    return CompileResult(path=Path(destination), profiles=lowered.active, digest=digest), emission


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
            _kernel_pin(lowered, lowered.kernel_source(profile), pins),
        )

    own = sorted(set(lowered.profile_mkosi) | set(lowered.profile_kernels))
    default = _kernel_pin(lowered, lowered.kernel_source(lowered.default_profile), pins)
    return replace(
        _emit_config_for(lowered, lowered.mkosi, lowered.kernel, default),
        profiles={name: config(name) for name in own},
    )


def _kernel_pin(
    lowered: Lowered, spec: KernelSource | None, pins: Mapping[str, LockedFetch]
) -> dict[str, object]:
    """``EmitConfig`` fields for the kernel source *spec*: its pin and checkout directory."""
    pin = None if spec is None or not lowered.fetches_sources else spec.pin_from(pins)
    if spec is None or pin is None:
        return {}
    return {"kernel_pin": pin, "kernel_checkout": spec.pin_dir(pin)}


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
