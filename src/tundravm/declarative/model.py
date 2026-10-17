"""Immutable declaration values of the declarative frontend (design doc section 2).

Every record is a frozen, slotted dataclass. ``__post_init__`` rejects malformed
values at declaration time (empty names, relative paths, unknown phases,
out-of-range modes) plus the structural rules of the design
(``Disk.key`` holds a ``Key`` ref, ``Secrets.store`` a ``Disk`` ref).
Lists passed for tuple fields are frozen into tuples.
"""

from __future__ import annotations

import functools
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, MISSING, dataclass, field
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import ClassVar, Literal, get_args, overload

from tundravm._source import CargoBuild as Cargo
from tundravm._source import DotnetBuild as Dotnet
from tundravm._source import FrozenMap
from tundravm._source import GoBuild as Go
from tundravm.diagnostics import Diagnostic
from tundravm.errors import ValidationError
from tundravm.models import unit_name
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
BUILTIN_STEPS: tuple[str, ...] = ("keys", "disks", "secrets")
"""The runtime-init steps the compiler emits for keys, disks and secrets, in their rank."""
ALL_STEPS = "*"
"""The step name of :data:`RUNTIME_INIT`: the whole runtime-init chain."""

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


def _require_items(owner: Fragment | Recipe | Platform, items: tuple[object, ...]) -> None:
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

    ``changes`` edit or remove inherited declarations by identity (``key.ref.edit(size=32)``,
    ``Unit.ref("ssh.service").remove()``) before ``add`` adds its own.
    """

    name: str
    inherits: str | None = BASE_VARIANT
    add: Fragment = EMPTY_FRAGMENT
    changes: tuple[VariantChange, ...] = ()
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
        _freeze(self, "changes", "outputs")
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
        _check_changes(self)
        for target in self.outputs:
            _require_choice(self, target, TARGETS, "output")
        if len(set(self.outputs)) != len(self.outputs):
            raise _fail(self, f"variant {self.name!r} lists an output twice.")


def _check_changes(variant: Variant) -> None:
    """Each change is an ``Edit`` or ``Remove``, and none touches what another already does."""
    touched: dict[Ref, set[str] | None] = {}
    for change in variant.changes:
        if not isinstance(change, (Edit, Remove)):
            raise _fail(
                variant,
                f"changes= of {variant.name!r} holds {change!r}, not an edit or a removal.",
                hint="Use value.ref.edit(FIELD=...) or value.ref.remove() "
                '(Unit.ref("ssh.service").remove() without the value).',
            )
        target = change.target
        seen = touched.get(target, set())
        if target in touched and (seen is None or isinstance(change, Remove)):
            raise _fail(variant, f"variant {variant.name!r} changes {target} twice.")
        if isinstance(change, Remove):
            touched[target] = None
            continue
        assert seen is not None
        for name in change.fields:
            if name in seen:
                raise _fail(variant, f"variant {variant.name!r} edits {target}.{name} twice.")
            seen.add(name)
        touched[target] = seen


@dataclass(frozen=True, slots=True)
class Mkosi:
    """Compiler configuration: tree layout, emission dialect and generated helper files.

    ``init_script`` is written to ``mkosi.skeleton/init`` (mode 0755);
    ``version_script`` emits ``mkosi.version``; ``cloud_postoutput`` emits the
    Azure/GCP disk conversion postoutput scripts; ``strip_os_release`` strips
    ``IMAGE_VERSION`` from ``os-release`` (``None``: when ``Platform.epoch`` is set).
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


Boot = Literal["systemd", "uki"]
BOOT_PACKAGES: dict[str, tuple[str, ...]] = {
    "systemd": ("systemd", "systemd-sysv", "udev", "kmod", "systemd-boot-efi"),
    "uki": ("kmod", "systemd", "systemd-boot-efi"),
}
"""The packages each ``Debian(boot=...)`` mode installs, after the kernel package.

``systemd`` boots systemd as init (``systemd-sysv``) with ``udev``; ``uki`` is a
unified kernel image whose ``/init`` the image supplies: ``kmod``, systemd's tools
and the UKI's EFI stub (``systemd-boot-efi``).
"""


