"""Resolution: fragments expanded, variant ancestry applied, references checked.

``resolve()`` raises ``ValidationError`` on the first error-level problem;
``lint()`` reports every problem as a :class:`Diagnostic` instead and raises
only for malformed input (unknown variants, ``inherits`` cycles).
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from tundravm._location import declared_at, display_location, located
from tundravm._modules.init import order_steps
from tundravm._platforms import metadata_units
from tundravm.diagnostics import Severity
from tundravm.errors import ValidationError
from tundravm.models import unit_name

from ._component import exports
from ._network import (
    expand_firewall,
)
from .model import (
    ALL_STEPS,
    BASE_VARIANT,
    EMPTY_FRAGMENT,
    PHASES,
    TARGETS,
    BootStep,
    Build,
    Declaration,
    Declared,
    Diagnostic,
    Disk,
    Edit,
    File,
    Firewall,
    Fragment,
    Hook,
    Key,
    Mount,
    Recipe,
    Ref,
    Resolved,
    Secrets,
    Service,
    Target,
    Task,
    Unit,
    UnitReady,
    Variant,
)
from .systemd import target_unit

Identity = tuple[str, ...]

FragmentRef = tuple[str, str]
"""One fragment on an item's path: its name and its class (``Fragment`` or a Composite)."""

Action = Literal["declared", "added", "edited", "removed"]
"""How one level touched an item: ``common`` declares, a variant adds, edits or removes."""

COMMON_LEVEL = "common"
"""The :attr:`Origin.variant` of declarations that come from ``Recipe.items``."""
PLATFORM_LEVEL = "platform"
"""The :attr:`Origin.variant` of what ``Recipe.platform`` declares (every variant gets it)."""


@dataclass(frozen=True, slots=True)
class Origin:
    """One step of an item's resolution: who touched it, how, and through which fragments.

    ``variant`` is ``"platform"`` for what ``Recipe.platform`` declares and
    ``"common"`` for ``Recipe.items``; ``fragments`` runs from the
    level's own fragment down to the one holding the declaration (empty for
    ``changes``, which name declarations by ref, and for direct ``Recipe.items``);
    ``fields`` are the fields an ``edited`` step set. ``location`` is the absolute
    ``file:line`` of the declaration, or of the edit or removal, ``""`` when unknown.
    """

    action: Action
    variant: str
    fragments: tuple[FragmentRef, ...] = ()
    fields: tuple[str, ...] = ()
    location: str = ""

    def describe(self) -> str:
        """``size edited at image.py:30 in profile small-key``, ``added in the image via base``."""
        where = level_name(self.variant)
        chain = " > ".join(
            name if kind in ("Fragment", "") else f"{name} ({kind})"
            for name, kind in self.fragments
        )
        edited = f"{', '.join(self.fields)} " if self.fields else ""
        at = f" at {display_location(self.location)}" if self.location else ""
        return f"{edited}{self.action}{at} {where}" + (f" via {chain}" if chain else "")


def level_name(level: str) -> str:
    """``in the image``, ``in the platform`` or ``in profile <name>`` for an origin *level*."""
    if level == COMMON_LEVEL:
        return "in the image"
    return "in the platform" if level == PLATFORM_LEVEL else f"in profile {level}"


CLOUD_TARGETS: frozenset[str] = frozenset({"azure", "gcp"})
DEFAULT_TARGET: Target = "qemu"


def identity(item: Declaration) -> Identity:
    """Type plus natural key: what changes target and collisions compare (``item.ref``)."""
    if not isinstance(item, Declared):
        raise ValidationError(
            f"{item!r} is not a declaration.",
            hint="Items must be declarations such as Package(), File() or a service.",
        )
    return ref_identity(item.ref)


def ref_identity(ref: Ref) -> Identity:
    """The identity a :class:`Ref` names."""
    return (ref.kind, *ref.identity)


_SPOKEN_ALONE = {
    "Debloat": "debloat",
    "RuntimeTools": "runtime tools",
    "Kernel": "Kernel",
    "Firewall": "firewall",
}
"""How a message names a declaration a profile has at most one of."""


