"""Profile handles: one named slice of an :class:`~tundravm.image.Image` recipe.

``img.profile("azure")`` returns a :class:`Profile`. Use it as a context manager
or call the declaration API on it directly::

    azure = img.profile("azure")
    azure.install("walinuxagent").targets("azure")

Every declaration call runs with only that profile active and returns the
``Profile``, so chains stay on it. Inspection and build calls (``explain``,
``check``, ``compile``, ``bake``, ...) are scoped to the profile as well.
Image-wide setters (``set_policy``, ``set_kernel``, ``set_mkosi``, ``pin_mirror``)
are not on a profile; call them on ``profile.image``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Literal, NoReturn

from .check import Diagnostic
from .diff import TreeDiff
from .image import Image
from .lockfile import LockDrift
from .measure import Measurements
from .models import BakeResult, CompileResult, DeployResult, OutputTarget, ProfileState
from .modules.base import Module
from .observability import Reporter
from .source import Resolver

IMAGE_WIDE_SETTERS = frozenset({"pin_mirror", "set_kernel", "set_mkosi", "set_policy"})
"""``Image`` declaration methods that configure the whole image, never one profile."""


def _scoped[**P](
    method: Callable[Concatenate[Image, P], object],
) -> Callable[Concatenate[Profile, P], Profile]:
    attr = method.__name__

    def scoped(self: Profile, /, *args: P.args, **kwargs: P.kwargs) -> Profile:
        return self._declare(attr, *args, **kwargs)

    scoped.__name__ = attr
    scoped.__qualname__ = f"Profile.{attr}"
    scoped.__doc__ = method.__doc__
    return scoped


class _ImageWide:
    """Raise ``AttributeError`` pointing at ``profile.image`` for an image-wide setter."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def __get__(self, obj: object, objtype: type | None = None) -> NoReturn:
        raise AttributeError(
            f"'Profile' object has no attribute {self.name!r}; Image.{self.name} is "
            f"image-wide, use profile.image.{self.name}(...)"
        )


class Profile:
    """A named profile of an :class:`Image`; see the module docstring."""

    __slots__ = ("_entered", "image", "name")

    def __init__(self, image: Image, name: str) -> None:
        self.image = image
        self.name = name
        self._entered: list[AbstractContextManager[Image]] = []

    def __repr__(self) -> str:
        return f"Profile({self.name!r})"

    def __enter__(self) -> Image:
        context = self.image.profiles(self.name)
        context.__enter__()
        self._entered.append(context)
        return self.image

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        return self._entered.pop().__exit__(exc_type, exc, tb)

    def _declare(self, attr: str, /, *args: Any, **kwargs: Any) -> Profile:
        method = getattr(self.image, attr)
        with self.image.profiles(self.name):
            method(*args, **kwargs)
        return self

    # --- Declarations: each runs with only this profile active ---

    apply = _scoped(Image.apply)
    install = _scoped(Image.install)
    build_packages = _scoped(Image.build_packages)
    mount_build_source = _scoped(Image.mount_build_source)
    build_from = _scoped(Image.build_from)
    repository = _scoped(Image.repository)
    file = _scoped(Image.file)
    copy_tree = _scoped(Image.copy_tree)
    template = _scoped(Image.template)
    skeleton = _scoped(Image.skeleton)
    group = _scoped(Image.group)
    user = _scoped(Image.user)
    service = _scoped(Image.service)
    enable = _scoped(Image.enable)
    disable = _scoped(Image.disable)
    mask = _scoped(Image.mask)
    partition = _scoped(Image.partition)
    targets = _scoped(Image.targets)
    debloat = _scoped(Image.debloat)
    shell = _scoped(Image.shell)
    runtime_init = _scoped(Image.runtime_init)
    strip_image_version = _scoped(Image.strip_image_version)
    efi_stub = _scoped(Image.efi_stub)
    backports = _scoped(Image.backports)

    # --- Image-wide setters live on profile.image ---

    set_policy = _ImageWide()
    set_kernel = _ImageWide()
    set_mkosi = _ImageWide()
    pin_mirror = _ImageWide()

    # --- Profile-scoped inspection and build ---

    @property
    def state(self) -> ProfileState:
        """This profile's recorded state."""
        return self.image.state.ensure_profile(self.name)

    def applied_modules(self, *, inherited: bool = False) -> tuple[Module, ...]:
        """Modules applied to this profile; with *inherited*, its base profile's first."""
        return self.image.applied_modules(profile=self.name, inherited=inherited)

    def explain(self) -> dict[str, object]:
        return self.image.explain(profile=self.name)

    def explain_debloat(self) -> dict[str, object]:
        return self.image.explain_debloat(profile=self.name)

    def summary(self) -> str:
        return self.image.summary(profile=self.name)

    def check(self) -> list[Diagnostic]:
        """Diagnostics for this profile only."""
        return [d for d in self.image.check(profiles=(self.name,)) if d.profile == self.name]

    def compile(self, path: str | Path, *, force: bool = False) -> CompileResult:
        """Compile this profile only."""
        return self.image.compile(path, force=force, profiles=(self.name,))

    def lock(
        self,
        path: str | Path | None = None,
        *,
        resolver: Resolver | None = None,
        offline: bool = False,
    ) -> Path:
        return self.image.lock(path, resolver=resolver, offline=offline, profiles=(self.name,))

    def lock_status(
        self, path: str | Path | None = None, *, resolver: Resolver | None = None
    ) -> LockDrift:
        return self.image.lock_status(path, resolver=resolver, profiles=(self.name,))

    def diff(self, against: str | Path) -> TreeDiff:
        return self.image.diff(against, profiles=(self.name,))

    def bake(
        self,
        output_dir: str | Path | None = None,
        *,
        frozen: bool = False,
        force: bool = False,
        reporter: Reporter | None = None,
    ) -> BakeResult:
        """Bake this profile only."""
        return self.image.bake(
            output_dir, frozen=frozen, force=force, reporter=reporter, profiles=(self.name,)
        )

    def measure(
        self,
        *,
        backend: Literal["rtmr", "azure", "gcp"],
        allow_placeholder: bool = False,
    ) -> Measurements:
        return self.image.measure(
            backend=backend, profile=self.name, allow_placeholder=allow_placeholder
        )

    def deploy(
        self,
        *,
        target: OutputTarget,
        parameters: Mapping[str, str] | None = None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> DeployResult:
        return self.image.deploy(
            target=target,
            profile=self.name,
            parameters=parameters,
            memory=memory,
            cpus=cpus,
        )


__all__ = ["IMAGE_WIDE_SETTERS", "Profile"]