@dataclass(frozen=True, slots=True)
class Platform:
    """What a recipe builds on: a distribution *release*, its archive and its boot set.

    ``mirror`` and ``tools_mirror`` are mirror roots that mkosi completes itself
    (``debian`` or ``archive/debian/<snapshot>``); with a ``snapshot`` and no
    ``mirror`` mkosi reads ``https://snapshot.debian.org``. ``epoch`` is
    ``SOURCE_DATE_EPOCH`` (``None``: a non-reproducible build). ``items`` are
    declarations and fragments every variant gets, standalone ones included, after
    :attr:`packages`; their provenance level is ``platform``. Spell a platform with
    a distribution's class, :class:`Debian`.
    """

    distribution: ClassVar[str] = ""
    release: str
    _: KW_ONLY
    arch: Literal["x86_64", "aarch64"] = "x86_64"
    snapshot: str | None = None
    """A distribution snapshot ID (``20251113T083151Z`` for Debian): mkosi's ``Snapshot=``."""
    mirror: str | None = None
    tools_mirror: str | None = None
    epoch: int | None = 0
    items: tuple[Declaration | Fragment, ...] = ()

    def __post_init__(self) -> None:
        if not self.distribution:
            raise _fail(self, "name a distribution: Debian('trixie').")
        _freeze(self, "items")
        _require_name(self, self.release, "release")
        if "/" in self.release:
            raise _fail(
                self,
                f"release {self.release!r} is a release name, not a base.",
                hint=f"Pass the release alone: {type(self).__name__}('trixie').",
            )
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
        _require_items(self, self.items)

    @property
    def name(self) -> str:
        """``debian/trixie``: the distribution and release, mkosi's ``Distribution``/``Release``."""
        return f"{self.distribution}/{self.release}"

    @property
    def packages(self) -> tuple[str, ...]:
        """The package names the platform installs in every variant."""
        return ()

    def contribution(self) -> tuple[Declaration | Fragment, ...]:
        """What the platform declares in every variant: :attr:`packages`, then ``items``."""
        return (*(Package(name) for name in self.packages), *self.items)


@dataclass(frozen=True, slots=True, kw_only=True)
class Debian(Platform):
    """A Debian *release*: ``Debian("trixie", snapshot="20251113T083151Z")``.

    ``boot`` picks the boot set (:data:`BOOT_PACKAGES`) installed after
    ``kernel_package`` (``None``: the image declares its own ``Kernel``);
    ``boot=None`` installs neither, for a non-bootable image. ``efi_stub`` pins the
    UKI's EFI stub to that ``systemd-boot-efi`` version, from the platform's own
    archive (:attr:`efi_archive`), so the stub and the packages cannot disagree.
    """

    distribution: ClassVar[str] = "debian"
    boot: Boot | None = "systemd"
    kernel_package: str | None = "linux-image-amd64"
    efi_stub: str | None = None

    def __post_init__(self) -> None:
        Platform.__post_init__(self)
        if self.boot is not None:
            _require_choice(self, self.boot, tuple(BOOT_PACKAGES), "boot")
        if self.kernel_package is not None:
            _require_name(self, self.kernel_package, "kernel_package")
        if self.efi_stub is not None:
            _require_name(self, self.efi_stub, "efi_stub")
            if self.efi_archive is None:
                raise _fail(
                    self,
                    "efi_stub needs the archive it comes from: the platform pins no snapshot.",
                    hint="Pass snapshot='20251113T083151Z' (a snapshot of the release that "
                    "ships that systemd-boot-efi version).",
                )

    @property
    def packages(self) -> tuple[str, ...]:
        """``kernel_package`` and the ``boot`` set; nothing when ``boot`` is ``None``."""
        if self.boot is None:
            return ()
        kernel = () if self.kernel_package is None else (self.kernel_package,)
        return (*kernel, *BOOT_PACKAGES[self.boot])

    @property
    def efi_archive(self) -> str | None:
        """Where :attr:`efi_stub` comes from: the snapshot (under ``mirror`` when set).

        Without a snapshot, a ``mirror`` that is itself a snapshot archive URL
        (``.../archive/debian/<snapshot>/``, the ``nethermind-v1`` spelling).
        """
        if self.snapshot is not None:
            if self.mirror is None:
                return self.snapshot
            return f"{self.mirror.rstrip('/')}/archive/debian/{self.snapshot}"
        if self.mirror is not None and "/archive/" in self.mirror:
            return self.mirror
        return None

    def contribution(self) -> tuple[Declaration | Fragment, ...]:
        """:attr:`packages`, the pinned EFI stub, then ``items``."""
        from .utils import EfiStub

        stub: tuple[Fragment, ...] = ()
        if self.efi_stub is not None and self.efi_archive is not None:
            stub = (EfiStub(snapshot=self.efi_archive, version=self.efi_stub),)
        return (*(Package(name) for name in self.packages), *stub, *self.items)


