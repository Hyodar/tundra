"""Every code the package reports is a registered ``Rule``: tundravm's own in
``tundravm.check.RULES``, a shipped component's in its class's ``rules``."""

from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import tundravm
from tundravm.check import RULES
from tundravm.components import Component

PACKAGE = Path(tundravm.__file__).parent
CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")
REPORTERS = {"Diagnostic": (0, 1), "_diagnostic": (0, 1), "_finding": (2, 3), "report": (0, None)}
"""The calls that report a finding, and where their code and severity are passed."""
UNKNOWN = "?"
"""The severity of a report whose level the reporter does not pass (a yielded tuple)."""

type Reported = dict[str, set[str]]
"""Each code reported, with the severities it is reported at."""


def _strings(node: ast.expr | None, scope: ast.AST) -> set[str]:
    """The string literals *node* can be: a constant, either arm of ``a if c else b``, or a
    name *scope* assigns one of those to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body, scope) | _strings(node.orelse, scope)
    if isinstance(node, ast.Name):
        return {value for expr in _assigned(node.id, scope) for value in _strings(expr, scope)}
    return set()


def _assigned(name: str, scope: ast.AST) -> list[ast.expr]:
    """What *scope* assigns to *name*, annotated or not."""
    found: list[ast.expr] = []
    for n in ast.walk(scope):
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in n.targets
        ):
            found.append(n.value)
        elif isinstance(n, ast.AnnAssign) and getattr(n.target, "id", None) == name and n.value:
            found.append(n.value)
    return found


def _resolved(node: ast.expr | None, scope: ast.AST) -> ast.expr | None:
    """*node*, or the one expression *scope* assigns to it when it is a name."""
    if isinstance(node, ast.Name):
        assigned = _assigned(node.id, scope)
        return assigned[0] if len(assigned) == 1 else node
    return node


def _paired(
    code: ast.expr | None, severity: ast.expr | None, scope: ast.AST
) -> set[tuple[str, str]]:
    """The ``(code, severity)`` pairs a reporter passes: ``a if c else b`` for both under one
    condition pairs arm with arm."""
    code, severity = _resolved(code, scope), _resolved(severity, scope)
    if (
        isinstance(code, ast.IfExp)
        and isinstance(severity, ast.IfExp)
        and ast.dump(code.test) == ast.dump(severity.test)
    ):
        return _paired(code.body, severity.body, scope) | _paired(
            code.orelse, severity.orelse, scope
        )
    levels = _strings(severity, scope)
    return {(c, level) for c in _strings(code, scope) for level in levels}


def _reported(tree: ast.AST, found: Reported) -> None:
    """Add each code *tree* reports to *found*, with the severities it uses
    (:data:`UNKNOWN` for one that is not a literal).

    A code is a reporter call's code argument, or the first item of a yielded tuple
    (``yield "lock-changed", section, message``), which a helper turns into a finding.
    """
    for scope in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(scope):
            if isinstance(node, ast.Yield) and isinstance(node.value, ast.Tuple):
                first = node.value.elts[0]
                if isinstance(first, ast.Constant) and CODE.fullmatch(str(first.value)):
                    found.setdefault(str(first.value), set()).add(UNKNOWN)
            if isinstance(node, ast.Call):
                for code, levels in _call_codes(node, scope):
                    found.setdefault(code, set()).update(levels)


def _call_codes(call: ast.Call, scope: ast.AST) -> list[tuple[str, set[str]]]:
    """The codes *call* reports, each with its severities, when it is a reporter."""
    callee = call.func
    name = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
    keywords = {k.arg: k.value for k in call.keywords}
    at, level = REPORTERS.get(name, (None, None))

    def argument(key: str, index: int | None) -> ast.expr | None:
        if key in keywords:
            return keywords[key]
        return call.args[index] if index is not None and len(call.args) > index else None

    code, severity = argument("code", at), argument("severity", level)
    if severity is None:
        default = "error" if name == "report" else UNKNOWN
        return [(c, {default}) for c in _strings(code, scope) if CODE.fullmatch(c)]
    found: dict[str, set[str]] = {}
    for c, severity_level in _paired(code, severity, scope):
        if CODE.fullmatch(c):
            found.setdefault(c, set()).add(severity_level)
    return list(found.items())


def _shipped_component(path: Path, node: ast.ClassDef) -> type[Component] | None:
    """The shipped :class:`Component` subclass *node* defines in *path*, if it is one."""
    if path.parent.name != "components":
        return None
    module = importlib.import_module(f"tundravm.components.{path.stem}")
    cls = getattr(module, node.name, None)
    return cls if isinstance(cls, type) and issubclass(cls, Component) else None


def _package() -> tuple[Reported, dict[type[Component], Reported]]:
    """What tundravm reports itself, and what each shipped component class reports."""
    own: Reported = {}
    components: dict[type[Component], Reported] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        for node in classes:
            cls = _shipped_component(path, node)
            if cls is not None:
                _reported(node, components.setdefault(cls, {}))
                tree.body.remove(node)
        _reported(tree, own)
    return own, components


def test_every_reported_code_is_declared_and_every_declared_code_is_reported() -> None:
    own, components = _package()
    assert sorted(set(own) - set(RULES)) == [], "reported codes with no Rule"
    assert sorted(set(RULES) - set(own)) == [], "Rules nothing reports"
    wrong = {
        code: (RULES[code].severity, sorted(levels))
        for code, levels in own.items()
        if not levels - {UNKNOWN} <= set(RULES[code].severity)
        or (UNKNOWN not in levels and levels != set(RULES[code].severity))
    }
    assert wrong == {}, "a Rule's severities are not the ones its reporters use"
    for cls, reported in components.items():
        declared = {rule.code: sorted(rule.severity) for rule in cls.rules}
        assert {code: sorted(levels) for code, levels in reported.items()} == declared, cls
        assert not set(declared) & set(RULES), cls.__name__
