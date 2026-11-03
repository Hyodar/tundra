"""``deploy``: publish and start a build on Azure or GCP, with a receipt; and replace it.

:func:`deploy` runs today's adapters (:mod:`tundravm.deploy`) on an explicit build and
writes an instance receipt (:mod:`tundravm.instances`) with the VM, its address, the
volumes it attached and the firewall rule the management port needs (tundravm does not
create cloud firewall rules). Settings come from ``[tool.tundravm.gcp]`` /
``[tool.tundravm.azure]`` or ``TUNDRAVM_GCP_*`` / ``TUNDRAVM_AZURE_*``
(:func:`tundravm.project.deployment_table`). Every volume the image declares must be
given: ``NAME=create:SIZE`` or ``NAME=existing:ID``; GCP attaches each as device
``tundravm-NAME``; Azure is refused (its data disks are identified by LUN, which no
``/dev/disk/by-id/*-tundravm-NAME`` the image waits for can match).

:func:`upgrade` and :func:`rollback` run plan 6.8's one replacement workflow on GCP, in
order, each step a receipt: launch the candidate without volumes, attest it and deliver
its credentials (control plane ready), stop the old instance (drain and fence: tundravm
cannot drain a workload's connections, so stopping is both), detach every volume from it
and attach it to the candidate, unlock the candidate's operator-key volumes, wait for
application readiness. Downtime runs from the stop to readiness. Refused, with the reason:
Azure; a build whose volumes differ from the instance's (names or unlock kinds); a
candidate whose attestation does not pass the release predicate (the old instance keeps
running and keeps its volumes). The old policy's retirement is the operator's.
"""

from __future__ import annotations

import functools
import json
import shlex
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from . import bootstrap
from . import manifest as mf
from .builds import Build
from .deploy import get_adapter
from .deploy._run import PLANNED, CommandRunner, Fresh, run_captured
from .deploy.azure import CONFIDENTIAL_FLAGS as AZURE_CONFIDENTIAL
from .deploy.azure import AzureDeployAdapter
from .deploy.gcp import CONFIDENTIAL_FLAGS as GCP_CONFIDENTIAL
from .deploy.gcp import GcpDeployAdapter
from .errors import DeploymentError, ValidationError
from .instances import (
    INSTANCE,
    MANAGEMENT_PORT,
    SSH_PORT,
    Instance,
    create,
    open_instance,
    reachable,
)
from .models import DeployRequest
from .project import deployment_table

CLOUDS = ("azure", "gcp")
READY_TIMEOUT = 600.0


def parse_volumes(raw: Sequence[str]) -> dict[str, tuple[str, str]]:
    """``--volume NAME=create:SIZE|existing:ID`` values by volume name."""
    found: dict[str, tuple[str, str]] = {}
    for item in raw:
        name, sep, spec = item.partition("=")
        how, colon, value = spec.partition(":")
        if not sep or not colon or how not in ("create", "existing") or not value or not name:
            raise ValidationError(
                f"--volume {item!r} is not NAME=create:SIZE or NAME=existing:ID.",
                hint="For example --volume data=create:100GiB or --volume data=existing:disk-1.",
            )
        found[name] = (how, value)
    return found


def _check_volumes(build: Build, given: Mapping[str, tuple[str, str]]) -> None:
    declared = {str(v["name"]) for v in build.runtime().get("volumes", ())}
    unknown = sorted(set(given) - declared)
    absent = sorted(declared - set(given))
    if unknown:
        raise ValidationError(
            f"The image declares no volume {', '.join(unknown)}.",
            hint=f"Volumes: {', '.join(sorted(declared)) or 'none'}.",
        )
    if absent:
        raise ValidationError(
            f"Volume {', '.join(absent)} needs a disk.",
            hint=f"Pass --volume {absent[0]}=create:SIZE (a new disk) or "
            f"--volume {absent[0]}=existing:ID (one that holds its data).",
        )