@dataclass(frozen=True, slots=True)
class Recipe:
    """The whole image: its platform, recipe-wide settings, its own items and the variants.

    ``platform`` (``Debian("trixie", ...)``) is the distribution and archive, and
    declares its packages and ``items`` in every variant first (provenance level
    ``platform``). ``items`` are the declarations and fragments every variant that
    inherits ``"base"`` (the default) starts from; their provenance level is ``common``.
    """

    name: str
    items: tuple[Declaration | Fragment, ...] = ()
    variants: tuple[Variant, ...] = (Variant("default", outputs=("qemu",)),)
    platform: Platform = field(default_factory=lambda: Debian("trixie", boot=None))
    """The distribution, its archive and what it declares (default: bare Debian trixie)."""
    mkosi: Mkosi = Mkosi()
    policy: Policy | None = None

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
        if not isinstance(self.platform, Platform):
            raise _fail(
                self,
                f"platform= holds {self.platform!r}, not a Platform.",
                hint="Pass a distribution: platform=Debian('trixie').",
            )
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


# ── References and changes ──────────────────────────────────────────────

_KEY_FIELDS: dict[str, tuple[str, ...]] = {
    "Package": ("name", "role"),
    "File": ("stage", "path"),
    "FileTree": ("stage", "path"),
    "Template": ("stage", "path"),
    "Setting": ("section", "key"),
    "Debloat": (),
    "RuntimeTools": (),
    "Kernel": (),
}
"""Identity fields of the declaration types that are not identified by ``name`` alone."""


def key_fields(kind: str) -> tuple[str, ...]:
    """The fields that identify a *kind* declaration, in identity order."""
    return _KEY_FIELDS.get(kind, ("name",))


def _key_part(kind: str, name: str, value: object) -> str:
    if name == "path":
        return posixpath.normpath(str(value))
    if kind in ("Unit", "Service"):
        return unit_name(str(value))
    return str(value)


def _declaration_type(owner: object, kind: str) -> type:
    for cls in DECLARATION_TYPES:
        if cls.__name__ == kind:
            return cls
    raise _fail(owner, f"{kind!r} is not a declaration type.")


@dataclass(frozen=True, slots=True)
class Ref:
    """A declaration named by its identity: ``kind`` plus the natural key ``identity``.

    ``value.ref`` is a value's ref, ``Unit.ref("ssh.service")`` one built from the
    key. ``Disk.key`` and ``Secrets.store`` hold refs; resolution binds each to the
    variant's final value (:meth:`Resolved.bind`). ``edit`` and ``remove`` make the
    :class:`Variant` ``changes`` that target it.
    """

    kind: str
    identity: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze(self, "identity")
        _declaration_type(self, self.kind)
        keys = key_fields(self.kind)
        if len(self.identity) != len(keys) or not all(isinstance(p, str) for p in self.identity):
            raise _fail(
                self,
                f"a {self.kind} ref holds {', '.join(keys) or 'nothing'}, got {self.identity!r}.",
                hint=f"Use {self.kind}.ref(...) or value.ref.",
            )

    def __str__(self) -> str:
        return f"{self.kind}({', '.join(self.identity)})" if self.identity else self.kind

    def edit(self, **fields: object) -> Edit:
        """A change that sets *fields* of the inherited declaration this ref names."""
        return Edit(self, fields)

    def remove(self) -> Remove:
        """A change that drops the inherited declaration this ref names."""
        return Remove(self)


