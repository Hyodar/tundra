import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from tundravm.deploy import DeployAdapter, get_adapter
from tundravm.deploy.azure import AzureDeployAdapter
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, OutputTarget


@pytest.mark.parametrize(
    ("target", "adapter", "match"),
    [
        ("azure", AzureDeployAdapter, "Azure CLI"),
        ("gcp", GcpDeployAdapter, "gcloud"),
    ],
    ids=["azure", "gcp"],
)
def test_each_adapter_requires_its_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: OutputTarget,
    adapter: Callable[[], DeployAdapter],
    match: str,
) -> None:
    """Each adapter raises when its CLI (az, gcloud) is not found."""
    request = _request(tmp_path, target=target)
    monkeypatch.setattr(f"tundravm.deploy.{target}.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match=match):
        adapter().deploy(request)


def test_gcp_adapter_requires_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GCP adapter raises when no project is specified."""
    request = _request(tmp_path, target="gcp")
    monkeypatch.setattr("tundravm.deploy.gcp.shutil.which", lambda _: "/usr/bin/gcloud")

    with pytest.raises(DeploymentError, match="project is required"):
        GcpDeployAdapter().deploy(request)


def test_get_adapter_rejects_unsupported_target() -> None:
    with pytest.raises(DeploymentError):
        get_adapter("unsupported")


def test_get_adapter_returns_correct_types() -> None:
    assert isinstance(get_adapter("azure"), AzureDeployAdapter)
    assert isinstance(get_adapter("gcp"), GcpDeployAdapter)


def _request(tmp_path: Path, *, target: OutputTarget) -> DeployRequest:
    artifact = tmp_path / f"{target}.img"
    artifact.write_text("artifact", encoding="utf-8")
    return DeployRequest(
        profile="default",
        target=target,
        artifact_path=artifact,
        parameters={"region": "local"},
    )


# ── command lines, with every external tool replaced by a recording runner ──


class Recorder:
    """A deploy runner that records each argv and answers from *replies* (else success)."""

    def __init__(self, replies: dict[str, subprocess.CompletedProcess[str]] | None = None):
        self.calls: list[list[str]] = []
        self.replies = replies or {}

    def __call__(self, cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(cmd)
        self.calls.append(argv)
        for prefix, reply in self.replies.items():
            if " ".join(argv).startswith(prefix):
                return reply
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _reply(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    ("params", "secure_boot"),
    [
        ({"secure_boot": "true", "signed": "true"}, "true"),
        ({"secure_boot": "true"}, None),
    ],
    ids=["signed", "unsigned-refused"],
)
def test_azure_secure_boot_needs_a_signed_image(
    tmp_path: Path, params: dict[str, str], secure_boot: str | None
) -> None:
    """A signed image boots with Secure Boot on; an unsigned one asking for it is refused
    before anything is uploaded (the walk covers the default, off)."""
    run = Recorder(
        {
            "az storage account show": _reply('"/subscriptions/s/storageAccounts/acct"\n'),
            "az sig image-version create": _reply('"/subscriptions/s/images/tdx-dev"\n'),
        }
    )
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct", **params},
    )
    if secure_boot is None:
        with pytest.raises(DeploymentError, match="Secure Boot needs a signed image") as exc:
            AzureDeployAdapter(runner=run).deploy(request)
        hint = exc.value.hint or ""
        assert "--param secure_boot=false" in hint and "--param signed=true" in hint
        assert run.calls == []
        return
    AzureDeployAdapter(runner=run).deploy(request)
    create = run.calls[-1]
    assert create[create.index("--enable-secure-boot") + 1] == secure_boot


def test_azure_reports_a_missing_image_id(tmp_path: Path) -> None:
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct"},
    )
    with pytest.raises(DeploymentError, match="printed no resource id"):
        AzureDeployAdapter(runner=Recorder()).deploy(request)


def test_gcp_upload_failure_names_the_gcloud_storage_command(tmp_path: Path) -> None:
    request = DeployRequest(
        profile="dev",
        target="gcp",
        artifact_path=_artifact(tmp_path, "disk.raw.tar.gz"),
        parameters={"project": "proj", "bucket": "bkt"},
    )
    run = Recorder({"gcloud storage cp": _reply(returncode=2)})
    with pytest.raises(DeploymentError, match="GCS upload failed") as exc:
        GcpDeployAdapter(runner=run).deploy(request)
    assert exc.value.context["command"].startswith("gcloud storage cp ")
    assert "gcloud components update" in (exc.value.hint or "")
    assert [cmd[:3] for cmd in run.calls] == [["gcloud", "storage", "cp"]]


def _artifact(tmp_path: Path, name: str) -> Path:
    artifact = tmp_path / name
    artifact.write_text("artifact", encoding="utf-8")
    return artifact