def _request(
    target: str,
    build: Build,
    volumes: Mapping[str, tuple[str, str]],
    *,
    volumes_later: bool,
) -> DeployRequest:
    """What :func:`deploy` and :func:`plan` hand the adapter, after the same refusals."""
    if target not in CLOUDS:
        raise ValidationError(f"Unknown cloud {target!r}.", hint="Deploy to azure or gcp.")
    artifact = build.artifact(target)
    if not volumes_later:
        _check_volumes(build, volumes)
    if target == "azure" and volumes:
        raise DeploymentError(
            "Azure cannot attach a volume the image can find.",
            hint="Azure data disks are identified by LUN; the image waits for "
            "/dev/disk/by-id/*-tundravm-NAME, which only GCP's device names and QEMU serials "
            "give. Deploy this image to GCP, or drop its volumes.",
        )
    params = deployment_table(target)
    if volumes:
        params["volumes"] = ";".join(f"{n}={how}:{v}" for n, (how, v) in sorted(volumes.items()))
    return DeployRequest(
        profile=build.image,
        target=target,  # type: ignore[arg-type]
        artifact_path=artifact,
        parameters=params,
    )


def _adapter(
    target: str, runner: CommandRunner | None, fresh: Fresh | None = None
) -> GcpDeployAdapter | AzureDeployAdapter:
    adapter = get_adapter(target)
    assert isinstance(adapter, GcpDeployAdapter | AzureDeployAdapter)
    if runner is not None:
        adapter.runner = runner
    if fresh is not None:
        adapter.fresh = fresh
    return adapter


def _firewall(ports: Sequence[int]) -> str:
    return f"allow tcp:{','.join(str(p) for p in ports)} to the VM (not managed by tundravm)"


def deploy(
    root: Path,
    target: str,
    build: Build,
    volumes: Mapping[str, tuple[str, str]],
    *,
    runner: CommandRunner | None = None,
    volumes_later: bool = False,
    out: TextIO,
) -> Instance:
    """Publish and start *build* on *target*; returns the new instance. *volumes_later*:
    the volumes are attached afterwards (an upgrade's candidate), so none is required."""
    request = _request(target, build, volumes, volumes_later=volumes_later)
    if not build.production:
        print(f"warning: {build.id} is a development build: never production", file=out)
    result = _adapter(target, runner).deploy(request)
    instance = create(root, build, kind="deploy", platform=target, mode="tdx")
    ports = build.tcp_ports()
    metadata = dict(result.metadata)
    instance.data.update(
        {
            "state": "running",
            "deployment": result.deployment_id,
            "endpoint": result.endpoint,
            "cloud": metadata,
            "volumes": {
                name: {"how": how, "value": v} for name, (how, v) in sorted(volumes.items())
            },
            "ports": {str(p): p for p in ports},
            "firewall": _firewall(ports),
        }
    )
    instance.data["address"] = _address(target, metadata, runner)
    instance.save()
    instance.event("deployed", target=target, build=build.id, deployment=result.deployment_id)
    return instance


def _cloud(instance: Instance | Mapping[str, Any]) -> Mapping[str, Any]:
    """An instance's cloud metadata (the adapter's), or *instance* when it is that already."""
    if isinstance(instance, Instance):
        return instance.data.get("cloud") or {}
    return instance


def _gcloud(instance: Instance | Mapping[str, Any], *argv: str) -> list[str]:
    cloud = _cloud(instance)
    return [
        "gcloud",
        "compute",
        "instances",
        *argv,
        str(cloud.get("vm_name")),
        f"--project={cloud.get('project')}",
        f"--zone={cloud.get('zone')}",
    ]


def _az(instance: Instance | Mapping[str, Any], *argv: str) -> list[str]:
    cloud = _cloud(instance)
    return [
        "az",
        "vm",
        *argv,
        "--name",
        str(cloud.get("vm_name")),
        "--resource-group",
        str(cloud.get("resource_group")),
    ]


def _call(runner: CommandRunner | None, argv: Sequence[str], what: str) -> str:
    result = (runner or run_captured)(argv)
    if result.returncode != 0:
        raise DeploymentError(
            f"{what} failed.",
            hint="Read the cloud CLI's error below.",
            context={"command": " ".join(argv), "stderr": (result.stderr or "")[:2000]},
        )
    return result.stdout or ""


