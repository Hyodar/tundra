"""Load image files for the CLI and other tooling.

An image file binds a :class:`~tundravm.Image`; :func:`load_file` returns it with the
backend it bakes with and the internal recipe it lowers to, and :func:`load_image` lowers
it for the compiler. Discovery is :func:`tundravm._image._api.image_of`, the one the public
:func:`tundravm.load` uses.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import traceback
from collections.abc import Iterator, Sequence
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

BACKEND_NAME = "backend"
"""Module-level name of the build backend an image file bakes with; optional."""

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


def load_image(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Lowered:
    """The image file at *path*, lowered for the compiler with its ``backend``."""
    return load_file(path, attribute=attribute, extra_paths=extra_paths).lowered()


def _backend(value: object, image_path: Path) -> BuildBackend:
    if all(callable(getattr(value, method, None)) for method in _BACKEND_METHODS):
        return cast(BuildBackend, value)
    raise ValidationError(
        f"Attribute {BACKEND_NAME!r} is a {type(value).__name__}, not a build backend.",
        hint="Bind a backend instance, e.g. backend = LimaMkosiBackend().",
        context={"image": str(image_path)},
    )


_BACKEND_METHODS = ("prepare", "execute", "cleanup")


def import_recipe_module(image_path: Path, *, extra_paths: Sequence[str | Path]) -> ModuleType:
    """Run the image file at *image_path* as a private module and return it.

    The file runs with ``__name__`` set to a private module name, so an
    ``if __name__ == "__main__":`` block is not executed. The file's directory, its
    project's (the nearest directory at or above it holding a ``pyproject.toml``) and
    every entry of *extra_paths* are importable while it runs, so sibling helper modules
    and the project's own packages resolve from any working directory.
    """
    digest = hashlib.sha256(str(image_path).encode("utf-8")).hexdigest()[:12]
    module_name = f"_tundravm_image_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, image_path)
    if spec is None or spec.loader is None:
        raise ValidationError(
            f"Cannot import image file: {image_path}",
            hint="The image file must be a .py file.",
            context={"image": str(image_path)},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    roots = (image_path.parent, project_directory(image_path.parent))
    with _import_paths(*map(str, roots), *(str(Path(p).resolve()) for p in extra_paths)):
        keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True
        try:
            _exec_source(image_path, module)
        except TdxError:
            sys.modules.pop(module_name, None)
            raise
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise recipe_load_error(exc, image_path) from exc
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        finally:
            sys.dont_write_bytecode = keep
    return module


def _exec_source(image_path: Path, module: ModuleType) -> None:
    """Run the image file from its current source, never from (or into) ``__pycache__``.

    A ``.pyc`` is validated by source mtime and size, so an edit within the same
    second that keeps the size would otherwise run the stale bytecode.
    """
    code = compile(image_path.read_bytes(), str(image_path), "exec", dont_inherit=True)
    exec(code, module.__dict__)


_PACKAGE_DIR = Path(__file__).resolve().parent


def recipe_load_error(exc: Exception, image_path: Path) -> ValidationError:
    """*exc*, raised while running the image file, as an ``E_VALIDATION`` error.

    The context names the failing ``file:line`` (for a syntax error the offending
    line; else the innermost frame outside tundravm and the import machinery) and
    the original ``Type: message``.
    """
    location = _failure_location(exc, image_path)
    kind = "syntax error" if isinstance(exc, SyntaxError) else type(exc).__name__
    detail = exc.msg if isinstance(exc, SyntaxError) else str(exc)
    return ValidationError(
        f"{image_path.name} failed to load: {kind} at {location}"
        + (f": {detail}" if detail else ""),
        hint=(
            f"Run `python {display_path(image_path)}` to see the full traceback, "
            "or pass --traceback."
        ),
        context={
            "image": str(image_path),
            "location": location,
            "error": f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__,
        },
    )


def _failure_location(exc: Exception, image_path: Path) -> str:
    if isinstance(exc, SyntaxError):
        filename = exc.filename or str(image_path)
        return f"{display_path(Path(filename))}:{exc.lineno or 0}"
    found: tuple[str, int] | None = None
    for frame in traceback.extract_tb(exc.__traceback__):
        filename = frame.filename
        if filename.startswith("<") or Path(filename).resolve().is_relative_to(_PACKAGE_DIR):
            continue
        found = (filename, frame.lineno or 0)
    if found is None:
        return display_path(image_path)
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


@dataclass(slots=True)
class RecipeFile:
    """A loaded image file: the ``Image`` it binds (*declared*), the internal recipe that
    image lowers to, its ``backend`` and the lowered image once built.

    *backend* is the file's own ``backend`` when it binds one, else :func:`default_backend`;
    *backend_origin* says which.
    """

    path: Path
    declared: Image
    recipe: Recipe
    image: Lowered | None
    backend: BuildBackend | None
    backend_origin: BackendOrigin = "recipe"

    @property
    def variants(self) -> tuple[str, ...]:
        """Declared profile names."""
        return tuple(v.name for v in self.recipe.variants)

    def lowered(self) -> Lowered:
        """The image with every profile lowered, building with the file's backend."""
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
    """Import an image file without lowering it: the ``Image`` it binds to *attribute*,
    else to ``image`` (:func:`tundravm._image._api.image_of`), with its ``backend``."""
    from tundravm._image import recipe_of
    from tundravm._image._api import image_of

    image_path = Path(path).expanduser().resolve()
    if not image_path.is_file():
        raise ValidationError(
            f"Image file not found: {image_path}",
            hint="Pass the path of a Python file that binds an Image.",
            context={"image": str(image_path)},
        )
    module = import_recipe_module(image_path, extra_paths=extra_paths)
    declared = image_of(module, image_path, attribute=attribute)
    found = recipe_of(declared)
    raw = getattr(module, BACKEND_NAME, None)
    if raw is not None:
        return RecipeFile(image_path, declared, found, None, _backend(raw, image_path))
    backend, origin = default_backend()
    return RecipeFile(image_path, declared, found, None, backend, origin)


__all__ = [
    "BACKEND_ENV",
    "BACKEND_KINDS",
    "BACKEND_NAME",
    "BackendOrigin",
    "RecipeFile",
    "default_backend",
    "import_recipe_module",
    "load_file",
    "load_image",
]