def describe(ident: Identity) -> str:
    """The value an identity names, as an image file spells it: ``Service('app')`` for
    the unit a service (or a scheduled one) lowers to, ``File('/etc/motd')``,
    ``Tree('/srv/www')``, ``Package('curl')``, ``Volume('data')``, ``Mkosi`` for a setting;
    what has no public spelling is named in words (``user 'app'``, ``unit ssh.socket``)."""
    kind, *key = ident
    if not key:
        return _SPOKEN_ALONE.get(kind, kind.lower())
    if kind in ("Service", "Task") and key[0].endswith(".service"):
        return f"Service({key[0].removesuffix('.service')!r})"
    if kind == "Unit":
        return f"Service.packaged({key[0]!r})" if key[0].endswith(".service") else f"unit {key[0]}"
    if kind in ("File", "Template"):
        return f"File({key[1]!r})"
    if kind == "FileTree":
        return f"Tree({key[1]!r})"
    if kind == "Package":
        return f"Package({key[0]!r})"
    if kind == "Disk":
        return f"Volume({key[0]!r})"
    if kind == "Setting":
        return f"Mkosi(settings={{{key[1]!r}: ...}})"
    if kind == "Build":
        return f"source build {key[0]!r}"
    if kind == "Mount":
        return f"the mount at {key[0]}"
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).lower()
    return f"{words} {key[0]!r}"


@dataclass(slots=True)
class _Resolution:
    """Mutable working state of one variant's resolution."""

    variant: str
    items: dict[Identity, Declaration] = field(default_factory=dict)
    fragments: dict[str, Fragment] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    trace: dict[Identity, list[Origin]] = field(default_factory=dict)
    declared: dict[Identity, Declaration] = field(default_factory=dict)
    """Each declaration as a level first added it, before any edit."""

    def report(
        self,
        code: str,
        message: str,
        *,
        subject: str = "",
        severity: Severity = "error",
        at: object = None,
        hint: str | None = None,
    ) -> None:
        """Report one finding located where *at* (a declaration, fragment or change) was written."""
        location = display_location(declared_at(at))
        self.diagnostics.append(
            Diagnostic(
                code,
                severity,
                message,
                subject=subject,
                profile=self.variant,
                location=location,
                hint=hint,
            )
        )

    def expand(self, fragment: Fragment) -> list[tuple[Declaration, tuple[FragmentRef, ...]]]:
        """*fragment*'s declarations in order with their fragment paths.

        Repeated identical fragments expand once.
        """
        out: list[tuple[Declaration, tuple[FragmentRef, ...]]] = []
        self._expand(fragment, out, ())
        return out

    def expand_items(
        self, items: Iterable[Declaration | Fragment]
    ) -> list[tuple[Declaration, tuple[FragmentRef, ...]]]:
        """``Recipe.items`` in order with their fragment paths (direct items have none)."""
        out: list[tuple[Declaration, tuple[FragmentRef, ...]]] = []
        for item in items:
            if isinstance(item, Fragment):
                self._expand(item, out, ())
            else:
                out.append((item, ()))
        return out

    def _expand(
        self,
        fragment: Fragment,
        out: list[tuple[Declaration, tuple[FragmentRef, ...]]],
        parents: tuple[FragmentRef, ...],
    ) -> None:
        seen = self.fragments.get(fragment.name)
        if seen is not None:
            if seen != fragment:
                self.report(
                    "component-conflict",
                    f"two different components are named {fragment.name!r}",
                    subject=fragment.name,
                    at=fragment,
                )
            return
        self.fragments[fragment.name] = fragment
        path = (*parents, (fragment.name, type(fragment).__name__))
        for item in fragment.items:
            if isinstance(item, Fragment):
                self._expand(item, out, path)
            else:
                out.append((item, path))

    def record(self, key: Identity, origin: Origin) -> None:
        self.trace.setdefault(key, []).append(origin)

    def add_level(
        self,
        items: Iterable[tuple[Declaration, tuple[FragmentRef, ...]]],
        *,
        where: str,
        level: str,
    ) -> None:
        """Add one level's declarations; equal repeats dedupe, different ones collide."""
        action: Action = "declared" if level in (COMMON_LEVEL, PLATFORM_LEVEL) else "added"
        for item, path in items:
            key = identity(item)
            existing = self.items.get(key)
            if existing is None or existing == item:
                self.record(key, Origin(action, level, path, location=item.declared_at))
            if existing is None:
                self.items[key] = item
                self.declared.setdefault(key, item)
            elif existing != item:
                first = display_location(existing.declared_at)
                self.report(
                    "identity-collision",
                    f"{describe(key)} is declared twice with different values ({where})"
                    + (f"; first declared at {first}" if first else ""),
                    subject=describe(key),
                    at=item,
                )

    def apply_changes(self, variant: Variant) -> None:
        """Apply *variant*'s edits and removals to what it inherits."""
        for change in variant.changes:
            key = ref_identity(change.target)
            current = self.items.get(key)
            verb = "edits" if isinstance(change, Edit) else "removes"
            if current is None:
                self.report(
                    "edit-missing" if isinstance(change, Edit) else "remove-missing",
                    f"profile {variant.name!r} {verb} {describe(key)}, which it does not inherit",
                    subject=describe(key),
                    at=change,
                )
            elif isinstance(change, Edit):
                edited = replace(cast(Any, current), **change.fields)
                self.items[key] = located(edited, current.declared_at)
                fields = tuple(change.fields)
                self.record(
                    key, Origin("edited", variant.name, fields=fields, location=change.declared_at)
                )
            else:
                del self.items[key]
                self.record(key, Origin("removed", variant.name, location=change.declared_at))