@dataclass(frozen=True, slots=True)
class Edit:
    """Set ``fields`` of the inherited declaration ``target``: ``key.ref.edit(size=32)``.

    Fields must exist on the target's type and may not be identity fields.
    """

    target: Ref
    fields: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.target, Ref):
            raise _fail(self, f"target must be a Ref, got {self.target!r}.")
        frozen = _freeze_map(self, "fields")
        if not frozen:
            raise _fail(self, f"the edit of {self.target} sets no field.")
        values = {k: tuple(v) if isinstance(v, list) else v for k, v in frozen.items()}
        object.__setattr__(self, "fields", FrozenMap(values))
        cls = _declaration_type(self, self.target.kind)
        known = [f.name for f in dataclass_fields(cls) if f.init]
        for name in values:
            if name in key_fields(self.target.kind):
                raise _fail(
                    self,
                    f"{name!r} identifies {self.target}; an edit cannot change it.",
                    hint=f"Remove {self.target} and add the new declaration instead.",
                )
            if name not in known:
                raise _fail(
                    self,
                    f"{self.target.kind} has no field {name!r}.",
                    hint=f"{self.target.kind} fields: {', '.join(known)}.",
                )


@dataclass(frozen=True, slots=True)
class Remove:
    """Drop the inherited declaration ``target``: ``Unit.ref("ssh.service").remove()``."""

    target: Ref

    def __post_init__(self) -> None:
        if not isinstance(self.target, Ref):
            raise _fail(self, f"target must be a Ref, got {self.target!r}.")


type VariantChange = Edit | Remove
"""What :attr:`Variant.changes` holds."""


def _type_ref(cls: type, *args: object, **kwargs: object) -> Ref:
    """``cls.ref(*args, **kwargs)``: the key fields bound like ``cls``'s constructor."""
    kind = cls.__name__
    keys = key_fields(kind)
    params = [f for f in dataclass_fields(cls) if f.name in keys]
    if len(args) > len(params):
        raise ValidationError(
            f"{kind}.ref() takes {len(params)} key argument(s), got {len(args)}.",
            hint=f"{kind} is identified by {', '.join(keys) or 'its type alone'}.",
        )
    values: dict[str, object] = dict(zip((f.name for f in params), args, strict=False))
    for name, value in kwargs.items():
        if name not in keys or name in values:
            raise ValidationError(
                f"{kind}.ref() got an unexpected or repeated argument {name!r}.",
                hint=f"{kind} is identified by {', '.join(keys) or 'its type alone'}.",
            )
        values[name] = value
    for param in params:
        if param.name not in values:
            if param.default is MISSING:
                raise ValidationError(
                    f"{kind}.ref() needs {param.name}.",
                    hint=f"{kind} is identified by {', '.join(keys)}.",
                )
            values[param.name] = param.default
    return Ref(kind, tuple(_key_part(kind, name, values[name]) for name in keys))


class _RefAccessor:
    """``value.ref`` is the value's :class:`Ref`; ``Type.ref(*key)`` builds one from the key."""

    @overload
    def __get__(self, obj: None, owner: type) -> Callable[..., Ref]: ...
    @overload
    def __get__(self, obj: object, owner: type | None = None) -> Ref: ...
    def __get__(self, obj: object, owner: type | None = None) -> Ref | Callable[..., Ref]:
        if obj is None:
            assert owner is not None
            return functools.partial(_type_ref, owner)
        kind = type(obj).__name__
        return Ref(kind, tuple(_key_part(kind, k, getattr(obj, k)) for k in key_fields(kind)))


class Declared:
    """Base of the declaration types: each value has a :class:`Ref` (``value.ref``)."""

    __slots__ = ()
    ref = _RefAccessor()