def _address(platform: str, cloud: Mapping[str, Any], runner: CommandRunner | None) -> str | None:
    """The VM's public address: on Azure what ``az vm create`` printed, on GCP the NAT IP
    ``instances describe`` reports."""
    if platform != "gcp":
        return cloud.get("public_ip") or None
    try:
        text = _call(runner, [*_gcloud(cloud, "describe"), "--format=json"], "Describing the VM")
        info = json.loads(text)
        return str(info["networkInterfaces"][0]["accessConfigs"][0]["natIP"])
    except (DeploymentError, ValueError, KeyError, IndexError, TypeError):
        return None


def _status_call(platform: str, cloud: Mapping[str, Any]) -> list[str]:
    """The cloud call ``deploy status`` makes for the VM's state."""
    if platform == "gcp":
        return [*_gcloud(cloud, "describe"), "--format=json"]
    return [*_az(cloud, "get-instance-view"), "--output", "json"]


def _service_ports(ports: Iterable[str | int]) -> list[int]:
    """The application's ports: every port but the management endpoint's and SSH's."""
    return [int(p) for p in ports if int(p) not in (MANAGEMENT_PORT, SSH_PORT)]


def status(instance: Instance, *, runner: CommandRunner | None = None) -> dict[str, object]:
    """``deploy status``: the cloud's view, then control-plane and application readiness,
    probing only the ports the build's firewall opens (no 8745 without the endpoint)."""
    text = _call(runner, _status_call(instance.platform, _cloud(instance)), "Describing the VM")
    if instance.platform == "gcp":
        state = str(json.loads(text or "{}").get("status", "unknown")).lower()
    else:
        statuses = json.loads(text or "{}").get("instanceView", {}).get("statuses", [])
        state = next(
            (s.get("displayStatus") for s in statuses if "PowerState" in s.get("code", "")),
            "unknown",
        )
    host = instance.host
    open_ports = instance.data.get("ports") or {}
    services = {p: reachable(host, p) for p in _service_ports(open_ports)}
    if str(MANAGEMENT_PORT) not in open_ports:
        control = "none declared"
    else:
        control = "listening" if reachable(host, MANAGEMENT_PORT) else "not listening"
    return {
        "id": instance.id,
        "platform": instance.platform,
        "mode": instance.mode,
        "state": state,
        "address": instance.data.get("address"),
        "build": instance.data["build"],
        "control_plane": control,
        "application": "listening" if services and all(services.values()) else "not listening",
        "successor": instance.data.get("successor"),
        "previous": instance.data.get("previous"),
    }


def logs(instance: Instance, *, runner: CommandRunner | None = None) -> str:
    """``deploy logs``: the VM's serial console as the cloud keeps it."""
    if instance.platform == "gcp":
        return _call(runner, _gcloud(instance, "get-serial-port-output"), "Reading the serial port")
    cloud = instance.data.get("cloud") or {}
    argv = [
        "az",
        "vm",
        "boot-diagnostics",
        "get-boot-log",
        "--name",
        str(cloud.get("vm_name")),
        "--resource-group",
        str(cloud.get("resource_group")),
    ]
    return _call(runner, argv, "Reading the boot log")


# ── replacement (plan 6.8) ───────────────────────────────────────────────


def _volume_shape(build: Build) -> dict[str, str]:
    return {
        str(v["name"]): str((v.get("unlock") or {}).get("kind"))
        for v in build.runtime().get("volumes", ())
    }


def _disk_names(instance: Instance) -> dict[str, str]:
    """Each volume's GCP disk name, as the VM reports it (device ``tundravm-NAME``)."""
    found: dict[str, str] = {}
    disks = (instance.data.get("cloud") or {}).get("disks")
    if isinstance(disks, dict):
        found.update({str(k): str(v) for k, v in disks.items()})
    for name, spec in (instance.data.get("volumes") or {}).items():
        if name not in found and spec.get("how") == "existing":
            found[name] = str(spec["value"])
    return found


def _refresh_disks(instance: Instance, runner: CommandRunner | None) -> dict[str, str]:
    text = _call(runner, [*_gcloud(instance, "describe"), "--format=json"], "Describing the VM")
    disks: dict[str, str] = {}
    for disk in json.loads(text or "{}").get("disks", []):
        device = str(disk.get("deviceName", ""))
        if device.startswith("tundravm-"):
            disks[device.removeprefix("tundravm-")] = str(disk.get("source", "")).rsplit("/", 1)[-1]
    instance.data.setdefault("cloud", {})["disks"] = disks
    instance.save()
    return disks