def ancestry(recipe: Recipe, name: str) -> tuple[Variant, ...]:
    """The variants from the root down to *name*; raises on unknown ``inherits`` and cycles."""
    chain: list[Variant] = []
    current: Variant | None = recipe.variant(name)
    while current is not None:
        if any(v.name == current.name for v in chain):
            names = " -> ".join(v.name for v in (*reversed(chain), current))
            raise ValidationError(
                f"Profiles extend each other in a cycle: {names}.",
                hint="Point one profile's extends= at a profile outside the loop, or drop it.",
            )
        chain.append(current)
        inherits = current.inherits
        if inherits is None or inherits == BASE_VARIANT:
            break
        try:
            current = recipe.variant(inherits)
        except ValidationError as exc:
            raise ValidationError(
                f"Profile {chain[-1].name!r} extends unknown profile {inherits!r}.",
                hint="Extend None (the image itself) or a declared profile.",
            ) from exc
    return tuple(reversed(chain))


def _targets(chain: Sequence[Variant]) -> tuple[Target, ...]:
    targets: tuple[Target, ...] = (DEFAULT_TARGET,)
    for variant in chain:
        if variant.outputs:
            targets = variant.outputs
    return targets


def recipe_targets(recipe: Recipe) -> tuple[Target, ...]:
    """Every target any variant of *recipe* builds, ``qemu`` first, in ``TARGETS`` order.

    The payload carries the glue of each (inert off its platform), so it is the same
    whichever variants a build selects; a variant whose ``inherits`` does not resolve adds
    nothing (resolving it reports why).
    """
    found: set[str] = {DEFAULT_TARGET}
    for variant in recipe.variants:
        try:
            found.update(_targets(ancestry(recipe, variant.name)))
        except ValidationError:
            continue
    return tuple(target for target in TARGETS if target in found)  # type: ignore[misc]


type StepNode = tuple[str, tuple[str, ...], tuple[str, ...]]
"""A runtime-init step as ``(name, after, before)`` step names."""


def builtin_steps(items: Iterable[Declaration]) -> list[tuple[str, tuple[str, ...]]]:
    """The built-in steps *items* produce, in rank order, each with the steps it runs after.

    ``disks`` runs after ``keys``; ``secrets`` after ``disks`` when a ``Secrets``
    stores them on a disk, and so does each ``delivery-<name>`` step waiting
    for an :class:`~tundravm.declarative.model.ExternalDelivery`.
    """
    items = list(items)
    keys = any(isinstance(item, Key) for item in items)
    disks = any(isinstance(item, Disk) for item in items)
    secrets = [item for item in items if isinstance(item, Secrets)]
    steps: list[tuple[str, tuple[str, ...]]] = []
    if keys:
        steps.append(("keys", ()))
    if disks:
        steps.append(("disks", ("keys",) if keys else ()))
    if secrets:
        stored = disks and any(entry.store is not None for entry in secrets)
        steps.append(("secrets", ("disks",) if stored else ()))
        waited = (entry.delivery for entry in secrets if entry.delivery is not None)
        for step in dict.fromkeys(delivery.step for delivery in waited if delivery.waits):
            steps.append((step, ("disks",) if stored else ()))
    return steps


