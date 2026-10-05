"""Typed SDK error model with stable, machine-readable error codes."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ErrorCode(StrEnum):
    """Stable error identifiers used across API surfaces."""

    VALIDATION = "E_VALIDATION"
    LOCKFILE = "E_LOCKFILE"
    REPRODUCIBILITY = "E_REPRODUCIBILITY"
    BACKEND_EXECUTION = "E_BACKEND_EXECUTION"
    MEASUREMENT = "E_MEASUREMENT"
    DEPLOYMENT = "E_DEPLOYMENT"
    ARTIFACT_CHANGED = "E_ARTIFACT_CHANGED"
    POLICY = "E_POLICY"
    STATE = "E_STATE"
    LINT = "E_LINT"
    SOURCE = "E_SOURCE"


class TdxError(Exception):
    """Base error class that carries code, optional hint, and context."""

    code: str
    hint: str | None
    context: Mapping[str, str]

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code.value
        self.hint = hint
        self.context = dict(context or {})

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        if self.context:
            for k, v in self.context.items():
                if v:
                    parts.append(f"  {k}: {v}")
        return "\n".join(parts)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "message": str(self),
            "context": dict(self.context),
        }
        if self.hint is not None:
            payload["hint"] = self.hint
        return payload


class ValidationError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.VALIDATION, hint=hint, context=context)


class SourceError(TdxError):
    """A source build's source could not be resolved to a pin.

    ``source`` names it (``git <url> @ <ref>``, ``http <url>``); ``reason`` says why,
    e.g. ``ref 'main' not found``, ``repository unreachable: <git error>`` or
    ``HTTP 404``.
    """

    source: str
    reason: str

    def __init__(
        self,
        message: str,
        *,
        source: str,
        reason: str,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.SOURCE, hint=hint, context=context)
        self.source = source
        self.reason = reason


class LockfileError(TdxError):
    """A lockfile is missing, unreadable or stale, or locking failed.

    ``failures`` maps each source build that ``lock()`` could not resolve to its
    :class:`SourceError`; it is empty for every other lockfile error.
    """

    failures: Mapping[str, SourceError]

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
        failures: Mapping[str, SourceError] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.LOCKFILE, hint=hint, context=context)
        self.failures = MappingProxyType(dict(failures or {}))


class ReproducibilityError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.REPRODUCIBILITY, hint=hint, context=context)


class BackendExecutionError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.BACKEND_EXECUTION, hint=hint, context=context)


class MeasurementError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.MEASUREMENT, hint=hint, context=context)


class DeploymentError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.DEPLOYMENT, hint=hint, context=context)


class ArtifactError(TdxError):
    """A baked artifact's bytes no longer match the sha256 ``bake-result.json`` recorded."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.ARTIFACT_CHANGED, hint=hint, context=context)


class PolicyError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.POLICY, hint=hint, context=context)


class StateError(TdxError):
    """Persisted SDK state (such as ``bake-result.json``) is missing or unreadable."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.STATE, hint=hint, context=context)


class LintError(TdxError):
    """``bake()`` refused: the compiler's checks reported error-level diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.LINT, hint=hint, context=context)


__all__ = [
    "ArtifactError",
    "BackendExecutionError",
    "DeploymentError",
    "ErrorCode",
    "LintError",
    "LockfileError",
    "MeasurementError",
    "PolicyError",
    "ReproducibilityError",
    "SourceError",
    "StateError",
    "TdxError",
    "ValidationError",
]
