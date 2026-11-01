"""The generated reference: ``python -m tundravm._reference DIR`` writes ``DIR/cli.md``,
``DIR/api.md`` and ``DIR/lint.md``.

``cli.md`` is every command's ``--help`` (and every ``secrets`` action's) exactly as the
parser prints it at a fixed width. ``api.md`` has one section per public namespace
(:data:`MODULES`), each listing every name of its ``__all__``: its signature (a
dataclass's is its fields with their defaults), the first paragraph of its docstring, and
a class's public methods and properties. A name has one home, so an object exported by
two namespaces is an error, not a cross-reference. ``lint.md`` is every code of the
registry ``tundravm check`` explains (``tundravm.check.RULES``), then the rules each shipped
component declares. All are pure functions of
the installed tundravm
and the Python minor version (argparse's layout changes between them), so a committed copy
is compared with a fresh one the way ``check --tree`` compares an mkosi tree.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import importlib
import inspect
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tundravm.diagnostics import Rule

__all__ = [
    "FILES",
    "HELP_WIDTH",
    "MODULES",
    "PYTHON",
    "REGENERATE",
    "api_markdown",
    "cli_markdown",
    "lint_markdown",
    "main",
    "render",
    "slug",
    "write",
]

HELP_WIDTH = 100
"""The terminal width argparse formats the help for."""

MODULES: tuple[str, ...] = (
    "tundravm",
    "tundravm.ports",
    "tundravm.systemd",
    "tundravm.components",
    "tundravm.storage",
    "tundravm.secrets",
    "tundravm.sources",
    "tundravm.errors",
    "tundravm.testing",
)
"""The public namespaces, in the order an image author meets them: the API reference
covers each one's ``__all__``, and every public name is in exactly one of them."""

REGENERATE = "uv run python -m tundravm._reference docs/reference"
"""The command that rewrites the committed reference."""

PYTHON = f"{sys.version_info.major}.{sys.version_info.minor}"
"""The Python minor version this process renders for; each page's header names it."""

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
_ROLE = re.compile(r":(?:class|func|meth|attr|data|mod|exc|obj):`~?([^`]+)`")


def _header(title: str, what: str) -> list[str]:
    return [
        f"# {title}",
        "",
        f"Generated from {what} by `{REGENERATE}` (Python {PYTHON}). Do not edit it by "
        "hand: `tests/docs/test_reference.py` fails when it is stale.",
        "",
    ]


def _cell(text: str) -> str:
    """*text* safe inside a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def slug(heading: str) -> str:
    """The anchor GitHub gives a heading (before it numbers duplicates)."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


# ── cli.md ───────────────────────────────────────────────────────────────


@contextmanager
def _width(columns: int) -> Iterator[None]:
    """argparse sizes its help from ``COLUMNS``; pin it while formatting."""
    saved = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = str(columns)
    try:
        yield
    finally:
        if saved is None:
            del os.environ["COLUMNS"]
        else:
            os.environ["COLUMNS"] = saved


