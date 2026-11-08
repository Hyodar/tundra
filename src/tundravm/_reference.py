"""The generated reference: ``python -m tundravm._reference DIR`` writes ``DIR/cli.md``,
``DIR/api.md`` and ``DIR/lint.md``.

``cli.md`` is every command's ``--help`` (and every ``secrets`` action's) exactly as the
parser prints it at a fixed width. ``api.md`` has one section per public namespace
(:data:`MODULES`), each listing every name of its ``__all__``: its signature (a
dataclass's is its fields with their defaults), the first paragraph of its docstring, and
a class's public methods and properties; then each type alias a signature uses. Every type
is spelled by the name its namespace exports, never a module path. A name has one home,
so an object exported by
two namespaces is an error, not a cross-reference. ``lint.md`` is every code of the
registry ``tundravm check`` explains (``tundravm.check.RULES``), then the rules each shipped
component declares. All are pure functions of
the installed tundravm
and the Python minor version (argparse's layout changes between them), so a committed copy
is compared with a fresh one the way ``check --tree`` compares an mkosi tree.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import collections.abc as abc
import dataclasses
import enum
import functools
import importlib
import importlib.util
import inspect
import os
import re
import sys
import types
import typing
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
    "tundravm.components.tee",
    "tundravm.components.network",
    "tundravm.components.access",
    "tundravm.components.container",
    "tundravm.components.observability",
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


_MISSING = object()
_TOKEN = re.compile(r"""'[^']*'|"[^"]*"|[A-Za-z_][\w.]*""")


def _is_alias(obj: object) -> bool:
    """Whether *obj* is a type alias: ``type X = ...``, or a union or subscripted generic
    bound to a module-level name (``Target = Literal[...]``)."""
    if isinstance(obj, type):
        return False
    alias = isinstance(obj, typing.TypeAliasType | types.UnionType)
    return alias or typing.get_origin(obj) is not None


@functools.cache
def _statements(module: str) -> tuple[ast.stmt, ...]:
    try:
        return tuple(ast.parse(inspect.getsource(sys.modules[module])).body)
    except (OSError, TypeError):
        return ()


@functools.cache
def _deferred(module: str) -> dict[str, object]:
    """The names *module* imports under ``if TYPE_CHECKING:``, resolved."""
    found: dict[str, object] = {}
    for node in _statements(module):
        if not (isinstance(node, ast.If) and ast.unparse(node.test).endswith("TYPE_CHECKING")):
            continue
        for statement in node.body:
            if isinstance(statement, ast.ImportFrom):
                relative = "." * statement.level + (statement.module or "")
                package = sys.modules[module].__package__
                source = importlib.import_module(importlib.util.resolve_name(relative, package))
                for name in statement.names:
                    found[name.asname or name.name] = getattr(source, name.name, _MISSING)
    return found


def _scopes(obj: object) -> tuple[str, ...]:
    """The modules *obj*'s annotations are written in: its own, then its bases'."""
    owners = obj.__mro__ if isinstance(obj, type) else (obj,)
    found = (getattr(owner, "__module__", None) for owner in owners)
    return tuple(dict.fromkeys(name for name in found if name in sys.modules))


def _lookup(token: str, scopes: tuple[str, ...]) -> object:
    head, *rest = token.split(".")
    value: object = _MISSING
    for module in scopes:
        value = vars(sys.modules[module]).get(head, _deferred(module).get(head, _MISSING))
        if value is not _MISSING:
            break
    else:
        value = vars(builtins).get(head, sys.modules.get(head, _MISSING))
    for attribute in rest:
        value = getattr(value, attribute, _MISSING)
    return value


def _assignment(module: str, name: str) -> tuple[str, str] | None:
    """The source of alias *name*'s value in *module*, and the docstring after it."""
    statements = _statements(module)
    for index, node in enumerate(statements):
        if isinstance(node, ast.TypeAlias):
            target: ast.expr = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        after = statements[index + 1] if index + 1 < len(statements) else None
        doc = ""
        if isinstance(after, ast.Expr) and isinstance(after.value, ast.Constant):
            doc = after.value.value if isinstance(after.value.value, str) else ""
        return ast.unparse(node.value), doc
    return None


