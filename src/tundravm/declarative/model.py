"""Immutable declaration values of the declarative frontend (design doc section 2).

Every record is a frozen, slotted dataclass. ``__post_init__`` rejects what the
fluent ``Image`` API rejects at declaration time (empty names, relative paths,
unknown phases, out-of-range modes) plus the structural rules of the design
(a ``Disk`` references a ``Key`` object, ``Secrets.store`` is a ``Disk``).
Lists passed for tuple fields are frozen into tuples.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Literal, get_args

from tundravm._source import CargoBuild as Cargo
from tundravm._source import DotnetBuild as Dotnet
from tundravm._source import GoBuild as Go
from tundravm.errors import ValidationError
from tundravm.policy import Policy

Target = Literal["qemu", "azure", "gcp"]
Phase = Literal[
    "sync",
    "skeleton",
    "prepare",
    "build",
    "extra",
    "postinst",
    "finalize",
    "postoutput",
    "clean",
    "repart",
    "boot",
]
Pairs = tuple[tuple[str, str], ...]
type Check = Callable[[Resolved], tuple[Diagnostic, ...]]

TARGETS: tuple[str, ...] = get_args(Target)
PHASES: tuple[str, ...] = get_args(Phase)
BASE_PARENT = "base"
"""The reserved parent name that stands for ``Recipe.common``."""
BUILTIN_INITS: dict[str, int] = {"keys": 10, "disks": 20, "secrets": 30}
"""Runtime-init fragments the compiler emits for keys, disks and secrets, by priority."""

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]*\$?")
_ENTRY_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_BUILD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CACHE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@/-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{type(owner).__name__}: {message}", hint=hint)


def _recipe_name(recipe: Go | Cargo | Dotnet) -> str:
    """The public alias of a build recipe (its class is the internal ``GoBuild``, ...)."""
    return "Go" if isinstance(recipe, Go) else "Cargo" if isinstance(recipe, Cargo) else "Dotnet"


def _freeze(owner: object, *names: str) -> None:
    """Turn list values of the tuple fields *names* into tuples."""
    for name in names:
        value = getattr(owner, name)
        if isinstance(value, list):
            object.__setattr__(owner, name, tuple(value))


def _require_name(owner: object, value: str, what: str = "name") -> None:
    if not isinstance(value, str) or not value.strip():
        raise _fail(owner, f"{what} must be a non-empty string, got {value!r}.")


def _require_absolute(owner: object, path: str, what: str = "path") -> None:
    if not isinstance(path, str) or not path.startswith("/"):
        raise _fail(owner, f"{what} {path!r} must be an absolute path.")


def _require_relative(owner: object, path: str, what: str) -> None:
    parts = path.rstrip("/").split("/")
    if not path or path.startswith("/") or ".." in parts:
        raise _fail(owner, f"{what} {path!r} must be relative and stay inside the tree.")


def _require_mode(owner: object, mode: int | None) -> None:
    if mode is None:
        return
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise _fail(owner, f"mode {mode!r} must be an int between 0 and 0o7777.")


def _require_choice(owner: object, value: object, choices: tuple[str, ...], what: str) -> None:
    if value not in choices:
        raise _fail(owner, f"invalid {what} {value!r}.", hint=f"Expected one of: {choices}.")


def _require_pairs(owner: object, pairs: Pairs) -> None:
    seen: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise _fail(owner, f"env entries must be (name, value) pairs, got {pair!r}.")
        key, value = pair
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key):
            raise _fail(owner, f"invalid environment variable name {key!r}.")
        if not isinstance(value, str) or "\n" in value:
            raise _fail(owner, f"environment value for {key!r} must be one line of text.")
        if key in seen:
            raise _fail(owner, f"environment variable {key!r} is set twice.")
        seen.add(key)


def _require_names(owner: object, values: tuple[str, ...], what: str) -> None:
    for value in values:
        _require_name(owner, value, what)


# ── Root and composition ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Fragment:
    """A named group of declarations and nested fragments (what a module returns)."""

    name: str
    items: tuple[Declaration | Fragment, ...] = ()
    requires: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "items", "requires", "checks")
        _require_name(self, self.name)
        for item in self.items:
            if not isinstance(item, (Fragment, *DECLARATION_TYPES)):
                raise _fail(
                    self,
                    f"{self.name!r} item {item!r} is not a declaration or fragment.",
                    hint="Unpack generators with *(...) and nest fragments directly.",
                )
        _require_names(self, self.requires, "required fragment name")
        for check in self.checks:
            if not callable(check):
                raise _fail(self, f"{self.name!r} check {check!r} is not callable.")


EMPTY_FRAGMENT = Fragment("empty")


@dataclass(frozen=True, slots=True)
class Variant:
    """An overlay on its parent: ``base`` is ``Recipe.common``, ``None`` is standalone."""

    name: str
    parent: str | None = BASE_PARENT
    add: Fragment = EMPTY_FRAGMENT
    replace: tuple[Declaration, ...] = ()
    remove: tuple[Declaration, ...] = ()
    target: Target | None = None
    targets: tuple[Target, ...] = ()
    """Several outputs from one variant; ``target`` is the one-output shorthand."""

    def __post_init__(self) -> None:
        _freeze(self, "replace", "remove", "targets")
        _require_name(self, self.name)
        if self.name == BASE_PARENT:
            raise _fail(self, f"{BASE_PARENT!r} is reserved for Recipe.common.")
        if self.parent is not None:
            _require_name(self, self.parent, "parent")
            if self.parent == self.name:
                raise _fail(self, f"variant {self.name!r} cannot be its own parent.")
        if not isinstance(self.add, Fragment):
            raise _fail(self, f"add= of {self.name!r} must be a Fragment.")
        for what, items in (("replace", self.replace), ("remove", self.remove)):
            for item in items:
                if not isinstance(item, DECLARATION_TYPES):
                    raise _fail(
                        self, f"{what}= of {self.name!r} holds {item!r}, not a declaration."
                    )
        if self.target is not None:
            _require_choice(self, self.target, TARGETS, "target")
        if self.target is not None and self.targets:
            raise _fail(
                self,
                f"variant {self.name!r} sets both target= and targets=.",
                hint="target=X is the shorthand for targets=(X,); pass one of them.",
            )
        for target in self.targets:
            _require_choice(self, target, TARGETS, "target")
        if len(set(self.targets)) != len(self.targets):
            raise _fail(self, f"variant {self.name!r} lists a target twice.")

    @property
    def outputs(self) -> tuple[Target, ...]:
        """The targets this variant sets itself (empty: inherited)."""
        return self.targets or (() if self.target is None else (self.target,))


@dataclass(frozen=True, slots=True)
class Mkosi:
    """Compiler configuration: tree layout, emission dialect and generated helper files.

    ``init_script`` is written to ``mkosi.skeleton/init`` (mode 0755);
    ``version_script`` emits ``mkosi.version``; ``cloud_postoutput`` emits the
    Azure/GCP disk conversion postoutput scripts; ``strip_os_release`` strips
    ``IMAGE_VERSION`` from ``os-release`` (``None``: when ``Recipe.epoch`` is set).
    """

    layout: Literal["directories", "native"] = "directories"
    dialect: Literal["current", "nethermind-v1"] = "current"
    init_script: str | None = None
    version_script: bool = False
    cloud_postoutput: bool = True
    strip_os_release: bool | None = None

    def __post_init__(self) -> None:
        _require_choice(self, self.layout, ("directories", "native"), "layout")
        _require_choice(self, self.dialect, ("current", "nethermind-v1"), "dialect")
        if self.init_script is not None:
            _require_name(self, self.init_script, "init_script")


@dataclass(frozen=True, slots=True)
class Recipe:
    """The whole image: recipe-wide settings, the common fragment and the variants."""

    name: str
    common: Fragment
    variants: tuple[Variant, ...] = (Variant("default", target="qemu"),)
    base: str = "debian/trixie"
    arch: Literal["x86_64", "aarch64"] = "x86_64"
    mirror: str | None = None
    tools_mirror: str | None = None
    epoch: int | None = 0
    mkosi: Mkosi = Mkosi()
    policy: Policy | None = None

    def __post_init__(self) -> None:
        _freeze(self, "variants")
        _require_name(self, self.name)
        if not isinstance(self.common, Fragment):
            raise _fail(self, "common= must be a Fragment.")
        if not self.variants:
            raise _fail(self, "a recipe needs at least one variant.")
        names: set[str] = set()
        for variant in self.variants:
            if not isinstance(variant, Variant):
                raise _fail(self, f"variants= holds {variant!r}, not a Variant.")
            if variant.name in names:
                raise _fail(self, f"variant {variant.name!r} is declared twice.")
            names.add(variant.name)
        _require_name(self, self.base, "base")
        _require_choice(self, self.arch, ("x86_64", "aarch64"), "arch")
        for what, url in (("mirror", self.mirror), ("tools_mirror", self.tools_mirror)):
            if url is not None:
                _require_name(self, url, what)
        if self.epoch is not None and (isinstance(self.epoch, bool) or self.epoch < 0):
            raise _fail(self, f"epoch {self.epoch!r} must be a non-negative int or None.")
        if not isinstance(self.mkosi, Mkosi):
            raise _fail(self, "mkosi= must be a Mkosi value.")
        if self.policy is not None and not isinstance(self.policy, Policy):
            raise _fail(self, "policy= must be a tundravm.Policy value.")

    def variant(self, name: str) -> Variant:
        """The variant called *name*; ``ValidationError`` when there is none."""
        for variant in self.variants:
            if variant.name == name:
                return variant
        known = ", ".join(v.name for v in self.variants)
        raise ValidationError(f"Unknown variant {name!r}.", hint=f"Declared variants: {known}.")


# ── Declarations ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    role: Literal["runtime", "build"] = "runtime"

    def __post_init__(self) -> None:
        _require_name(self, self.name)
        if any(ch.isspace() for ch in self.name):
            raise _fail(self, f"package name {self.name!r} contains whitespace.")
        _require_choice(self, self.role, ("runtime", "build"), "role")


@dataclass(frozen=True, slots=True)
class File:
    path: str
    content: str | bytes | Path
    mode: int = 0o644
    stage: Literal["skeleton", "extra"] = "extra"

    def __post_init__(self) -> None:
        _require_absolute(self, self.path)
        if not isinstance(self.content, (str, bytes, Path)):
            raise _fail(self, f"content of {self.path!r} must be str, bytes or Path.")
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")


@dataclass(frozen=True, slots=True)
class Directory:
    path: str
    source: Path
    exclude: tuple[str, ...] = ()
    mode: int | None = None
    stage: Literal["skeleton", "extra"] = "extra"

    def __post_init__(self) -> None:
        _freeze(self, "exclude")
        _require_absolute(self, self.path)
        if not isinstance(self.source, Path):
            raise _fail(self, f"source of {self.path!r} must be a pathlib.Path.")
        _require_names(self, self.exclude, "exclude pattern")
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    system: bool = True
    gid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _GROUP_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid group name {self.name!r}.")
        if self.gid is not None and self.gid < 0:
            raise _fail(self, f"gid {self.gid} must not be negative.")


@dataclass(frozen=True, slots=True)
class User:
    name: str
    system: bool = True
    home: str | None = None
    shell: str = "/usr/sbin/nologin"
    uid: int | None = None
    primary_group: str | int | None = None
    groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "groups")
        if not isinstance(self.name, str) or not _GROUP_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid user name {self.name!r}.")
        if self.home is not None:
            _require_absolute(self, self.home, "home")
        _require_absolute(self, self.shell, "shell")
        if self.uid is not None and self.uid < 0:
            raise _fail(self, f"uid {self.uid} must not be negative.")
        if isinstance(self.primary_group, str):
            _require_name(self, self.primary_group, "primary_group")
        elif isinstance(self.primary_group, int) and self.primary_group < 0:
            raise _fail(self, f"primary_group {self.primary_group} must not be negative.")
        _require_names(self, self.groups, "group")


@dataclass(frozen=True, slots=True)
class Unit:
    """A systemd unit: ``content`` ships the unit file, ``None`` controls a packaged one."""

    name: str
    content: str | Path | None = None
    enabled: bool | None = None
    masked: bool | None = None
    after_init: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _UNIT_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid unit name {self.name!r}.")
        if self.content is not None:
            if not isinstance(self.content, (str, Path)):
                raise _fail(self, f"content of {self.name!r} must be str or Path.")
            if "." not in self.name:
                raise _fail(
                    self,
                    f"unit {self.name!r} ships a file and needs its type suffix.",
                    hint=f"Use {self.name + '.service'!r}.",
                )
        elif self.enabled is None and self.masked is None:
            raise _fail(
                self,
                f"unit {self.name!r} has no content and neither enables, disables nor masks it.",
            )


@dataclass(frozen=True, slots=True)
class Service:
    """A generated systemd service: the compiler renders the unit from these fields.

    ``security`` selects the hardening profile (``strict``, ``default`` or
    ``none``). With ``after_init`` the unit waits for ``runtime-init.service``
    whenever the variant has a runtime-init step. ``Unit`` ships verbatim text.
    """

    name: str
    exec_start: str | tuple[str, ...]
    _: KW_ONLY
    description: str | None = None
    user: str | None = None
    group: str | None = None
    working_dir: str | None = None
    env: Pairs = ()
    env_file: str | None = None
    exec_start_pre: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    wanted_by: str | None = None
    type: Literal["simple", "exec", "oneshot", "notify", "forking"] | None = None
    restart: Literal["always", "on-failure", "no"] = "no"
    limits: tuple[tuple[str, str | int], ...] = ()
    kill_mode: Literal["control-group", "mixed", "process", "none"] | None = None
    timeout_stop: str | None = None
    security: Literal["strict", "default", "none"] = "default"
    after_init: bool = True

    def __post_init__(self) -> None:
        _freeze(self, "exec_start", "env", "exec_start_pre", "after", "requires", "wants")
        _freeze(self, "limits")
        if not isinstance(self.name, str) or not _UNIT_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid service name {self.name!r}.")
        if self.name.endswith(".target") or (
            "." in self.name and not self.name.endswith(".service")
        ):
            raise _fail(self, f"service {self.name!r} must be a .service unit.")
        if isinstance(self.exec_start, str):
            _require_name(self, self.exec_start, "exec_start")
        elif not self.exec_start:
            raise _fail(self, f"service {self.name!r} has an empty exec_start.")
        else:
            _require_names(self, self.exec_start, "exec_start argument")
        for what in ("user", "group", "env_file", "wanted_by", "timeout_stop"):
            value = getattr(self, what)
            if value is not None:
                _require_name(self, value, what)
        for what in ("working_dir", "env_file"):
            value = getattr(self, what)
            if value is not None:
                _require_absolute(self, value, what)
        _require_pairs(self, self.env)
        for what in ("exec_start_pre", "after", "requires", "wants"):
            _require_names(self, getattr(self, what), what)
        if self.type is not None:
            _require_choice(
                self, self.type, ("simple", "exec", "oneshot", "notify", "forking"), "type"
            )
        _require_choice(self, self.restart, ("always", "on-failure", "no"), "restart")
        if self.kill_mode is not None:
            _require_choice(
                self, self.kill_mode, ("control-group", "mixed", "process", "none"), "kill_mode"
            )
        _require_choice(self, self.security, ("strict", "default", "none"), "security")
        seen: set[str] = set()
        for pair in self.limits:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _fail(self, f"limits entries must be (resource, value) pairs, got {pair!r}.")
            resource, value = pair
            _require_name(self, resource, "limit resource")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise _fail(self, f"limit {resource!r} must be a str or int, got {value!r}.")
            if resource in seen:
                raise _fail(self, f"limit {resource!r} is set twice.")
            seen.add(resource)


@dataclass(frozen=True, slots=True)
class Template:
    """A file rendered from *template* with ``str.format_map(variables)`` at lowering time."""

    path: str
    template: str | Path
    variables: tuple[tuple[str, str | int | float], ...] = ()
    mode: int = 0o644
    stage: Literal["skeleton", "extra"] = "extra"

    def __post_init__(self) -> None:
        _freeze(self, "variables")
        _require_absolute(self, self.path)
        if not isinstance(self.template, (str, Path)):
            raise _fail(self, f"template of {self.path!r} must be str or Path.")
        seen: set[str] = set()
        for pair in self.variables:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _fail(self, f"variables must be (name, value) pairs, got {pair!r}.")
            key, value = pair
            _require_name(self, key, "variable name")
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise _fail(self, f"variable {key!r} must be str, int or float, got {value!r}.")
            if key in seen:
                raise _fail(self, f"variable {key!r} is set twice.")
            seen.add(key)
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")


@dataclass(frozen=True, slots=True)
class Hook:
    name: str
    phase: Phase
    script: str
    env: Pairs = ()
    cwd: str | None = None
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "env", "after")
        _require_name(self, self.name)
        _require_choice(self, self.phase, PHASES, "phase")
        _require_name(self, self.script, "script")
        _require_pairs(self, self.env)
        if self.cwd is not None:
            _require_name(self, self.cwd, "cwd")
        _require_names(self, self.after, "after")
        if self.name in self.after:
            raise _fail(self, f"hook {self.name!r} cannot run after itself.")


@dataclass(frozen=True, slots=True)
class Init:
    """A runtime-init fragment: lower priority runs first, ``after`` orders equal ones."""

    name: str
    script: str
    priority: int = 100
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "after")
        _require_name(self, self.name)
        if self.name in BUILTIN_INITS:
            raise _fail(
                self,
                f"{self.name!r} is the runtime-init step of the built-in {self.name} declarations.",
                hint="Pick another name; reference it through after=.",
            )
        _require_name(self, self.script, "script")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise _fail(self, f"priority {self.priority!r} must be an int.")
        _require_names(self, self.after, "after")
        if self.name in self.after:
            raise _fail(self, f"init {self.name!r} cannot run after itself.")


@dataclass(frozen=True, slots=True)
class Repository:
    name: str
    url: str
    suite: str
    components: tuple[str, ...] = ("main",)
    keyring: str | None = None
    priority: int = 100

    def __post_init__(self) -> None:
        _freeze(self, "components")
        _require_name(self, self.name)
        _require_name(self, self.url, "url")
        _require_name(self, self.suite, "suite")
        _require_names(self, self.components, "component")
        if self.keyring is not None:
            _require_name(self, self.keyring, "keyring")


@dataclass(frozen=True, slots=True)
class Partition:
    name: str
    size: str
    mount: str
    filesystem: str = "ext4"

    def __post_init__(self) -> None:
        _require_name(self, self.name)
        _require_name(self, self.size, "size")
        _require_absolute(self, self.mount, "mount")
        _require_name(self, self.filesystem, "filesystem")


@dataclass(frozen=True, slots=True)
class Debloat:
    enabled: bool = True
    remove: tuple[str, ...] | None = None
    extra_remove: tuple[str, ...] = ()
    keep_paths: tuple[str, ...] = ()
    minimize_systemd: bool = True
    keep_units: tuple[str, ...] | None = None
    keep_binaries: tuple[str, ...] | None = None
    keep_units_extra: tuple[str, ...] = ()
    """Units kept on top of ``keep_units`` (or its default list)."""
    keep_paths_by_variant: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """``(variant, paths)``: paths kept only when building that variant."""

    def __post_init__(self) -> None:
        _freeze(
            self,
            "remove",
            "extra_remove",
            "keep_paths",
            "keep_units",
            "keep_binaries",
            "keep_units_extra",
            "keep_paths_by_variant",
        )
        for what in ("remove", "extra_remove", "keep_paths"):
            for path in getattr(self, what) or ():
                _require_absolute(self, path, what)
        for what in ("keep_units", "keep_binaries", "keep_units_extra"):
            _require_names(self, getattr(self, what) or (), what)
        frozen: list[tuple[str, tuple[str, ...]]] = []
        for pair in self.keep_paths_by_variant:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise _fail(self, f"keep_paths_by_variant holds {pair!r}, not (variant, paths).")
            variant, paths = pair
            _require_name(self, variant, "variant")
            paths = tuple(paths)
            for path in paths:
                _require_absolute(self, path, "keep_paths_by_variant path")
            frozen.append((variant, paths))
        object.__setattr__(self, "keep_paths_by_variant", tuple(frozen))


@dataclass(frozen=True, slots=True)
class Setting:
    """An mkosi ``[section] key=value...`` escape hatch."""

    section: str
    key: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        _freeze(self, "values")
        _require_name(self, self.section, "section")
        _require_name(self, self.key, "key")
        if not isinstance(self.values, tuple):
            raise _fail(self, f"{self.section}.{self.key} values must be a tuple of strings.")
        for value in self.values:
            if not isinstance(value, str):
                raise _fail(self, f"{self.section}.{self.key} value {value!r} is not a string.")


# ── Keys, disks and secrets ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Key:
    name: str
    output: str | None = None
    strategy: Literal["random", "pipe"] = "random"
    persist_in_tpm: bool = True
    size: int = 64
    pipe: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ENTRY_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid key name {self.name!r}.")
        if self.output is not None:
            _require_absolute(self, self.output, "output")
        _require_choice(self, self.strategy, ("random", "pipe"), "strategy")
        if self.strategy == "pipe" and not self.pipe:
            raise _fail(self, f"key {self.name!r}: the pipe strategy requires pipe=.")
        if self.strategy != "pipe" and self.pipe is not None:
            raise _fail(self, f"key {self.name!r}: pipe= is only valid with strategy='pipe'.")
        if self.pipe is not None:
            _require_absolute(self, self.pipe, "pipe")
        if isinstance(self.size, bool) or self.size <= 0:
            raise _fail(self, f"key {self.name!r}: size {self.size!r} must be positive.")


@dataclass(frozen=True, slots=True)
class Disk:
    name: str
    mount: str
    device: str | None = None
    key: Key | Path | None = None
    mapper: str | None = None
    format: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    directories: tuple[str, ...] = ("ssh", "data", "logs")

    def __post_init__(self) -> None:
        _freeze(self, "directories")
        if not isinstance(self.name, str) or not _ENTRY_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid disk name {self.name!r}.")
        _require_absolute(self, self.mount, "mount")
        if self.device is not None:
            _require_absolute(self, self.device, "device")
        if self.key is not None and not isinstance(self.key, (Key, Path)):
            raise _fail(
                self,
                f"disk {self.name!r}: key must be a Key declaration or a key file Path, "
                f"got {self.key!r}.",
                hint="Pass the Key object itself (key=my_key), not its name.",
            )
        if self.mapper is not None:
            _require_name(self, self.mapper, "mapper")
            if self.key is None:
                raise _fail(self, f"disk {self.name!r}: a plain disk cannot set mapper=.")
        _require_choice(
            self, self.format, ("always", "on_initialize", "on_fail", "never"), "format"
        )
        _require_names(self, self.directories, "directory")


@dataclass(frozen=True, slots=True)
class Schema:
    kind: Literal["string", "json"] = "string"
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "enum")
        _require_choice(self, self.kind, ("string", "json"), "kind")
        for what in ("min_length", "max_length"):
            value = getattr(self, what)
            if value is not None and value < 0:
                raise _fail(self, f"{what} {value} must not be negative.")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise _fail(self, "min_length exceeds max_length.")


@dataclass(frozen=True, slots=True)
class SecretFile:
    path: str
    mode: int = 0o400
    owner: str | None = None

    def __post_init__(self) -> None:
        _require_absolute(self, self.path)
        _require_mode(self, self.mode)
        if self.owner is not None:
            _require_name(self, self.owner, "owner")


@dataclass(frozen=True, slots=True)
class SecretEnv:
    name: str
    service: str | None = None  # None means global environment.

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ENV_KEY.fullmatch(self.name):
            raise _fail(self, f"invalid environment variable name {self.name!r}.")
        if self.service is not None:
            _require_name(self, self.service, "service")


@dataclass(frozen=True, slots=True)
class Secret:
    name: str
    targets: tuple[SecretFile | SecretEnv, ...]
    required: bool = True
    schema: Schema | None = None

    def __post_init__(self) -> None:
        _freeze(self, "targets")
        _require_name(self, self.name)
        if not self.targets:
            raise _fail(self, f"secret {self.name!r} requires at least one delivery target.")
        for target in self.targets:
            if not isinstance(target, (SecretFile, SecretEnv)):
                raise _fail(self, f"secret {self.name!r} target {target!r} is not a target.")


@dataclass(frozen=True, slots=True)
class Secrets:
    name: str = "secrets"
    entries: tuple[Secret, ...] = ()
    store: Disk | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_directory: str = "/root/.ssh"
    ssh_key_path: str | None = "/etc/root_key"

    def __post_init__(self) -> None:
        _freeze(self, "entries")
        _require_name(self, self.name)
        names: set[str] = set()
        for entry in self.entries:
            if not isinstance(entry, Secret):
                raise _fail(self, f"entries= holds {entry!r}, not a Secret.")
            if entry.name in names:
                raise _fail(self, f"secret {entry.name!r} is declared twice.")
            names.add(entry.name)
        if self.store is not None and not isinstance(self.store, Disk):
            raise _fail(
                self,
                f"store must be a Disk declaration, got {self.store!r}.",
                hint="Pass the Disk object itself (store=my_disk), not its name.",
            )
        _require_name(self, self.host, "host")
        if isinstance(self.port, bool) or not 0 < self.port < 65536:
            raise _fail(self, f"port {self.port!r} is out of range.")
        _require_absolute(self, self.ssh_directory, "ssh_directory")
        if self.ssh_key_path is not None:
            _require_absolute(self, self.ssh_key_path, "ssh_key_path")


@dataclass(frozen=True, slots=True)
class RuntimeTools:
    source: Git
    key_config: str = "/etc/tdx/key-gen.yaml"
    disk_config: str = "/etc/tdx/disk-setup.yaml"
    secret_config: str = "/etc/tdx/secrets.yaml"
    secret_manifest: str = "/etc/tdx/secrets.json"

    def __post_init__(self) -> None:
        if not isinstance(self.source, Git):
            raise _fail(self, "source must be a Git source.")
        for what in ("key_config", "disk_config", "secret_config", "secret_manifest"):
            _require_absolute(self, getattr(self, what), what)


# ── Source builds ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Git:
    url: str
    ref: str
    subdir: str | None = None
    submodules: bool = False

    def __post_init__(self) -> None:
        _require_name(self, self.url, "url")
        _require_name(self, self.ref, "ref")
        if self.subdir is not None:
            _require_relative(self, self.subdir, "subdir")


@dataclass(frozen=True, slots=True)
class Http:
    url: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        _require_name(self, self.url, "url")
        if self.sha256 is not None and not _SHA256.fullmatch(self.sha256):
            raise _fail(self, "sha256 must be 64 lowercase hex characters.")


@dataclass(frozen=True, slots=True)
class Install:
    """Install *source* (relative to the build directory) at *destination*."""

    source: str
    destination: str
    mode: int | None = 0o755
    directory: bool = False

    def __post_init__(self) -> None:
        _require_relative(self, self.source, "source")
        _require_absolute(self, self.destination, "destination")
        _require_mode(self, self.mode)
        if self.directory and self.mode is not None:
            raise _fail(
                self,
                f"directory {self.source!r} takes no mode (pass mode=None).",
                hint="Directory copies keep the built files' modes.",
            )
        if not self.directory and self.source.endswith("/"):
            raise _fail(self, f"{self.source!r} names a directory; pass directory=True.")


@dataclass(frozen=True, slots=True)
class Build:
    """Build the fetched source with *script* or a *recipe* and install its results.

    Set exactly one of ``script`` (a shell script run in the source tree, with
    ``env`` exported and ``packages`` installed for it) and ``recipe`` (:class:`Go`,
    :class:`Cargo` or :class:`Dotnet`, which carry their own ``env`` and
    ``packages``). ``install`` paths are relative to the source tree; a recipe's
    output is at its ``artifact`` path, e.g. ``build/<output>`` for ``Go``.
    ``cache_key`` names the build cache entry; ``None`` derives
    ``<name>-<url digest>-<ref>`` from the source.
    """

    name: str
    source: Git | Http
    script: str | None = None
    install: tuple[Install, ...] = ()
    packages: tuple[str, ...] = ()
    env: Pairs = ()
    cache_key: str | None = None
    recipe: Go | Cargo | Dotnet | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        _freeze(self, "install", "packages", "env")
        if not isinstance(self.name, str) or not _BUILD_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid build name {self.name!r}.")
        if (self.script is None) == (self.recipe is None):
            raise _fail(
                self,
                f"build {self.name!r} needs exactly one of script= and recipe=.",
                hint="script='make' runs a shell script; recipe=Go(...), Cargo(...) or "
                "Dotnet(...) renders the toolchain's build command.",
            )
        if self.recipe is not None:
            if not isinstance(self.recipe, (Go, Cargo, Dotnet)):
                raise _fail(self, f"build {self.name!r} recipe must be Go, Cargo or Dotnet.")
            if self.packages or self.env:
                raise _fail(
                    self,
                    f"build {self.name!r} takes packages and env on its recipe.",
                    hint=f"Pass them to {_recipe_name(self.recipe)}(packages=..., env=...).",
                )
        if self.cache_key is not None and (
            not isinstance(self.cache_key, str) or not _CACHE_KEY.fullmatch(self.cache_key)
        ):
            raise _fail(
                self,
                f"invalid cache_key {self.cache_key!r} for build {self.name!r}.",
                hint="Use letters, digits and '._+@/-' ('/' is cached as '_').",
            )
        if not isinstance(self.source, (Git, Http)):
            raise _fail(self, f"build {self.name!r} source must be Git or Http.")
        if self.script is not None:
            _require_name(self, self.script, "script")
        if not self.install:
            raise _fail(self, f"build {self.name!r} installs nothing.")
        names: list[str] = []
        for step in self.install:
            if not isinstance(step, Install):
                raise _fail(self, f"build {self.name!r} install step {step!r} is not Install.")
            names.append(posixpath.basename(step.destination.rstrip("/")))
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise _fail(
                self,
                f"build {self.name!r} install destinations share the file name "
                f"{', '.join(duplicates)}.",
                hint="Each artifact is cached under its destination's file name.",
            )
        _require_names(self, self.packages, "package")
        _require_pairs(self, self.env)


@dataclass(frozen=True, slots=True)
class Kernel:
    version: str
    source: Git | Http
    config: Path | None = None
    cmdline: str = ""
    tdx: bool = True

    def __post_init__(self) -> None:
        _require_name(self, self.version, "version")
        if not isinstance(self.source, (Git, Http)):
            raise _fail(self, "source must be Git or Http.")
        if self.config is not None and not isinstance(self.config, Path):
            raise _fail(self, "config must be a pathlib.Path.")


type Declaration = (
    Package
    | File
    | Directory
    | Group
    | User
    | Unit
    | Service
    | Template
    | Hook
    | Init
    | Repository
    | Partition
    | Debloat
    | Setting
    | Key
    | Disk
    | Secrets
    | RuntimeTools
    | Build
    | Kernel
)

DECLARATION_TYPES: tuple[type, ...] = (
    Package,
    File,
    Directory,
    Group,
    User,
    Unit,
    Service,
    Template,
    Hook,
    Init,
    Repository,
    Partition,
    Debloat,
    Setting,
    Key,
    Disk,
    Secrets,
    RuntimeTools,
    Build,
    Kernel,
)


# ── Results ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    level: Literal["error", "warning", "info"] = "error"
    variant: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        _require_name(self, self.code, "code")
        _require_choice(self, self.level, ("error", "warning", "info"), "level")


@dataclass(frozen=True, slots=True)
class Resolved:
    """One variant after expansion, overlays and reference checks."""

    variant: str
    target: Target
    items: tuple[Declaration, ...]
    fragments: tuple[str, ...]
    targets: tuple[Target, ...] = ()
    """Every output of the variant (``target`` is the first)."""

    def __post_init__(self) -> None:
        _freeze(self, "targets")
        if not self.targets:
            object.__setattr__(self, "targets", (self.target,))


__all__ = [
    "BASE_PARENT",
    "BUILTIN_INITS",
    "DECLARATION_TYPES",
    "PHASES",
    "TARGETS",
    "Build",
    "Cargo",
    "Check",
    "Debloat",
    "Declaration",
    "Diagnostic",
    "Directory",
    "Disk",
    "Dotnet",
    "EMPTY_FRAGMENT",
    "File",
    "Fragment",
    "Git",
    "Go",
    "Group",
    "Hook",
    "Http",
    "Init",
    "Install",
    "Kernel",
    "Key",
    "Mkosi",
    "Package",
    "Pairs",
    "Partition",
    "Phase",
    "Recipe",
    "Repository",
    "Resolved",
    "RuntimeTools",
    "Schema",
    "Secret",
    "SecretEnv",
    "SecretFile",
    "Secrets",
    "Service",
    "Setting",
    "Target",
    "Template",
    "Unit",
    "User",
    "Variant",
]