def _wait_ready(instance: Instance, timeout: float) -> bool:
    ports = _service_ports(instance.data.get("ports") or {})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(reachable(instance.host, port) for port in ports):
            return True
        time.sleep(5)
    return False


def upgrade(
    root: Path,
    instance: Instance,
    candidate_build: Build,
    *,
    policy: Path,
    credentials: Sequence[str],
    passphrases: Callable[[str], bytes],
    identity: Path | None = None,
    validator: str | None = None,
    runner: CommandRunner | None = None,
    out: TextIO,
    ready_timeout: float = READY_TIMEOUT,
    **bootstrap_options: Any,
) -> Instance:
    """Replace *instance* by a new VM of *candidate_build*, moving its volumes (plan 6.8)."""
    if instance.platform != "gcp":
        raise DeploymentError(
            f"deploy upgrade replaces GCP instances; {instance.id} runs on {instance.platform}.",
            hint="Azure data disks cannot be moved to a VM the image finds them on (they are "
            "identified by LUN); deploy the new build next to it and move the data yourself.",
        )
    old_shape = _volume_shape(instance.build)
    new_shape = _volume_shape(candidate_build)
    if old_shape != new_shape:
        raise DeploymentError(
            "The new build's volumes differ from the instance's: "
            f"{old_shape or 'none'} -> {new_shape or 'none'}.",
            hint="A replacement keeps the volumes and their unlock; change them in a deployment "
            "of its own, with an explicit migration.",
        )
    disks = _refresh_disks(instance, runner) if old_shape else {}
    print(f"1/7 launching the candidate ({candidate_build.id}) without volumes", file=out)
    candidate = deploy(root, "gcp", candidate_build, {}, runner=runner, volumes_later=True, out=out)
    candidate.data["volumes"] = instance.data.get("volumes", {})
    candidate.data["previous"] = instance.id
    candidate.save()
    print(f"2/7 attesting {candidate.id} and delivering its credentials", file=out)
    result = bootstrap.attest(
        candidate, policy, identity=identity, validator=validator, **bootstrap_options
    )
    refused = bootstrap.release_predicate(candidate, result)
    if refused is not None:
        candidate.event("upgrade-refused", reason=refused)
        raise DeploymentError(
            f"The candidate {candidate.id} did not attest ({refused}); {instance.id} keeps "
            "running and keeps its volumes.",
            hint=f"Inspect it, then delete the VM {candidate.data['cloud'].get('vm_name')}.",
        )
    if credentials:
        bootstrap.deliver(
            candidate,
            policy,
            credentials,
            validator=validator,
            **bootstrap_options,
        )
    print(
        f"3/7 control plane ready on {candidate.id}; stopping {instance.id} (drain, fence)",
        file=out,
    )
    _call(runner, _gcloud(instance, "stop"), "Stopping the old VM")
    instance.data["state"] = "stopped"
    instance.event("fenced", successor=candidate.id)
    print("4/7 moving the volumes (downtime starts)", file=out)
    for name, disk in sorted(disks.items()):
        _call(runner, [*_gcloud(instance, "detach-disk"), f"--disk={disk}"], f"Detaching {disk}")
        _call(
            runner,
            [
                *_gcloud(candidate, "attach-disk"),
                f"--disk={disk}",
                f"--device-name=tundravm-{name}",
            ],
            f"Attaching {disk}",
        )
        candidate.event("volume-attached", volume=name, disk=disk, previous=instance.id)
    candidate.data.setdefault("cloud", {})["disks"] = disks
    print("5/7 unlocking the candidate's volumes", file=out)
    for name, kind in sorted(new_shape.items()):
        if kind == "operator-key":
            bootstrap.unlock(
                candidate,
                name,
                functools.partial(passphrases, name),
                validator=validator,
                **bootstrap_options,
            )
    print("6/7 waiting for application readiness", file=out)
    ready = _wait_ready(candidate, ready_timeout)
    instance.data.update({"state": "retired", "successor": candidate.id})
    instance.save()
    candidate.event("upgraded", previous=instance.id, ready=ready)
    candidate.save()
    print(
        f"7/7 {candidate.id} {'is application-ready' if ready else 'is NOT application-ready'}; "
        f"{instance.id} is stopped and kept for `tundravm deploy rollback {candidate.id}`; "
        "retire the old policy when the rollback window closes",
        file=out,
    )
    if not ready:
        raise DeploymentError(
            f"{candidate.id} did not become application-ready in {int(ready_timeout)}s.",
            hint=f"Read `tundravm deploy logs {candidate.id}`; `tundravm deploy rollback "
            f"{candidate.id}` moves the volumes back.",
        )
    return candidate


