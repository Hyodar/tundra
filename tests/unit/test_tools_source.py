"""The default source of the runtime tools: this repository's ``tools/``, from a checkout or
at the installed commit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tundravm import templates
from tundravm._modules import base
from tundravm._source import GitSource, LocalSource
from tundravm.errors import ValidationError

COMMIT = "c" * 40
FROM_GIT = json.dumps(
    {"url": "https://github.com/Hyodar/tundra.git", "vcs_info": {"vcs": "git", "commit_id": COMMIT}}
)
EDITABLE_COPY = json.dumps({"url": "file:///nowhere/tundra", "dir_info": {"editable": True}})


@pytest.mark.parametrize(
    ("checkout", "direct_url", "expected"),
    [
        (True, None, LocalSource(base.TOOLS_LOCAL, directory=Path("/x/tools"))),
        (False, FROM_GIT, GitSource("https://github.com/Hyodar/tundra", COMMIT, subdir="tools")),
        (False, EDITABLE_COPY, 'Tdxs(source=Git("https://github.com/Hyodar/tundra", COMMIT'),
    ],
    ids=["a-checkout", "installed-from-git", "a-copy-without-a-commit"],
)
def test_the_tools_build_from_this_repository(
    monkeypatch: pytest.MonkeyPatch, checkout: bool, direct_url: str | None, expected: object
) -> None:
    monkeypatch.setattr(base, "tools_checkout", lambda: Path("/x/tools") if checkout else None)
    monkeypatch.setattr(
        templates, "tundravm_source", lambda: templates.source_from_direct_url(direct_url)
    )
    if isinstance(expected, str):
        with pytest.raises(ValidationError) as refused:
            base.tools_source.__wrapped__()
        assert expected in str(refused.value.hint) and "git+" in str(refused.value.hint)
        return
    source = base.tools_source.__wrapped__()
    assert source == expected
    if isinstance(source, LocalSource):
        assert source.directory == Path("/x/tools")


@pytest.mark.parametrize(
    ("version", "commit", "ref"),
    [
        ("2.0.0", COMMIT, COMMIT),
        ("2.0.0.post1.dev3+gccccccc", COMMIT, COMMIT),
        ("2.0.0.post1.dev3+gccccccc.d20261101", COMMIT, None),
        ("2.0.0", None, None),
    ],
    ids=["a-release", "between-tags", "an-edited-tree", "no-recorded-commit"],
)
def test_an_installed_wheel_builds_the_tools_at_its_commit(
    monkeypatch: pytest.MonkeyPatch, version: str, commit: str | None, ref: str | None
) -> None:
    monkeypatch.setattr(base, "tools_checkout", lambda: None)
    monkeypatch.setattr(
        templates,
        "tundravm_source",
        lambda: templates.release_source(version, commit) or templates.source_from_direct_url(None),
    )
    if ref is None:
        with pytest.raises(ValidationError):
            base.tools_source.__wrapped__()
        return
    assert base.tools_source.__wrapped__() == GitSource(
        templates.TUNDRAVM_REPOSITORY, ref, subdir="tools"
    )


def test_this_checkout_builds_its_own_tools() -> None:
    assert base.tools_checkout() == Path(__file__).resolve().parents[2] / "tools"