def _children(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _summaries(parser: argparse.ArgumentParser) -> dict[str, str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return {choice.dest: choice.help or "" for choice in action._choices_actions}
    return {}


def _help_block(parser: argparse.ArgumentParser) -> list[str]:
    text = parser.format_help().rstrip()
    return ["```text", *(line.rstrip() for line in text.splitlines()), "```", ""]


def cli_markdown(parser: argparse.ArgumentParser | None = None) -> str:
    """``cli.md``: the top-level help, then each command's, in the parser's order."""
    if parser is None:
        from tundravm.cli import build_parser

        parser = build_parser()
    from tundravm.completion import INSTALL_HINTS

    with _width(HELP_WIDTH):
        lines = _header("Command reference", "the `tundravm` argument parser")
        commands = _children(parser)
        summaries = _summaries(parser)
        lines += ["| Command | Does |", "|---|---|"]
        lines += [
            f"| [`{name}`](#{slug(f'tundravm {name}')}) | {_cell(summaries[name])} |"
            for name in commands
        ]
        lines += ["", "## tundravm", "", *_help_block(parser)]
        for name, command in commands.items():
            lines += [f"## tundravm {name}", "", summaries[name], "", *_help_block(command)]
            action_help = _summaries(command)
            for action, child in _children(command).items():
                lines += [f"### tundravm {name} {action}", "", action_help[action], ""]
                lines += _help_block(child)
        lines += [
            "## Shell completion",
            "",
            "There is no `completion` command: `python -m tundravm.completion SHELL` prints a "
            "script generated from this parser.",
            "",
            "| Shell | Install |",
            "|---|---|",
            *(f"| {shell} | {_cell(hint)} |" for shell, hint in INSTALL_HINTS.items()),
            "",
        ]
    return "\n".join(lines)


# ── api.md ───────────────────────────────────────────────────────────────


def _markup(text: str) -> str:
    """Docstring reST as Markdown: roles and double backticks become code spans."""
    text = _ROLE.sub(lambda m: f"`{m.group(1).rsplit('.', 1)[-1]}`", text)
    return _ADDRESS.sub("", text.replace("``", "`"))


def _first_paragraph(doc: str) -> str:
    first = inspect.cleandoc(doc).split("\n\n", 1)[0].strip()
    text = _markup(" ".join(line.strip() for line in first.splitlines()))
    return text.removesuffix(":") if text.endswith("::") else text


def _paragraph(obj: object) -> str:
    """The first paragraph of *obj*'s own docstring, on one line; ``""`` without one."""
    doc = getattr(obj, "__doc__", None)
    if not isinstance(doc, str) or not doc.strip():
        return ""
    if isinstance(obj, type) and dataclasses.is_dataclass(obj) and doc.startswith(obj.__name__):
        return ""  # the docstring dataclass() writes for an undocumented class
    return _first_paragraph(doc)


def _annotation(value: object) -> str:
    if value is inspect.Parameter.empty:
        return ""
    if isinstance(value, str):
        return value
    return inspect.formatannotation(value)


def _repr(value: object) -> str:
    return _ADDRESS.sub("", repr(value))


class _Literal:
    """Text that ``repr`` returns as is: how a rendered annotation or default prints."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _parameter(param: inspect.Parameter, factories: Mapping[str, str]) -> str:
    annotation = _annotation(param.annotation)
    default: object = param.default
    if default is not param.empty:
        default = _Literal(factories.get(param.name) or _repr(default))
    shown = _Literal(annotation) if annotation else param.empty
    return str(param.replace(annotation=shown, default=default))


def _signature(
    name: str,
    obj: Callable[..., object],
    *,
    factories: Mapping[str, str] | None = None,
    method: bool = False,
    wrap: bool = True,
) -> str | None:
    """``name(params) -> returns`` with annotations unquoted.

    One parameter per line when long and *wrap*; *factories* renders a dataclass field's
    ``default_factory``; *method* drops ``self``.
    """
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    params = list(signature.parameters.values())[1 if method else 0 :]
    rendered: list[str] = []
    positional_only = False
    keyword_marker = True
    for param in params:
        if param.kind is param.POSITIONAL_ONLY:
            positional_only = True
        elif positional_only:
            rendered.append("/")
            positional_only = False
        if param.kind is param.VAR_POSITIONAL:
            keyword_marker = False
        elif param.kind is param.KEYWORD_ONLY and keyword_marker:
            rendered.append("*")
            keyword_marker = False
        rendered.append(_parameter(param, factories or {}))
    if positional_only:
        rendered.append("/")
    returns = _annotation(signature.return_annotation)
    tail = f" -> {returns}" if returns and not isinstance(obj, type) else ""
    one_line = f"{name}({', '.join(rendered)}){tail}"
    if not wrap or len(one_line) <= 88 or not rendered:
        return one_line
    body = "".join(f"    {param},\n" for param in rendered)
    return f"{name}(\n{body}){tail}"


def _factory(factory: Callable[[], object]) -> str:
    """How a ``default_factory`` reads: ``Kernel.default()``, or a lambda's body."""
    name = getattr(factory, "__qualname__", "factory")
    if not name.endswith("<lambda>"):
        return f"{name}()"
    try:
        source = inspect.getsource(factory).strip()
    except (OSError, TypeError):
        return "..."
    match = re.search(r"lambda:\s*(.*)\)\s*,?$", source)
    return match.group(1) if match else "..."


def _factories(cls: type) -> dict[str, str]:
    """``{field: "Factory()"}`` for each field of dataclass *cls* with a ``default_factory``."""
    return {
        field.name: _factory(field.default_factory)
        for field in dataclasses.fields(cls)
        if field.default_factory is not dataclasses.MISSING
    }


def _members(cls: type) -> list[tuple[str, str]]:
    """``(signature, paragraph)`` of the public methods and properties *cls* itself defines."""
    found: list[tuple[str, str]] = []
    for name, value in sorted(vars(cls).items()):
        qualified = f"{cls.__name__}.{name}"
        if name.startswith("_"):
            continue
        if isinstance(value, property):
            found.append((qualified, _paragraph(value.fget)))
        elif isinstance(value, classmethod | staticmethod):
            signature = _signature(qualified, getattr(cls, name), wrap=False)
            found.append((signature or qualified, _paragraph(value.__func__)))
        elif inspect.isfunction(value):
            signature = _signature(qualified, value, method=True, wrap=False)
            found.append((signature or qualified, _paragraph(value)))
    return found


def _kind(obj: object) -> str:
    if isinstance(obj, type):
        if issubclass(obj, BaseException):
            return "exception"
        if issubclass(obj, enum.Enum):
            return "enum"
        return "dataclass" if dataclasses.is_dataclass(obj) else "class"
    if inspect.isfunction(obj) or inspect.isbuiltin(obj):
        return "function"
    return "value"


def _error_code(cls: type) -> str | None:
    try:
        error = cls("reference")
    except Exception:  # an exception that needs more arguments has no code to show here
        return None
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _entry(name: str, obj: object) -> list[str]:
    kind = _kind(obj)
    lines = [f"### `{name}`", "", f"*{kind}*", ""]
    if kind == "value":
        return [*lines, "```python", f"{name} = {_repr(obj)[:300]}", "```", ""]
    assert callable(obj)
    paragraph = _paragraph(obj)
    signature: str | None
    if isinstance(obj, type) and kind == "exception":
        signature = f"class {name}({', '.join(base.__name__ for base in obj.__bases__)})"
        code = _error_code(obj)
        paragraph = f"{paragraph} Code `{code}`." if code else paragraph
    elif isinstance(obj, type) and kind == "dataclass":
        signature = _signature(name, obj, factories=_factories(obj))
    else:
        signature = _signature(name, obj)
    if signature is not None:
        lines += ["```python", signature, "```", ""]
    if paragraph:
        lines += [paragraph, ""]
    members = _members(obj) if isinstance(obj, type) and kind != "exception" else []
    if members:
        lines += ["| Member | Does |", "|---|---|"]
        lines += [f"| `{_cell(member)}` | {_cell(does)} |" for member, does in members]
        lines.append("")
    return lines


def api_markdown(modules: tuple[str, ...] = MODULES) -> str:
    """``api.md``: one section per module, its ``__all__`` sorted.

    An object two modules export is a :class:`ValueError`: each public name has one home.
    """
    loaded = [importlib.import_module(name) for name in modules]
    lines = _header("API reference", "the `__all__` of `" + "`, `".join(modules) + "`")
    lines += [f"- [`{module.__name__}`](#{slug(module.__name__)})" for module in loaded]
    lines.append("")
    seen: dict[int, str] = {}
    for module in loaded:
        names = sorted(getattr(module, "__all__", ()), key=lambda n: (n.lower(), n))
        lines += [f"## {module.__name__}", "", _first_paragraph(module.__doc__ or ""), ""]
        for name in names:
            obj = getattr(module, name)
            if _kind(obj) != "value" and id(obj) in seen:
                raise ValueError(
                    f"{module.__name__}.{name} is the same object as {seen[id(obj)]}: "
                    "a public name has one home."
                )
            if _kind(obj) != "value":
                seen[id(obj)] = f"{module.__name__}.{name}"
            lines += _entry(name, obj)
    return "\n".join(lines)


# ── lint.md ──────────────────────────────────────────────────────────────


def lint_markdown() -> str:
    """``lint.md``: every code tundravm reports, sorted, with what it checks and how to fix
    it, then the codes each shipped component declares, by component."""
    from tundravm import components
    from tundravm.check import RULES

    lines = _header("Lint reference", "the `tundravm check` rule registry")
    lines += [
        "Every code tundravm itself reports. `tundravm check --explain CODE` prints one "
        "row. The severity is the default, then each other level a code reports at and "
        "when.",
        "",
        "| Code | Severity | What it checks | How to fix |",
        "|---|---|---|---|",
        *(f"| {_rule_row(rule)} |" for rule in sorted(RULES.values(), key=_code)),
        "",
        "## Shipped components",
        "",
        "The codes each component of `tundravm.components` reports, as its class declares "
        "them (`rules`). A project's own components declare theirs the same way and are "
        "not listed here: `tundravm check IMAGE --explain CODE` prints one.",
        "",
        "| Component | Code | Severity | What it checks | How to fix |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(components.__all__):
        cls = getattr(components, name)
        if isinstance(cls, type) and issubclass(cls, components.Component):
            lines += [f"| `{name}` | {_rule_row(rule)} |" for rule in sorted(cls.rules, key=_code)]
    return "\n".join(lines)


def _code(rule: Rule) -> str:
    return rule.code


def _rule_row(rule: Rule) -> str:
    """*rule*'s code, severity, summary and fix, as the cells of a table row."""
    return (
        f"`{rule.code}` | {_cell(rule.severity_text)} | {_cell(_markup(rule.summary))} "
        f"| {_cell(_markup(rule.fix))}"
    )


# ── writing ──────────────────────────────────────────────────────────────

FILES: Mapping[str, Callable[[], str]] = {
    "cli.md": cli_markdown,
    "api.md": api_markdown,
    "lint.md": lint_markdown,
}
"""What :func:`write` puts in the directory, and what renders each."""


def render(name: str) -> str:
    """The text of reference page *name*, newline-terminated as it is written."""
    return FILES[name]().rstrip("\n") + "\n"


def write(directory: Path) -> list[Path]:
    """Render every reference page into *directory*; returns the paths written."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name in FILES:
        path = directory / name
        path.write_text(render(name), encoding="utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if len(words) != 1:
        sys.stderr.write("usage: python -m tundravm._reference DIR\n")
        return 2
    for path in write(Path(words[0])):
        sys.stdout.write(f"wrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
