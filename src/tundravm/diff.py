"""Compiled-tree diffing: ``tundravm diff`` and ``compile --check``."""

from __future__ import annotations

import argparse
import difflib
import glob
import os
import posixpath
import secrets
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from .backends.base import is_mkosi_state
from .formats import annotation_path, md_cell, md_fence, md_table, resolve_format, workflow_command

if TYPE_CHECKING:
    from .declarative._lowered import Lowered

ChangeStatus = Literal["added", "removed", "modified", "mode"]

_BINARY_PROBE = 8192
_STAT_CODES: dict[ChangeStatus, str] = {"added": "A", "removed": "D", "modified": "M", "mode": "T"}
_RED, _GREEN, _CYAN, _BOLD, _RESET = "\x1b[31m", "\x1b[32m", "\x1b[36m", "\x1b[1m", "\x1b[0m"
MARKDOWN_DIFF_LINES = 400
"""Unified-diff lines ``TreeDiff.markdown()`` keeps before truncating."""
_GITHUB_CHANGES: dict[ChangeStatus, tuple[str, str]] = {
    "added": ("notice", "added: the recipe now emits this file"),
    "removed": ("warning", "removed: the recipe no longer emits this file"),
    "modified": ("warning", "modified: differs from what the recipe compiles to"),
    "mode": ("warning", "mode changed: the executable bit differs from the recipe"),
}


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file that differs between two trees; text is ``None`` when absent or binary."""

    path: str
    status: ChangeStatus
    old_mode: int | None
    new_mode: int | None
    old_text: str | None
    new_text: str | None
    is_binary: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "status": self.status,
            "old_mode": None if self.old_mode is None else f"{self.old_mode:04o}",
            "new_mode": None if self.new_mode is None else f"{self.new_mode:04o}",
            "old_text": self.old_text,
            "new_text": self.new_text,
            "is_binary": self.is_binary,
        }


@dataclass(frozen=True, slots=True)
class TreeDiff:
    """Changes needed to turn the old tree into the new one, sorted by path."""

    changes: tuple[FileChange, ...]

    @property
    def is_clean(self) -> bool:
        return not self.changes

    def stat(self) -> str:
        lines = [f"{_STAT_CODES[change.status]}  {change.path}" for change in self.changes]
        count = len(self.changes)
        lines.append(f"{count} file{'' if count == 1 else 's'} changed")
        return "\n".join(lines) + "\n"

    def unified(self, *, color: bool = False, context: int = 3) -> str:
        painted = (
            _paint(kind, line, color)
            for change in self.changes
            for kind, line in _change_lines(change, context)
        )
        return "".join(painted)

    def markdown(self, *, max_lines: int = MARKDOWN_DIFF_LINES) -> str:
        """Changed files as a Markdown table, then the unified diff in a ``diff`` fence.

        The diff is cut after *max_lines* lines with a note saying how many were kept.
        """
        if self.is_clean:
            return "**Tree is up to date with the recipe.**\n"
        rows = [
            (md_cell(change.status), md_cell(change.path, code=True)) for change in self.changes
        ]
        count = len(self.changes)
        parts = [
            md_table(("Status", "File"), rows),
            f"**{count} file{'' if count == 1 else 's'} changed**",
        ]
        lines = self.unified().splitlines(keepends=True)
        if lines:
            parts.append(md_fence("".join(lines[:max_lines]), "diff"))
        if len(lines) > max_lines:
            parts.append(
                f"_Diff truncated: showing {max_lines} of {len(lines)} lines. "
                "Run `tundravm diff` locally for the rest._"
            )
        return "\n\n".join(parts) + "\n"

    def github(self, *, root: str | None = None, title: str = "compiled tree drift") -> str:
        """GitHub workflow commands: ``::warning`` per changed file (``::notice`` if added).

        *root* prefixes each annotation's ``file=`` so it points into the repository.
        The unified diff follows in a collapsed ``::group::`` with workflow commands
        disabled, so file contents cannot inject annotations.
        """
        lines: list[str] = []
        for change in self.changes:
            command, message = _GITHUB_CHANGES[change.status]
            path = posixpath.join(root, change.path) if root and root != "." else change.path
            lines.append(workflow_command(command, f"{path} {message}", file=path, title=title))
        count = len(self.changes)
        lines.append(f"{count} file{'' if count == 1 else 's'} changed")
        token = f"tundravm-{secrets.token_hex(8)}"
        lines += ["::group::unified diff", f"::stop-commands::{token}"]
        text = "\n".join(lines) + "\n" + self.unified()
        return text + f"::{token}::\n::endgroup::\n"

    def render(self, fmt: str, *, root: str | None = None, color: bool = False) -> str:
        """Render in a resolved ``--format``: text (unified), stat, markdown or github."""
        if fmt == "stat":
            return self.stat()
        if fmt == "markdown":
            return self.markdown()
        if fmt == "github":
            return self.github(root=root)
        return self.unified(color=color)

    def to_dict(self) -> dict[str, object]:
        return {"clean": self.is_clean, "changes": [change.to_dict() for change in self.changes]}


def diff_trees(old: Path, new: Path, *, ignore: Sequence[str] = ()) -> TreeDiff:
    """Compare two directory trees by file bytes and executable bit.

    A missing *old* or *new* root counts as an empty tree. *ignore* holds
    ``fnmatch`` globs matched against POSIX paths relative to the roots.
    """
    old_files = _collect(Path(old), ignore)
    new_files = _collect(Path(new), ignore)
    changes: list[FileChange] = []
    for rel in sorted(old_files.keys() | new_files.keys()):
        change = _compare(rel, old_files.get(rel), new_files.get(rel))
        if change is not None:
            changes.append(change)
    return TreeDiff(changes=tuple(changes))


def diff_against(
    image: Lowered, against: str | Path, *, profiles: Sequence[str] | None = None
) -> TreeDiff:
    """Diff the tree at *against* to what *image* compiles to for *profiles*.

    ``None`` compiles the image's active profiles.

    Profile directories on disk that were not compiled are left out, so a tree
    holding more profiles than are active is not reported as removed.
    """
    from .declarative._compile import emit

    root = Path(against)
    scoped = image.select(profiles)
    with tempfile.TemporaryDirectory(prefix="tundravm-diff-") as tmp:
        emit(scoped, Path(tmp))
        return diff_trees(root, Path(tmp), ignore=_foreign_profile_globs(root, scoped.active))


def cmd_diff(
    args: argparse.Namespace, out: TextIO, img: Lowered, *, profiles: Sequence[str] | None = None
) -> int:
    against = args.against if args.against is not None else Path(img.build_dir) / "mkosi"
    result = diff_against(img, against, profiles=profiles)
    if result.is_clean:
        print("tree is up to date with the recipe", file=out)
        return 0
    fmt = resolve_format(args.format, alias="stat" if args.stat else None)
    root = annotation_path(against)
    out.write(result.render(fmt, root=root, color=_wants_color(args.color, out)))
    return 1


@dataclass(frozen=True, slots=True)
class _Entry:
    data: bytes
    mode: int


def _collect(root: Path, ignore: Sequence[str]) -> dict[str, _Entry]:
    files: dict[str, _Entry] = {}
    if not root.is_dir():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if not is_mkosi_state((base / d).as_posix())]
        for name in filenames:
            path = Path(dirpath, name)
            rel = path.relative_to(root).as_posix()
            if is_mkosi_state(rel) or any(fnmatchcase(rel, pattern) for pattern in ignore):
                continue
            info = path.lstat()
            if path.is_symlink():
                data = f"-> {os.readlink(path)}\n".encode()
            else:
                data = path.read_bytes()
            files[rel] = _Entry(data=data, mode=0o755 if info.st_mode & 0o111 else 0o644)
    return files


def _compare(rel: str, old: _Entry | None, new: _Entry | None) -> FileChange | None:
    status: ChangeStatus
    if old is None:
        status = "added"
    elif new is None:
        status = "removed"
    elif old.data != new.data:
        status = "modified"
    elif old.mode != new.mode:
        status = "mode"
    else:
        return None
    is_binary = any(_is_binary(entry.data) for entry in (old, new) if entry is not None)
    return FileChange(
        path=rel,
        status=status,
        old_mode=None if old is None else old.mode,
        new_mode=None if new is None else new.mode,
        old_text=_text(old, is_binary),
        new_text=_text(new, is_binary),
        is_binary=is_binary,
    )


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_PROBE]


def _text(entry: _Entry | None, is_binary: bool) -> str | None:
    if entry is None or is_binary:
        return None
    return entry.data.decode("utf-8", errors="replace")


def _change_lines(change: FileChange, context: int) -> Iterator[tuple[str, str]]:
    """Yield ``(kind, line)`` pairs in git's diff layout; kind drives coloring."""
    a_path = f"a/{change.path}"
    b_path = f"b/{change.path}"
    yield "meta", f"diff --git {a_path} {b_path}\n"
    if change.status == "added":
        yield "meta", f"new file mode 100{change.new_mode:o}\n"
    elif change.status == "removed":
        yield "meta", f"deleted file mode 100{change.old_mode:o}\n"
    elif change.old_mode != change.new_mode:
        yield "meta", f"old mode 100{change.old_mode:o}\n"
        yield "meta", f"new mode 100{change.new_mode:o}\n"
    if change.status == "mode":
        return
    from_file = "/dev/null" if change.status == "added" else a_path
    to_file = "/dev/null" if change.status == "removed" else b_path
    if change.is_binary:
        yield "meta", f"Binary files {from_file} and {to_file} differ\n"
        return
    old_lines = (change.old_text or "").splitlines(keepends=True)
    new_lines = (change.new_text or "").splitlines(keepends=True)
    hunks = difflib.unified_diff(old_lines, new_lines, from_file, to_file, n=context)
    for index, line in enumerate(hunks):
        if index < 2:
            yield "meta", line
            continue
        kind = "hunk" if line.startswith("@@") else line[0]
        if line.endswith("\n"):
            yield kind, line
        else:
            yield kind, line + "\n"
            yield " ", "\\ No newline at end of file\n"


def _paint(kind: str, line: str, color: bool) -> str:
    code = {"meta": _BOLD, "hunk": _CYAN, "+": _GREEN, "-": _RED}.get(kind)
    if not color or code is None:
        return line
    return f"{code}{line.rstrip(chr(10))}{_RESET}\n"


def _foreign_profile_globs(root: Path, compiled: Sequence[str]) -> list[str]:
    """Ignore globs for profile directories under *root* that were not compiled."""
    keep = set(compiled)
    candidates: list[tuple[str, Path]] = []
    if root.is_dir():
        candidates += [
            ("", child)
            for child in root.iterdir()
            if child.is_dir() and (child / "mkosi.conf").is_file()
        ]
    if (root / "mkosi.profiles").is_dir():
        candidates += [
            ("mkosi.profiles/", child)
            for child in (root / "mkosi.profiles").iterdir()
            if child.is_dir()
        ]
    return sorted(
        f"{prefix}{glob.escape(child.name)}/*"
        for prefix, child in candidates
        if child.name not in keep
    )


def _wants_color(mode: str, out: TextIO) -> bool:
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return out.isatty()


__all__ = ["ChangeStatus", "FileChange", "TreeDiff", "cmd_diff", "diff_against", "diff_trees"]