def step_nodes(items: Sequence[Declaration]) -> list[StepNode]:
    """Every runtime-init step of *items*: the built-ins, then each ``BootStep``."""
    nodes: list[StepNode] = [(name, after, ()) for name, after in builtin_steps(items)]
    for step in (item for item in items if isinstance(item, BootStep)):
        nodes.append(
            (step.name, tuple(r.step for r in step.after), tuple(r.step for r in step.before))
        )
    return nodes


def has_init(items: Sequence[Declaration]) -> bool:
    """Whether *items* produce any runtime-init step (a ``BootStep`` or the built-ins)."""
    return bool(step_nodes(items))


def _stable_topological[T](
    nodes: Sequence[T],
    name: Callable[[T], str],
    deps: Callable[[T], Iterable[str]],
) -> list[T]:
    """*nodes* with each after the *deps* among them, otherwise in the given order."""
    by_name = {name(node): node for node in nodes}
    placed: set[str] = set()
    ordered: list[T] = []
    visiting: set[str] = set()

    def visit(node: T) -> None:
        key = name(node)
        if key in placed:
            return
        if key in visiting:
            raise ValidationError(
                f"Ordering cycle through {key!r}.",
                hint="Remove one of the after= dependencies that form the loop.",
            )
        visiting.add(key)
        for dep in deps(node):
            if dep in by_name:
                visit(by_name[dep])
        visiting.discard(key)
        placed.add(key)
        ordered.append(node)

    for node in nodes:
        visit(node)
    return ordered


def order_boot_steps(items: Sequence[Declaration]) -> list[str]:
    """The runtime-init step names of *items* in run order; raises on a cycle."""
    return order_steps(step_nodes(items))


def order_hooks(hooks: Sequence[Hook]) -> list[Hook]:
    """*hooks* reordered inside each phase so every hook follows its ``after`` hooks.

    Hooks keep the slots their phase occupies in *hooks*; an ``after`` naming a
    hook of a later phase raises.
    """
    phase_of = {hook.name: hook.phase for hook in hooks}
    for hook in hooks:
        for dep in hook.after:
            if dep in phase_of and PHASES.index(phase_of[dep]) > PHASES.index(hook.phase):
                raise ValidationError(
                    f"hook {hook.name!r} ({hook.phase}) runs after {dep!r}, a later "
                    f"{phase_of[dep]} hook.",
                    hint=f"Drop {dep!r} from after=, or move {hook.name!r} to a later phase.",
                )
    slots: dict[str, list[int]] = {}
    for index, hook in enumerate(hooks):
        slots.setdefault(hook.phase, []).append(index)
    result = list(hooks)
    for phase, indexes in slots.items():
        members = [hooks[i] for i in indexes]
        ordered = _stable_topological(
            members,
            lambda h: h.name,
            lambda h, phase=phase: (d for d in h.after if phase_of.get(d) == phase),  # type: ignore[misc]
        )
        for index, hook in zip(indexes, ordered, strict=True):
            result[index] = hook
    return result