def _home(name: str, value: object) -> str:
    """The module that defines alias *value* as *name*."""
    if isinstance(value, typing.TypeAliasType) and value.__module__:
        return value.__module__
    for module in sorted(sys.modules):
        namespace = vars(sys.modules[module])
        if module.split(".")[0] == "tundravm" and namespace.get(name) is value:
            if _assignment(module, name) is not None:
                return module
    raise ValueError(f"no module defines the alias {name}")


@dataclasses.dataclass
class _Names:
    """How the reference spells a type: by the name a public namespace exports it under.

    An alias a signature uses is collected in *aliases* (name: value and home module) for
    an entry of its own. Any other tundravm type prints as its bare name, or, where that is
    a public name for something else, the module that defines it: the compiler's ``Service``
    is not the image's.
    """

    public: dict[int, str]
    exported: frozenset[str]
    aliases: dict[str, tuple[object, str]] = dataclasses.field(default_factory=dict)

    @classmethod
    def of(cls, modules: list[types.ModuleType]) -> _Names:
        public = {}
        for module in modules:
            for name in getattr(module, "__all__", ()):
                value = getattr(module, name)
                if callable(value) or _is_alias(value):
                    public[id(value)] = name
        return cls(public, frozenset(public.values()))

    def spell(self, text: str, scopes: tuple[str, ...]) -> str:
        """*text*, an annotation, with each name in it spelled for a reader."""
        return _TOKEN.sub(lambda match: self._word(match.group(0), scopes), text)

    def _word(self, token: str, scopes: tuple[str, ...]) -> str:
        if token[0] in "'\"":
            return token
        value = _lookup(token, scopes)
        if id(value) in self.public:
            return self.public[id(value)]
        bare = token.rsplit(".", 1)[-1]
        if any(vars(module).get(bare) is value for module in (typing, abc, builtins)):
            return token
        if value is not _MISSING and _is_alias(value):
            name = value.__name__ if isinstance(value, typing.TypeAliasType) else bare
            known = self.aliases.setdefault(name, (value, _home(name, value)))
            if known[0] is not value:
                raise ValueError(f"two aliases are named {name}: {known[1]}, {_home(name, value)}")
            return name
        module = getattr(value, "__module__", None)
        if value is not _MISSING and bare in self.exported and module:
            return f"{module}.{bare}"
        if value is _MISSING or (module or "").startswith("tundravm"):
            return bare
        return token


def _value(value: object) -> str:
    """A default as a user writes it: ``{}`` for an empty mapping, ``Debian()`` for the
    value a class constructs with no arguments."""
    if isinstance(value, Mapping) and not dataclasses.is_dataclass(value):
        return repr(dict(value))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            if type(value)() == value:
                return f"{type(value).__name__}()"
        except Exception:  # a class that needs arguments has no bare spelling
            pass
    return _ADDRESS.sub("", repr(value))


class _Literal:
    """Text that ``repr`` returns as is: how a rendered annotation or default prints."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


def _parameter(
    param: inspect.Parameter, defaults: Mapping[str, str], names: _Names, scopes: tuple[str, ...]
) -> str:
    annotation = param.annotation
    if annotation is param.empty:
        shown: object = param.empty
    else:
        text = annotation if isinstance(annotation, str) else inspect.formatannotation(annotation)
        shown = _Literal(names.spell(text, scopes))
    default: object = param.default
    if default is not param.empty:
        mode = param.name == "mode" and type(default) is int
        text = oct(default) if mode and isinstance(default, int) else _value(default)
        default = _Literal(defaults.get(param.name) or text)
    return str(param.replace(annotation=shown, default=default))


def _signature(
    name: str,
    obj: Callable[..., object],
    names: _Names,
    *,
    defaults: Mapping[str, str] | None = None,
    method: bool = False,
    wrap: bool = True,
) -> str | None:
    """``name(params) -> returns`` with annotations unquoted and spelled for a reader.

    One parameter per line when long and *wrap*; *defaults* renders a dataclass field's
    default where its own value does not read as one; *method* drops ``self``.
    """
    try:
        signature = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    scopes = _scopes(obj)
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
        rendered.append(_parameter(param, defaults or {}, names, scopes))
    if positional_only:
        rendered.append("/")
    returns = signature.return_annotation
    tail = ""
    if returns is not signature.empty and not isinstance(obj, type):
        text = returns if isinstance(returns, str) else inspect.formatannotation(returns)
        tail = f" -> {names.spell(text, scopes)}"
    one_line = f"{name}({', '.join(rendered)}){tail}"
    if not wrap or len(one_line) <= 88 or not rendered:
        return one_line
    body = "".join(f"    {param},\n" for param in rendered)
    return f"{name}(\n{body}){tail}"


def _factory(factory: Callable[[], object], names: _Names) -> str:
    """How a ``default_factory`` reads: ``Kernel.default()``, the empty value a container
    type makes, a lambda's body, or ``...`` for a factory a user cannot call."""
    if isinstance(factory, type) and not dataclasses.is_dataclass(factory):
        return _value(factory())
    name = getattr(factory, "__qualname__", "factory")
    if name.endswith("<lambda>"):
        try:
            source = inspect.getsource(factory).strip()
        except (OSError, TypeError):
            return "..."
        match = re.search(r"lambda:\s*(.*)\)\s*,?$", source)
        return match.group(1) if match else "..."
    owner = getattr(factory, "__self__", factory)
    return f"{name}()" if id(owner) in names.public else "..."


