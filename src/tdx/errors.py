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


__all__ = [
    "BackendExecutionError",
    "DeploymentError",
    "ErrorCode",
    "LockfileError",
    "MeasurementError",
    "ReproducibilityError",
    "TdxError",
    "ValidationError",
]
