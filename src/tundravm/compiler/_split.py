"""The shared payload and each profile's overlay, from every profile's effective state.

Plan section 3.3: tundravm resolves each profile's whole image (its ``extends`` chain and
its ``changes`` applied), then the tree carries what every profile builds once, in the
shared payload, and what one profile builds beyond it, in that profile's overlay. mkosi
profiles only add files and settings, so the shared payload is the intersection of the
profiles, not the default profile: a declaration one profile removes or replaces in a
way an overlay cannot express (a package, an account, an enablement, a hook out of order)
moves out of the shared payload into the overlay of every profile that keeps it, the
default profile's included.

What an overlay expresses on top of the shared payload, and so stays shared:

- a file, template or skeleton file at a path every profile ships: the shared payload
  carries the default profile's, and an overlay whose content differs ships its own, which
  mkosi copies later (``ExtraTrees=`` and ``SkeletonTrees=`` apply in order);
- a service every profile ships, enabled alike: an overlay whose unit differs ships its
  unit file over the shared one;
- a repository every profile declares: an overlay whose differs writes its own source over
  the shared one (``SandboxTrees=`` apply in order);
- each phase's commands, as far as every profile runs the same ones in the same order:
  an overlay's scripts run after the shared payload's.

A container every profile ships alike is the shared payload's; one only some profiles ship
is the overlay's ``Dependencies=`` and ``ExtraTrees=`` line, while its subimage directory,
``mkosi.images/<name>/``, is one per tree, which a profile that does not depend on it never
builds.

The split depends on the recipe's profiles, never on which of them a compile selects.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from tundravm._units.render import service_unit
from tundravm.models import (
    CommandSpec,
    DebloatConfig,
    FileEntry,
    Phase,
    ProfileState,
    ServiceSpec,
    UserSpec,
    unit_name,
)

UNIT_DIRECTORY = "/usr/lib/systemd/system"
"""Where a service's unit file lands in the image."""


@dataclass(frozen=True, slots=True)
class Split:
    """The shared payload and, per profile, its overlay."""

    shared: ProfileState
    overlays: Mapping[str, ProfileState]
    """Per profile (the default one included), what it builds beyond :attr:`shared`."""


def split_profiles(states: Mapping[str, ProfileState], default: str) -> Split:
    """*states* (every profile's effective state, *default*'s first in its order) as the
    shared payload and each profile's overlay."""
    order = [states[default], *(state for name, state in states.items() if name != default)]
    shared = _shared(order, default)
    return Split(
        shared=shared,
        overlays={name: _overlay(state, shared) for name, state in states.items()},
    )


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/"))


def _keyed_common[T](lists: Sequence[Sequence[T]], key: Callable[[T], Hashable]) -> list[T]:
    """The entries of ``lists[0]`` whose key every list holds (the first list's entries)."""
    keys = [{key(item) for item in items} for items in lists[1:]]
    return [item for item in lists[0] if all(key(item) in found for found in keys)]


def _equal_common[T](lists: Sequence[Sequence[T]]) -> list[T]:
    """The entries of ``lists[0]`` every list holds as they are."""
    return [item for item in lists[0] if all(item in items for items in lists[1:])]


def _prefix[T](lists: Sequence[Sequence[T]]) -> list[T]:
    """The longest run every list starts with."""
    common: list[T] = []
    for entries in zip(*lists, strict=False):
        if any(entry != entries[0] for entry in entries[1:]):
            break
        common.append(entries[0])
    return common


def _service_key(service: ServiceSpec) -> str:
    return unit_name(service.name)


def _ships_unit_file(service: ServiceSpec) -> bool:
    """Whether *service* renders a unit file (enablement-only entries and targets do not)."""
    return bool(service.command) and not service.name.endswith(".target")


def _unit_file(service: ServiceSpec) -> FileEntry:
    """*service*'s unit file as a tree entry."""
    name = service.name if service.name.endswith(".service") else f"{service.name}.service"
    return FileEntry(path=f"{UNIT_DIRECTORY}/{name}", content=service_unit(service))


def _account_groups(user: UserSpec) -> set[str]:
    named = {user.gid} if isinstance(user.gid, str) else set()
    return named | set(user.groups)


