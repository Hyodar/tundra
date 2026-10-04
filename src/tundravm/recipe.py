"""Load ``Image`` recipes from Python files for the CLI and other tooling."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from .errors import ValidationError
from .image import Image

RECIPE_OBJECT_NAMES: tuple[str, ...] = ("img", "image", "IMAGE", "recipe", "RECIPE")
"""Module-level names checked first when looking for an ``Image`` instance."""

RECIPE_FACTORY_NAMES: tuple[str, ...] = (
    "build",
    "build_image",
    "make_image",
    "create_image",
    "recipe",
)
"""Zero-argument callables checked first when looking for an ``Image`` factory."""


def load_recipe(
    path: str | Path,
    *,
    attr: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Image:
    """Import a recipe file and return the ``Image`` it defines.

    Resolution order when *attr* is not given:

    1. A module-level ``Image`` bound to one of ``RECIPE_OBJECT_NAMES``.
    2. A zero-argument callable named in ``RECIPE_FACTORY_NAMES`` returning an ``Image``.
    3. The only module-level ``Image`` instance, if exactly one exists.
    4. The only zero-argument function defined in the file whose name starts with
       ``build`` or whose return annotation is ``Image``, if exactly one exists.

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
    if attr is not None:
        return _resolve_attr(module, attr, recipe_path)
    return _discover(module, recipe_path)


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


def _resolve_attr(module: ModuleType, attr: str, recipe_path: Path) -> Image:
    if not hasattr(module, attr):
        raise ValidationError(
            f"Recipe has no attribute {attr!r}.",
            hint=f"Available names: {', '.join(_public_names(module)) or '(none)'}",
            context={"recipe": str(recipe_path), "attr": attr},
        )
    value = getattr(module, attr)
    if isinstance(value, Image):
        return value
    if callable(value):
        return _call_factory(value, attr, recipe_path)
    raise ValidationError(
        f"Recipe attribute {attr!r} is a {type(value).__name__}, not an Image or a callable.",
        context={"recipe": str(recipe_path), "attr": attr},
    )


def _discover(module: ModuleType, recipe_path: Path) -> Image:
    for name in RECIPE_OBJECT_NAMES:
        value = getattr(module, name, None)
        if isinstance(value, Image):
            return value

    for name in RECIPE_FACTORY_NAMES:
        value = getattr(module, name, None)
        if callable(value) and not isinstance(value, type):
            return _call_factory(value, name, recipe_path)

    instances = {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, Image) and not name.startswith("_")
    }
    if len(instances) == 1:
        return next(iter(instances.values()))
    if len(instances) > 1:
        raise ValidationError(
            f"Recipe defines several Image instances: {', '.join(sorted(instances))}.",
            hint="Name one of them `img`, or pass --attr NAME.",
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
            f"Recipe defines several Image factories: {', '.join(sorted(factories))}.",
            hint="Name one of them `build`, or pass --attr NAME.",
            context={"recipe": str(recipe_path)},
        )

    raise ValidationError(
        "Recipe does not define an Image.",
        hint=(
            "Bind an Image to a module-level `img`, or define a zero-argument "
            "`build() -> Image` function, or pass --attr NAME."
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
    return annotation is Image or annotation == "Image"


def _call_factory(factory: Callable[..., object], name: str, recipe_path: Path) -> Image:
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
    if isinstance(result, Image):
        return result
    if result is None:
        raise ValidationError(
            f"Recipe factory {name}() returned None.",
            hint=(
                "Return the Image from the factory instead of compiling or baking inside it; "
                "the CLI runs those steps."
            ),
            context={"recipe": str(recipe_path), "attr": name},
        )
    raise ValidationError(
        f"Recipe factory {name}() returned {type(result).__name__}, expected Image.",
        context={"recipe": str(recipe_path), "attr": name},
    )


def _public_names(module: ModuleType) -> list[str]:
    return sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and (isinstance(value, Image) or inspect.isfunction(value))
    )


__all__ = ["RECIPE_FACTORY_NAMES", "RECIPE_OBJECT_NAMES", "load_recipe"]