@dataclass(frozen=True, slots=True)
class Ready:
    """A readiness handle: "runtime-init step *step* has run".

    ``key.ready``, ``disk.ready``, ``secrets.ready`` and ``step.ready`` (a
    :class:`BootStep`) are the handles ``BootStep(after=/before=)`` and
    ``Unit``/``Service(needs=)`` take; :data:`RUNTIME_INIT` stands for every step.
    """

    step: str

    def __post_init__(self) -> None:
        if not isinstance(self.step, str) or not (
            self.step == ALL_STEPS or _ENTRY_NAME.fullmatch(self.step)
        ):
            raise _fail(self, f"invalid step name {self.step!r}.")

    def __str__(self) -> str:
        return self.step


RUNTIME_INIT = Ready(ALL_STEPS)
"""``needs=[RUNTIME_INIT]``: wait for the whole runtime-init chain (``runtime-init.service``)."""


def _require_steps(owner: object, what: str, *, sentinel: bool = False) -> None:
    """Normalize field *what* of *owner* to a tuple of :class:`Ready` handles.

    Accepts ``Ready`` handles, :class:`BootStep` values and step names; only
    *sentinel* fields (``needs``) take :data:`RUNTIME_INIT`.
    """
    values = getattr(owner, what)
    if isinstance(values, (str, Ready, BootStep)) or not isinstance(values, (tuple, list)):
        raise _fail(owner, f"{what} must be a list of readiness handles, got {values!r}.")
    handles: list[Ready] = []
    for value in values:
        if isinstance(value, BootStep):
            value = value.ready
        elif isinstance(value, str):
            value = Ready(value)
        if not isinstance(value, Ready):
            raise _fail(
                owner,
                f"{what} entry {value!r} is not a readiness handle.",
                hint="Pass key.ready, disk.ready, secrets.ready or a BootStep's .ready.",
            )
        if value == RUNTIME_INIT and not sentinel:
            raise _fail(owner, f"{what} cannot name RUNTIME_INIT.", hint="Name the steps.")
        handles.append(value)
    object.__setattr__(owner, what, tuple(dict.fromkeys(handles)))


# ── Declarations ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Package(Declared):
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
class File(Declared):
    """*content* at *path*; ``allow_secret`` silences ``secret-in-file`` for it.

    Spell it :meth:`File.text` for text and :meth:`File.copy` for a host file; the
    constructor takes either (a ``Path`` *content* is read when the recipe lowers)
    and ``bytes``. ``mode=None`` is for a copied file only: ``0755`` when the
    source has an execute bit, else ``0644`` (the bits git records).
    """

    path: str
    content: str | bytes | Path
    mode: int | None = 0o644
    stage: Literal["skeleton", "extra"] = "extra"
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _require_absolute(self, self.path)
        if not isinstance(self.content, (str, bytes, Path)):
            raise _fail(self, f"content of {self.path!r} must be str, bytes or Path.")
        _require_mode(self, self.mode)
        if self.mode is None and not isinstance(self.content, Path):
            raise _fail(
                self,
                f"mode=None keeps a copied file's execute bit; {self.path!r} is not copied.",
                hint="Pass a mode such as mode=0o644.",
            )
        _require_choice(self, self.stage, ("skeleton", "extra"), "stage")
        _require_bool(self, self.allow_secret, "allow_secret")

    @classmethod
    def text(
        cls,
        path: str,
        text: str,
        *,
        mode: int = 0o644,
        stage: Literal["skeleton", "extra"] = "extra",
        allow_secret: bool = False,
    ) -> File:
        """The text *text* at *path*, written as UTF-8."""
        if not isinstance(text, str):
            raise ValidationError(
                f"File.text({path!r}) takes str text, got {type(text).__name__}.",
                hint="Use File.copy(path, source) for a host file, File(path, data) for bytes.",
            )
        return cls(path, text, mode, stage, allow_secret)

    @classmethod
    def copy(
        cls,
        path: str,
        source: Path,
        *,
        mode: int | None = None,
        stage: Literal["skeleton", "extra"] = "extra",
        allow_secret: bool = False,
    ) -> File:
        """The host file *source*, read when the recipe lowers, at *path*.

        ``mode=None`` ships ``0755`` if *source* has an execute bit, else ``0644``.
        """
        if not isinstance(source, Path):
            raise ValidationError(
                f"File.copy({path!r}) takes a pathlib.Path source, got {source!r}.",
                hint="Use File.text(path, text) for text, or Resources.file() for a path.",
            )
        return cls(path, source, mode, stage, allow_secret)


