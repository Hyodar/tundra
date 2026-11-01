"""Every code the package reports is a registered ``Rule`` (``tundravm.check.RULES``)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import tundravm
from tundravm.check import RULES

PACKAGE = Path(tundravm.__file__).parent
CODE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")
REPORTERS = {"Diagnostic": (0, 1), "_diagnostic": (0, 1), "_finding": (2, 3), "report": (0, None)}
"""The calls that report a finding, and where their code and severity are passed."""
UNKNOWN = "?"
"""The severity of a report whose level is computed (``drift_severity(...)``)."""


def _strings(node: ast.expr | None, scope: ast.AST) -> set[str]:
    """The string literals *node* can be: a constant, either arm of ``a if c else b``, or a
    name *scope* assigns one of those to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body, scope) | _strings(node.orelse, scope)
    if isinstance(node, ast.Name):
        assigned = (
            n.value
            for n in ast.walk(scope)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == node.id for t in n.targets)
        )
        return {value for expr in assigned for value in _strings(expr, scope)}
    return set()


def _reported() -> dict[str, set[str]]:
    """Each code a module of the package reports, with the severities it uses
    (:data:`UNKNOWN` for one that is not a literal).

    A code is a reporter call's code argument, or the first item of a yielded tuple
    (``yield "lock-changed", section, message``), which a helper turns into a finding.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(scope):
                if isinstance(node, ast.Yield) and isinstance(node.value, ast.Tuple):
                    first = node.value.elts[0]
                    if isinstance(first, ast.Constant) and CODE.fullmatch(str(first.value)):
                        found.setdefault(str(first.value), set()).add(UNKNOWN)
                if isinstance(node, ast.Call):
                    for code, levels in _call_codes(node, scope):
                        found.setdefault(code, set()).update(levels)
    return found


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

    severity = argument("severity", level)
    default = {"error"} if name == "report" and severity is None else {UNKNOWN}
    levels = _strings(severity, scope) or default
    return [(c, levels) for c in _strings(argument("code", at), scope) if CODE.fullmatch(c)]


def test_every_reported_code_is_registered_and_every_registered_code_is_reported() -> None:
    reported = _reported()
    assert sorted(set(reported) - set(RULES)) == [], "reported codes with no Rule"
    assert sorted(set(RULES) - set(reported)) == [], "Rules nothing reports"
    wrong = {
        code: (RULES[code].severity, sorted(levels))
        for code, levels in reported.items()
        if UNKNOWN not in levels and RULES[code].severity not in levels
    }
    assert wrong == {}, "a Rule's severity is one its reporters never use"
