"""Review output formats: ``--format`` resolution, GitHub workflow commands, Markdown tables.

``check``, ``diff``, ``compile --check``, ``lock --check`` and ``ci`` default to
``--format auto``: ``github`` when ``GITHUB_ACTIONS=true``, ``text`` otherwise.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

AUTO = "auto"

FORMAT_PRECEDENCE = (
    "Precedence: an explicit --format, {alias}then `auto` (the default), which picks "
    "`github` when the GITHUB_ACTIONS environment variable is `true` and `text` otherwise."
)


def format_help(alias: str | None = None) -> str:
    """The ``--format`` precedence sentence for a command whose shorthand flag is *alias*."""
    shorthand = (
        f"then {alias} (shorthand for --format {alias.lstrip('-')}; not combinable with --format), "
        if alias
        else ""
    )
    return FORMAT_PRECEDENCE.format(alias=shorthand)


def resolve_format(
    requested: str | None,
    *,
    alias: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Pick the output format: an explicit *requested* value, then *alias*, then ``auto``."""
    chosen = requested or alias or AUTO
    if chosen != AUTO:
        return chosen
    env = os.environ if environ is None else environ
    return "github" if env.get("GITHUB_ACTIONS") == "true" else "text"


def workflow_command(command: str, message: str, **properties: str | None) -> str:
    """One GitHub Actions workflow command line, e.g. ``::error file=x.py,title=T::msg``."""
    props = ",".join(
        f"{key}={_escape_property(value)}" for key, value in properties.items() if value
    )
    head = f"::{command} {props}" if props else f"::{command}"
    return f"{head}::{_escape_data(message)}"


def annotation_path(path: str | Path) -> str:
    """*path* as GitHub resolves annotation files: relative to the working directory if under it."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(Path.cwd()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix()


def md_cell(value: object, *, code: bool = False) -> str:
    """Escape *value* for a Markdown table cell; ``code=True`` wraps it in a code span."""
    text = " ".join(str(value).split()) if value is not None else ""
    if not text:
        return "—"
    if code:
        ticks = "`" * (max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)
        pad = " " if "`" in text else ""
        text = f"{ticks}{pad}{text}{pad}{ticks}"
    return text.replace("|", "\\|")


def md_table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    """A GitHub-flavored Markdown table; cells must already be escaped (``md_cell``)."""
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def md_fence(text: str, language: str = "") -> str:
    """Wrap *text* in a code fence longer than any backtick run inside it."""
    longest = max((len(run) for run in re.findall(r"`{3,}", text)), default=2)
    fence = "`" * (longest + 1)
    body = text if text.endswith("\n") or not text else text + "\n"
    return f"{fence}{language}\n{body}{fence}"


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


__all__ = [
    "AUTO",
    "annotation_path",
    "format_help",
    "md_cell",
    "md_fence",
    "md_table",
    "resolve_format",
    "workflow_command",
]
