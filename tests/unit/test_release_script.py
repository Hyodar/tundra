"""scripts/release.py: ``cut`` moves the Unreleased notes under the version, and ``notes``
prints a version's section or refuses a tag without one."""

from __future__ import annotations

import datetime

import pytest
from scripts import release

from tests.helpers import REPO_ROOT

TODAY = datetime.date(2026, 11, 1)
LOG = "# Changelog\n\nThe format note.\n\n## Unreleased\n\n### SDK\n\n- A change.\n"
CUT = (
    "# Changelog\n\nThe format note.\n\n## Unreleased\n\n## 2.0.0 - 2026-11-01\n\n"
    "### SDK\n\n- A change.\n"
)


@pytest.mark.parametrize(
    ("action", "text", "version", "result", "refused"),
    [
        ("cut", LOG, "2.0.0", CUT, None),
        ("cut", CUT, "2.0.0", None, "already has ## 2.0.0"),
        ("cut", CUT, "2.0.1", None, "no notes under ## Unreleased"),
        ("cut", LOG, "v2.0.0", None, "not a release version"),
        ("notes", CUT, "2.0.0", "### SDK\n\n- A change.", None),
        ("notes", LOG, "2.0.0", None, "release.py cut 2.0.0"),
    ],
    ids=["cut", "cut-twice", "cut-nothing", "cut-a-tag", "notes", "notes-of-an-uncut-tag"],
)
def test_release_sections(
    action: str, text: str, version: str, result: str | None, refused: str | None
) -> None:
    def run() -> str:
        return (
            release.cut(text, version, TODAY) if action == "cut" else release.notes(text, version)
        )

    if refused is not None:
        with pytest.raises(release.ReleaseError, match=refused):
            run()
        return
    assert run() == result


def test_the_changelog_opens_on_unreleased_and_every_release_has_notes() -> None:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first, *releases = release.sections(text)
    assert first == "Unreleased"
    assert all(release.notes(text, version) for version in releases)
