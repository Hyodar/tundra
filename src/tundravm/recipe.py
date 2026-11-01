"""Load recipes from Python files for the CLI and other tooling.

A recipe file binds a declarative :class:`~tundravm.declarative.Recipe`;
:func:`load_image` also lowers it for the compiler.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import os
import sys
import traceback
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Literal, cast

from ._reporting import display_path
from .backends.base import BuildBackend
from .declarative.model import Recipe, project_directory
from .errors import TdxError, ValidationError

if TYPE_CHECKING:
    from ._image import Image
    from .declarative._lowered import Lowered

IMAGE_NAMES: tuple[str, ...] = ("image", "IMAGE")
"""Module-level names checked first: the public :class:`~tundravm.Image` a file binds."""

RECIPE_OBJECT_NAMES: tuple[str, ...] = ("recipe", "RECIPE")
"""Module-level names checked first when looking for a ``Recipe``."""

BACKEND_NAME = "backend"
"""Module-level name of the build backend a recipe file bakes with; optional."""

BACKEND_ENV = "TUNDRAVM_BACKEND"
"""Names the backend kind (``local`` or ``lima``) for a file that binds none."""

BACKEND_KINDS: tuple[str, ...] = ("local", "lima")

BackendOrigin = Literal["recipe", "env", "host"]
"""Where a loaded file's backend came from: its own ``backend``, :data:`BACKEND_ENV`, or
this host (Linux x86-64 builds locally, anything else in Lima)."""


def default_backend() -> tuple[BuildBackend, BackendOrigin]:
    """The backend a file that binds none bakes with, and where the choice came from.

    :data:`BACKEND_ENV` wins; otherwise Linux x86-64 builds with the local backend and
    every other host with Lima. ``--backend`` and the project file's ``backend`` override
    both on the command line; a build never switches backend on failure.
    """
    from .capabilities import Host
    from .capabilities import default_backend as host_default
    from .declarative.lifecycle import Backend

    kind = os.environ.get(BACKEND_ENV, "").strip()
    if kind:
        if kind not in BACKEND_KINDS:
            raise ValidationError(
                f"{BACKEND_ENV}={kind!r} is not a build backend.",
                hint=f"Use one of: {', '.join(BACKEND_KINDS)}.",
            )
        return Backend(kind).build_backend(), "env"  # type: ignore[arg-type]
    return Backend(host_default(Host())).build_backend(), "host"


RECIPE_FACTORY_NAMES: tuple[str, ...] = ("build", "recipe")
"""Zero-argument callables checked first when looking for a ``Recipe`` factory."""


def load_recipe(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Recipe:
    """Import a recipe file and return the ``Recipe`` it defines (also ``tundravm.load``).

    *attribute* names the module-level ``Recipe``, or a zero-argument factory
    returning one. Without it, discovery is the CLI's:

    1. A module-level ``Recipe`` bound to one of ``RECIPE_OBJECT_NAMES``.
    2. A zero-argument callable named in ``RECIPE_FACTORY_NAMES`` returning one.
    3. The only module-level ``Recipe`` value, if exactly one exists.
    4. The only zero-argument function defined in the file whose name starts with
       ``build`` or whose return annotation is ``Recipe``, if exactly one exists.

    The file runs with ``__name__`` set to a private module name, so an
    ``if __name__ == "__main__":`` block is not executed. The file's directory, its
    project's (the nearest directory at or above it holding a ``pyproject.toml``) and
    every entry of *extra_paths* are importable while it runs, so sibling helper modules
    and the project's own packages resolve from any working directory.
    """
    return load_file(path, attribute=attribute, extra_paths=extra_paths).recipe


load = load_recipe
"""``tundravm.load``: the same function as :func:`load_recipe`."""


def load_image(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Lowered:
    """The recipe file at *path*, lowered for the compiler with its ``backend``."""
    return load_file(path, attribute=attribute, extra_paths=extra_paths).lowered()


def _backend(value: object, recipe_path: Path) -> BuildBackend:
    if all(callable(getattr(value, method, None)) for method in _BACKEND_METHODS):
        return cast(BuildBackend, value)
    raise ValidationError(
        f"Attribute {BACKEND_NAME!r} is a {type(value).__name__}, not a build backend.",
        hint="Bind a backend instance, e.g. backend = LimaMkosiBackend().",
        context={"image": str(recipe_path)},
    )


_BACKEND_METHODS = ("prepare", "execute", "cleanup")


def import_recipe_module(recipe_path: Path, *, extra_paths: Sequence[str | Path]) -> ModuleType:
    digest = hashlib.sha256(str(recipe_path).encode("utf-8")).hexdigest()[:12]
    module_name = f"_tundravm_recipe_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, recipe_path)
    if spec is None or spec.loader is None:
        raise ValidationError(
            f"Cannot import image file: {recipe_path}",
            hint="The image file must be a .py file.",
            context={"image": str(recipe_path)},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    roots = (recipe_path.parent, project_directory(recipe_path.parent))
    with _import_paths(*map(str, roots), *(str(Path(p).resolve()) for p in extra_paths)):
        keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            _exec_source(recipe_path, module)
        except TdxError:
            sys.modules.pop(module_name, None)
            raise
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise recipe_load_error(exc, recipe_path) from exc
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        finally:
            sys.dont_write_bytecode = keep
    return module


def _exec_source(recipe_path: Path, module: ModuleType) -> None:
    """Run the recipe from its current source, never from (or into) ``__pycache__``.

    A ``.pyc`` is validated by source mtime and size, so an edit within the same
    second that keeps the size would otherwise run the stale bytecode.
    """
    code = compile(recipe_path.read_bytes(), str(recipe_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)


_PACKAGE_DIR = Path(__file__).resolve().parent


def recipe_load_error(exc: Exception, recipe_path: Path) -> ValidationError:
    """*exc*, raised while running the recipe file, as an ``E_VALIDATION`` error.

    The context names the failing ``file:line`` (for a syntax error the offending
    line; else the innermost frame outside tundravm and the import machinery) and
    the original ``Type: message``.
    """
    location = _failure_location(exc, recipe_path)
    kind = "syntax error" if isinstance(exc, SyntaxError) else type(exc).__name__
    detail = exc.msg if isinstance(exc, SyntaxError) else str(exc)
    return ValidationError(
        f"{recipe_path.name} failed to load: {kind} at {location}"
        + (f": {detail}" if detail else ""),
        hint=(
            f"Run `python {display_path(recipe_path)}` to see the full traceback, "
            "or pass --traceback."
        ),
        context={
            "image": str(recipe_path),
            "location": location,
            "error": f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
        },
    )


def _failure_location(exc: Exception, recipe_path: Path) -> str:
    if isinstance(exc, SyntaxError):
        filename = exc.filename or str(recipe_path)
        return f"{display_path(Path(filename))}:{exc.lineno or 0}"
    found: tuple[str, int] | None = None
    for frame in traceback.extract_tb(exc.__traceback__):
        filename = frame.filename
        if filename.startswith("<") or Path(filename).resolve().is_relative_to(_PACKAGE_DIR):
            continue
        found = (filename, frame.lineno or 0)
    if found is None:
        return display_path(recipe_path)
    return f"{display_path(Path(found[0]))}:{found[1]}"


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


def _resolve_attr(module: ModuleType, attr: str, recipe_path: Path) -> Recipe:
    if not hasattr(module, attr):
        raise ValidationError(
            f"{recipe_path.name} has no attribute {attr!r}.",
            hint=f"Available names: {', '.join(_public_names(module)) or '(none)'}",
            context={"image": str(recipe_path), "attr": attr},
        )
    value = getattr(module, attr)
    found = _as_recipe(value)
    if found is not None:
        return found
    if callable(value):
        return _call_factory(value, attr, recipe_path)
    raise ValidationError(
        f"Attribute {attr!r} is a {type(value).__name__}, not an Image or a zero-argument "
        "function returning one.",
        hint="Pass --attr NAME where NAME binds an Image, or a function that returns one.",
        context={"image": str(recipe_path), "attr": attr},
    )


def _as_recipe(value: object) -> Recipe | None:
    """*value* as the internal recipe when it is an ``Image`` or already a recipe."""
    from tundravm._image import Image, recipe_of

    if isinstance(value, Recipe):
        return value
    if isinstance(value, Image):
        return recipe_of(value)
    return None


def _discover_image(module: ModuleType, recipe_path: Path) -> Recipe | None:
    """The ``Image`` the file binds, as the internal recipe; ``None`` when it binds none."""
    from tundravm._image import recipe_of

    image = _find_image(module, recipe_path)
    return None if image is None else recipe_of(image)


def _find_image(module: ModuleType, recipe_path: Path) -> Image | None:
    """The ``Image`` the file binds; ``None`` when it binds none."""
    from tundravm._image import Image

    for name in IMAGE_NAMES:
        value = getattr(module, name, None)
        if isinstance(value, Image):
            return value
    images = {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, Image) and not name.startswith("_")
    }
    if len(images) > 1:
        raise ValidationError(
            f"File defines several images: {', '.join(sorted(images))}.",
            hint="Name one of them `image`, or pass --attr NAME.",
            context={"image": str(recipe_path)},
        )
    if images:
        return next(iter(images.values()))
    return None


def _discover(module: ModuleType, recipe_path: Path) -> Recipe:
    found = _discover_image(module, recipe_path)
    if found is not None:
        return found

    for name in RECIPE_OBJECT_NAMES:
        value = getattr(module, name, None)
        if isinstance(value, Recipe):
            return value

    for name in RECIPE_FACTORY_NAMES:
        value = getattr(module, name, None)
        if callable(value) and not isinstance(value, type):
            return _call_factory(value, name, recipe_path)

    instances = {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, Recipe) and not name.startswith("_")
    }
    if len(instances) == 1:
        return next(iter(instances.values()))
    if len(instances) > 1:
        raise ValidationError(
            f"File defines several images: {', '.join(sorted(instances))}.",
            hint="Bind one tundravm.Image to `image`, or pass --attr NAME.",
            context={"image": str(recipe_path)},
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
            f"File defines several image factories: {', '.join(sorted(factories))}.",
            hint="Bind the Image to `image`, name one factory `build`, or pass --attr NAME.",
            context={"image": str(recipe_path)},
        )

    raise ValidationError(
        "File does not define an Image.",
        hint=("Bind a tundravm.Image to a module-level `image`, or pass --attr NAME."),
        context={"image": str(recipe_path)},
    )


def _looks_like_factory(name: str, value: object, module: ModuleType) -> bool:
    if name.startswith("_") or not inspect.isfunction(value):
        return False
    if value.__module__ != module.__name__:
        return False
    if name.startswith("build"):
        return True
    annotation = inspect.signature(value).return_annotation
    return annotation is Recipe or annotation == "Recipe"


def _call_factory(factory: Callable[..., object], name: str, recipe_path: Path) -> Recipe:
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
                f"Factory {name}() requires arguments: {', '.join(required)}.",
                hint="An image factory must be callable with no arguments.",
                context={"image": str(recipe_path), "attr": name},
            )
    try:
        result = factory()
    except TdxError:
        raise
    except Exception as exc:
        raise recipe_load_error(exc, recipe_path) from exc
    found = _as_recipe(result)
    if found is not None:
        return found
    if result is None:
        raise ValidationError(
            f"Factory {name}() returned None.",
            hint=(
                "Return the Image from the factory instead of compiling or building inside it; "
                "the CLI runs those steps."
            ),
            context={"image": str(recipe_path), "attr": name},
        )
    raise ValidationError(
        f"Factory {name}() returned {type(result).__name__}, not an Image.",
        hint="Return the Image from the factory.",
        context={"image": str(recipe_path), "attr": name},
    )


def _public_names(module: ModuleType) -> list[str]:
    from tundravm._image import Image

    return sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and (isinstance(value, Recipe | Image) or inspect.isfunction(value))
    )


@dataclass(slots=True)
class RecipeFile:
    """A loaded recipe file: its ``recipe``, its ``backend`` and the lowered image once built.

    *backend* is the file's own ``backend`` when it binds one, else :func:`default_backend`;
    *backend_origin* says which. *declared* is the ``Image`` the file binds, which *recipe*
    lowers; ``None`` for a file that binds an internal recipe.
    """

    path: Path
    recipe: Recipe
    image: Lowered | None
    backend: BuildBackend | None
    backend_origin: BackendOrigin = "recipe"
    declared: Image | None = None

    @property
    def variants(self) -> tuple[str, ...]:
        """Declared variant names."""
        return tuple(v.name for v in self.recipe.variants)

    def lowered(self) -> Lowered:
        """The recipe with every variant lowered, building with the file's backend."""
        if self.image is None:
            from .declarative.lower import lower

            self.image = replace(lower(self.recipe), backend=self.backend)
        return self.image