def rollback(
    root: Path,
    instance: Instance,
    *,
    policy: Path,
    credentials: Sequence[str],
    passphrases: Callable[[str], bytes],
    identity: Path | None = None,
    validator: str | None = None,
    runner: CommandRunner | None = None,
    out: TextIO,
    ready_timeout: float = READY_TIMEOUT,
    **bootstrap_options: Any,
) -> Instance:
    """Move *instance*'s volumes back to the instance it replaced and start that again."""
    previous_id = instance.data.get("previous")
    if not previous_id:
        raise DeploymentError(
            f"{instance.id} replaced nothing: there is nothing to roll back to.",
            hint="Roll back the instance `deploy upgrade` created.",
        )
    previous = open_instance(root, str(previous_id))
    disks = _refresh_disks(instance, runner) if _volume_shape(instance.build) else {}
    print(f"1/6 starting {previous.id} again ({previous.data['build']['id']})", file=out)
    _call(runner, _gcloud(previous, "start"), "Starting the previous VM")
    previous.data["address"] = _address(
        previous.platform, _cloud(previous), runner
    ) or previous.data.get("address")
    previous.data.pop("attested", None)
    previous.data.pop("bootstrap", None)
    previous.save()
    print(f"2/6 attesting {previous.id} and delivering its credentials", file=out)
    result = bootstrap.attest(
        previous, policy, identity=identity, validator=validator, **bootstrap_options
    )
    refused = bootstrap.release_predicate(previous, result)
    if refused is not None:
        raise DeploymentError(
            f"{previous.id} did not attest ({refused}); {instance.id} keeps its volumes.",
            hint="Stop it again, and investigate before retrying.",
        )
    if credentials:
        bootstrap.deliver(
            previous,
            policy,
            credentials,
            validator=validator,
            **bootstrap_options,
        )
    print(f"3/6 stopping {instance.id}", file=out)
    _call(runner, _gcloud(instance, "stop"), "Stopping the VM")
    print("4/6 moving the volumes back", file=out)
    for name, disk in sorted(disks.items()):
        _call(runner, [*_gcloud(instance, "detach-disk"), f"--disk={disk}"], f"Detaching {disk}")
        _call(
            runner,
            [*_gcloud(previous, "attach-disk"), f"--disk={disk}", f"--device-name=tundravm-{name}"],
            f"Attaching {disk}",
        )
    print("5/6 unlocking", file=out)
    for name, kind in sorted(_volume_shape(previous.build).items()):
        if kind == "operator-key":
            bootstrap.unlock(
                previous,
                name,
                functools.partial(passphrases, name),
                validator=validator,
                **bootstrap_options,
            )
    ready = _wait_ready(previous, ready_timeout)
    instance.data.update({"state": "rolled-back", "successor": previous.id})
    instance.save()
    previous.data.update({"state": "running", "successor": None})
    previous.save()
    previous.event("rolled-back", from_=instance.id, ready=ready)
    print(f"6/6 {previous.id} {'is' if ready else 'is NOT'} application-ready", file=out)
    return previous


# ── plan ─────────────────────────────────────────────────────────────────


