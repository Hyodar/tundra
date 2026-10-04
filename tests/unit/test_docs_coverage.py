"""The reference docs cover the public surface: exports, CLI verbs and flags, lint codes, errors."""

from __future__ import annotations

import argparse
import re

import tundravm
from tests.helpers import REPO_ROOT
from tundravm.cli import build_parser
from tundravm.declarative import utils
from tundravm.errors import ErrorCode

API_DOC = REPO_ROOT / "docs" / "api.md"
CLI_DOC = REPO_ROOT / "docs" / "cli.md"
PACKAGE = REPO_ROOT / "src" / "tundravm"

# templates.py holds starter recipes whose checks are user code, like the examples'.
RULE_SOURCES_SKIPPED = {"templates.py"}
RULE_CODE_PATTERNS = (
    re.compile(r'\bcode="([a-z][a-z-]+)"'),
    re.compile(r'\b(?:report|Diagnostic)\(\s*"([a-z][a-z-]+)"'),
    re.compile(r'\byield\s+"([a-z]+-[a-z-]+)",'),
)
FLAGS_NOT_DOCUMENTED = {"--help"}


def _api_text() -> str:
    return API_DOC.read_text(encoding="utf-8")


def _cli_text() -> str:
    return CLI_DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section of *text*, up to the next level-2 heading."""
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match is not None, f"docs have no '## {heading}' section"
    return match.group(1)


def _documented(name: str, text: str) -> bool:
    """*name* is a code span (`` `name` ``, `` `module.name(...)` ``) or a code-block signature."""
    if re.search(rf"`(?:[\w.]+\.)?{re.escape(name)}[`(]", text):
        return True
    blocks = re.findall(r"^```[a-z]*\n(.*?)^```", text, re.M | re.S)
    return any(re.search(rf"^{re.escape(name)}\(", block, re.M) for block in blocks)


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("tundravm's parser has no subcommands")


def _long_flags(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option not in FLAGS_NOT_DOCUMENTED
    }


def _emitted_rule_codes() -> set[str]:
    codes: set[str] = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name in RULE_SOURCES_SKIPPED:
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in RULE_CODE_PATTERNS:
            codes.update(pattern.findall(source))
    return codes


def test_every_top_level_export_is_in_the_api_reference() -> None:
    text = _api_text()
    missing = sorted(name for name in tundravm.__all__ if not _documented(name, text))
    assert missing == [], f"tundravm.__all__ names missing from docs/api.md: {missing}"


def test_every_shipped_fragment_export_is_in_the_api_reference() -> None:
    text = _api_text()
    missing = sorted(name for name in utils.__all__ if not _documented(name, text))
    assert missing == [], (
        f"tundravm.declarative.utils.__all__ names missing from docs/api.md: {missing}"
    )


def test_every_cli_verb_is_in_the_cli_reference() -> None:
    text = _cli_text()
    missing = sorted(
        verb
        for verb in _subcommands()
        if re.search(rf"`{re.escape(verb)}[`\s]", text) is None
        and re.search(rf"^\|\s*`?{re.escape(verb)}`?\s*\|", text, re.M) is None
    )
    assert missing == [], f"CLI verbs missing from docs/cli.md: {missing}"


def test_every_cli_long_flag_is_in_the_cli_reference() -> None:
    text = _cli_text()
    parsers = {"tundravm": build_parser(), **_subcommands()}
    missing = {
        verb: sorted(
            flag
            for flag in _long_flags(parser)
            if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", text) is None
        )
        for verb, parser in parsers.items()
    }
    missing = {verb: flags for verb, flags in missing.items() if flags}
    assert missing == {}, f"CLI flags missing from docs/cli.md, by verb: {missing}"


def test_every_lint_code_is_in_the_rule_table() -> None:
    codes = _emitted_rule_codes()
    # A floor, so a refactor that hides the codes from these patterns fails here, not silently.
    assert {"fragment-conflict", "kernel-missing", "lock-changed", "tool-missing"} <= codes
    rules = _section(_api_text(), "Lint rules")
    missing = sorted(code for code in codes if f"`{code}`" not in rules)
    assert missing == [], f"lint codes missing from docs/api.md#lint-rules: {missing}"


def test_every_error_code_is_in_the_errors_table() -> None:
    errors = _section(_api_text(), "Errors")
    missing = sorted(code.value for code in ErrorCode if f"`{code.value}`" not in errors)
    assert missing == [], f"ErrorCode values missing from docs/api.md#errors: {missing}"