def _shared(states: Sequence[ProfileState], name: str) -> ProfileState:
    first = states[0]
    phases: dict[Phase, list[CommandSpec]] = {}
    for phase in dict.fromkeys(p for state in states for p in state.phases):
        common = _prefix([state.phases.get(phase, []) for state in states])
        if common:
            phases[phase] = common
    services = _keyed_common([s.services for s in states], _service_key)
    by_key = [{_service_key(s): s for s in state.services} for state in states]
    services = [
        service
        for service in services
        if all(found[_service_key(service)].enabled == service.enabled for found in by_key)
    ]
    groups = _equal_common([s.groups for s in states])
    declared_groups = {g.name for state in states for g in state.groups}
    kept_groups = {g.name for g in groups}
    users = [
        user
        for user in _equal_common([s.users for s in states])
        if not (_account_groups(user) & declared_groups) - kept_groups
    ]
    debloats = {state.debloat for state in states}
    debloat = first.debloat if len(debloats) == 1 else DebloatConfig(enabled=False)
    packages = set(first.packages).intersection(*(s.packages for s in states[1:]))
    builds = set(first.build_packages).intersection(*(s.build_packages for s in states[1:]))
    sources = {
        key: build
        for key, build in first.source_builds.items()
        if all(state.source_builds.get(key) == build for state in states[1:])
    }
    return ProfileState(
        name=name,
        packages=packages,
        build_packages=builds,
        build_sources=_equal_common([s.build_sources for s in states]),
        output_targets=first.output_targets,
        output_targets_explicit=first.output_targets_explicit,
        phases=phases,
        repositories=_keyed_common([s.repositories for s in states], lambda r: r.name),
        files=_keyed_common([s.files for s in states], lambda f: _norm(f.path)),
        skeleton_files=_keyed_common([s.skeleton_files for s in states], lambda f: _norm(f.path)),
        templates=_keyed_common([s.templates for s in states], lambda t: _norm(t.path)),
        groups=groups,
        users=users,
        services=services,
        unit_states=_equal_common([s.unit_states for s in states]),
        partitions=_equal_common([s.partitions for s in states]),
        hooks=_prefix([s.hooks for s in states]),
        secrets=_equal_common([s.secrets for s in states]),
        init_scripts=_equal_common([s.init_scripts for s in states]),
        debloat=debloat,
        debloat_explicit=first.debloat_explicit if len(debloats) == 1 else False,
        source_builds=sources,
        containers=_equal_common([s.containers for s in states]),
    )


def _beyond[T](items: Iterable[T], shared: Iterable[T], key: Callable[[T], Hashable]) -> list[T]:
    """*items* the shared payload does not carry as they are (absent, or differing)."""
    found = {key(item): item for item in shared}
    return [item for item in items if found.get(key(item)) != item]


def _overlay(state: ProfileState, shared: ProfileState) -> ProfileState:
    phases: dict[Phase, list[CommandSpec]] = {}
    for phase, commands in state.phases.items():
        rest = commands[len(shared.phases.get(phase, [])) :]
        if rest:
            phases[phase] = rest
    common = {_service_key(s): s for s in shared.services}
    services: list[ServiceSpec] = []
    overrides: list[FileEntry] = []
    for service in state.services:
        found = common.get(_service_key(service))
        if found is None:
            services.append(service)
        elif found != service and _ships_unit_file(service):
            overrides.append(_unit_file(service))
    return replace(
        state,
        extends=None,
        packages=state.packages - shared.packages,
        build_packages=state.build_packages - shared.build_packages,
        build_sources=[s for s in state.build_sources if s not in shared.build_sources],
        output_targets=(),
        phases=phases,
        repositories=_beyond(state.repositories, shared.repositories, lambda r: r.name),
        files=[*_beyond(state.files, shared.files, lambda f: _norm(f.path)), *overrides],
        skeleton_files=_beyond(
            state.skeleton_files, shared.skeleton_files, lambda f: _norm(f.path)
        ),
        templates=_beyond(state.templates, shared.templates, lambda t: _norm(t.path)),
        groups=[g for g in state.groups if g not in shared.groups],
        users=[u for u in state.users if u not in shared.users],
        services=services,
        unit_states=[u for u in state.unit_states if u not in shared.unit_states],
        partitions=[p for p in state.partitions if p not in shared.partitions],
        hooks=state.hooks[len(shared.hooks) :],
        secrets=[s for s in state.secrets if s not in shared.secrets],
        init_scripts=[e for e in state.init_scripts if e not in shared.init_scripts],
        debloat=state.debloat if not shared.debloat.enabled else DebloatConfig(enabled=False),
        source_builds={
            key: build
            for key, build in state.source_builds.items()
            if shared.source_builds.get(key) != build
        },
        containers=[c for c in state.containers if c not in shared.containers],
    )


def is_empty(overlay: ProfileState) -> bool:
    """Whether *overlay* adds nothing to the shared payload."""
    return not (
        overlay.packages
        or overlay.build_packages
        or overlay.phases
        or overlay.repositories
        or overlay.files
        or overlay.skeleton_files
        or overlay.templates
        or overlay.groups
        or overlay.users
        or overlay.services
        or overlay.unit_states
        or overlay.debloat.enabled
        or overlay.containers
    )


__all__ = ["Split", "is_empty", "split_profiles"]
