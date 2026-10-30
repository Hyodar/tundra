"""Loading an image file, and the lifecycle functions that take an :class:`Image`.

The image is the public value; the compiler still works on the internal recipe, so
each function here converts once (:func:`~tundravm.image._lower.recipe_of`) and then
calls the lifecycle unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from tundravm.declarative import lifecycle
from tundravm.declarative.lower import lower as lower_recipe
from tundravm.errors import ValidationError
from tundravm.recipe import import_recipe_module

from ._image import Image
from ._lower import recipe_of

if TYPE_CHECKING:
    from tundravm.declarative._lowered import Lowered
    from tundravm.declarative.lifecycle import Artifact, Backend, Lock, Tree

__all__ = ["IMAGE_OBJECT_NAMES", "bake", "compile", "load", "lower"]

IMAGE_OBJECT_NAMES: tuple[str, ...] = ("image", "IMAGE")
"""The module-level names an image file binds its :class:`Image` to."""


def load(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Image:
    """The :class:`Image` the file at *path* defines.

    The file binds it to ``image`` (or *attribute*, or a zero-argument function
    returning one). It runs with ``__name__`` set to a private module name, so an
    ``if __name__ == "__main__":`` block does not execute, and its directory plus
    *extra_paths* are importable while it runs.
    """
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValidationError(
            f"Image file not found: {source}",
            hint="Pass the path of a Python file that binds an Image.",
            context={"image": str(source)},
        )
    module = import_recipe_module(source, extra_paths=extra_paths)
    names = (attribute,) if attribute is not None else IMAGE_OBJECT_NAMES
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, Image):
            return value
        if value is not None and callable(value) and not isinstance(value, type):
            result = value()
            if isinstance(result, Image):
                return result
    found = [name for name, value in vars(module).items() if isinstance(value, Image)]
    if len(found) == 1:
        value = getattr(module, found[0])
        assert isinstance(value, Image)
        return value
    raise _no_image(source, module_names=vars(module), several=found)


def _no_image(source: Path, *, module_names: dict[str, object], several: list[str]) -> Exception:
    if several:
        return ValidationError(
            f"{source.name} defines several images: {', '.join(sorted(several))}.",
            hint="Name one of them `image`, or pass the attribute explicitly.",
            context={"image": str(source)},
        )
    from tundravm.declarative.model import Recipe

    if any(isinstance(value, Recipe) for value in module_names.values()):
        return ValidationError(
            f"{source.name} defines a Recipe, not an Image.",
            hint="Bind a tundravm.Image to `image`: Image('name', services=[...]). "
            "Recipe is internal and has no public spelling.",
            context={"image": str(source)},
        )
    return ValidationError(
        f"{source.name} does not define an Image.",
        hint="Bind a tundravm.Image to a module-level `image`.",
        context={"image": str(source)},
    )


def lower(image: Image, *, profiles: Sequence[str] | None = None) -> Lowered:
    """*image* as the compiler's value, with *profiles* (default: all) lowered."""
    return lower_recipe(recipe_of(image), variants=profiles)


def compile(  # noqa: A001 - the verb the CLI and the docs use
    image: Image,
    *,
    profiles: Sequence[str] | None = None,
    lock: Lock | None = None,
) -> Tree:
    """The mkosi tree *image* compiles to, with *lock*'s source pins applied."""
    return lifecycle.compile(recipe_of(image), variants=profiles, lock=lock)


def bake(
    image: Image,
    *,
    lock: Lock,
    backend: Backend,
    build: Path,
    profiles: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
    fetch: bool = True,
) -> tuple[Artifact, ...]:
    """Build *image*'s artifacts into *build*, for *profiles* (default: all)."""
    return lifecycle.bake(
        recipe_of(image),
        lock=lock,
        backend=backend,
        build=build,
        variants=profiles,
        progress=progress,
        fetch=fetch,
    )
