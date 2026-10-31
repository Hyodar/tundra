from tundravm.errors import (
    BackendExecutionError,
    DeploymentError,
    ErrorCode,
    LockfileError,
    MeasurementError,
    ReproducibilityError,
    ValidationError,
)


def test_error_codes_are_stable_and_machine_readable() -> None:
    errors = [
        ValidationError("bad input"),
        LockfileError("lock mismatch"),
        ReproducibilityError("drift detected"),
        BackendExecutionError("backend failed"),
        MeasurementError("measurement mismatch"),
        DeploymentError("deploy failed"),
    ]
    assert [error.code for error in errors] == [
        ErrorCode.VALIDATION.value,
        ErrorCode.LOCKFILE.value,
        ErrorCode.REPRODUCIBILITY.value,
        ErrorCode.BACKEND_EXECUTION.value,
        ErrorCode.MEASUREMENT.value,
        ErrorCode.DEPLOYMENT.value,
    ]