def _check_references(items: Sequence[Declaration]) -> None:
    """Every boot step, boot task and build hook a declaration orders itself against is
    declared: an image file cannot name one, so a missing one is a lowering bug (ordering
    silently ignores an unknown name)."""
    step_names = {name for name, _, _ in step_nodes(items)}
    for step in (item for item in items if isinstance(item, BootStep)):
        for edge, refs in (("after", step.after), ("before", step.before)):
            for handle in refs:
                assert handle.step in step_names, (
                    f"boot step {step.name!r} runs {edge} {handle.step!r}, which the lowering "
                    "did not declare"
                )
    finished = {item.unit for item in items if isinstance(item, Task) and item.schedule is None}
    for unit in (item for item in items if isinstance(item, (Unit, Service, Task, Mount))):
        for handle in unit.needs:
            if isinstance(handle, UnitReady):
                assert handle.unit in finished, (
                    f"{type(unit).__name__.lower()} {unit_label(unit)!r} needs boot task "
                    f"{handle.unit!r}, which the lowering did not declare"
                )
            else:
                assert handle.step == ALL_STEPS or handle.step in step_names, (
                    f"{type(unit).__name__.lower()} {unit_label(unit)!r} needs step "
                    f"{handle.step!r}, which the lowering did not declare"
                )
    hook_names = {item.name for item in items if isinstance(item, Hook)}
    for hook in (item for item in items if isinstance(item, Hook)):
        for dep in hook.after:
            assert dep in hook_names, (
                f"hook {hook.name!r} runs after {dep!r}, which the lowering did not declare"
            )


def _resolve(recipe: Recipe, name: str) -> tuple[Resolved, list[Diagnostic]]:
    resolved, state = _run(recipe, name)
    return resolved, state.diagnostics


def _run(recipe: Recipe, name: str) -> tuple[Resolved, _Resolution]:
    chain = ancestry(recipe, name)
    # Problems inside ``common`` belong to no single variant: lint() reports them once.
    state = _Resolution(variant="")
    platform = recipe.platform.contribution()
    if platform:
        state.add_level(state.expand_items(platform), where="platform", level=PLATFORM_LEVEL)
    if chain[0].inherits == BASE_VARIANT:
        state.add_level(state.expand_items(recipe.items), where="common", level=COMMON_LEVEL)
    state.variant = name
    for variant in chain:
        state.apply_changes(variant)
        if variant.add != EMPTY_FRAGMENT:
            state.add_level(
                state.expand(variant.add), where=f"profile {variant.name!r}", level=variant.name
            )
    _expand_firewall(state, metadata_units(recipe_targets(recipe)), production=chain[-1].release)
    items = tuple(state.items.values())
    _check_unit_files(state, items)
    _check_references(items)
    _check_exports(state)
    _check_executables(state, items)
    resolved = Resolved(
        profile=name,
        targets=_targets(chain),
        items=items,
        components=tuple(state.fragments),
        production=chain[-1].release,
    )
    for fragment in state.fragments.values():
        for required in fragment.requires:
            if required not in state.fragments:
                state.report(
                    "component-requires-missing",
                    f"component {fragment.name!r} requires component {required!r}, which this "
                    "profile does not include",
                    subject=fragment.name,
                    at=fragment,
                )
        for check in fragment.checks:
            for diagnostic in locate(check(resolved), items):
                found = diagnostic if diagnostic.profile else replace(diagnostic, profile=name)
                state.diagnostics.append(found)
    return resolved, state


def _unit_files(item: Declaration) -> tuple[str, ...]:
    """The unit files *item* writes: a ``Service``'s or ``Task``'s (with its socket or
    timer), a ``Mount``'s, a ``Unit``'s that ships text."""
    if isinstance(item, Service):
        unit = unit_name(item.name)
        stem = unit.removesuffix(".service").removesuffix("@")
        return (unit, *((stem + ".socket",) if item.socket is not None else ()))
    if isinstance(item, Task):
        timer = item.unit.removesuffix(".service") + ".timer"
        return (item.unit, *((timer,) if item.schedule is not None else ()))
    if isinstance(item, Mount):
        return (target_unit(item.ref),)
    if isinstance(item, Unit) and item.content is not None:
        return (item.name,)
    return ()


def _check_unit_files(state: _Resolution, items: Sequence[Declaration]) -> None:
    """Report two declarations writing one unit file: one file would silently replace the
    other (``Service("app")`` and ``Unit("app.service", text)``, ``Task`` and ``Service``)."""
    writers: dict[str, Declaration] = {}
    for item in items:
        for unit in _unit_files(item):
            first = writers.setdefault(unit, item)
            if first is item:
                continue
            where = display_location(declared_at(first))
            state.report(
                "unit-collision",
                f"{describe(identity(item))} and {describe(identity(first))}"
                + (f" (declared at {where})" if where else "")
                + f" both write {unit}",
                subject=unit,
                at=item,
                hint="Rename one of them; a Service's own unit changes through its fields "
                "and unit=DropIn(...).",
            )


