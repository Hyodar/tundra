from pathlib import Path

from tundravm.errors import (
    BackendExecutionError,
    DeploymentError,
    ErrorCode,
    LockfileError,
    MeasurementError,
    ReproducibilityError,
    ValidationError,
)
from tundravm.models import (
    ArtifactRef,
    BakeResult,
    ProfileBuildResult,
    RecipeState,
)


def test_recipe_state_initializes_default_profile() -> None:
    state = RecipeState.initialize(
        base="debian/bookworm",
        arch="x86_64",
        default_profile="default",
    )
    assert state.base == "debian/bookworm"
    assert state.arch == "x86_64"
    assert state.default_profile == "default"
    assert "default" in state.profiles


def test_bake_result_artifact_lookup() -> None:
    artifact = ArtifactRef(target="qemu", path=Path("build/default/disk.raw"))
    result = BakeResult(
        profiles={
            "default": ProfileBuildResult(
                profile="default",
                artifacts={"qemu": artifact},
            )
        }
    )
    resolved = result.artifact_for(profile="default", target="qemu")
    assert resolved == artifact


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
