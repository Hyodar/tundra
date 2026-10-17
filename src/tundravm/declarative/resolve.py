"""Resolution: fragments expanded, variant ancestry applied, references checked.

``resolve()`` raises ``ValidationError`` on the first error-level problem;
``lint()`` reports every problem as a :class:`Diagnostic` instead and raises
only for malformed input (unknown variants, ``inherits`` cycles).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from tundravm.diagnostics import Severity
from tundravm.errors import ValidationError

from .model import (
    BASE_VARIANT,
    BUILTIN_INITS,
    EMPTY_FRAGMENT,
    PHASES,
    Declaration,
    Declared,
    Diagnostic,
    Disk,
    Edit,
    Fragment,
    Hook,
    Init,
    Key,
    Recipe,
    Ref,
    Resolved,
    Secrets,
    Target,
    Unit,
    Variant,
)

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
    ``fields`` are the fields an ``edited`` step set.
    """

    action: Action
    variant: str
    fragments: tuple[FragmentRef, ...] = ()
    fields: tuple[str, ...] = ()

    def describe(self) -> str:
        """``size edited in variant small-key``, ``added in common via base``."""
        where = level_name(self.variant)
        chain = " > ".join(
            name if kind in ("Fragment", "") else f"{name} ({kind})"
            for name, kind in self.fragments
        )
        edited = f"{', '.join(self.fields)} " if self.fields else ""
        return f"{edited}{self.action} {where}" + (f" via {chain}" if chain else "")


def level_name(level: str) -> str:
    """``in common``, ``in the platform`` or ``in variant <name>`` for an origin *level*."""
    if level == COMMON_LEVEL:
        return "in common"
    return "in the platform" if level == PLATFORM_LEVEL else f"in variant {level}"


CLOUD_TARGETS: frozenset[str] = frozenset({"azure", "gcp"})
DEFAULT_TARGET: Target = "qemu"


def identity(item: Declaration) -> Identity:
    """Type plus natural key: what changes target and collisions compare (``item.ref``)."""
    if not isinstance(item, Declared):
        raise ValidationError(
            f"{item!r} is not a declaration.",
            hint="Fragment items must be declarations such as Package(), File() or Unit().",
        )
    return ref_identity(item.ref)


def ref_identity(ref: Ref) -> Identity:
    """The identity a :class:`Ref` names."""
    return (ref.kind, *ref.identity)


def describe(ident: Identity) -> str:
    """``Package(curl, runtime)`` for an identity."""
    kind, *key = ident
    return f"{kind}({', '.join(key)})" if key else kind