def _expand_firewall(state: _Resolution, glue: Sequence[Declaration], *, production: bool) -> None:
    """Replace the variant's :class:`Firewall` with what it renders, now that the services whose
    ports it opens are final; the rendered declarations keep its location and provenance.
    *glue* are the cloud units lowering adds later, whose own egress the ruleset accepts."""
    key = ("Firewall",)
    firewall = state.items.get(key)
    if not isinstance(firewall, Firewall):
        return
    del state.items[key]
    expanded = expand_firewall(firewall, (*state.items.values(), *glue), production=production)
    for item in expanded:
        rendered = located(item, firewall.declared_at)
        ident = identity(rendered)
        existing = state.items.get(ident)
        if existing is not None and existing != rendered:
            state.report(
                "identity-collision",
                f"{describe(ident)} is declared, and the profile's Firewall renders another",
                subject=describe(ident),
                at=existing,
            )
            continue
        state.items.setdefault(ident, rendered)
        state.declared.setdefault(ident, rendered)
        state.trace.setdefault(ident, []).extend(state.trace.get(key, ()))


def _check_exports(state: _Resolution) -> None:
    """Report exports bound to a provider instance (``provider=self``) this variant changed.

    Another configuration of the provider's fragment is an error: the consumer
    holds the values the other one published. An edit or removal of something
    the provider composed is a warning: exports are computed from the
    provider's configuration and do not follow edits of its declarations.
    """
    for consumer in state.fragments.values():
        for name, export in exports(consumer):
            provider = export.provider
            if export.origin is None or not isinstance(provider, str):
                continue
            actual = state.fragments.get(provider)
            what = f"the {type(export).__name__} component {consumer.name!r} holds in {name}"
            if actual is not None and actual != export.origin:
                state.report(
                    "export-stale",
                    f"{what} comes from {export.origin!r}, but this profile includes "
                    f"{actual!r} as component {provider!r}",
                    subject=consumer.name,
                    at=consumer,
                )
            elif actual is not None and (changed := _changed_inside(state, provider)):
                state.report(
                    "export-stale",
                    f"{what} was computed from the configuration of {provider!r}, but this "
                    f"profile changes {', '.join(changed)} inside it",
                    subject=consumer.name,
                    severity="warning",
                    at=consumer,
                )


def _changed_inside(state: _Resolution, fragment: str) -> list[str]:
    """The declarations *fragment* composed that a variant edited or removed."""
    changed: list[str] = []
    for key, origins in state.trace.items():
        inside = any(name == fragment for o in origins for name, _ in o.fragments)
        if inside and any(o.action in ("edited", "removed") for o in origins):
            changed.append(describe(key))
    return changed


def _check_executables(state: _Resolution, items: Sequence[Declaration]) -> None:
    """Report services whose command names a path a ``Build`` installed before a variant
    edited or removed it (``GoBinary(...).executable`` is computed once, at construction)."""
    before = {
        install.destination: (key, item.name)
        for key, item in state.declared.items()
        if isinstance(item, Build)
        for install in item.install
    }
    after = {install.destination for i in items if isinstance(i, Build) for install in i.install}
    after.update(item.path for item in items if isinstance(item, File))
    for service in (item for item in items if isinstance(item, Service)):
        for word in _command_words(service):
            if word not in before or word in after:
                continue
            key, build = before[word]
            change = next(
                (o for o in reversed(state.trace.get(key, [])) if o.action != "declared"), None
            )
            how = f"{change.action} in profile {change.variant!r}" if change else "changed"
            state.report(
                "executable-stale",
                f"service {service.name!r} runs {word}, which Build {build!r} no longer installs "
                f"(it was {how}); the command still names the old path",
                subject=service.name,
                at=service,
            )
            break


