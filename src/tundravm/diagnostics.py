"""The one finding type every image check reports, :class:`Diagnostic`, and the
:class:`Rule` that says what a code checks.

Resolution and component checks, the compiler's rules
(:mod:`tundravm.check`), each module's own checks, lock drift and ``doctor``
all report it; ``tundravm check`` renders it and ``check --json`` prints
:meth:`Diagnostic.to_dict` for each. Every code is a :class:`Rule`: tundravm's own in
``tundravm.check.RULES``, a component's in its class's ``rules``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from .errors import ValidationError

if TYPE_CHECKING:
    from .declarative._lowered import Lowered
    from .models import ProfileState

Severity = Literal["error", "warning", "info"]
SEVERITIES: tuple[Severity, ...] = get_args(Severity)
Context = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding: a lint *code*, its *severity* and a one-line *message*.

    *subject* names what it is about (a path, unit, user, lockfile section;
    ``""`` for none). *profile* is the profile it is in, ``""`` for the image as a
    whole (what every profile shares, the lockfile, the host). *location* is the source
    line it points at, ``file:line`` with the file relative to the working directory
    (absolute outside it), ``""`` when unknown: built-in checks fill it from the
    declaration, component or profile change at fault, and a component check's finding
    without one gets its subject's declaration; lock, host and lockfile findings have
    none. ``tundravm check`` in GitHub Actions annotates that line. *hint* says what to
    do about it.
    *context* holds extra ``(key, value)`` facts, sorted by key (a mapping is
    converted).
    """

    code: str
    severity: Severity
    message: str
    subject: str = ""
    profile: str = ""
    location: str = ""
    hint: str | None = None
    context: Context = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValidationError(
                f"Diagnostic: code must be a non-empty string, got {self.code!r}.",
                hint="Pass a lint code such as 'app-privileged-port'.",
            )
        if self.severity not in SEVERITIES:
            raise ValidationError(
                f"Diagnostic: invalid severity {self.severity!r}.",
                hint=f"Expected one of: {', '.join(SEVERITIES)}.",
            )
        context: object = self.context
        pairs = context.items() if isinstance(context, Mapping) else self.context
        object.__setattr__(self, "context", tuple(sorted((str(k), str(v)) for k, v in pairs)))

    def to_dict(self) -> dict[str, object]:
        """The JSON form ``check --json`` prints: every field, *context* as an object."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "profile": self.profile,
            "location": self.location,
            "hint": self.hint,
            "context": dict(self.context),
        }


RuleCheck = Callable[["Lowered", str, "ProfileState"], Iterator[Diagnostic]]
"""A compiler pass: the findings of one profile of the lowered image."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One code ``tundravm check`` reports: its severity, what it checks and how to fix it.

    A component declares the codes its ``check()`` reports on its class, as
    ``rules = (Rule("app-privileged-port", "error", summary, fix),)``, so
    ``tundravm check --explain CODE`` prints them; reporting a code it does not declare is
    a ``rule-undeclared`` error. *summary* is one line: what it checks and why it matters;
    *fix* is what clears it. *checks* are the passes of tundravm's own compiler that report
    it; a component's rule has none.
    """

    code: str
    severity: Severity
    summary: str
    fix: str
    checks: tuple[RuleCheck, ...] = ()

    def __post_init__(self) -> None:
        for name in ("code", "summary", "fix"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"Rule: {name} must be a non-empty string, got {value!r}.",
                    hint='Pass Rule("app-privileged-port", "error", "What it checks.", '
                    '"How to fix it.").',
                )
        if self.severity not in SEVERITIES:
            raise ValidationError(
                f"Rule {self.code}: invalid severity {self.severity!r}.",
                hint=f"Expected one of: {', '.join(SEVERITIES)}.",
            )


__all__ = ["SEVERITIES", "Context", "Diagnostic", "Rule", "RuleCheck", "Severity"]