@dataclass(frozen=True, slots=True)
class FileTree(Declared):
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
class Group(Declared):
    name: str
    system: bool = True
    gid: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _GROUP_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid group name {self.name!r}.")
        if self.gid is not None and self.gid < 0:
            raise _fail(self, f"gid {self.gid} must not be negative.")


@dataclass(frozen=True, slots=True)
class User(Declared):
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
class Unit(Declared):
    """A systemd unit: ``content`` ships the unit file, ``None`` controls a packaged one.

    ``needs`` lists the runtime-init steps (readiness handles) the shipped unit
    waits for: ``After=``/``Requires=`` their units. ``allow_secret`` silences
    ``secret-in-env`` for the shipped unit file.
    """

    name: str
    content: str | Path | None = None
    enabled: bool | None = None
    masked: bool | None = None
    needs: Sequence[Ready] = ()
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _require_steps(self, "needs", sentinel=True)
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

    @classmethod
    def from_file(
        cls,
        name: str,
        source: Path,
        *,
        enabled: bool | None = None,
        masked: bool | None = None,
        needs: Sequence[Ready] = (),
        allow_secret: bool = False,
    ) -> Unit:
        """The unit file *source* (UTF-8, read when the recipe lowers) shipped as *name*."""
        if not isinstance(source, Path):
            raise ValidationError(
                f"Unit.from_file({name!r}) takes a pathlib.Path source, got {source!r}.",
                hint="Pass Unit(name, text) for unit text, or Resources.file() for a path.",
            )
        return cls(name, source, enabled, masked, needs, allow_secret)


