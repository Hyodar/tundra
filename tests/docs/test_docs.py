"""The docs run as written: code blocks execute, commands parse, links resolve.

Covered pages: README.md, docs/guide.md, docs/trust.md and the generated reference.

- Every ```python block of the prose pages executes in a fresh namespace, and every
  ``Image`` it binds compiles. A block preceded by the line ``<!-- fragment -->`` is
  part of something larger: it is parsed, not run. Zero-argument ``test_*`` functions a
  block defines are called, so the testing examples are tests.
- Every ``tundravm ...`` line of a ```console or ```bash block parses with the real
  argument parser: the command exists, every flag is one of its own, every choice valid.
- Every relative link resolves to a file in the repository, and every ``#anchor`` to a
  heading of its target (GitHub's slugs, duplicates numbered).

No network: nothing here locks, fetches or builds.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import re
import shlex
import sys
import types
from collections.abc import Iterator

import pytest

from tests.helpers import REPO_ROOT
from tundravm import Image
from tundravm import compile as compile_image
from tundravm._reference import slug
from tundravm.cli import build_parser

PROSE = ("README.md", "docs/guide.md", "docs/trust.md")
"""Pages whose code blocks run and whose commands parse."""
LINKED = (*PROSE, "docs/reference/cli.md", "docs/reference/api.md")
"""Pages whose links must resolve."""
FRAGMENT = "<!-- fragment -->"
"""The line before a python block that is parsed but not run."""

FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.M | re.S)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)|<(?:a|img)\s[^>]*(?:href|src)=\"([^\"]+)\"")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
SEPARATORS = re.compile(r"\s(?:&&|\|\||;|\|)\s")


def _text(page: str) -> str:
    return (REPO_ROOT / page).read_text(encoding="utf-8")


def _blocks(page: str) -> Iterator[tuple[str, str, int, bool]]:
    """``(language, body, line, fragment)`` of every fenced block of *page*."""
    text = _text(page)
    for match in FENCE.finditer(text):
        before = text[: match.start()].rstrip("\n").rsplit("\n", 1)[-1].strip()
        line = text.count("\n", 0, match.start()) + 1
        yield match.group(1), match.group(2), line, before == FRAGMENT


def _python_blocks() -> list[tuple[str, int, str, bool]]:
    return [
        (page, line, body, fragment)
        for page in PROSE
        for language, body, line, fragment in _blocks(page)
        if language == "python"
    ]


@pytest.mark.parametrize(
    ("page", "line", "body", "fragment"),
    [pytest.param(*block, id=f"{block[0]}:{block[1]}") for block in _python_blocks()],
)
def test_python_blocks_run(
    page: str, line: int, body: str, fragment: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = f"{page}:{line}"
    tree = ast.parse(body, filename=origin)
    if fragment:
        return
    # A real module, so a Component (a dataclass) defined in the block finds its globals.
    module = types.ModuleType("docs_block")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = module.__dict__
    exec(compile(tree, origin, "exec"), namespace)
    for name, value in list(namespace.items()):
        if isinstance(value, Image):
            compile_image(value)
        elif name.startswith("test_") and callable(value):
            code = getattr(value, "__code__", None)
            if code is not None and code.co_argcount == 0:
                value()


def test_the_prose_has_python_blocks_that_run() -> None:
    runnable = [block for block in _python_blocks() if not block[3]]
    assert len(runnable) >= 8


def _commands() -> list[tuple[str, int, list[str]]]:
    found: list[tuple[str, int, list[str]]] = []
    for page in PROSE:
        for language, body, line, _ in _blocks(page):
            if language not in ("console", "bash", "sh", "shell"):
                continue
            for offset, raw in enumerate(body.splitlines()):
                command = raw.strip().removeprefix("$ ")
                for part in SEPARATORS.split(command):
                    words = shlex.split(part, comments=True)
                    if words[:2] == ["uv", "run"]:
                        words = words[2:]
                    if words[:1] == ["uvx"] and "tundravm" in words:
                        words = words[words.index("tundravm") :]
                    if words[:1] == ["tundravm"]:
                        found.append((page, line + 1 + offset, words[1:]))
    return found


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _command_parser(words: list[str]) -> argparse.ArgumentParser:
    parser = build_parser()
    for word in words:
        children = _subparsers(parser)
        if word.startswith("-") or word not in children:
            break
        parser = children[word]
    return parser


@pytest.mark.parametrize(
    ("page", "line", "words"),
    [pytest.param(*found, id=f"{found[0]}:{found[1]}") for found in _commands()],
)
def test_documented_commands_parse(page: str, line: int, words: list[str]) -> None:
    where = f"{page}:{line}: tundravm {' '.join(words)}"
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            _, extras = build_parser().parse_known_args(words)
    except SystemExit as exc:
        if words and words[-1] in ("-h", "--help") and exc.code == 0:
            return
        raise AssertionError(f"{where}: {stderr.getvalue().strip()}") from None
    unknown = [word for word in extras if word.startswith("-")]
    assert unknown == [], f"{where}: unknown flags {unknown}"
    own = _command_parser(words)._option_string_actions
    flags = [word.split("=", 1)[0] for word in words if word.startswith("--")]
    assert [flag for flag in flags if flag not in own] == [], where


def test_the_prose_shows_every_command() -> None:
    shown = {words[0] for _, _, words in _commands() if words}
    assert shown == set(_subparsers(build_parser()))


def _anchors(page: str) -> set[str]:
    text = FENCE.sub("", _text(page))
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    for match in HEADING.finditer(text):
        base = slug(re.sub(r"`|\*|\[([^\]]*)\]\([^)]*\)", r"\1", match.group(2)))
        count = seen.get(base, 0)
        anchors.add(base if count == 0 else f"{base}-{count}")
        seen[base] = count + 1
    return anchors


def _links() -> list[tuple[str, str]]:
    found = []
    for page in LINKED:
        text = FENCE.sub("", _text(page))
        text = re.sub(r"`[^`\n]*`", "", text)
        for match in LINK.finditer(text):
            target = match.group(1) or match.group(2)
            if not re.match(r"[a-z][a-z0-9+.-]*:", target):
                found.append((page, target))
    return found


@pytest.mark.parametrize(("page", "target"), _links())
def test_relative_links_resolve(page: str, target: str) -> None:
    path, _, anchor = target.partition("#")
    resolved = (REPO_ROOT / page).parent / path if path else REPO_ROOT / page
    assert resolved.exists(), f"{page}: {target} does not exist"
    if anchor:
        relative = resolved.resolve().relative_to(REPO_ROOT).as_posix()
        assert anchor in _anchors(relative), f"{page}: no heading #{anchor} in {relative}"


def test_slugs_follow_github() -> None:
    assert slug("tundravm secrets deliver") == "tundravm-secrets-deliver"
    assert slug("`Image`") == "image"
    assert slug("Volumes, secrets and ingress") == "volumes-secrets-and-ingress"
    assert slug("tundravm.components") == "tundravmcomponents"


@pytest.mark.parametrize(
    ("page", "limit"), [("README.md", 600), ("docs/guide.md", 8000), ("docs/trust.md", 2500)]
)
def test_pages_stay_short(page: str, limit: int) -> None:
    assert len(_text(page).split()) <= limit


def test_every_error_code_is_in_the_guide() -> None:
    from tundravm.errors import ErrorCode

    section = _text("docs/guide.md").split("## When something fails", 1)[1]
    missing = sorted(code.value for code in ErrorCode if f"`{code.value}`" not in section)
    assert missing == [], f"ErrorCode values missing from the guide's error table: {missing}"