@dataclass(slots=True)
class _Resolution:
    """Mutable working state of one variant's resolution."""

    variant: str
    items: dict[Identity, Declaration] = field(default_factory=dict)
    fragments: dict[str, Fragment] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    trace: dict[Identity, list[Origin]] = field(default_factory=dict)

    def report(
        self,
        code: str,
        message: str,
        *,
        subject: str = "",
        severity: Severity = "error",
    ) -> None:
        self.diagnostics.append(
            Diagnostic(code, severity, message, subject=subject, variant=self.variant)
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
                    "fragment-conflict",
                    f"two different fragments are named {fragment.name!r}",
                    subject=fragment.name,
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
                self.record(key, Origin(action, level, path))
            if existing is None:
                self.items[key] = item
            elif existing != item:
                self.report(
                    "identity-collision",
                    f"{describe(key)} is declared twice with different values ({where})",
                    subject=describe(key),
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
                    f"variant {variant.name!r} {verb} {change.target}, which it does not inherit",
                    subject=str(change.target),
                )
            elif isinstance(change, Edit):
                try:
                    self.items[key] = replace(cast(Any, current), **change.fields)
                except ValidationError as exc:
                    self.report(
                        "edit-invalid",
                        f"variant {variant.name!r} edits {change.target}: {exc.args[0]}",
                        subject=str(change.target),
                    )
                    continue
                self.record(key, Origin("edited", variant.name, fields=tuple(change.fields)))
            else:
                del self.items[key]
                self.record(key, Origin("removed", variant.name))


def ancestry(recipe: Recipe, name: str) -> tuple[Variant, ...]:
    """The variants from the root down to *name*; raises on unknown ``inherits`` and cycles."""
    chain: list[Variant] = []
    current: Variant | None = recipe.variant(name)
    while current is not None:
        if any(v.name == current.name for v in chain):
            names = " -> ".join(v.name for v in (*reversed(chain), current))
            raise ValidationError(
                f"Variant inherits= form a cycle: {names}.",
                hint="Point one variant's inherits= at a variant outside the loop, or drop it.",
            )
        chain.append(current)
        inherits = current.inherits
        if inherits is None or inherits == BASE_VARIANT:
            break
        try:
            current = recipe.variant(inherits)
        except ValidationError as exc:
            raise ValidationError(
                f"Variant {chain[-1].name!r} inherits unknown variant {inherits!r}.",
                hint=f"Use {BASE_VARIANT!r}, None, or a declared variant.",
            ) from exc
    return tuple(reversed(chain))


def _targets(chain: Sequence[Variant]) -> tuple[Target, ...]:
    targets: tuple[Target, ...] = (DEFAULT_TARGET,)
    for variant in chain:
        if variant.outputs:
            targets = variant.outputs
    return targets


def _check_targets(state: _Resolution, chain: Sequence[Variant]) -> None:
    inherited: tuple[Target, ...] = (DEFAULT_TARGET,)
    for variant in chain:
        own = variant.outputs
        clouds = [t for t in inherited if t in CLOUD_TARGETS]
        if own and clouds and not set(clouds) <= set(own):
            state.report(
                "target-inconsistent",
                f"variant {variant.name!r} targets {', '.join(own)} but inherits the "
                f"{', '.join(clouds)} platform integration of the variant it inherits",
                subject=variant.name,
            )
        if own:
            inherited = own


def _builtin_inits(items: Iterable[Declaration]) -> dict[str, int]:
    present = {
        "keys": any(isinstance(item, Key) for item in items),
        "disks": any(isinstance(item, Disk) for item in items),
        "secrets": any(isinstance(item, Secrets) for item in items),
    }
    return {name: BUILTIN_INITS[name] for name, here in present.items() if here}


def has_init(items: Sequence[Declaration]) -> bool:
    """Whether *items* produce any runtime-init fragment (an ``Init`` or the built-ins)."""
    return any(isinstance(item, Init) for item in items) or bool(_builtin_inits(items))


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


def order_inits(inits: Sequence[Init], *, builtins: dict[str, int]) -> list[Init]:
    """*inits* by priority, ``after`` ordering equal priorities; raises on contradictions.

    *builtins* maps the built-in steps present (``keys``/``disks``/``secrets``) to
    their priority; an ``after`` naming neither an init nor a built-in is ignored
    here (``lint`` reports it).
    """
    priorities = {init.name: init.priority for init in inits} | builtins
    for init in inits:
        for dep in init.after:
            if dep in priorities and priorities[dep] > init.priority:
                raise ValidationError(
                    f"init {init.name!r} (priority {init.priority}) runs after {dep!r} "
                    f"(priority {priorities[dep]}), which runs later.",
                    hint=f"Give {init.name!r} a priority of at least {priorities[dep]}.",
                )
    groups: dict[int, list[Init]] = {}
    for init in inits:
        groups.setdefault(init.priority, []).append(init)
    ordered: list[Init] = []
    for priority in sorted(groups):
        ordered.extend(_stable_topological(groups[priority], lambda i: i.name, lambda i: i.after))
    return ordered


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


def _references(item: Declaration) -> Iterable[tuple[str, Ref]]:
    """The refs *item* holds, each with the diagnostic code of a dangling one."""
    match item:
        case Disk(key=Ref() as ref):
            yield "disk-key-undefined", ref
        case Secrets(store=Ref() as ref):
            yield "secret-store-undefined", ref


def _check_references(state: _Resolution, items: Sequence[Declaration]) -> None:
    present = {identity(item) for item in items}
    for item in items:
        for code, ref in _references(item):
            if ref_identity(ref) not in present:
                state.report(
                    code,
                    f"{describe(identity(item))} references {ref}, which variant "
                    f"{state.variant!r} does not declare",
                    subject=item.ref.identity[-1],
                )

    inits = [item for item in items if isinstance(item, Init)]
    builtins = _builtin_inits(items)
    init_names = {init.name for init in inits} | set(builtins)
    for init in inits:
        for dep in init.after:
            if dep not in init_names:
                state.report(
                    "init-after-undefined",
                    f"init {init.name!r} runs after {dep!r}, which this variant does not declare",
                    subject=init.name,
                )
    try:
        order_inits(inits, builtins=builtins)
    except ValidationError as exc:
        state.report("init-order", str(exc))

    hooks = [item for item in items if isinstance(item, Hook)]
    hook_names = {hook.name for hook in hooks}
    for hook in hooks:
        for dep in hook.after:
            if dep not in hook_names:
                state.report(
                    "hook-after-undefined",
                    f"hook {hook.name!r} runs after {dep!r}, which this variant does not declare",
                    subject=hook.name,
                )
    try:
        order_hooks(hooks)
    except ValidationError as exc:
        state.report("hook-order", str(exc))

    if not has_init(items):
        for unit in (i for i in items if isinstance(i, Unit) and i.after_init):
            state.report(
                "unit-after-init-without-init",
                f"unit {unit.name!r} waits for runtime-init, but this variant declares no init "
                "step",
                subject=unit.name,
                severity="warning",
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
                state.expand(variant.add), where=f"variant {variant.name!r}", level=variant.name
            )
    _check_targets(state, chain)
    items = tuple(state.items.values())
    _check_references(state, items)
    resolved = Resolved(
        variant=name,
        outputs=_targets(chain),
        items=items,
        fragments=tuple(state.fragments),
    )
    for fragment in state.fragments.values():
        for required in fragment.requires:
            if required not in state.fragments:
                state.report(
                    "fragment-requires-missing",
                    f"fragment {fragment.name!r} requires fragment {required!r}, which this "
                    "variant does not include",
                    subject=fragment.name,
                )
        for check in fragment.checks:
            for diagnostic in check(resolved):
                found = diagnostic if diagnostic.variant else replace(diagnostic, variant=name)
                state.diagnostics.append(found)
    return resolved, state


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
        raise ValidationError(
            f"variant {variant!r}: {first.code}: {first.message}{more}",
            hint="Run tundravm.declarative.lint(recipe) for every diagnostic.",
            context={"variant": variant, "codes": ", ".join(d.code for d in errors[:5])},
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
    "order_hooks",
    "order_inits",
    "provenance",
    "ref_identity",
    "resolve",
    "resolve_all",
]
