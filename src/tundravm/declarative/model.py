"""Immutable declaration values of the declarative frontend (design doc section 2).

Every record is a frozen, slotted dataclass. ``__post_init__`` rejects malformed
values at declaration time (empty names, relative paths, unknown phases,
out-of-range modes) plus the structural rules of the design
(a ``Disk`` references a ``Key`` object, ``Secrets.store`` is a ``Disk``).
Lists passed for tuple fields are frozen into tuples.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Literal, get_args

from tundravm._source import CargoBuild as Cargo
from tundravm._source import DotnetBuild as Dotnet
from tundravm._source import FrozenMap
from tundravm._source import GoBuild as Go
from tundravm.diagnostics import Diagnostic
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
BASE_VARIANT = "base"
"""The reserved ``Variant.inherits`` name that stands for ``Recipe.items``."""
BUILTIN_INITS: dict[str, int] = {"keys": 10, "disks": 20, "secrets": 30}
"""Runtime-init fragments the compiler emits for keys, disks and secrets, by priority."""

_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNIT_NAME = re.compile(r"[A-Za-z0-9:_.@\\-]+")
_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]*\$?")
_ENTRY_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_BUILD_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CACHE_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+@/-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{type(owner).__name__}: {message}", hint=hint)


def _toolchain_name(using: Go | Cargo | Dotnet) -> str:
    """The public alias of a build toolchain (its class is the internal ``GoBuild``, ...)."""
    return "Go" if isinstance(using, Go) else "Cargo" if isinstance(using, Cargo) else "Dotnet"


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


def _require_bool(owner: object, value: object, what: str) -> None:
    if not isinstance(value, bool):
        raise _fail(owner, f"{what} must be True or False, got {value!r}.")


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


def _freeze_map(owner: object, name: str) -> FrozenMap[object]:
    """Copy the mapping field *name* into a :class:`FrozenMap`; anything else is refused."""
    value = getattr(owner, name)
    if not isinstance(value, Mapping):
        raise _fail(
            owner,
            f"{name}= must be a mapping, got {value!r}.",
            hint=f"Pass a dict: {name}={{'KEY': 'value'}}.",
        )
    for key in value:
        _require_name(owner, key, f"{name} key")
    frozen: FrozenMap[object] = FrozenMap(value)
    object.__setattr__(owner, name, frozen)
    return frozen


def _require_env(owner: object, name: str = "env") -> None:
    for key, value in _freeze_map(owner, name).items():
        if not _ENV_KEY.fullmatch(key):
            raise _fail(owner, f"invalid environment variable name {key!r}.")
        if not isinstance(value, str) or "\n" in value:
            raise _fail(owner, f"environment value for {key!r} must be one line of text.")


def _require_names(owner: object, values: tuple[str, ...], what: str) -> None:
    for value in values:
        _require_name(owner, value, what)


def _require_items(owner: Fragment | Recipe, items: tuple[object, ...]) -> None:
    if not isinstance(items, tuple):
        raise _fail(owner, f"{owner.name!r} items must be a list or tuple, got {items!r}.")
    for item in items:
        if not isinstance(item, (Fragment, *DECLARATION_TYPES)):
            raise _fail(
                owner,
                f"{owner.name!r} item {item!r} is not a declaration or fragment.",
                hint="Unpack generators with *(...) and nest fragments directly.",
            )


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
        _require_items(self, self.items)
        _require_names(self, self.requires, "required fragment name")
        for check in self.checks:
            if not callable(check):
                raise _fail(self, f"{self.name!r} check {check!r} is not callable.")


EMPTY_FRAGMENT = Fragment("empty")


@dataclass(frozen=True, slots=True)
class Variant:
    """An overlay on the variant it ``inherits``: ``"base"`` is ``Recipe.items``, ``None`` is
    standalone. ``outputs`` names the targets it builds (empty: inherited, ``qemu`` at the root).
    """

    name: str
    inherits: str | None = BASE_VARIANT
    add: Fragment = EMPTY_FRAGMENT
    replace: tuple[Declaration, ...] = ()
    remove: tuple[Declaration, ...] = ()
    outputs: tuple[Target, ...] = ()
    release: bool = True
    """Whether ``tundravm release`` ships it by default; ``False`` for a development variant
    (``DevTools``), which ``release`` then skips unless named with ``--allow-ineligible``.
    Only ``False`` is part of the recipe digest."""

    def __post_init__(self) -> None:
        if isinstance(self.outputs, str):
            raise _fail(
                self,
                f"outputs= of {self.name!r} must be a sequence of targets, got {self.outputs!r}.",
                hint=f"Pass outputs=({self.outputs!r},).",
            )
        _freeze(self, "replace", "remove", "outputs")
        _require_name(self, self.name)
        if not isinstance(self.release, bool):
            raise _fail(self, f"release= of {self.name!r} must be True or False.")
        if self.name == BASE_VARIANT:
            raise _fail(self, f"{BASE_VARIANT!r} is reserved for Recipe.items.")
        if self.inherits is not None:
            _require_name(self, self.inherits, "inherits")
            if self.inherits == self.name:
                raise _fail(self, f"variant {self.name!r} cannot inherit itself.")
        if not isinstance(self.add, Fragment):
            raise _fail(self, f"add= of {self.name!r} must be a Fragment.")
        for what, items in (("replace", self.replace), ("remove", self.remove)):
            for item in items:
                if not isinstance(item, DECLARATION_TYPES):
                    raise _fail(
                        self, f"{what}= of {self.name!r} holds {item!r}, not a declaration."
                    )
        for target in self.outputs:
            _require_choice(self, target, TARGETS, "output")
        if len(set(self.outputs)) != len(self.outputs):
            raise _fail(self, f"variant {self.name!r} lists an output twice.")


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
    """The whole image: recipe-wide settings, its own items and the variants.

    ``items`` are the declarations and fragments every variant that inherits
    ``"base"`` (the default) starts from; their provenance level is ``common``.
    """

    name: str
    items: tuple[Declaration | Fragment, ...] = ()
    variants: tuple[Variant, ...] = (Variant("default", outputs=("qemu",)),)
    base: str = "debian/trixie"
    arch: Literal["x86_64", "aarch64"] = "x86_64"
    mirror: str | None = None
    tools_mirror: str | None = None
    epoch: int | None = 0
    mkosi: Mkosi = Mkosi()
    policy: Policy | None = None
    snapshot: str | None = None
    """A distribution snapshot ID (``20251113T083151Z`` for Debian): mkosi's ``Snapshot=``.

    ``mirror`` and ``tools_mirror`` are mirror roots that mkosi completes itself
    (``debian`` or ``archive/debian/<snapshot>``); with a snapshot and no ``mirror``
    mkosi reads ``https://snapshot.debian.org``.
    """

    def __post_init__(self) -> None:
        _freeze(self, "items", "variants")
        _require_name(self, self.name)
        _require_items(self, self.items)
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
        if self.snapshot is not None:
            _require_name(self, self.snapshot, "snapshot")
            if "/" in self.snapshot or any(ch.isspace() for ch in self.snapshot):
                raise _fail(
                    self,
                    f"snapshot {self.snapshot!r} must be a snapshot ID, not a URL.",
                    hint="Pass the ID alone, e.g. snapshot='20251113T083151Z'; the mirror "
                    "root goes in mirror= (default https://snapshot.debian.org).",
                )
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


def packages(*names: str, role: Literal["runtime", "build"] = "runtime") -> Fragment:
    """``Package(name, role)`` for each of *names*, as one immutable fragment.

    It goes wherever a ``Fragment`` does (``Recipe.items``, a ``Fragment``'s items,
    ``Variant.add``) and is named after the call, ``packages(curl, jq)`` (with
    ``, role=build`` for build packages), so provenance shows where the packages
    came from without an invented fragment name.
    """
    if not names:
        raise ValidationError(
            "packages() needs at least one package name.",
            hint="Pass the names: packages('curl', 'jq').",
        )
    suffix = "" if role == "runtime" else f", role={role}"
    return Fragment(
        f"packages({', '.join(names)}{suffix})",
        items=tuple(Package(name, role) for name in names),
    )


@dataclass(frozen=True, slots=True)
class File:
    """*content* at *path*; ``allow_secret`` silences ``secret-in-file`` for it."""

    path: str
    content: str | bytes | Path
    mode: int = 0o644
    stage: Literal["skeleton", "extra"] = "extra"
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _require_absolute(self, self.path)
        if not isinstance(self.content, (str, bytes, Path)):
            raise _fail(self, f"content of {self.path!r} must be str, bytes or Path.")
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")
        _require_bool(self, self.allow_secret, "allow_secret")


@dataclass(frozen=True, slots=True)
class FileTree:
    """The host tree *source* at *path*.

    ``mode=None`` keeps each file's and empty directory's permission bits;
    a ``mode`` sets every file to it. ``symlinks="preserve"`` ships links as
    links, ``"follow"`` ships what they point to. ``allow_secret`` silences
    ``secret-in-file`` for every file it imports.
    """

    path: str
    source: Path
    exclude: tuple[str, ...] = ()
    mode: int | None = None
    stage: Literal["skeleton", "extra"] = "extra"
    symlinks: Literal["preserve", "follow"] = "preserve"
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _freeze(self, "exclude")
        _require_absolute(self, self.path)
        if not isinstance(self.source, Path):
            raise _fail(self, f"source of {self.path!r} must be a pathlib.Path.")
        _require_names(self, self.exclude, "exclude pattern")
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")
        _require_choice(self, self.symlinks, ("preserve", "follow"), "symlinks")
        _require_bool(self, self.allow_secret, "allow_secret")


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
    """A systemd unit: ``content`` ships the unit file, ``None`` controls a packaged one.

    ``allow_secret`` silences ``secret-in-env`` for the shipped unit file.
    """

    name: str
    content: str | Path | None = None
    enabled: bool | None = None
    masked: bool | None = None
    after_init: bool = False
    allow_secret: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _UNIT_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid unit name {self.name!r}.")
        _require_bool(self, self.allow_secret, "allow_secret")
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
    ``allow_secret`` silences ``secret-in-env`` for the rendered unit, every field.
    """

    name: str
    exec_start: str | tuple[str, ...]
    _: KW_ONLY
    description: str | None = None
    user: str | None = None
    group: str | None = None
    working_dir: str | None = None
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    env_file: str | None = None
    exec_start_pre: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    wants: tuple[str, ...] = ()
    wanted_by: str | None = None
    type: Literal["simple", "exec", "oneshot", "notify", "forking"] | None = None
    restart: Literal["always", "on-failure", "no"] = "no"
    limits: Mapping[str, str | int] = field(default_factory=FrozenMap)
    kill_mode: Literal["control-group", "mixed", "process", "none"] | None = None
    timeout_stop: str | None = None
    security: Literal["strict", "default", "none"] = "default"
    after_init: bool = True
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _freeze(self, "exec_start", "exec_start_pre", "after", "requires", "wants")
        _require_bool(self, self.allow_secret, "allow_secret")
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
        _require_env(self)
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
        for resource, value in _freeze_map(self, "limits").items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise _fail(self, f"limit {resource!r} must be a str or int, got {value!r}.")


