import io
from pathlib import Path

import pytest

from tundravm.cli import EXIT_OK, EXIT_SDK_ERROR, main
from tundravm.declarative import (
    Artifact,
    Azure,
    Backend,
    Fragment,
    Package,
    Qemu,
    Recipe,
    Variant,
    bake,
    deploy,
    lock,
    measure,
    verify_artifact,
)
from tundravm.deploy import qemu as qemu_mod
from tundravm.errors import ArtifactError, DeploymentError
from tundravm.models import DeployRequest, DeployResult


def _bake(tmp_path: Path, *variants: Variant) -> tuple[Artifact, ...]:
    """Bake *variants* with the in-process backend into ``tmp_path / "build"``."""
    recipe = Recipe(
        "deploy",
        Fragment("deploy", items=(Package("curl"), Package("linux-image-amd64"))),
        variants=variants,
    )
    return bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), out=tmp_path / "build")


def _mock_qemu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the QEMU adapter so deploying needs no QEMU."""

    def mock_deploy(self: object, request: DeployRequest) -> DeployResult:
        return DeployResult(
            target="qemu",
            deployment_id=f"qemu-{request.profile}",
            endpoint="ssh://localhost:2222",
            metadata={
                "artifact_path": str(request.artifact_path),
                **dict(request.parameters),
            },
        )

    monkeypatch.setattr(qemu_mod.QemuDeployAdapter, "deploy", mock_deploy)


def test_bake_respects_global_and_variant_output_targets(tmp_path: Path) -> None:
    artifacts = _bake(
        tmp_path,
        Variant("default", target="qemu"),
        Variant("azure", target="azure"),
        Variant("gcp", target="gcp"),
    )

    by_variant = {(a.variant, a.target): a for a in artifacts}

    assert set(by_variant) == {("default", "qemu"), ("azure", "azure"), ("gcp", "gcp")}
    assert by_variant["default", "qemu"].path.name == "disk.qcow2"
    assert by_variant["azure", "azure"].path.name == "disk.vhd"
    assert by_variant["gcp", "gcp"].path.name == "disk.raw.tar.gz"
    assert all(a.path.is_file() and a.simulated for a in artifacts)


def test_deploy_fails_when_target_artifact_not_baked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", target="qemu"))
    using = Azure(storage_account="acct")

    with pytest.raises(DeploymentError) as excinfo:
        deploy(artifact, using=using, allow_simulated=True)

    assert "cannot deploy a qemu artifact to azure" in str(excinfo.value).lower()
    assert excinfo.value.code == "E_DEPLOYMENT"

    manifest = str(tmp_path / "build" / "bake-result.json")
    argv = ["deploy", manifest, "--target", "azure", "--param", "storage_account=acct"]
    assert main(argv, stdout=io.StringIO()) == EXIT_SDK_ERROR
    assert "No baked artifact for default/azure." in capsys.readouterr().err


def test_verify_artifact_passes_for_untouched_bytes(tmp_path: Path) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", target="qemu"))
    verify_artifact(artifact)


def test_changed_artifact_is_refused_by_measure_and_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", target="qemu"))
    _mock_qemu(monkeypatch)
    artifact.path.write_bytes(b"swapped after the bake\n")

    for attempt in (
        lambda: verify_artifact(artifact),
        lambda: measure(artifact, allow_placeholder=True),
        lambda: deploy(artifact, using=Qemu(), allow_simulated=True),
    ):
        with pytest.raises(ArtifactError) as excinfo:
            attempt()
        assert excinfo.value.code == "E_ARTIFACT_CHANGED"
        assert excinfo.value.context["recorded"] == artifact.sha256

    artifact.path.unlink()
    with pytest.raises(ArtifactError, match="Cannot read artifact"):
        verify_artifact(artifact)


def test_deploy_cli_reports_a_changed_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", target="qemu"))
    artifact.path.write_bytes(b"swapped\n")
    manifest = str(tmp_path / "build" / "bake-result.json")
    argv = ["deploy", manifest, "--target", "qemu", "--allow-simulated-artifact"]
    assert main(argv, stdout=io.StringIO()) == EXIT_SDK_ERROR
    assert "E_ARTIFACT_CHANGED" in capsys.readouterr().err


def test_deploy_returns_result_when_target_was_baked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (artifact,) = _bake(tmp_path, Variant("default", target="qemu"))
    _mock_qemu(monkeypatch)

    result = deploy(artifact, using=Qemu(memory="4G"), allow_simulated=True)
    metadata = dict(result.metadata)
    artifact_path = Path(metadata["artifact_path"])

    assert result.target == "qemu"
    assert result.id == "qemu-default"
    assert result.endpoint == "ssh://localhost:2222"
    assert artifact_path.exists()
    assert metadata["memory"] == "4G"


def test_deploy_requires_explicit_variant_for_multi_variant_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _bake(tmp_path, Variant("dev", target="qemu"), Variant("prod", target="qemu"))
    _mock_qemu(monkeypatch)
    manifest = str(tmp_path / "build" / "bake-result.json")
    argv = ["deploy", manifest, "--target", "qemu", "--allow-simulated-artifact"]

    assert main(argv, stdout=io.StringIO()) == EXIT_SDK_ERROR
    assert "pass --variant NAME" in capsys.readouterr().err

    out = io.StringIO()
    assert main([*argv, "--variant", "dev"], stdout=out) == EXIT_OK
    lines = out.getvalue().splitlines()
    assert lines[0] == "deployed dev to qemu"
    assert lines[1].split() == ["deployment", "qemu-dev"]