class _Recorded:
    """A runner that runs nothing: it records each call and answers what a plan can (an
    ``az --query`` prints the id that call returns when the deploy runs it)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        stdout = json.dumps(f"<the id call {len(self.calls)} prints>") if "--query" in argv else ""
        return subprocess.CompletedProcess(list(argv), 0, stdout, "")


def _flag(argv: Sequence[str], name: str) -> str | None:
    """The value of ``--name=VALUE`` or ``--name VALUE`` in *argv*."""
    for index, word in enumerate(argv):
        if word.startswith(f"{name}="):
            return word.split("=", 1)[1]
        if word == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _confidential(argv: Sequence[str], flags: Sequence[str]) -> list[str]:
    return [f"{name}={_flag(argv, name)}" for name in flags if _flag(argv, name) is not None]


def _gcp_steps(cloud: Mapping[str, Any], calls: Sequence[list[str]]) -> list[dict[str, object]]:
    create = next(c for c in calls if c[:4] == ["gcloud", "compute", "instances", "create"])
    image = next(c for c in calls if c[:4] == ["gcloud", "compute", "images", "create"])
    return [
        {
            "step": "image",
            "name": cloud["image_name"],
            "project": cloud["project"],
            "guest_os_features": _flag(image, "--guest-os-features"),
        },
        {
            "step": "instance",
            "name": cloud["vm_name"],
            "shape": cloud["machine_type"],
            "where": cloud["zone"],
            "confidential": _confidential(create, GCP_CONFIDENTIAL),
        },
    ]


def _azure_steps(cloud: Mapping[str, Any], calls: Sequence[list[str]]) -> list[dict[str, object]]:
    definition = next(c for c in calls if c[:4] == ["az", "sig", "image-definition", "create"])
    version = next(c for c in calls if c[:4] == ["az", "sig", "image-version", "create"])
    create = next(c for c in calls if c[:3] == ["az", "vm", "create"])
    return [
        {
            "step": "image",
            "name": f"{_flag(version, '--gallery-image-definition')} "
            f"{_flag(version, '--gallery-image-version')}",
            "gallery": _flag(version, "--gallery-name"),
            "resource_group": cloud["resource_group"],
            "features": _flag(definition, "--features"),
        },
        {
            "step": "instance",
            "name": cloud["vm_name"],
            "shape": cloud["vm_size"],
            "where": cloud["location"],
            "confidential": _confidential(create, AZURE_CONFIDENTIAL),
        },
    ]


def _policy_step(build: Build, target: str) -> dict[str, object]:
    path = build.path / mf.MEASUREMENTS
    records = [
        r
        for r in mf.records_of(json.loads(path.read_text(encoding="utf-8")))
        if r.get("target") == target and r.get("boot") == "disk"
    ]
    record = records[0] if records else {"state": "absent"}
    found = record.get("policy")
    policy: Mapping[str, Any] = found if isinstance(found, dict) else {}
    return {
        "step": "policy",
        "written": None,
        "source": f"{path} ({target}/disk)",
        "state": record.get("state"),
        "registers": policy.get("registers", {}),
        "unverifiable": policy.get("unverifiable", {}),
        "vtpm_pcrs": policy.get("vtpm_pcrs", {}),
    }


def plan(
    root: Path, target: str, build: Build, volumes: Mapping[str, tuple[str, str]]
) -> dict[str, object]:
    """What ``deploy`` would do, in order, without running anything or needing credentials:
    each step, then every cloud call it makes (``XXXXXXXX`` and ``EPOCH`` stand for what
    the deploy draws when it runs). The refusals are the deploy's own."""
    request = _request(target, build, volumes, volumes_later=False)
    recorded = _Recorded()
    result = _adapter(target, recorded, PLANNED).deploy(request)
    cloud = dict(result.metadata)
    _address(target, cloud, recorded)
    artifact = request.artifact_path
    digest = build.manifest.get("artifacts", {}).get(artifact.name, {})
    upload = recorded.calls[0][-1] if target == "gcp" else _flag(recorded.calls[-2], "--os-vhd-uri")
    ports = build.tcp_ports()
    steps: list[dict[str, object]] = [
        {
            "step": "upload",
            "artifact": str(artifact),
            "size": digest.get("size"),
            "sha256": digest.get("sha256"),
            "to": upload,
        },
        *(_gcp_steps if target == "gcp" else _azure_steps)(cloud, recorded.calls),
        {
            "step": "volumes",
            "volumes": [
                {"name": name, "how": how, "value": value, "device": f"tundravm-{name}"}
                for name, (how, value) in sorted(volumes.items())
            ],
        },
        {"step": "firewall", "ports": list(ports), "rule": _firewall(ports), "managed": False},
        _policy_step(build, target),
        {"step": "receipt", "path": str(root / f"{build.image}-N" / INSTANCE)},
        {
            "step": "status",
            "call": _status_call(target, cloud),
            "control_plane": MANAGEMENT_PORT if MANAGEMENT_PORT in ports else None,
            "application": _service_ports(ports),
        },
    ]
    return {
        "target": target,
        "build": {
            "id": build.id,
            "image": build.image,
            "kind": build.kind,
            "path": str(build.path),
        },
        "steps": steps,
        "calls": recorded.calls,
    }


