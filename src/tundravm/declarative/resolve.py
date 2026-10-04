"""Resolution: fragments expanded, variant ancestry applied, references checked.

``resolve()`` raises ``ValidationError`` on the first error-level problem;
``lint()`` reports every problem as a :class:`Diagnostic` instead and raises
only for malformed input (unknown variants, parent cycles).
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from tundravm.errors import ValidationError
from tundravm.models import unit_name

from .model import (
    BASE_PARENT,
    BUILTIN_INITS,
    EMPTY_FRAGMENT,
    PHASES,
    Build,
    Debloat,
    Declaration,
    Diagnostic,
    Directory,
    Disk,
    File,
    Fragment,
    Group,
    Hook,
    Init,
    Kernel,
    Key,
    Package,
    Partition,
    Recipe,
    Repository,
    Resolved,
    RuntimeTools,
    Secrets,
    Setting,
    Target,
    Unit,
    User,
    Variant,
)

Identity = tuple[str, ...]

CLOUD_TARGETS: frozenset[str] = frozenset({"azure", "gcp"})
DEFAULT_TARGET: Target = "qemu"


def identity(item: Declaration) -> Identity:
    """Type plus natural key: what ``replace``/``remove`` match and collisions compare."""
    kind = type(item).__name__
    match item:
        case Package():
            return (kind, item.name, item.role)
        case File() | Directory():
            return (kind, item.stage, posixpath.normpath(item.path))
        case Unit():
            return (kind, unit_name(item.name))
        case Setting():
            return (kind, item.section, item.key)
        case Debloat() | RuntimeTools() | Kernel():
            return (kind,)
        case (
            Group()
            | User()
            | Hook()
            | Init()
            | Repository()
            | Partition()
            | Key()
            | Disk()
            | Secrets()
            | Build()
        ):
            return (kind, item.name)
    raise ValidationError(f"{item!r} is not a declaration.")


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

    def report(
        self,
        code: str,
        message: str,
        *,
        subject: str = "",
        level: Literal["error", "warning", "info"] = "error",
    ) -> None:
        self.diagnostics.append(
            Diagnostic(code, message, level=level, variant=self.variant, subject=subject)
        )

    def expand(self, fragment: Fragment) -> list[Declaration]:
        """*fragment*'s declarations in order; repeated identical fragments expand once."""
        out: list[Declaration] = []
        self._expand(fragment, out)
        return out

    def _expand(self, fragment: Fragment, out: list[Declaration]) -> None:
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
        for item in fragment.items:
            if isinstance(item, Fragment):
                self._expand(item, out)
            else:
                out.append(item)

    def add_level(self, items: Iterable[Declaration], *, where: str) -> None:
        """Add one level's declarations; equal repeats dedupe, different ones collide."""
        for item in items:
            key = identity(item)
            existing = self.items.get(key)
            if existing is None:
                self.items[key] = item
            elif existing != item:
                self.report(
                    "identity-collision",
                    f"{describe(key)} is declared twice with different values ({where})",
                    subject=describe(key),
                )

    def apply_changes(self, variant: Variant) -> None:
        for item in variant.replace:
            key = identity(item)
            if key in self.items:
                self.items[key] = item
            else:
                self.report(
                    "replace-missing",
                    f"variant {variant.name!r} replaces {describe(key)}, which it does not inherit",
                    subject=describe(key),
                )
        for item in variant.remove:
            key = identity(item)
            if self.items.pop(key, None) is None:
                self.report(
                    "remove-missing",
                    f"variant {variant.name!r} removes {describe(key)}, which it does not inherit",
                    subject=describe(key),
                )


