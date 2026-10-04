"""Profile handles: one named slice of an :class:`~tundravm.image.Image` recipe.

``img.profile("azure")`` returns a :class:`Profile`. Use it as a context manager,
exactly like before, or call the declaration API on it directly::

    azure = img.profile("azure")
    azure.install("walinuxagent").output_targets("azure")

Every declaration call runs with only that profile active and returns the
``Profile``, so chains stay on it. Inspection and build calls (``explain``,
``check``, ``compile``, ``bake``, ...) are scoped to the profile as well.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Concatenate, Literal, Self

from .check import Diagnostic
from .diff import TreeDiff
from .image import Image
from .measure import Measurements
from .models import BakeResult, CompileResult, DeployResult, OutputTarget, ProfileState
from .modules.base import Module

_DECLARATIONS: dict[type[Image], frozenset[str]] = {}


def declaration_methods(cls: type[Image]) -> frozenset[str]:
    """Public methods of *cls* annotated to return ``Self``: the fluent declaration API."""
    cached = _DECLARATIONS.get(cls)
    if cached is not None:
        return cached
    names: set[str] = set()
    for klass in reversed(cls.__mro__):
        for attr, member in vars(klass).items():
            if attr.startswith("_") or not inspect.isfunction(member):
                continue
            returns = member.__annotations__.get("return")
            if returns == "Self" or returns is Self:
                names.add(attr)
            else:
                names.discard(attr)
    _DECLARATIONS[cls] = frozenset(names)
    return _DECLARATIONS[cls]


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

    def __getattr__(self, attr: str) -> Callable[..., Profile]:
        if attr.startswith("_") or attr in self.__slots__:
            raise AttributeError(attr)
        if attr not in declaration_methods(type(self.image)):
            hint = (
                f"; Image.{attr} is not profile-scoped, use profile.image.{attr}"
                if hasattr(self.image, attr)
                else ""
            )
            raise AttributeError(f"'Profile' object has no attribute {attr!r}{hint}")

        def bound(*args: Any, **kwargs: Any) -> Profile:
            return self._declare(attr, *args, **kwargs)

        bound.__name__ = attr
        bound.__qualname__ = f"Profile.{attr}"
        bound.__doc__ = getattr(type(self.image), attr).__doc__
        return bound

    def __dir__(self) -> list[str]:
        return sorted({*super().__dir__(), *declaration_methods(type(self.image))})

    def _declare(self, attr: str, /, *args: Any, **kwargs: Any) -> Profile:
        method = getattr(self.image, attr)
        with self.image.profiles(self.name):
            method(*args, **kwargs)
        return self

    # --- Declarations (typed; the rest of Image's fluent API goes through __getattr__) ---

    install = _scoped(Image.install)
    file = _scoped(Image.file)
    directory = _scoped(Image.directory)
    template = _scoped(Image.template)
    group = _scoped(Image.group)
    user = _scoped(Image.user)
    service = _scoped(Image.service)
    enable = _scoped(Image.enable)
    disable = _scoped(Image.disable)
    mask = _scoped(Image.mask)
    pin_mirror = _scoped(Image.pin_mirror)
    apply = _scoped(Image.apply)
    output_targets = _scoped(Image.output_targets)
    debloat = _scoped(Image.debloat)
    run = _scoped(Image.run)
    hook = _scoped(Image.hook)
    repository = _scoped(Image.repository)
    partition = _scoped(Image.partition)
    add_init_script = _scoped(Image.add_init_script)

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
        """Compile with only this profile active."""
        with self.image.profiles(self.name):
            return self.image.compile(path, force=force)

    def lock(self, path: str | Path | None = None) -> Path:
        with self.image.profiles(self.name):
            return self.image.lock(path)

    def diff(self, against: str | Path) -> TreeDiff:
        with self.image.profiles(self.name):
            return self.image.diff(against)

    def bake(
        self,
        output_dir: str | Path | None = None,
        *,
        frozen: bool = False,
        force: bool = False,
    ) -> BakeResult:
        """Bake with only this profile active."""
        with self.image.profiles(self.name):
            return self.image.bake(output_dir, frozen=frozen, force=force)

    def measure(self, *, backend: Literal["rtmr", "azure", "gcp"]) -> Measurements:
        return self.image.measure(backend=backend, profile=self.name)

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


__all__ = ["Profile", "declaration_methods"]
