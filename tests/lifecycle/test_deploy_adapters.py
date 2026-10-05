import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm.deploy import get_adapter
from tundravm.deploy.azure import AzureDeployAdapter
from tundravm.deploy.gcp import GcpDeployAdapter
from tundravm.deploy.qemu import QemuDeployAdapter
from tundravm.errors import DeploymentError
from tundravm.models import DeployRequest, OutputTarget


def test_qemu_adapter_requires_qemu_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QEMU adapter raises when qemu-system-x86_64 is not found."""
    request = _request(tmp_path, target="qemu")
    monkeypatch.setattr("tundravm.deploy.qemu.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="QEMU binary not found"):
        QemuDeployAdapter().deploy(request)


def test_azure_adapter_requires_az_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure adapter raises when az CLI is not found."""
    request = _request(tmp_path, target="azure")
    monkeypatch.setattr("tundravm.deploy.azure.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="Azure CLI"):
        AzureDeployAdapter().deploy(request)


def test_gcp_adapter_requires_gcloud_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GCP adapter raises when gcloud is not found."""
    request = _request(tmp_path, target="gcp")
    monkeypatch.setattr("tundravm.deploy.gcp.shutil.which", lambda _: None)

    with pytest.raises(DeploymentError, match="gcloud"):
        GcpDeployAdapter().deploy(request)


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
    assert isinstance(get_adapter("qemu"), QemuDeployAdapter)
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


def _qemu_request(tmp_path: Path, name: str = "node.efi", **params: str) -> DeployRequest:
    artifact = tmp_path / "build" / "dev" / "output" / name
    artifact.parent.mkdir(parents=True)
    artifact.write_text("uki", encoding="utf-8")
    return DeployRequest(profile="dev", target="qemu", artifact_path=artifact, parameters=params)


@pytest.fixture
def firmware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    paths = {}
    for name, constant in (
        ("OVMF_CODE.fd", "OVMF_CODE_PATHS"),
        ("OVMF_VARS.fd", "OVMF_VARS_PATHS"),
        ("OVMF.fd", "TDVF_PATHS"),
    ):
        path = tmp_path / "fw" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("fw", encoding="utf-8")
        monkeypatch.setattr(f"tundravm.deploy.qemu.{constant}", (str(path),))
        paths[name] = str(path)
    return paths


def test_qemu_detaches_with_a_serial_log_and_monitor_socket_not_stdio(
    tmp_path: Path, firmware: dict[str, str]
) -> None:
    run = Recorder()
    result = QemuDeployAdapter(runner=run).deploy(_qemu_request(tmp_path))

    run_dir = tmp_path / "build" / "dev"
    (cmd,) = run.calls
    assert "-nographic" not in cmd and "mon:stdio" not in cmd
    assert cmd[cmd.index("-display") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == f"file:{run_dir}/qemu-serial.log"
    assert cmd[cmd.index("-monitor") + 1] == f"unix:{run_dir}/qemu.monitor,server,nowait"
    assert cmd[-3:] == ["-daemonize", "-pidfile", f"{run_dir}/qemu.pid"]
    assert f"file={firmware['OVMF_CODE.fd']},if=pflash,format=raw,readonly=on" in cmd
    assert f"file={run_dir}/qemu-ovmf-vars.fd,if=pflash,format=raw" in cmd
    assert (run_dir / "qemu-ovmf-vars.fd").read_text(encoding="utf-8") == "fw"
    assert result.metadata["serial_log"] == f"{run_dir}/qemu-serial.log"
    assert result.metadata["monitor"] == f"{run_dir}/qemu.monitor"
    assert result.metadata["pidfile"] == f"{run_dir}/qemu.pid"


def test_qemu_attached_keeps_the_console_on_stdio(tmp_path: Path, firmware: dict[str, str]) -> None:
    run = Recorder()
    result = QemuDeployAdapter(runner=run).deploy(_qemu_request(tmp_path, daemonize="false"))

    (cmd,) = run.calls
    assert cmd[-3:] == ["-nographic", "-serial", "mon:stdio"]
    assert "-daemonize" not in cmd and "-monitor" not in cmd
    assert "serial_log" not in result.metadata


def test_qemu_tdx_boots_tdvf_through_bios_with_a_split_irqchip(
    tmp_path: Path, firmware: dict[str, str]
) -> None:
    run = Recorder()
    QemuDeployAdapter(runner=run).deploy(_qemu_request(tmp_path, tdx="true"))

    (cmd,) = run.calls
    assert cmd[1:5] == [
        "-machine",
        "q35,accel=kvm,kernel-irqchip=split,confidential-guest-support=tdx0",
        "-object",
        "tdx-guest,id=tdx0",
    ]
    assert cmd[cmd.index("-bios") + 1] == firmware["OVMF.fd"]
    assert not any("if=pflash" in arg for arg in cmd)
    assert "-kernel" in cmd


def test_qemu_forwards_every_port_besides_ssh(tmp_path: Path, firmware: dict[str, str]) -> None:
    run = Recorder()
    request = _qemu_request(tmp_path, ssh_port="2200", forward="8080:8080,9443:443")
    result = QemuDeployAdapter(runner=run).deploy(request)

    (cmd,) = run.calls
    assert cmd[cmd.index("-netdev") + 1] == (
        "user,id=net0,hostfwd=tcp::2200-:22,hostfwd=tcp::8080-:8080,hostfwd=tcp::9443-:443"
    )
    assert result.metadata["forward"] == "8080:8080,9443:443"


@pytest.mark.parametrize("forward", ["8080", "x:1", "8080:0", "2222:8080"])
def test_qemu_rejects_a_bad_or_clashing_forward(
    tmp_path: Path, firmware: dict[str, str], forward: str
) -> None:
    with pytest.raises(DeploymentError, match="port forward|same host port"):
        QemuDeployAdapter(runner=Recorder()).deploy(_qemu_request(tmp_path, forward=forward))


def test_qemu_launch_failure_carries_the_command(tmp_path: Path, firmware: dict[str, str]) -> None:
    run = Recorder({"qemu-system-x86_64": _reply(returncode=1)})
    with pytest.raises(DeploymentError, match="QEMU launch failed") as excinfo:
        QemuDeployAdapter(runner=run).deploy(_qemu_request(tmp_path))
    assert excinfo.value.context["command"].startswith("qemu-system-x86_64 -machine q35")


AZURE_IMAGE = (
    "/subscriptions/s/resourceGroups/tdx-vms/providers/Microsoft.Compute/galleries/"
    "tdx_images/images/tdx-dev/versions/1.0.1"
)


def test_azure_publishes_a_confidential_gallery_image_then_creates_the_vm(
    tmp_path: Path,
) -> None:
    run = Recorder(
        {
            "az storage account show": _reply('"/subscriptions/s/storageAccounts/acct"\n'),
            "az sig image-version create": _reply(f'"{AZURE_IMAGE}"\n'),
        }
    )
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct", "secure_boot": "true", "signed": "true"},
    )
    result = AzureDeployAdapter(runner=run).deploy(request)

    verbs = [" ".join(cmd[:4]) for cmd in run.calls]
    assert verbs == [
        "az storage container create",
        "az storage blob upload",
        "az storage account show",
        "az sig create --resource-group",
        "az sig image-definition create",
        "az sig image-version create",
        "az vm create --resource-group",
    ]
    container, upload, _, _, definition, version, create = run.calls
    assert container[container.index("--name") + 1] == "tdx-images"
    blob = upload[upload.index("--name") + 1]
    assert re.fullmatch(r"disk-[0-9a-f]{8}\.vhd", blob)
    assert definition[definition.index("--features") + 1] == "SecurityType=ConfidentialVMSupported"
    assert definition[definition.index("--hyper-v-generation") + 1] == "V2"
    assert version[version.index("--os-vhd-uri") + 1] == (
        f"https://acct.blob.core.windows.net/tdx-images/{blob}"
    )
    assert version[version.index("--os-vhd-storage-account") + 1] == (
        "/subscriptions/s/storageAccounts/acct"
    )
    assert create[create.index("--image") + 1] == AZURE_IMAGE
    assert create[create.index("--size") + 1] == "Standard_DC2es_v5"
    for flag, value in (
        ("--security-type", "ConfidentialVM"),
        ("--os-disk-security-encryption-type", "VMGuestStateOnly"),
        ("--enable-vtpm", "true"),
        ("--enable-secure-boot", "true"),
    ):
        assert create[create.index(flag) + 1] == value
    assert "--specialized" in create
    assert result.metadata["blob"] == f"tdx-images/{blob}"
    assert result.metadata["image"] == AZURE_IMAGE


def test_azure_defaults_to_secure_boot_off(tmp_path: Path) -> None:
    run = Recorder(
        {
            "az storage account show": _reply('"/subscriptions/s/storageAccounts/acct"\n'),
            "az sig image-version create": _reply(f'"{AZURE_IMAGE}"\n'),
        }
    )
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct"},
    )
    result = AzureDeployAdapter(runner=run).deploy(request)
    create = run.calls[-1]
    assert create[create.index("--enable-secure-boot") + 1] == "false"
    assert "signed" not in result.metadata


def test_azure_refuses_secure_boot_for_an_unsigned_image_before_uploading(
    tmp_path: Path,
) -> None:
    run = Recorder()
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct", "secure_boot": "true"},
    )
    with pytest.raises(DeploymentError, match="Secure Boot needs a signed image") as exc:
        AzureDeployAdapter(runner=run).deploy(request)
    assert exc.value.code == "E_DEPLOYMENT"
    hint = exc.value.hint or ""
    assert "--param secure_boot=false" in hint and "--param signed=true" in hint
    assert run.calls == []


def test_azure_reports_a_missing_image_id(tmp_path: Path) -> None:
    request = DeployRequest(
        profile="dev",
        target="azure",
        artifact_path=_artifact(tmp_path, "disk.vhd"),
        parameters={"storage_account": "acct"},
    )
    with pytest.raises(DeploymentError, match="printed no resource id"):
        AzureDeployAdapter(runner=Recorder()).deploy(request)


def test_gcp_creates_a_tdx_instance_on_c3_after_gcloud_storage_upload(tmp_path: Path) -> None:
    run = Recorder()
    request = DeployRequest(
        profile="dev",
        target="gcp",
        artifact_path=_artifact(tmp_path, "disk.raw.tar.gz"),
        parameters={"project": "proj", "bucket": "bkt"},
    )
    result = GcpDeployAdapter(runner=run).deploy(request)

    upload, image, instance = run.calls
    assert upload[:3] == ["gcloud", "storage", "cp"]
    image_name = result.metadata["image_name"]
    assert upload[-1] == f"gs://bkt/tdx-images/{image_name}.tar.gz"
    assert "--guest-os-features=UEFI_COMPATIBLE,GVNIC,TDX_CAPABLE" in image
    assert instance[:4] == ["gcloud", "compute", "instances", "create"]
    assert "--machine-type=c3-standard-4" in instance
    assert "--confidential-compute-type=TDX" in instance
    assert "--maintenance-policy=TERMINATE" in instance
    assert "--confidential-compute" not in instance
    assert result.metadata["blob"] == upload[-1]


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


def test_qemu_refuses_a_monitor_path_too_long_for_a_unix_socket(
    tmp_path: Path, firmware: dict[str, str]
) -> None:
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    with pytest.raises(DeploymentError, match="too long for a unix socket"):
        QemuDeployAdapter(runner=Recorder()).deploy(_qemu_request(deep))