def ancestry(recipe: Recipe, name: str) -> tuple[Variant, ...]:
    """The variants from the root down to *name*; raises on unknown parents and cycles."""
    chain: list[Variant] = []
    current: Variant | None = recipe.variant(name)
    while current is not None:
        if any(v.name == current.name for v in chain):
            names = " -> ".join(v.name for v in (*reversed(chain), current))
            raise ValidationError(f"Variant parents form a cycle: {names}.")
        chain.append(current)
        parent = current.parent
        if parent is None or parent == BASE_PARENT:
            break
        try:
            current = recipe.variant(parent)
        except ValidationError as exc:
            raise ValidationError(
                f"Variant {chain[-1].name!r} has unknown parent {parent!r}.",
                hint=f"Use {BASE_PARENT!r}, None, or a declared variant.",
            ) from exc
    return tuple(reversed(chain))


def _target(chain: Sequence[Variant]) -> Target:
    target: Target = DEFAULT_TARGET
    for variant in chain:
        if variant.target is not None:
            target = variant.target
    return target


def _check_targets(state: _Resolution, chain: Sequence[Variant]) -> None:
    inherited: Target = DEFAULT_TARGET
    for variant in chain:
        own = variant.target
        if own is not None and inherited in CLOUD_TARGETS and own != inherited:
            state.report(
                "target-inconsistent",
                f"variant {variant.name!r} targets {own} but inherits the {inherited} "
                "platform integration of its parent",
                subject=variant.name,
            )
        if own is not None:
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
            raise ValidationError(f"Ordering cycle through {key!r}.")
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
                    f"{phase_of[dep]} hook."
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


def _check_references(state: _Resolution, items: Sequence[Declaration]) -> None:
    keys = {item.name: item for item in items if isinstance(item, Key)}
    disks = {item.name: item for item in items if isinstance(item, Disk)}
    for disk in disks.values():
        if not isinstance(disk.key, Key):
            continue
        declared = keys.get(disk.key.name)
        if declared is None:
            state.report(
                "disk-key-undefined",
                f"disk {disk.name!r} uses key {disk.key.name!r}, which this variant does not "
                "declare",
                subject=disk.name,
            )
        elif declared != disk.key:
            state.report(
                "disk-key-mismatch",
                f"disk {disk.name!r} references a key {disk.key.name!r} that differs from the "
                "declared one",
                subject=disk.name,
            )
    for secrets in (item for item in items if isinstance(item, Secrets)):
        if secrets.store is None:
            continue
        declared_disk = disks.get(secrets.store.name)
        if declared_disk is None:
            state.report(
                "secret-store-undefined",
                f"secrets {secrets.name!r} are stored on disk {secrets.store.name!r}, which this "
                "variant does not declare",
                subject=secrets.name,
            )
        elif declared_disk != secrets.store:
            state.report(
                "secret-store-mismatch",
                f"secrets {secrets.name!r} reference a disk {secrets.store.name!r} that differs "
                "from the declared one",
                subject=secrets.name,
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
                level="warning",
            )


def _resolve(recipe: Recipe, name: str) -> tuple[Resolved, list[Diagnostic]]:
    chain = ancestry(recipe, name)
    # Problems inside ``common`` belong to no single variant: lint() reports them once.
    state = _Resolution(variant="")
    if chain[0].parent == BASE_PARENT:
        state.add_level(state.expand(recipe.common), where="common")
    state.variant = name
    for variant in chain:
        state.apply_changes(variant)
        if variant.add != EMPTY_FRAGMENT:
            state.add_level(state.expand(variant.add), where=f"variant {variant.name!r}")
    _check_targets(state, chain)
    items = tuple(state.items.values())
    _check_references(state, items)
    resolved = Resolved(
        variant=name,
        target=_target(chain),
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
    return resolved, state.diagnostics


def resolve(recipe: Recipe, *, variant: str) -> Resolved:
    """Resolve *variant*; raises ``ValidationError`` on any error-level diagnostic."""
    resolved, diagnostics = _resolve(recipe, variant)
    errors = [d for d in diagnostics if d.level == "error"]
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
    "Identity",
    "ancestry",
    "describe",
    "has_init",
    "identity",
    "lint",
    "order_hooks",
    "order_inits",
    "resolve",
    "resolve_all",
]
