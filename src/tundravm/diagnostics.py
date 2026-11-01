"""The one finding type every image check reports, :class:`Diagnostic`, and the
:class:`Rule` that says what a code checks.

Resolution and component checks, the compiler's rules
(:mod:`tundravm.check`), each module's own checks, lock drift and ``doctor``
all report it; ``tundravm check`` renders it and ``check --json`` prints
:meth:`Diagnostic.to_dict` for each. Every code is a :class:`Rule`: tundravm's own in
``tundravm.check.RULES``, a component's in its class's ``rules``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from ._keywords import Keywords
from .errors import ValidationError

if TYPE_CHECKING:
    from .declarative._lowered import Lowered
    from .models import ProfileState

Severity = Literal["error", "warning", "info"]
SEVERITIES: tuple[Severity, ...] = get_args(Severity)
Context = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Diagnostic(Keywords):
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


@dataclass(frozen=True, slots=True, init=False)
class Rule(Keywords):
    """One code ``tundravm check`` reports: its severity, what it checks and how to fix it.

    A component declares the codes its ``check()`` reports on its class, as
    ``rules = (Rule("app-privileged-port", "error", summary, fix),)``, so
    ``tundravm check --explain CODE`` prints them; reporting a code it does not declare is
    a ``rule-undeclared`` error. *severity* is the level it reports at, or every level it
    reports at, the default first and then in the order the policy escalates to them;
    *when* says, for each level after the default, when the code reports at it
    (``"under Policy(storage_safety='warn')"``). *summary* is one line: what it checks and
    why it matters; *fix* is what clears it, in the words of an ``Image`` file. *checks* are
    the passes of tundravm's own compiler that report it; a component's rule has none.
    *internal* marks a code only the internal model reaches: an ``Image`` file cannot write
    what it checks, so one reported for an image file is a bug in tundravm's own lowering.
    """

    code: str
    severity: tuple[Severity, ...]
    """Every level the code reports at, the default first."""
    summary: str
    fix: str
    checks: tuple[RuleCheck, ...]
    when: tuple[str, ...]
    """When the code reports at each level after the default, one phrase per level."""
    internal: bool
    """Only the internal model reaches it; ``docs/reference/lint.md`` lists it apart and
    ``check --explain`` says so."""

    def __init__(
        self,
        code: str,
        severity: Severity | Sequence[Severity],
        summary: str,
        fix: str,
        checks: Sequence[RuleCheck] = (),
        *,
        when: Sequence[str] = (),
        internal: bool = False,
    ) -> None:
        levels = (severity,) if isinstance(severity, str) else tuple(severity)
        for name, value in (("code", code), ("summary", summary), ("fix", fix)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(
                    f"Rule: {name} must be a non-empty string, got {value!r}.",
                    hint='Pass Rule("app-privileged-port", "error", "What it checks.", '
                    '"How to fix it.").',
                )
        wrong = [level for level in levels if level not in SEVERITIES]
        if not levels or wrong or len(set(levels)) != len(levels):
            raise ValidationError(
                f"Rule {code}: invalid severity {severity!r}.",
                hint=f"Pass one of {', '.join(SEVERITIES)}, or a tuple of distinct ones with "
                "the default first.",
            )
        phrases = (when,) if isinstance(when, str) else tuple(when)
        if len(phrases) != len(levels) - 1:
            raise ValidationError(
                f"Rule {code}: {len(levels) - 1} level(s) after the default, "
                f"{len(phrases)} when= phrase(s).",
                hint="Pass when= one phrase per level after the default, saying when the code "
                "reports at it: when=(\"under Policy(storage_safety='warn')\",).",
            )
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "severity", levels)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "fix", fix)
        object.__setattr__(self, "checks", tuple(checks))
        object.__setattr__(self, "when", phrases)
        object.__setattr__(self, "internal", bool(internal))

    @property
    def severity_text(self) -> str:
        """The severity as the lint reference and ``--explain`` print it:
        ``error (warning under Policy(storage_safety='warn'))``."""
        default, *others = self.severity
        if not others:
            return default
        changes = (f"{level} {phrase}" for level, phrase in zip(others, self.when, strict=True))
        return f"{default} ({', '.join(changes)})"


def tundravm_sets(what: str) -> str:
    """The fix of a finding about *what* no image file declares: tundravm's own."""
    return f"tundravm sets this itself ({what}); report it."


__all__ = [
    "SEVERITIES",
    "Context",
    "Diagnostic",
    "Rule",
    "RuleCheck",
    "Severity",
    "tundravm_sets",
]
