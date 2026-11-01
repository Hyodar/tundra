"""Cut a release section of CHANGELOG.md, or print one for the release workflow.

    uv run python scripts/release.py cut 2.0.0     # ## Unreleased becomes ## 2.0.0 - <today>
    uv run python scripts/release.py notes 2.0.0   # print that section; exit 1 without one

``cut`` leaves an empty ``## Unreleased`` above the new section. ``notes`` is what
``.github/workflows/release.yml`` runs on a ``v2.0.0`` tag: a tag whose CHANGELOG.md has no
``## 2.0.0`` section is refused.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?")
_SECTION = re.compile(r"^## (\S+)(?: - \d{4}-\d{2}-\d{2})?$", re.MULTILINE)


class ReleaseError(Exception):
    """A changelog the release cannot use; the message says what to do."""


def sections(text: str) -> dict[str, tuple[int, int]]:
    """``{name: (start, end)}`` of every ``## name`` section's body."""
    heads = list(_SECTION.finditer(text))
    ends = [head.start() for head in heads[1:]] + [len(text)]
    return {head.group(1): (head.end(), end) for head, end in zip(heads, ends, strict=True)}


def _checked(version: str) -> str:
    if not _VERSION.fullmatch(version):
        raise ReleaseError(f"{version!r} is not a release version: name one like 2.0.0.")
    return version


def notes(text: str, version: str) -> str:
    """The body of *text*'s ``## version`` section, stripped."""
    span = sections(text).get(_checked(version))
    body = "" if span is None else text[span[0] : span[1]].strip()
    if not body:
        raise ReleaseError(
            f"CHANGELOG.md has no notes under ## {version}: run "
            f"`uv run python scripts/release.py cut {version}`, commit it and tag that commit."
        )
    return body


def cut(text: str, version: str, today: datetime.date) -> str:
    """*text* with its Unreleased notes under ``## version - today`` and Unreleased empty."""
    found = sections(text)
    if _checked(version) in found:
        raise ReleaseError(f"CHANGELOG.md already has ## {version}: pick the next version.")
    span = found.get("Unreleased")
    if span is None or not text[span[0] : span[1]].strip():
        raise ReleaseError("CHANGELOG.md has no notes under ## Unreleased to release.")
    return f"{text[: span[0]]}\n\n## {version} - {today.isoformat()}{text[span[0] :]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("cut", "notes"))
    parser.add_argument("version", help="the release, without the v of its tag: 2.0.0")
    args = parser.parse_args(argv)
    text = CHANGELOG.read_text(encoding="utf-8")
    try:
        if args.action == "notes":
            print(notes(text, args.version))
        else:
            CHANGELOG.write_text(cut(text, args.version, datetime.date.today()), encoding="utf-8")
            print(f"CHANGELOG.md: ## Unreleased is now ## {args.version}; commit it, then tag.")
    except ReleaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
