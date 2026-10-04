"""Load recipes from Python files for the CLI and other tooling.

A recipe file binds a declarative :class:`~tundravm.declarative.Recipe` (lowered
here onto the compiler's ``Image``) or, for older files, an ``Image``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import cast

from .backends.base import BuildBackend
from .declarative.model import Recipe
from .errors import ValidationError
from .image import Image

RECIPE_OBJECT_NAMES: tuple[str, ...] = ("img", "image", "IMAGE", "recipe", "RECIPE")
"""Module-level names checked first when looking for a ``Recipe`` (or ``Image``)."""

BACKEND_NAME = "backend"
"""Module-level name of the build backend a declarative recipe file bakes with."""

Loaded = Recipe | Image
_LOADED_TYPES = (Recipe, Image)

RECIPE_FACTORY_NAMES: tuple[str, ...] = (
    "build",
    "build_image",
    "make_image",
    "create_image",
    "recipe",
)
"""Zero-argument callables checked first when looking for a ``Recipe`` factory."""


def load_recipe(
    path: str | Path,
    *,
    attr: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Image:
    """Import a recipe file and return the ``Image`` it defines.

    Resolution order when *attr* is not given:

    1. A module-level ``Recipe`` (or ``Image``) bound to one of ``RECIPE_OBJECT_NAMES``.
    2. A zero-argument callable named in ``RECIPE_FACTORY_NAMES`` returning one.
    3. The only module-level ``Recipe``/``Image`` value, if exactly one exists.
    4. The only zero-argument function defined in the file whose name starts with
       ``build`` or whose return annotation is ``Recipe``/``Image``, if exactly one exists.

    A ``Recipe`` is lowered with :func:`tundravm.declarative.lower`; a module-level
    ``backend`` (a build backend instance) becomes the image's backend.

    The file runs with ``__name__`` set to a private module name, so an
    ``if __name__ == "__main__":`` block is not executed. The file's directory and
    every entry of *extra_paths* are importable while the recipe runs, so sibling
    helper modules and project-local packages resolve.
    """
    recipe_path = Path(path).expanduser().resolve()
    if not recipe_path.is_file():
        raise ValidationError(
            f"Recipe file not found: {recipe_path}",
            hint="Pass the path to a Python file that builds an Image.",
            context={"recipe": str(recipe_path)},
        )
    module = _import_recipe_module(recipe_path, extra_paths=extra_paths)
    found = (
        _resolve_attr(module, attr, recipe_path)
        if attr is not None
        else _discover(module, recipe_path)
    )
    if isinstance(found, Image):
        return found
    from .declarative.lower import lower

    img = lower(found)
    backend = getattr(module, BACKEND_NAME, None)
    if backend is not None:
        img.backend = _backend(backend, recipe_path)
    return img


def _backend(value: object, recipe_path: Path) -> BuildBackend:
    if all(callable(getattr(value, method, None)) for method in _BACKEND_METHODS):
        return cast(BuildBackend, value)
    raise ValidationError(
        f"Recipe attribute {BACKEND_NAME!r} is a {type(value).__name__}, not a build backend.",
        hint="Bind a backend instance, e.g. backend = LimaMkosiBackend().",
        context={"recipe": str(recipe_path)},
    )


_BACKEND_METHODS = ("prepare", "execute", "cleanup")


def _import_recipe_module(recipe_path: Path, *, extra_paths: Sequence[str | Path]) -> ModuleType:
    digest = hashlib.sha256(str(recipe_path).encode("utf-8")).hexdigest()[:12]
    module_name = f"_tundravm_recipe_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, recipe_path)
    if spec is None or spec.loader is None:
        raise ValidationError(
            f"Cannot import recipe: {recipe_path}",
            hint="The recipe must be a .py file.",
            context={"recipe": str(recipe_path)},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with _import_paths(str(recipe_path.parent), *(str(Path(p).resolve()) for p in extra_paths)):
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    return module


@contextmanager
def _import_paths(*paths: str) -> Iterator[None]:
    """Temporarily prepend *paths* (first wins) to ``sys.path``."""
    added = [p for p in dict.fromkeys(paths) if p not in sys.path]
    for p in reversed(added):
        sys.path.insert(0, p)
    try:
        yield
    finally:
        for p in added:
            try:
                sys.path.remove(p)
            except ValueError:
                pass


def _resolve_attr(module: ModuleType, attr: str, recipe_path: Path) -> Loaded:
    if not hasattr(module, attr):
        raise ValidationError(
            f"Recipe has no attribute {attr!r}.",
            hint=f"Available names: {', '.join(_public_names(module)) or '(none)'}",
            context={"recipe": str(recipe_path), "attr": attr},
        )
    value = getattr(module, attr)
    if isinstance(value, _LOADED_TYPES):
        return value
    if callable(value):
        return _call_factory(value, attr, recipe_path)
    raise ValidationError(
        f"Recipe attribute {attr!r} is a {type(value).__name__}, not a Recipe or a callable.",
        context={"recipe": str(recipe_path), "attr": attr},
    )


def _discover(module: ModuleType, recipe_path: Path) -> Loaded:
    for name in RECIPE_OBJECT_NAMES:
        value = getattr(module, name, None)
        if isinstance(value, _LOADED_TYPES):
            return value

    for name in RECIPE_FACTORY_NAMES:
        value = getattr(module, name, None)
        if callable(value) and not isinstance(value, type):
            return _call_factory(value, name, recipe_path)

    instances = {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, _LOADED_TYPES) and not name.startswith("_")
    }
    if len(instances) == 1:
        return next(iter(instances.values()))
    if len(instances) > 1:
        raise ValidationError(
            f"Recipe defines several recipes: {', '.join(sorted(instances))}.",
            hint="Name one of them `recipe`, or pass --attr NAME.",
            context={"recipe": str(recipe_path)},
        )

    factories = {
        name: value
        for name, value in vars(module).items()
        if _looks_like_factory(name, value, module)
    }
    if len(factories) == 1:
        name, factory = next(iter(factories.items()))
        return _call_factory(factory, name, recipe_path)
    if len(factories) > 1:
        raise ValidationError(
            f"Recipe defines several recipe factories: {', '.join(sorted(factories))}.",
            hint="Name one of them `build`, or pass --attr NAME.",
            context={"recipe": str(recipe_path)},
        )

    raise ValidationError(
        "Recipe file does not define a Recipe.",
        hint=(
            "Bind a tundravm.Recipe to a module-level `recipe`, or define a zero-argument "
            "`build() -> Recipe` function, or pass --attr NAME."
        ),
        context={"recipe": str(recipe_path)},
    )


def _looks_like_factory(name: str, value: object, module: ModuleType) -> bool:
    if name.startswith("_") or not inspect.isfunction(value):
        return False
    if value.__module__ != module.__name__:
        return False
    if name.startswith("build"):
        return True
    annotation = inspect.signature(value).return_annotation
    return annotation in _LOADED_TYPES or annotation in ("Recipe", "Image")


def _call_factory(factory: Callable[..., object], name: str, recipe_path: Path) -> Loaded:
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        required = [
            p.name
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        if required:
            raise ValidationError(
                f"Recipe factory {name}() requires arguments: {', '.join(required)}.",
                hint="Recipe factories must be callable with no arguments.",
                context={"recipe": str(recipe_path), "attr": name},
            )
    result = factory()
    if isinstance(result, _LOADED_TYPES):
        return result
    if result is None:
        raise ValidationError(
            f"Recipe factory {name}() returned None.",
            hint=(
                "Return the Recipe from the factory instead of compiling or baking inside it; "
                "the CLI runs those steps."
            ),
            context={"recipe": str(recipe_path), "attr": name},
        )
    raise ValidationError(
        f"Recipe factory {name}() returned {type(result).__name__}, expected Recipe.",
        context={"recipe": str(recipe_path), "attr": name},
    )


def _public_names(module: ModuleType) -> list[str]:
    return sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and (isinstance(value, _LOADED_TYPES) or inspect.isfunction(value))
    )


def load_declarative(
    path: str | Path,
    *,
    attr: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Recipe:
    """Import a recipe file and return its ``Recipe``, discovered as in :func:`load_recipe`."""
    recipe_path = Path(path).expanduser().resolve()
    if not recipe_path.is_file():
        raise ValidationError(
            f"Recipe file not found: {recipe_path}",
            context={"recipe": str(recipe_path)},
        )
    module = _import_recipe_module(recipe_path, extra_paths=extra_paths)
    found = (
        _resolve_attr(module, attr, recipe_path)
        if attr is not None
        else _discover(module, recipe_path)
    )
    if not isinstance(found, Recipe):
        raise ValidationError(
            "Recipe file defines an Image, not a declarative Recipe.",
            hint="Bind a tundravm.Recipe to a module-level `recipe`.",
            context={"recipe": str(recipe_path)},
        )
    return found


__all__ = [
    "BACKEND_NAME",
    "RECIPE_FACTORY_NAMES",
    "RECIPE_OBJECT_NAMES",
    "load_declarative",
    "load_recipe",
]