@dataclass(frozen=True, slots=True)
class Template:
    """A file rendered from *template* with ``str.format_map(variables)`` at lowering time.

    ``allow_secret`` silences ``secret-in-file`` for the rendered file.
    """

    path: str
    template: str | Path
    variables: Mapping[str, str | int | float] = field(default_factory=FrozenMap)
    mode: int = 0o644
    stage: Literal["skeleton", "extra"] = "extra"
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _require_absolute(self, self.path)
        _require_bool(self, self.allow_secret, "allow_secret")
        if not isinstance(self.template, (str, Path)):
            raise _fail(self, f"template of {self.path!r} must be str or Path.")
        for key, value in _freeze_map(self, "variables").items():
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise _fail(self, f"variable {key!r} must be str, int or float, got {value!r}.")
        _require_mode(self, self.mode)
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")


@dataclass(frozen=True, slots=True)
class Hook:
    name: str
    phase: Phase
    script: str
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    cwd: str | None = None
    after: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "after")
        _require_name(self, self.name)
        _require_choice(self, self.phase, PHASES, "phase")
        _require_name(self, self.script, "script")
        _require_env(self)
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
    in_image: bool = True
    """Also list the repository in the image's ``/etc/apt``, not only for the build's apt."""

    def __post_init__(self) -> None:
        _freeze(self, "components")
        if not isinstance(self.in_image, bool):
            raise _fail(self, f"in_image {self.in_image!r} must be a bool.")
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
    keep_paths_by_variant: Mapping[str, Sequence[str]] = field(default_factory=FrozenMap)
    """``{variant: paths}``: paths kept only when building that variant."""

    def __post_init__(self) -> None:
        _freeze(
            self,
            "remove",
            "extra_remove",
            "keep_paths",
            "keep_units",
            "keep_binaries",
            "keep_units_extra",
        )
        for what in ("remove", "extra_remove", "keep_paths"):
            for path in getattr(self, what) or ():
                _require_absolute(self, path, what)
        for what in ("keep_units", "keep_binaries", "keep_units_extra"):
            _require_names(self, getattr(self, what) or (), what)
        frozen: dict[str, tuple[str, ...]] = {}
        for variant, paths in _freeze_map(self, "keep_paths_by_variant").items():
            if isinstance(paths, str) or not isinstance(paths, Sequence):
                raise _fail(self, f"keep_paths_by_variant[{variant!r}] must be a list of paths.")
            for path in paths:
                _require_absolute(self, path, "keep_paths_by_variant path")
            frozen[variant] = tuple(paths)
        object.__setattr__(self, "keep_paths_by_variant", FrozenMap(frozen))


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
    """Environment variable *name*: of *service* (``"app"`` or ``"app.service"``), else global."""

    name: str
    service: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ENV_KEY.fullmatch(self.name):
            raise _fail(self, f"invalid environment variable name {self.name!r}.")
        if self.service is not None:
            if not isinstance(self.service, str) or not _UNIT_NAME.fullmatch(self.service):
                raise _fail(self, f"invalid service name {self.service!r}.")
            if "." in self.service and not self.service.endswith(".service"):
                raise _fail(
                    self,
                    f"service {self.service!r} must be a .service unit.",
                    hint=f"Use {self.service.rsplit('.', 1)[0]!r}.",
                )

    @property
    def unit(self) -> str | None:
        """The service's unit name (``app.service``); ``None`` for the global environment."""
        if self.service is None:
            return None
        return self.service if self.service.endswith(".service") else f"{self.service}.service"


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


