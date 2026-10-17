"""The one finding type every recipe check reports: :class:`Diagnostic`.

Resolution and fragment checks (``tundravm.lint``), the compiler's rules
(:mod:`tundravm.check`), each module's own checks, lock drift and ``doctor``
all report it; ``tundravm lint``, ``watch``, ``ci`` and ``inspect`` render it
and ``lint --format json`` prints :meth:`Diagnostic.to_dict` for each.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, get_args

from .errors import ValidationError

Severity = Literal["error", "warning", "info"]
SEVERITIES: tuple[Severity, ...] = get_args(Severity)
Context = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding: a lint *code*, its *severity* and a one-line *message*.

    *subject* names what it is about (a path, unit, user, lockfile section;
    ``""`` for none). *variant* is the variant it is in, ``""`` for the recipe as a
    whole (``Recipe.items``, the lockfile, the host). *location* is reserved for
    the source position it points at, ``file:line`` (``""`` when unknown, which it is
    for every built-in check: a declaration does not record where it was written);
    ``lint --format github`` annotates that line. *hint* says what to do about it.
    *context* holds extra ``(key, value)`` facts, sorted by key (a mapping is
    converted).
    """

    code: str
    severity: Severity
    message: str
    subject: str = ""
    variant: str = ""
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
        """The JSON form ``lint --format json`` prints: every field, *context* as an object."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "variant": self.variant,
            "location": self.location,
            "hint": self.hint,
            "context": dict(self.context),
        }


__all__ = ["SEVERITIES", "Context", "Diagnostic", "Severity"]
