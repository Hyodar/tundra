"""Emit a :class:`~tundravm.declarative._lowered.Lowered` recipe as an mkosi tree (internal).

:func:`emit` generates runtime-init into a scratch copy of the state (so
compiling never changes what lint, lock or a second compile see), renders each
pinned source build's hook at its pin and hands the state with its ``EmitConfig``
to the emitter, which writes the one mkosi-native tree: the payload with a profile
overlay per active content variant, the QEMU disk and a wrapper per active cloud target.
:func:`mkosi_argv` is how a build selects one variant of that tree.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from tundravm._modules.init import HELD_TARGET, Init, held_steps, step_unit, wait_units
from tundravm.compiler import EmitConfig, MkosiEmission, emit_mkosi_tree
from tundravm.errors import PolicyError
from tundravm.lockfile import LockedFetch
from tundravm.models import (
    NETWORK_SETUP_UNIT,
    CompileResult,
    ProfileState,
    ServiceSpec,
    enable_unit,
    ships_unit,
    unit_name,
)
from tundravm.project import lock_command

from ._lowered import Lowered


def emit(lowered: Lowered, destination: Path) -> tuple[CompileResult, MkosiEmission]:
    """Emit the mkosi tree of *lowered*'s active profiles to *destination*.

    The result's digest is that of the state runtime-init was generated into.
    """
    pins = lowered.build_pins()
    _enforce_source_policy(lowered, pins)
    initialized = _with_init(lowered)
    emission = emit_mkosi_tree(
        recipe=initialized.pinned_state(pins, lowered.sdk_pins()),
        destination=Path(destination),
        config=_emit_config(lowered, pins),
    )
    digest = initialized.digest()
    return CompileResult(path=Path(destination), profiles=lowered.active, digest=digest), emission


def mkosi_argv(lowered: Lowered, variant: str) -> list[str]:
    """The mkosi options that select *variant* in the tree (run with ``--directory`` at its root).

    The main image (the QEMU disk, which needs the payload) is always built; a content
    variant adds ``--profile=<variant>`` (its ``payload/mkosi.profiles/<variant>/``
    overlay) and each cloud target ``--dependency=<target>`` (its wrapper subimage).
    The directory, output directory, verb and ``--force`` are the caller's.
    """
    profile = lowered.select([variant]).profile()
    return [
        *(f"--profile={name}" for name in lowered.mkosi_profiles(profile)),
        *(f"--dependency={target}" for target in lowered.clouds(profile)),
    ]


def _with_init(lowered: Lowered) -> Lowered:
    """A copy of *lowered* with runtime-init generated and wired into its services.

    Covers every profile, selected or not: the shared payload is what all of them
    build. An extending profile only gets its own runtime-init files when it adds
    init scripts; otherwise it inherits the default profile's.
    """
    copied = replace(lowered, state=copy.deepcopy(lowered.state))
    state = copied.state
    init = Init()
    names = [state.default_profile, *state.profiles]
    targets = [state.ensure_profile(n) for n in dict.fromkeys(names)]
    generators: list[ProfileState] = []
    for profile in targets:
        merged = copied.init_scripts(profile.name)
        if not merged:
            continue
        network_setup = _needs_network_setup(copied, profile.name)
        if profile.extends is not None and not profile.init_scripts:
            # inherits the default profile's runtime-init files and enablement,
            # unless its own network-setup.service changes the units
            if network_setup != _needs_network_setup(copied, profile.extends):
                init.apply(profile, scripts=merged, network_setup=network_setup)
            continue
        init.apply(profile, scripts=merged, network_setup=network_setup)
        generators.append(profile)
    service = init.service_name
    for profile in targets:
        entries = copied.init_scripts(profile.name)
        steps = {entry.name for entry in entries}
        patched: list[ServiceSpec] = []
        for svc in profile.services:
            waits = wait_units(svc.needs, steps=steps)
            if svc.name == service or svc.name.endswith(".target") or not waits:
                patched.append(svc)
                continue
            after = (*(unit for unit in waits if unit not in svc.after), *svc.after)
            requires = (*(unit for unit in waits if unit not in svc.requires), *svc.requires)
            patched.append(replace(svc, after=after, requires=requires))
        profile.services = _held(patched, {step_unit(step) for step in held_steps(entries)})
    # Enable runtime-init (systemctl enable + minimal.target.wants) where it is generated,
    # and the held target where a step waits for a handed-over key.
    for profile in generators:
        if not any(s.name == service for s in profile.services):
            enable_unit(profile, service)
        if held_steps(copied.init_scripts(profile.name)):
            enable_unit(profile, HELD_TARGET)
    return copied


def _held(services: list[ServiceSpec], held: set[str]) -> list[ServiceSpec]:
    """*services* with each enabled unit that requires or runs after a *held* unit (a held
    step, or a unit held that way) installed into :data:`HELD_TARGET` instead of the boot
    target, so it starts once the key arrives and the boot completes without it."""
    if not held:
        return services
    held = set(held)
    moved: set[str] = set()
    grown = True
    while grown:
        grown = False
        for svc in services:
            name = unit_name(svc.name)
            if name in moved or not svc.enabled or svc.name.endswith(".target"):
                continue
            if held & {unit_name(unit) for unit in (*svc.requires, *svc.after)}:
                held.add(name)
                moved.add(name)
                grown = True
    return [
        replace(svc, wanted_by=HELD_TARGET) if unit_name(svc.name) in moved else svc
        for svc in services
    ]


def _needs_network_setup(lowered: Lowered, profile: str) -> bool:
    """Whether runtime-init requires ``network-setup.service`` in *profile*: only when the
    profile declares that unit (else runtime-init waits for ``network-online.target``)."""
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
        hint=f"Run `{lock_command()}` to pin them.",
        context={"operation": "compile", "sources": names},
    )


def _emit_config(lowered: Lowered, pins: Mapping[str, LockedFetch]) -> EmitConfig:
    """The emitter's configuration: the payload's options and kernel, the active overlays
    and a wrapper for every cloud target of the recipe (the payload carries their glue
    whichever variants are selected; :func:`mkosi_argv` picks the wrappers to build)."""
    options = lowered.mkosi
    payload = lowered.state.ensure_profile(lowered.default_profile)
    targets = [t for t in payload.output_targets if t != "qemu"]
    emit_kwargs: dict[str, object] = {
        "base": lowered.base,
        "arch": lowered.arch,
        "image_id": lowered.image_id,
        "reproducible": lowered.reproducible,
        "epoch": lowered.epoch or 0,
        "kernel": lowered.kernel,
        "mirror": lowered.mirror,
        "tools_tree_mirror": lowered.tools_tree_mirror,
        "snapshot": lowered.snapshot,
        "with_network": options.with_network,
        "clean_package_metadata": options.clean_package_metadata,
        "kernel_modules": options.kernel_modules,
        "kernel_cmdline": options.kernel_cmdline,
        "manifest_format": options.manifest_format,
        "compress_output": options.compress_output,
        "output_directory": options.output_directory,
        "sandbox_trees": options.sandbox_trees,
        "sandbox_files": options.sandbox_files,
        "package_cache_directory": options.package_cache_directory,
        "generate_version_script": options.generate_version_script,
        "environment": dict(options.environment),
        "settings": options.settings,
        "bootable": options.bootable,
        "root": options.root,
        "uki_max_size": options.uki_max_size,
        "targets": tuple(targets),
        "overlays": tuple(name for name in lowered.overlays if name in lowered.active),
        "payload_overlays": lowered.overlays,
        "concerns": dict(lowered.concerns),
        "overlay_sandbox_files": {
            name: files
            for name, files in lowered.overlay_sandbox_files.items()
            if name in lowered.active
        },
        **_kernel_pin(lowered, pins),
    }
    if options.seed is not None:
        emit_kwargs["seed"] = options.seed
    return EmitConfig(**emit_kwargs)  # type: ignore[arg-type]


def _kernel_pin(lowered: Lowered, pins: Mapping[str, LockedFetch]) -> dict[str, object]:
    """``EmitConfig`` fields for the kernel: pin, checkout and distribution fingerprint."""
    default = lowered.default_profile
    spec = lowered.kernel_source(default)
    pin = None if spec is None else spec.pin_from(pins)
    files = tuple(
        (file.source.url, *file.mounted(file.pin_from(pins)))
        for file in lowered.kernel_files(default)
    )
    found: dict[str, object] = {"kernel_files": files} if files else {}
    if spec is None or pin is None:
        return found
    return {
        **found,
        "kernel_pin": pin,
        "kernel_checkout": spec.pin_dir(pin),
        "kernel_distribution": lowered.distribution_fingerprint(default),
    }


__all__ = ["emit", "mkosi_argv"]