SecretTargetKind = Literal["file", "env"]


@dataclass(frozen=True, slots=True)
class ExternalDelivery:
    """Something other than tundra-tools writes a ``Secrets`` block's targets at boot.

    *name* says what (``"gcp-secret-manager"``, ``"app-secrets.service"``); *supports*
    the target kinds it writes: ``"file"`` (``SecretFile``), ``"env"`` (``SecretEnv``).
    The SSH key flow still runs through tundra-tools.
    """

    name: str
    supports: tuple[SecretTargetKind, ...] = ("file", "env")

    def __post_init__(self) -> None:
        from tundravm._modules.secret_delivery import BUILTIN_SECRET_PROVIDER

        _freeze(self, "supports")
        _require_name(self, self.name)
        if self.name == BUILTIN_SECRET_PROVIDER:
            raise _fail(
                self,
                f"{self.name!r} is the built-in provider's name.",
                hint="Name what delivers the targets, e.g. ExternalDelivery('vault-agent').",
            )
        kinds = get_args(SecretTargetKind)
        if not self.supports or any(kind not in kinds for kind in self.supports):
            raise _fail(self, f"supports must name 'file' and/or 'env', got {self.supports!r}.")
        if len(set(self.supports)) != len(self.supports):
            raise _fail(self, f"supports {self.supports!r} names a kind twice.")