@dataclass(frozen=True, slots=True)
class Service(Declared):
    """A generated systemd service: the compiler renders the unit from these fields.

    ``security`` selects the hardening profile (``strict``, ``default`` or
    ``none``). ``needs`` lists the runtime-init steps the unit waits for (the
    default, :data:`RUNTIME_INIT`, is the whole chain when the variant has one;
    ``needs=()`` starts it without waiting). ``Unit`` ships verbatim text.
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
    needs: Sequence[Ready] = (RUNTIME_INIT,)
    allow_secret: bool = False

    def __post_init__(self) -> None:
        _freeze(self, "exec_start", "exec_start_pre", "after", "requires", "wants")
        _require_steps(self, "needs", sentinel=True)
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
class Template(Declared):
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
class Hook(Declared):
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
class BootStep(Declared):
    """A runtime-init step: *script* runs at boot after its ``after`` steps, before ``before``.

    ``after``/``before`` take readiness handles (``disk.ready``, ``secrets.ready``,
    another step's ``.ready``); the steps run in a deterministic topological
    order. ``.ready`` is this step's handle, for ``needs=`` and other steps.
    """

    name: str
    script: str
    after: Sequence[Ready] = ()
    before: Sequence[Ready] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _ENTRY_NAME.fullmatch(self.name):
            raise _fail(self, f"invalid step name {self.name!r}.", hint="Use [A-Za-z0-9_.-].")
        if self.name in BUILTIN_STEPS:
            raise _fail(
                self,
                f"{self.name!r} is the runtime-init step of the built-in {self.name} declarations.",
                hint="Pick another name; order against the built-in with its handle (disk.ready).",
            )
        _require_name(self, self.script, "script")
        _require_steps(self, "after")
        _require_steps(self, "before")
        if self.ready in self.after or self.ready in self.before:
            raise _fail(self, f"step {self.name!r} cannot run after or before itself.")

    @property
    def ready(self) -> Ready:
        """This step's readiness handle."""
        return Ready(self.name)


@dataclass(frozen=True, slots=True)
class Repository(Declared):
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
class Partition(Declared):
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
class Debloat(Declared):
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
class Setting(Declared):
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
class Key(Declared):
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

    @property
    def ready(self) -> Ready:
        """The handle of the ``keys`` runtime-init step, which creates every key."""
        return Ready("keys")


@dataclass(frozen=True, slots=True)
class Disk(Declared):
    name: str
    mount: str
    device: str | None = None
    key: Key | Ref | Path | None = None
    """The disk's key: a ``Key`` (stored as its ref, bound to the variant's key), or a key file."""
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
        if isinstance(self.key, Key):
            object.__setattr__(self, "key", self.key.ref)
        if self.key is not None and not (
            isinstance(self.key, Path) or (isinstance(self.key, Ref) and self.key.kind == "Key")
        ):
            raise _fail(
                self,
                f"disk {self.name!r}: key must be a Key, a Key ref or a key file Path, "
                f"got {self.key!r}.",
                hint="Pass the Key (key=my_key) or Key.ref(NAME), not its name.",
            )
        if self.mapper is not None:
            _require_name(self, self.mapper, "mapper")
            if self.key is None:
                raise _fail(self, f"disk {self.name!r}: a plain disk cannot set mapper=.")
        _require_choice(
            self, self.format, ("always", "on_initialize", "on_fail", "never"), "format"
        )
        _require_names(self, self.directories, "directory")

    @property
    def ready(self) -> Ready:
        """The handle of the ``disks`` runtime-init step, which opens and mounts every disk."""
        return Ready("disks")


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
class Secrets(Declared):
    """The secrets a variant expects at boot; *delivery* is who writes their targets.

    ``delivery=None`` is the built-in tundra-tools provider, which provisions only
    the SSH key (``BUILTIN_SECRET_CAPABILITIES``): a ``SecretFile`` or ``SecretEnv``
    entry needs an :class:`ExternalDelivery` that writes it (lint:
    ``secret-target-unsupported``).
    """

    name: str = "secrets"
    entries: tuple[Secret, ...] = ()
    store: Disk | Ref | None = None
    """The disk holding the secrets: a ``Disk`` (stored as its ref) or a Disk ref."""
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
        if isinstance(self.store, Disk):
            object.__setattr__(self, "store", self.store.ref)
        if self.store is not None and not (
            isinstance(self.store, Ref) and self.store.kind == "Disk"
        ):
            raise _fail(
                self,
                f"store must be a Disk or a Disk ref, got {self.store!r}.",
                hint="Pass the Disk (store=my_disk) or Disk.ref(NAME), not its name.",
            )
        _require_name(self, self.host, "host")
        if isinstance(self.port, bool) or not 0 < self.port < 65536:
            raise _fail(self, f"port {self.port!r} is out of range.")
        _require_absolute(self, self.ssh_directory, "ssh_directory")
        if self.ssh_key_path is not None:
            _require_absolute(self, self.ssh_key_path, "ssh_key_path")

    @property
    def ready(self) -> Ready:
        """The handle of the ``secrets`` runtime-init step, which runs every secret delivery."""
        return Ready("secrets")


@dataclass(frozen=True, slots=True)
class RuntimeTools(Declared):
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
class Build(Declared):
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
class Kernel(Declared):
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
    | BootStep
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
    BootStep,
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

    def bind(self, ref: Ref) -> Declaration:
        """The variant's final declaration *ref* names; ``ValidationError`` when absent."""
        for item in self.items:
            if item.ref == ref:
                return item
        raise ValidationError(
            f"variant {self.variant!r} does not declare {ref}.",
            hint=f"Declare {ref} in the variant, or reference one it declares.",
        )


__all__ = [
    "BASE_VARIANT",
    "ALL_STEPS",
    "BUILTIN_STEPS",
    "RUNTIME_INIT",
    "DECLARATION_TYPES",
    "PHASES",
    "TARGETS",
    "BootStep",
    "Build",
    "Cargo",
    "VariantChange",
    "Check",
    "Debloat",
    "Declaration",
    "Declared",
    "Diagnostic",
    "Disk",
    "Dotnet",
    "EMPTY_FRAGMENT",
    "Edit",
    "ExternalDelivery",
    "File",
    "FileTree",
    "Fragment",
    "Git",
    "Go",
    "Group",
    "Hook",
    "Http",
    "Install",
    "Kernel",
    "Key",
    "Mkosi",
    "Package",
    "Pairs",
    "Partition",
    "Phase",
    "Recipe",
    "Ready",
    "Ref",
    "Remove",
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
    "key_fields",
    "packages",
]