def _defaults(cls: type, names: _Names) -> dict[str, str]:
    """``{field: text}`` for each field of dataclass *cls* whose default does not read as a
    value: a ``default_factory``, or a marker standing in for what construction fills in
    (``Hardening``'s, which the strict profile replaces)."""
    shown: dict[str, str] = {}
    built: object = _MISSING
    parameters = inspect.signature(cls).parameters
    for field in dataclasses.fields(cls):
        default = parameters[field.name].default if field.name in parameters else _MISSING
        if field.default_factory is not dataclasses.MISSING:
            shown[field.name] = _factory(field.default_factory, names)
        elif default not in (_MISSING, inspect.Parameter.empty) and _value(default)[0] == "<":
            if built is _MISSING:
                built = cls()
            shown[field.name] = _value(getattr(built, field.name))
    return shown


def _members(cls: type, names: _Names, exported: str | None = None) -> list[tuple[str, str]]:
    """``(signature, paragraph)`` of the public methods and properties *cls* itself defines,
    qualified by *exported*, the name the namespace exports it under (default: its own).

    An undocumented override of a base class's method (a shipped component's
    ``compose()``) is the base's contract, which the base's entry lists.
    """
    found: list[tuple[str, str]] = []
    for name, value in sorted(vars(cls).items()):
        qualified = f"{exported or cls.__name__}.{name}"
        if name.startswith("_") or _undocumented_override(cls, name, value):
            continue
        if isinstance(value, property):
            found.append((qualified, _paragraph(value.fget)))
        elif isinstance(value, classmethod | staticmethod):
            signature = _signature(qualified, getattr(cls, name), names, wrap=False)
            found.append((signature or qualified, _paragraph(value.__func__)))
        elif inspect.isfunction(value):
            signature = _signature(qualified, value, names, method=True, wrap=False)
            found.append((signature or qualified, _paragraph(value)))
    return found


def _undocumented_override(cls: type, name: str, value: object) -> bool:
    inherited = any(name in vars(base) for base in cls.__mro__[1:])
    return inherited and inspect.isfunction(value) and not value.__doc__


def _kind(obj: object) -> str:
    if isinstance(obj, type):
        if issubclass(obj, BaseException):
            return "exception"
        if issubclass(obj, enum.Enum):
            return "enum"
        return "dataclass" if dataclasses.is_dataclass(obj) else "class"
    if inspect.isfunction(obj) or inspect.isbuiltin(obj):
        return "function"
    return "alias" if _is_alias(obj) else "value"


def _error_code(cls: type) -> str | None:
    try:
        error = cls("reference")
    except Exception:  # an exception that needs more arguments has no code to show here
        return None
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _union(name: str, text: str) -> str:
    """``name = text``, each member of a union once, one per line when long."""
    depth, start, members = 0, 0, []
    for index, char in enumerate(text):
        depth += (char in "[(") - (char in "])")
        if depth == 0 and text.startswith(" | ", index):
            members.append(text[start:index])
            start = index + 3
    members = list(dict.fromkeys([*members, text[start:]]))
    one_line = f"{name} = {' | '.join(members)}"
    if len(one_line) <= 88 or len(members) == 1:
        return one_line
    return f"{name} = (\n    " + "\n    | ".join(members) + "\n)"


