"""Host files that ship next to the module declaring them: :class:`Resources`.

``Resources.from_module(__name__)`` anchors relative paths at the declaring
module's directory (a package's own directory), not at the working directory, so
a recipe or fragment finds its files from a checkout, from another directory and
from an installed package alike::

    assets = Resources.from_module(__name__)
    Kernel("6.13.12", source, config=assets.file("kernel/kernel.config"))
    File.copy("/etc/app/app.conf", assets.file("app.conf"))

The paths it returns are absolute, so ``tundravm watch`` notices edits to the
files declarations name (:func:`~tundravm.declarative.inputs.recipe_inputs`) and
to the files :meth:`Resources.text` read (:func:`resource_reads`).
"""

from __future__ import annotations

import importlib
import importlib.resources
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from tundravm.errors import ValidationError

_READ: set[Path] = set()


@dataclass(frozen=True, slots=True)
class Resources:
    """The files under *root*, the directory of the module named *module*."""

    module: str
    root: Path

    @classmethod
    def from_module(cls, module: str) -> Resources:
        """The resources of the module named *module* (pass ``__name__``).

        The module must be imported (it is, while it runs) or importable. Its
        directory comes from ``importlib.resources``, or from its ``__file__`` for
        a script run as ``__main__``; it must be a directory on disk, not a zip.
        """
        if not isinstance(module, str) or not module:
            raise ValidationError(
                f"Resources.from_module() takes a module name, got {module!r}.",
                hint="Pass the declaring module's name: Resources.from_module(__name__).",
            )
        return cls(module, _directory(module, _module(module)))

    def file(self, relative: str) -> Path:
        """The absolute path of the file *relative* (``/``-separated) under :attr:`root`."""
        path = self._path(relative)
        if not path.is_file():
            raise self._missing(relative, "file", path)
        return path

    def directory(self, relative: str) -> Path:
        """The absolute path of the directory *relative* (a ``FileTree`` source)."""
        path = self._path(relative)
        if not path.is_dir():
            raise self._missing(relative, "directory", path)
        return path

    def text(self, relative: str, *, encoding: str = "utf-8") -> str:
        """The contents of the file *relative*, decoded with *encoding*."""
        path = self.file(relative)
        try:
            text = path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"Resource {relative!r} of module {self.module!r} is not {encoding} text.",
                hint="Ship it with File.copy(path, assets.file(...)) instead.",
                context={"module": self.module, "path": str(path)},
            ) from exc
        _READ.add(path)
        return text

    def _path(self, relative: str) -> Path:
        normal = posixpath.normpath(relative) if isinstance(relative, str) else ""
        if not normal or normal.startswith(("/", "../")) or normal in {".", ".."}:
            raise ValidationError(
                f"Resource path {relative!r} of module {self.module!r} must name a "
                "path inside the module's directory.",
                hint="Pass a relative path such as 'kernel/kernel.config'.",
                context={"module": self.module, "root": str(self.root)},
            )
        return self.root / normal

    def _missing(self, relative: str, kind: str, path: Path) -> ValidationError:
        return ValidationError(
            f"Module {self.module!r} has no resource {kind} {relative!r} ({path}).",
            hint=f"Create it under {self.root}, or ship it with the package.",
            context={"module": self.module, "path": relative, "root": str(self.root)},
        )


def resource_reads() -> frozenset[Path]:
    """Every file a :meth:`Resources.text` call read in this process."""
    return frozenset(_READ)


def _module(name: str) -> ModuleType:
    found = sys.modules.get(name)
    if found is not None:
        return found
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise ValidationError(
            f"Resources.from_module({name!r}): no such module ({exc}).",
            hint="Pass the declaring module's name: Resources.from_module(__name__).",
        ) from exc


def _directory(name: str, module: ModuleType) -> Path:
    root: object = None
    if getattr(module, "__spec__", None) is not None:
        try:
            root = importlib.resources.files(module)
        except (TypeError, ValueError, AttributeError):
            root = None
    if not isinstance(root, Path) and isinstance(getattr(module, "__file__", None), str):
        root = Path(module.__file__).parent  # type: ignore[arg-type]
    if not isinstance(root, Path):
        raise ValidationError(
            f"Module {name!r} has no directory on disk to read resources from.",
            hint="Install the package unpacked (not as a zip), or import it from a checkout.",
        )
    return root.resolve()


__all__ = ["Resources", "resource_reads"]
