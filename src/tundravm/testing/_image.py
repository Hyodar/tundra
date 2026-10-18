"""One variant planned in memory and read as values: plan(), fragment_plan(), Image."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Literal

from tundravm.declarative import lifecycle
from tundravm.declarative.lifecycle import Entry, Lock, Tree
from tundravm.declarative.model import (
    PHASES,
    BootStep,
    Build,
    Declaration,
    Fragment,
    Group,
    Hook,
    Package,
    Recipe,
    Resolved,
    User,
    Variant,
)
from tundravm.declarative.resolve import (
    Identity,
    Origin,
    order_boot_steps,
    order_hooks,
    provenance,
    resolve,
    step_nodes,
)
from tundravm.declarative.utils import Network
from tundravm.diagnostics import Diagnostic
from tundravm.models import unit_name

from ._units import UnitView, parse_conf

Role = Literal["runtime", "build"]
Stage = Literal["skeleton", "extra"]

TEST_PLATFORM = Fragment("test-platform", (Package("linux-image-amd64"), Network()))
"""What :func:`fragment_plan` boots on by default: the kernel package and a DHCP
:class:`~tundravm.declarative.utils.Network`, so ``kernel-missing`` and ``network-missing``
stay quiet and the fragment's own findings stand alone."""

UNIT_DIRS = ("/usr/lib/systemd/system", "/etc/systemd/system")
_ENABLE = re.compile(r"systemctl\s+(enable|mask|disable)\s+([^\n;&|]+)")


@dataclass(frozen=True, slots=True)
class ImageFile:
    """One file of the image tree: bytes, mode, stage and the declarations that put it there.

    ``origin`` is the file's provenance (empty for files the compiler writes
    itself, such as ``/usr/bin/runtime-init``); ``owner`` is always ``root``,
    since mkosi copies its trees as root (a script chowns at boot if needed).
    """

    path: str
    content: bytes
    mode: int
    stage: Stage
    origin: tuple[Origin, ...] = ()
    symlink: str | None = None
    owner: str = "root"

    @property
    def text(self) -> str:
        """The content decoded as UTF-8."""
        return self.content.decode("utf-8")

    @property
    def fragment(self) -> str | None:
        """The innermost fragment that declared the file, ``None`` for a compiler file."""
        for origin in reversed(self.origin):
            if origin.fragments:
                return origin.fragments[-1][0]
        return None


@dataclass(frozen=True, slots=True)
class InitStep:
    """One runtime-init step: a declared ``BootStep`` or a built-in (``keys``, ``disks``,
    ``secrets``).

    ``after`` is every step it runs after (its own ``after`` plus the steps naming
    it in ``before``). Built-in steps have no ``script``: the runtime tools they
    configure run them.
    """

    name: str
    after: tuple[str, ...] = ()
    script: str = ""
    builtin: bool = False


def _candidates(path: str, stage: Stage) -> list[Identity]:
    """The identities that can declare image *path* in *stage*, most specific first."""
    found: list[Identity] = [("File", stage, path), ("Template", stage, path)]
    if posixpath.dirname(path) in UNIT_DIRS:
        name = posixpath.basename(path)
        found += [("Service", name), ("Unit", name)]
    return found


def _origin(
    path: str, stage: Stage, trace: Mapping[Identity, tuple[Origin, ...]]
) -> tuple[Origin, ...]:
    for key in _candidates(path, stage):
        if key in trace:
            return trace[key]
    for key, origins in trace.items():
        if key[:2] == ("FileTree", stage) and path.startswith(key[2].rstrip("/") + "/"):
            return origins
    return ()


_LIST_SETTINGS = frozenset(
    {
        "Packages",
        "BuildPackages",
        "VolatilePackages",
        "RemovePackages",
        "RemoveFiles",
        "Repositories",
        "BuildSources",
        "Environment",
        "EnvironmentFiles",
        "PassEnvironment",
        "KernelCommandLine",
        "KernelModules",
        "KernelModulesInclude",
        "KernelModulesExclude",
        "Initrds",
        "InitrdPackages",
    }
)
"""mkosi settings whose values a later ``mkosi.conf`` adds to rather than replaces (with every
``*Scripts`` and ``*Trees``)."""