def _registers(step: Mapping[str, Any], shape: str) -> list[str]:
    """The policy's accepted values, one per line; on GCP, RTMR0's for the instance's
    machine type only (the rest are counted)."""
    lines: list[str] = []
    for name, values in step["registers"].items():
        other = 0
        for entry in values:
            value = entry if isinstance(entry, str) else entry["value"]
            label = "" if isinstance(entry, str) else entry.get("label") or ""
            if label and not label.startswith(f"{shape} "):
                other += 1
                continue
            what = (
                ""
                if isinstance(entry, str)
                else " ".join(p for p in (entry.get("firmware"), label) if p)
            )
            lines.append(f"{name:<6} {value}{f'  {what}' if what else ''}")
        if other:
            lines.append(f"{name:<6} and {other} values for other machine types")
    reasons: dict[str, list[str]] = {}
    for name, reason in step["unverifiable"].items():
        reasons.setdefault(reason, []).append(name)
    lines += [f"{', '.join(names)} unverifiable: {reason}" for reason, names in reasons.items()]
    pcrs = sorted(step["vtpm_pcrs"].items(), key=lambda item: int(item[0].removeprefix("PCR")))
    lines += [f"vTPM {pcr} {value}" for pcr, value in pcrs]
    return lines or [f"no prediction ({step['state']})"]


def render_plan(found: Mapping[str, Any]) -> str:
    """:func:`plan` as text: the steps, numbered, then the calls, numbered."""
    build = found["build"]
    lines = [
        f"plan: deploy {build['image']} {build['id']} ({build['kind']}) to {found['target']}; "
        "nothing runs",
        "  XXXXXXXX, EPOCH and <the id call N prints> stand for what the deploy draws or reads",
    ]
    steps = {step["step"]: step for step in found["steps"]}
    shape = str(steps["instance"]["shape"])
    for number, step in enumerate(found["steps"], 1):
        kind = step["step"]
        if kind == "upload":
            text = f"{step['artifact']}, {step['size']} bytes, sha256 {step['sha256']}"
            text += f"\n      to {step['to']}"
        elif kind == "image":
            text = (
                f"{step['name']} in project {step['project']}, guest OS features "
                f"{step['guest_os_features']}"
                if "project" in step
                else f"{step['name']} in gallery {step['gallery']} ({step['resource_group']}), "
                f"{step['features']}"
            )
        elif kind == "instance":
            text = f"{step['name']}, {step['shape']} in {step['where']}: "
            text += " ".join(step["confidential"])
        elif kind == "volumes":
            text = (
                "; ".join(
                    f"{v['name']}: {'new' if v['how'] == 'create' else 'existing'} {v['value']} "
                    f"as device {v['device']}"
                    for v in step["volumes"]
                )
                or "none"
            )
        elif kind == "firewall":
            text = step["rule"]
        elif kind == "policy":
            text = f"none written; attest --policy takes {step['source']} or an approved copy:"
            text += "".join(f"\n      {line}" for line in _registers(step, shape))
        elif kind == "receipt":
            text = step["path"]
        else:
            application = ", ".join(str(p) for p in step["application"]) or "none"
            control = step["control_plane"] or "none"
            text = (
                f"`{shlex.join(step['call'])}`,\n      then tcp {control} "
                f"(control plane) and {application} (application)"
            )
        lines.append(f"{number}. {kind:<9} {text}")
    lines.append("calls:")
    lines += [f"{n}. {shlex.join(call)}" for n, call in enumerate(found["calls"], 1)]
    return "\n".join(lines)


__all__ = [
    "CLOUDS",
    "deploy",
    "logs",
    "parse_volumes",
    "plan",
    "render_plan",
    "rollback",
    "status",
    "upgrade",
]