@dataclass(frozen=True, slots=True)
class Secrets:
    """The secrets a variant expects at boot; *delivery* is who writes their targets.

    ``delivery=None`` is the built-in tundra-tools provider, which provisions only
    the SSH key (``BUILTIN_SECRET_CAPABILITIES``): a ``SecretFile`` or ``SecretEnv``
    entry needs an :class:`ExternalDelivery` that writes it (lint:
    ``secret-target-unsupported``).
    """

    name: str = "secrets"
    entries: tuple[Secret, ...] = ()
    store: Disk | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_directory: str = "/root/.ssh"
    ssh_key_path: str | None = "/etc/root_key"
    delivery: ExternalDelivery | None = None

    def __post_init__(self) -> None:
        _freeze(self, "entries")
        _require_name(self, self.name)
        if self.delivery is not None and not isinstance(self.delivery, ExternalDelivery):
            raise _fail(
                self,
                f"delivery must be an ExternalDelivery or None, got {self.delivery!r}.",
                hint="Pass delivery=ExternalDelivery('NAME', supports=('file',)).",
            )
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
    """Install *source* (relative to the build directory) at *destination*.

    ``artifact=True`` installs the output of the toolchain the build is ``using``:
    *source* is its ``artifact`` path (``build/<output>`` for ``Go``), and the
    build caches it as the artifact
    the way the generated ``Key``/``Disk``/``Secrets`` builds do.
    """

    source: str
    destination: str
    mode: int | None = 0o755
    directory: bool = False
    artifact: bool = False

    def __post_init__(self) -> None:
        _require_relative(self, self.source, "source")
        _require_absolute(self, self.destination, "destination")
        _require_mode(self, self.mode)
        _require_bool(self, self.artifact, "artifact")
        if self.artifact and (self.directory or self.mode is None):
            raise _fail(
                self,
                f"artifact {self.source!r} is a file installed with a mode.",
                hint="Drop directory=True and mode=None, or artifact=True.",
            )
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
    """Build the fetched source with *script* or the toolchain it is ``using`` and install it.

    Set exactly one of ``script`` (a shell script run in the source tree, with
    ``env`` exported and ``packages`` installed for it) and ``using`` (:class:`Go`,
    :class:`Cargo` or :class:`Dotnet`, which carry their own ``env`` and
    ``packages``). ``install`` paths are relative to the source tree; a toolchain's
    output is at its ``artifact`` path, e.g. ``build/<output>`` for ``Go``.
    ``cache_namespace`` names the build cache entry (default: ``name``); the key
    itself is fingerprinted: the ``current`` dialect caches under
    ``<namespace>-<fingerprint>``, a digest of the source pin, the build and
    install specs, the architecture and the toolchain, so changing any of them
    rebuilds. ``nethermind-v1`` keeps its ``<namespace>-<pin>`` (default
    ``<name>-<url digest>-<ref>``) keys.
    """

    name: str
    source: Git | Http
    script: str | None = None
    install: tuple[Install, ...] = ()
    packages: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    cache_namespace: str | None = None
    using: Go | Cargo | Dotnet | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        _freeze(self, "install", "packages")
        if not isinstance(self.name, str) or not _BUILD_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid build name {self.name!r}.")
        if (self.script is None) == (self.using is None):
            raise _fail(
                self,
                f"build {self.name!r} needs exactly one of script= and using=.",
                hint="script='make' runs a shell script; using=Go(...), Cargo(...) or "
                "Dotnet(...) renders the toolchain's build command.",
            )
        if self.using is not None:
            if not isinstance(self.using, (Go, Cargo, Dotnet)):
                raise _fail(self, f"build {self.name!r} using= must be Go, Cargo or Dotnet.")
            if self.packages or self.env:
                raise _fail(
                    self,
                    f"build {self.name!r} takes packages and env on its toolchain.",
                    hint=f"Pass them to {_toolchain_name(self.using)}(packages=..., env=...).",
                )
        if self.cache_namespace is not None and (
            not isinstance(self.cache_namespace, str)
            or not _CACHE_NAMESPACE.fullmatch(self.cache_namespace)
        ):
            raise _fail(
                self,
                f"invalid cache_namespace {self.cache_namespace!r} for build {self.name!r}.",
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
            if step.artifact and (
                self.using is None or posixpath.normpath(step.source) != self.using.artifact
            ):
                expected = "a using=" if self.using is None else repr(self.using.artifact)
                raise _fail(
                    self,
                    f"build {self.name!r} installs {step.source!r} as its artifact, "
                    f"not {expected}.",
                    hint="artifact=True names the toolchain's output path; "
                    "install other files without it.",
                )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise _fail(
                self,
                f"build {self.name!r} install destinations share the file name "
                f"{', '.join(duplicates)}.",
                hint="Each artifact is cached under its destination's file name.",
            )
        _require_names(self, self.packages, "package")
        _require_env(self)


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
    | FileTree
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
    FileTree,
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
class Resolved:
    """One variant after expansion, overlays and reference checks."""

    variant: str
    outputs: tuple[Target, ...]
    """Every output of the variant, own or inherited (never empty)."""
    items: tuple[Declaration, ...]
    fragments: tuple[str, ...]

    def __post_init__(self) -> None:
        _freeze(self, "outputs")
        if not self.outputs:
            raise _fail(self, f"variant {self.variant!r} resolved to no outputs.")


__all__ = [
    "BASE_VARIANT",
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
    "Disk",
    "Dotnet",
    "EMPTY_FRAGMENT",
    "ExternalDelivery",
    "File",
    "FileTree",
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
    "packages",
]