def _alias_entry(name: str, home: str, names: _Names) -> list[str]:
    """An alias's entry: the values it stands for, and the docstring after its definition."""
    found = _assignment(home, name)
    if found is None:
        raise ValueError(f"{home} does not define the alias {name}")
    text, doc = found
    lines = [f"### `{name}`", "", "*alias*", ""]
    lines += ["```python", _union(name, names.spell(text, (home,))), "```", ""]
    return [*lines, _first_paragraph(doc), ""] if doc else lines


def _entry(name: str, obj: object, names: _Names) -> list[str]:
    kind = _kind(obj)
    if kind == "alias":
        return _alias_entry(name, _home(name, obj), names)
    lines = [f"### `{name}`", "", f"*{kind}*", ""]
    if name == "__version__":
        return [*lines, "The installed release, from its git tag; `tundravm --version`.", ""]
    if kind == "value":
        return [*lines, "```python", f"{name} = {_value(obj)[:300]}", "```", ""]
    assert callable(obj)
    paragraph = _paragraph(obj)
    signature: str | None
    if isinstance(obj, type) and kind == "exception":
        signature = f"class {name}({', '.join(base.__name__ for base in obj.__bases__)})"
        code = _error_code(obj)
        paragraph = f"{paragraph} Code `{code}`." if code else paragraph
    elif isinstance(obj, type) and kind == "dataclass":
        signature = _signature(name, obj, names, defaults=_defaults(obj, names))
    else:
        signature = _signature(name, obj, names)
    if signature is not None:
        lines += ["```python", signature, "```", ""]
    if paragraph:
        lines += [paragraph, ""]
    members = _members(obj, names, name) if isinstance(obj, type) and kind != "exception" else []
    if members:
        lines += ["| Member | Does |", "|---|---|"]
        lines += [f"| `{_cell(member)}` | {_cell(does)} |" for member, does in members]
        lines.append("")
    return lines


_ALIASES_INTRO = (
    "The names the signatures above use for a set of accepted values. None is imported: "
    "pass one of the values it stands for."
)
"""What the aliases section says before its entries."""


def api_markdown(modules: tuple[str, ...] = MODULES) -> str:
    """``api.md``: one section per module, its ``__all__`` sorted, then every alias a
    signature uses that no module exports, sorted.

    An object two modules export is a :class:`ValueError`: each public name has one home.
    """
    loaded = [importlib.import_module(name) for name in modules]
    names = _Names.of(loaded)
    lines = _header("API reference", "the `__all__` of `" + "`, `".join(modules) + "`")
    lines += [f"- [`{module.__name__}`](#{slug(module.__name__)})" for module in loaded]
    lines += ["- [Type aliases](#type-aliases)", ""]
    seen: dict[int, str] = {}
    for module in loaded:
        exported = sorted(getattr(module, "__all__", ()), key=lambda n: (n.lower(), n))
        lines += [f"## {module.__name__}", "", _first_paragraph(module.__doc__ or ""), ""]
        for name in exported:
            obj = getattr(module, name)
            if _kind(obj) != "value" and id(obj) in seen:
                raise ValueError(
                    f"{module.__name__}.{name} is the same object as {seen[id(obj)]}: "
                    "a public name has one home."
                )
            if _kind(obj) != "value":
                seen[id(obj)] = f"{module.__name__}.{name}"
            lines += _entry(name, obj, names)
    entries: dict[str, list[str]] = {}
    while pending := [name for name in names.aliases if name not in entries]:
        for name in pending:
            entries[name] = _alias_entry(name, names.aliases[name][1], names)
    lines += ["## Type aliases", "", _ALIASES_INTRO, ""]
    for name in sorted(entries, key=lambda n: (n.lower(), n)):
        lines += entries[name]
    return "\n".join(lines)


# ── lint.md ──────────────────────────────────────────────────────────────


def lint_markdown() -> str:
    """``lint.md``: every code tundravm reports, sorted, with what it checks and how to fix
    it, then the codes each shipped component declares, by component."""
    from tundravm.check import RULES, shipped_components

    lines = _header("Lint reference", "the `tundravm check` rule registry")
    rules = sorted(RULES.values(), key=_code)
    lines += [
        "Every code an `Image` file can hit, and what to change in it. `tundravm check "
        "--explain CODE` prints one row. The severity is the default, then each other level "
        "a code reports at and when.",
        "",
        "| Code | Severity | What it checks | How to fix |",
        "|---|---|---|---|",
        *(f"| {_rule_row(rule)} |" for rule in rules),
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
    for name, cls in shipped_components():
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