class Image:
    """One variant of a recipe, planned in memory: what its compiled tree holds, as values.

    Nothing compiles until a query reads the tree, so :meth:`lint` works on a
    recipe that does not compile. Declared things (users, groups, init steps,
    hooks, builds) come from the resolved variant; shipped things (files,
    units, packages, mkosi settings) from the compiled tree.
    """

    def __init__(self, recipe: Recipe, variant: str, *, lock: Lock | None = None) -> None:
        (self.variant,) = lifecycle.variant_names(recipe, [variant])
        self.recipe = recipe
        self.lock = lock

    def __repr__(self) -> str:
        return f"<Image of {self.recipe.name!r}, variant {self.variant!r}>"

    @cached_property
    def tree(self) -> Tree:
        """The compiled tree of this variant alone, as :func:`tundravm.compile` returns it."""
        return lifecycle.compile(self.recipe, variants=[self.variant], lock=self.lock)

    @cached_property
    def resolved(self) -> Resolved:
        """The resolved variant: every declaration after fragments, parents and changes."""
        return resolve(self.recipe, variant=self.variant)

    def lint(self) -> tuple[Diagnostic, ...]:
        """Every finding for this variant, as :func:`tundravm.lint` reports it."""
        return lifecycle.lint(self.recipe, variants=[self.variant], lock=self.lock)

    @cached_property
    def _layers(self) -> tuple[dict[str, Entry], ...]:
        return lifecycle.variant_layers(self.tree, self.variant)

    @cached_property
    def _entries(self) -> dict[str, Entry]:
        """The variant's effective files, by path relative to its layer: in the native
        layout the root (the default variant) with ``mkosi.profiles/<variant>/`` over it."""
        merged: dict[str, Entry] = {}
        for layer in self._layers:
            merged.update(layer)
        return merged

    @cached_property
    def files(self) -> Mapping[str, ImageFile]:
        """Every file and symlink the image tree ships, by absolute path (extra over skeleton)."""
        trace = provenance(self.recipe, variant=self.variant)
        found: dict[str, ImageFile] = {}
        for stage in ("skeleton", "extra"):
            top = f"mkosi.{stage}"
            for rel, entry in sorted(self._entries.items()):
                if not rel.startswith(top + "/") or (entry.content is None and not entry.symlink):
                    continue
                path = rel.removeprefix(top)
                found[path] = ImageFile(
                    path=path,
                    content=entry.content or b"",
                    mode=entry.mode,
                    stage=stage,
                    origin=_origin(path, stage, trace),
                    symlink=entry.symlink,
                )
        return dict(sorted(found.items()))

    def file(self, path: str) -> ImageFile:
        """The file at absolute image *path*; ``KeyError`` lists its directory's files."""
        path = "/" + posixpath.normpath(path).lstrip("/")
        if path in self.files:
            return self.files[path]
        here = posixpath.dirname(path)
        nearby = [p for p in self.files if posixpath.dirname(p) == here]
        raise KeyError(f"variant {self.variant!r} ships no {path}; in {here}: {nearby}")

    @cached_property
    def _unit_states(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for rel, entry in self._entries.items():
            if rel.startswith("scripts/") and entry.content is not None:
                for action, names in _ENABLE.findall(entry.content.decode("utf-8", "replace")):
                    states.update((n, action) for n in names.split() if not n.startswith("-"))
        return states

    def unit(self, name: str) -> UnitView:
        """The shipped unit *name* (a bare name means ``.service``), parsed."""
        name = unit_name(name)
        for directory in UNIT_DIRS:
            found = self.files.get(f"{directory}/{name}")
            if found is not None:
                enabled = self._unit_states.get(name) == "enable"
                return UnitView(name, found.path, found.text, enabled=enabled)
        shipped = sorted(
            posixpath.basename(p) for p in self.files if posixpath.dirname(p) in UNIT_DIRS
        )
        raise KeyError(f"variant {self.variant!r} ships no unit {name!r}; units: {shipped}")

    def service(self, name: str) -> UnitView:
        """The ``<name>.service`` unit; same as :meth:`unit` for a service name."""
        return self.unit(name)

    def settings(self, section: str) -> Mapping[str, tuple[str, ...]]:
        """The compiled ``mkosi.conf`` section, ``{key: values}`` (empty when absent). In the
        native layout the overlay's ``mkosi.conf`` merges over the root's as mkosi reads
        them: list settings (``Packages``, scripts, trees) accumulate, the others override."""
        merged: dict[str, tuple[str, ...]] = {}
        for layer in self._layers:
            conf = layer.get("mkosi.conf")
            text = conf.content.decode("utf-8") if conf is not None and conf.content else ""
            for key, values in parse_conf(text).get(section, {}).items():
                listed = key in _LIST_SETTINGS or key.endswith(("Scripts", "Trees"))
                merged[key] = (*merged.get(key, ()), *values) if listed else values
        return merged

    @property
    def packages(self) -> Mapping[str, Role]:
        """Every package the image installs (``runtime``) or builds with (``build``), by name."""
        content = self.settings("Content")
        roles: dict[str, Role] = dict.fromkeys(content.get("BuildPackages", ()), "build")
        roles.update(dict.fromkeys(content.get("Packages", ()), "runtime"))
        return dict(sorted(roles.items()))

    def _declared[T](self, kind: type[T]) -> list[T]:
        return [item for item in self.resolved.items if isinstance(item, kind)]

    @property
    def users(self) -> Mapping[str, User]:
        """The declared users, by name."""
        return {user.name: user for user in self._declared(User)}

    @property
    def groups(self) -> Mapping[str, Group]:
        """The declared groups, by name."""
        return {group.name: group for group in self._declared(Group)}

    @property
    def builds(self) -> Mapping[str, Build]:
        """The declared source builds, by name."""
        return {build.name: build for build in self._declared(Build)}

    @property
    def init_steps(self) -> tuple[InitStep, ...]:
        """The runtime-init steps in the order they run (the graph's topological order)."""
        items = self.resolved.items
        nodes = step_nodes(items)
        names = {name for name, _, _ in nodes}
        after: dict[str, list[str]] = {name: [] for name in names}
        for name, deps, before in nodes:
            after[name].extend(dep for dep in deps if dep in names)
            for later in (step for step in before if step in names):
                after[later].append(name)
        scripts = {step.name: step.script for step in self._declared(BootStep)}
        return tuple(
            InitStep(
                name,
                tuple(dict.fromkeys(after[name])),
                scripts.get(name, ""),
                builtin=name not in scripts,
            )
            for name in order_boot_steps(items)
        )

    def hooks(self, stage: str) -> tuple[Hook, ...]:
        """The build hooks of *stage* (``prepare``, ``postinst``, ``finalize``...) in run order."""
        if stage not in PHASES:
            raise ValueError(f"unknown hook stage {stage!r}; stages: {list(PHASES)}")
        return tuple(hook for hook in order_hooks(self._declared(Hook)) if hook.phase == stage)


def plan(recipe: Recipe, variant: str | None = None, *, lock: Lock | None = None) -> Image:
    """*variant* of *recipe* (default: its first variant) as an :class:`Image`.

    The tree compiles in memory on the first query that reads it; *lock*'s
    source pins apply to the compile and to :meth:`Image.lint`.
    """
    return Image(recipe, variant or recipe.variants[0].name, lock=lock)


def fragment_plan(
    *items: Declaration | Fragment,
    include: Sequence[Declaration | Fragment] = (),
    base: Fragment | Recipe | None = None,
    variant: str = "default",
    lock: Lock | None = None,
) -> Image:
    """*items* (a fragment, or declarations) planned on a minimal bootable test platform.

    *include* adds what the items need but are not under test (a storage
    fragment their ``requires`` names). *base* is the platform: a fragment
    (default :data:`TEST_PLATFORM`, the kernel package) under one ``qemu``
    variant called *variant*, or a whole recipe whose ``items`` gain them.
    """
    if len(items) == 1 and isinstance(items[0], Fragment):
        under_test = items[0]
    else:
        under_test = Fragment("under-test", tuple(items))
    if isinstance(base, Recipe):
        items = (*base.items, *include, under_test)
        return Image(replace(base, items=items), variant, lock=lock)
    platform = TEST_PLATFORM if base is None else base
    recipe = Recipe(
        name="fragment-plan",
        items=(platform, *include, under_test),
        variants=(Variant(variant, outputs=("qemu",)),),
    )
    return Image(recipe, variant, lock=lock)