def load_file(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> RecipeFile:
    """Import a recipe file without lowering it; discovery as in :func:`load_recipe`."""
    recipe_path = Path(path).expanduser().resolve()
    if not recipe_path.is_file():
        raise ValidationError(
            f"Image file not found: {recipe_path}",
            hint="Pass the path of a Python file that binds an Image.",
            context={"image": str(recipe_path)},
        )
    module = import_recipe_module(recipe_path, extra_paths=extra_paths)
    from tundravm._image import Image, recipe_of

    named = None if attribute is None else getattr(module, attribute, None)
    declared = _find_image(module, recipe_path) if attribute is None else named
    if not isinstance(declared, Image):
        declared = None
    if declared is not None:
        found = recipe_of(declared)
    elif attribute is not None:
        found = _resolve_attr(module, attribute, recipe_path)
    else:
        found = _discover(module, recipe_path)
    raw = getattr(module, BACKEND_NAME, None)
    if raw is not None:
        return RecipeFile(recipe_path, found, None, _backend(raw, recipe_path), declared=declared)
    backend, origin = default_backend()
    return RecipeFile(recipe_path, found, None, backend, origin, declared)


__all__ = [
    "BACKEND_ENV",
    "BACKEND_KINDS",
    "BACKEND_NAME",
    "BackendOrigin",
    "IMAGE_NAMES",
    "RECIPE_FACTORY_NAMES",
    "RECIPE_OBJECT_NAMES",
    "RecipeFile",
    "default_backend",
    "load",
    "import_recipe_module",
    "load_file",
    "load_image",
    "load_recipe",
]