def _command_words(service: Service) -> list[str]:
    words: list[str] = []
    pre = service.exec_start_pre
    for command in (service.exec_start, *((pre,) if isinstance(pre, str) else pre)):
        if not isinstance(command, str):
            words.extend(command)
            continue
        try:
            words.extend(shlex.split(command))
        except ValueError:
            words.extend(command.split())
    return words


def _subjects(items: Iterable[Declaration]) -> dict[str, list[Declaration]]:
    """The located *items* by every name a diagnostic subject may give them."""
    found: dict[str, list[Declaration]] = {}
    for item in items:
        if not item.declared_at:
            continue
        names = {describe(identity(item)), *item.ref.identity}
        for attr in ("name", "path"):
            if isinstance(value := getattr(item, attr, None), str):
                names.add(value)
        for name in names:
            found.setdefault(name, []).append(item)
    return found


def locate(diagnostics: Iterable[Diagnostic], items: Sequence[Declaration]) -> list[Diagnostic]:
    """*diagnostics*, each without a location pointed at the declaration of *items* its
    subject names (the one whose type starts the code, ``service-…`` for a ``Service``)."""
    subjects: dict[str, list[Declaration]] | None = None
    out: list[Diagnostic] = []
    for diagnostic in diagnostics:
        if not diagnostic.location and diagnostic.subject:
            subjects = _subjects(items) if subjects is None else subjects
            candidates = subjects.get(diagnostic.subject.removeprefix("sources."), ())
            prefix = diagnostic.code.split("-")[0]
            item = next(
                (c for c in candidates if type(c).__name__.lower() == prefix),
                candidates[0] if candidates else None,
            )
            if item is not None:
                location = display_location(item.declared_at)
                diagnostic = replace(diagnostic, location=location)
        out.append(diagnostic)
    return out


def unit_label(item: Unit | Service | Task | Mount) -> str:
    """How diagnostics name a unit declaration: its name, a mount's point."""
    return item.where if isinstance(item, Mount) else item.name


def provenance(recipe: Recipe, *, variant: str) -> dict[Identity, tuple[Origin, ...]]:
    """Every identity *variant*'s resolution touched, with the steps that touched it.

    Removed items are included (their last origin is ``removed``). Resolution
    problems are not raised here; :func:`resolve` and :func:`lint` report them.
    """
    _, state = _run(recipe, variant)
    return {key: tuple(origins) for key, origins in state.trace.items()}


def resolve(recipe: Recipe, *, variant: str) -> Resolved:
    """Resolve *variant*; raises ``ValidationError`` on any error-level diagnostic."""
    resolved, diagnostics = _resolve(recipe, variant)
    errors = [d for d in diagnostics if d.severity == "error"]
    if errors:
        first = errors[0]
        more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
        at = f" at {first.location}" if first.location else ""
        raise ValidationError(
            f"profile {variant!r}: {first.code}: {first.message}{at}{more}",
            hint="Run `tundravm check IMAGE` for every diagnostic.",
            context={"profile": variant, "codes": ", ".join(d.code for d in errors[:5])},
        )
    return resolved


def resolve_all(recipe: Recipe) -> tuple[Resolved, ...]:
    """Every declared variant, resolved, in declaration order."""
    return tuple(resolve(recipe, variant=v.name) for v in recipe.variants)


def lint(recipe: Recipe, *, variants: Sequence[str] | None = None) -> tuple[Diagnostic, ...]:
    """Every resolution, reference and fragment-check problem of *variants* (default: all)."""
    names = tuple(v.name for v in recipe.variants) if variants is None else tuple(variants)
    found: list[Diagnostic] = []
    for name in names:
        _, diagnostics = _resolve(recipe, name)
        found.extend(d for d in diagnostics if d not in found)
    return tuple(found)


__all__ = [
    "CLOUD_TARGETS",
    "recipe_targets",
    "COMMON_LEVEL",
    "PLATFORM_LEVEL",
    "Action",
    "FragmentRef",
    "Identity",
    "Origin",
    "ancestry",
    "describe",
    "has_init",
    "identity",
    "level_name",
    "lint",
    "locate",
    "order_hooks",
    "order_boot_steps",
    "builtin_steps",
    "step_nodes",
    "provenance",
    "ref_identity",
    "resolve",
    "resolve_all",
]
