"""GitHub Actions output: whether a command runs in a workflow, and its workflow commands.

``check`` and ``lock --check`` print their findings as workflow commands (annotations on
the pull request) when ``GITHUB_ACTIONS=true``, and as text otherwise.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def in_github_actions(environ: Mapping[str, str] | None = None) -> bool:
    """True when the GITHUB_ACTIONS environment variable is ``true``."""
    env = os.environ if environ is None else environ
    return env.get("GITHUB_ACTIONS") == "true"


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


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


__all__ = ["annotation_path", "in_github_actions", "workflow_command"]
