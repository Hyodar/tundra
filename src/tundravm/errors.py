"""Typed SDK error model with stable, machine-readable error codes."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error identifiers used across API surfaces."""

    VALIDATION = "E_VALIDATION"
    LOCKFILE = "E_LOCKFILE"
    REPRODUCIBILITY = "E_REPRODUCIBILITY"
    BACKEND_EXECUTION = "E_BACKEND_EXECUTION"
    MEASUREMENT = "E_MEASUREMENT"
    DEPLOYMENT = "E_DEPLOYMENT"
    POLICY = "E_POLICY"


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


class LockfileError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.LOCKFILE, hint=hint, context=context)


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


class PolicyError(TdxError):
    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        context: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message, code=ErrorCode.POLICY, hint=hint, context=context)


__all__ = [
    "BackendExecutionError",
    "DeploymentError",
    "ErrorCode",
    "LockfileError",
    "MeasurementError",
    "PolicyError",
    "ReproducibilityError",
    "TdxError",
    "ValidationError",
]
