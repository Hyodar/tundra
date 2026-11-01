"""The build hook that writes ``src/tundravm/_version.py``: the version hatch-vcs reads from the
release tag and the commit the build ran from, so an installed release builds ``tools/`` at
that commit without a checkout. A build from an sdist keeps the commit the sdist recorded."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

VERSION_FILE = "src/tundravm/_version.py"
_RECORDED = re.compile(r'^commit: str \| None = "([0-9a-f]{40})"$', re.MULTILINE)


class VersionFile(BuildHookInterface[BuilderConfig]):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        root = Path(self.root)
        target = root / VERSION_FILE
        commit = _head(root) if (root / ".git").exists() else _recorded(target)
        target.write_text(
            '"""Written by hatch_build.py when tundravm is built; never committed."""\n\n'
            f"version: str = {json.dumps(self.metadata.version)}\n"
            f"commit: str | None = {json.dumps(commit) if commit else 'None'}\n",
            encoding="utf-8",
        )
        build_data["artifacts"].append(f"/{VERSION_FILE}")


def _head(root: Path) -> str | None:
    done = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    head = done.stdout.strip()
    return head if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else None


def _recorded(target: Path) -> str | None:
    found = _RECORDED.search(target.read_text(encoding="utf-8")) if target.is_file() else None
    return found.group(1) if found else None
