"""Volumes, secrets, ingress, the root layout and the kernel command line: each slot lowers
onto today's stack.

The prover shape throughout: a volume an operator unlocks, a service keeping its state
on it and reading a delivered secret, and an attested TLS ingress in front of it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from tundravm import File, Image, Kernel, RootFS, Service
from tundravm._image import lower, recipe_of
from tundravm._image._values import MANAGEMENT_PORT
from tundravm.components import Egress, Endpoint, Network
from tundravm.declarative import Disk, Key, Secrets
from tundravm.errors import ValidationError
from tundravm.ports import TCP, TLS, AttestedTLS
from tundravm.secrets import Generated, Secret
from tundravm.sources import Git
from tundravm.storage import BrokerKey, OperatorKey, TpmSeal, Volume
from tundravm.systemd import TcpProbe
from tundravm.testing import PlannedImage, assert_clean, plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

data = Volume("data", mount="/persistent", unlock=OperatorKey())
token = Secret("api-token")
flags = Secret("flags", required=False, rotation="none")


def app(**changes: object) -> Service:
    """The loopback service an ingress fronts: state on *data*, reading *token*."""
    fields: dict[str, object] = {
        "readiness": TcpProbe(8080),
        "state": data.directory("app"),
        "credentials": [token],
    }
    fields.update(changes)
    return Service(
        "app",
        ["/usr/bin/app", "--listen", "127.0.0.1:8080", "--token", token.path],
        **fields,  # type: ignore[arg-type]
    )


def upstream(service: Service | None = None) -> Endpoint:
    return (service or app()).endpoint(8080)


def prover(**changes: object) -> Image:
    fields: dict[str, object] = {
        "services": [app()],
        "ingress": [AttestedTLS(443, upstream=upstream())],
        "network": Network(egress=Egress()),
    }
    fields.update(changes)
    return Image("prover", **fields)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def planned() -> PlannedImage:
    return plan(prover(), "production")


def _after(planned: PlannedImage, unit: str) -> list[str]:
    return " ".join(planned.unit(unit).values("Unit", "After")).split()


# ── Volumes ─────────────────────────────────────────────────────────────


def test_an_operator_key_volume_is_a_pipe_key_and_a_luks_disk(planned: PlannedImage) -> None:
    (key,) = [item for item in planned.resolved.items if isinstance(item, Key)]
    assert (key.strategy, key.pipe, key.persist_in_tpm) == ("pipe", data.pipe, False)
    (disk,) = [item for item in planned.resolved.items if isinstance(item, Disk)]
    assert (disk.device, disk.mount, disk.format) == (
        "/dev/disk/by-id/*-tundravm-data",
        "/persistent",
        "on_initialize",
    )
    keys = planned.file("/etc/tdx/key-gen.yaml").text
    assert 'strategy: "pipe"' in keys and 'pipe_path: "/run/tundravm/unlock/data"' in keys
    disks = planned.file("/etc/tdx/disk-setup.yaml").text
    assert 'pattern: "/dev/disk/by-id/*-tundravm-data"' in disks
    assert 'encryption_key_path: "/run/tundravm/keys/data"' in disks
    assert planned.packages["cryptsetup"] == "runtime"
    builds = lower(prover()).source_builds()
    assert {"key-generation", "disk-encryption", "attested-endpoint", "tdxs"} <= set(builds)


def test_the_volume_directory_is_made_for_its_service_once_the_volume_opens(
    planned: PlannedImage,
) -> None:
    (step,) = [step for step in planned.init_steps if step.name == "volume-data"]
    assert "install -d -o app -g app -m 0750 /persistent/app" in step.script
    unit = planned.service("app")
    assert "runtime-init-volume-data.service" in _after(planned, "app.service")
    assert unit.get("Service", "WorkingDirectory") == "/persistent/app"
    assert unit.values("Service", "ReadWritePaths") == ("-/persistent/app",)
    assert unit.values("Service", "Environment") == ("STATE_DIRECTORY=/persistent/app",)
    assert not unit.values("Service", "StateDirectory")


def test_a_shared_directory_needs_a_group_and_is_owned_by_it() -> None:
    shared = data.directory("chain", group="eth")
    with pytest.raises(ValidationError, match="says nothing about sharing it"):
        Image(
            "x",
            services=[
                Service("a", ["/usr/bin/a"], state=data.directory("chain")),
                Service("b", ["/usr/bin/b"], state=data.directory("chain")),
            ],
        )
    image = Image(
        "x",
        services=[
            Service("a", ["/usr/bin/a"], state=shared),
            Service("b", ["/usr/bin/b"], state=shared),
        ],
    )
    planned = plan(image, "production")
    (step,) = [step for step in planned.init_steps if step.name == "volume-data"]
    assert "install -d -o root -g eth -m 0750 /persistent/chain" in step.script
    assert planned.service("a").values("Service", "SupplementaryGroups") == ("eth",)
    assert "eth" in planned.groups


def test_a_profile_service_keeps_its_state_on_the_volume_too() -> None:
    from tundravm import Profile

    debug = Service("debug", ["/usr/bin/debug"], state=data.directory("debug"))
    image = prover(profiles={"debug": Profile(add=[debug], production=False)})
    (step,) = [s for s in plan(image, "debug").init_steps if s.name == "volume-data"]
    assert "/persistent/debug" in step.script
    assert "runtime-init-volume-data.service" in _after(plan(image, "debug"), "debug.service")


def test_a_tpm_sealed_volume_is_a_vtpm_key_and_no_operator_bootstrap() -> None:
    seal = TpmSeal(platform="azure", pcrs=[7], recovery="none")
    cache = Volume("cache", mount="/var/cache/app", unlock=seal)
    image = Image(
        "x",
        services=[Service("a", ["/usr/bin/a"], state=cache.directory("a"))],
        targets=("azure",),
    )
    planned = plan(image, "production")
    (key,) = [item for item in planned.resolved.items if isinstance(item, Key)]
    assert (key.strategy, key.persist_in_tpm) == ("random", True)
    assert planned.packages["tpm2-tools"] == "runtime"
    assert not [item for item in planned.resolved.items if isinstance(item, Secrets)]
    manifest = json.loads(planned.file("/etc/tundravm/runtime.json").text)
    assert manifest["volumes"][0]["unlock"] == {
        "kind": "tpm-seal",
        "platform": "azure",
        "pcrs": [7],
        "recovery": "none",
    }
    assert manifest["control_plane"] is None


@pytest.mark.parametrize(
    ("platform", "targets", "message"),
    [
        ("azure", ("qemu", "azure"), "qemu boots with swtpm"),
        ("azure", ("gcp",), "gcp boots with gcp"),
        ("gcp", ("qemu", "gcp", "azure"), "azure boots with azure"),
        ("swtpm", ("azure",), "azure boots with azure"),
    ],
)
def test_tpm_seal_is_refused_where_that_vtpm_does_not_boot(
    platform: str, targets: Sequence[str], message: str
) -> None:
    seal = TpmSeal(platform=platform, pcrs=[7], recovery="none")  # type: ignore[arg-type]
    state = Volume("v", mount="/v", unlock=seal).directory("a")
    with pytest.raises(ValidationError, match=message) as caught:
        Image("x", services=[Service("a", ["/a"], state=state)], targets=tuple(targets))  # type: ignore[arg-type]
    assert "cannot be TPM-sealed" in str(caught.value)


def test_tpm_seal_states_its_platform_policy_and_recovery() -> None:
    with pytest.raises(TypeError):
        TpmSeal()  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="not a vTPM"):
        TpmSeal(platform="aws", pcrs=[7], recovery="none")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="index in 0..23"):
        TpmSeal(platform="azure", pcrs=[24], recovery="none")
    with pytest.raises(ValidationError, match="not offered"):
        TpmSeal(platform="azure", pcrs=[7], recovery="operator-key")  # type: ignore[arg-type]


def test_a_volume_mounts_at_its_name_under_persistent_unless_told() -> None:
    assert Volume("db", unlock=OperatorKey()).directory("app").path == "/persistent/db/app"
    assert Volume("db", mount="/srv/db/", unlock=OperatorKey()).mount == "/srv/db"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Volume("Data", mount="/d", unlock=OperatorKey()), "not a volume name"),
        (lambda: Volume("much-too-long", mount="/d", unlock=OperatorKey()), "1 to 11"),
        (lambda: Volume("d", mount="/usr/d", unlock=OperatorKey()), "measured system"),
        (lambda: Volume("d", mount="/d", unlock="key"), "not an unlock policy"),  # type: ignore[arg-type]
        (lambda: Volume("d", mount="/d", unlock=OperatorKey(), filesystem="xfs"), "ext4"),  # type: ignore[arg-type]
        (lambda: data.directory("../etc"), "leaves the volume"),
    ],
)
def test_volume_values_are_checked(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make()  # type: ignore[operator]


def test_the_image_collects_the_volumes_and_secrets_its_services_bind() -> None:
    cert = Secret("tls")
    image = prover(ingress=[TLS(443, upstream=upstream(), certificate=cert)])
    assert (image.volumes, image.secrets) == ((data,), (token, cert))
    with pytest.raises(ValidationError, match="takes no keyword volumes="):
        Image("x", volumes=[data])  # type: ignore[call-arg]


# ── Secrets ─────────────────────────────────────────────────────────────


def test_a_secret_is_a_credential_the_service_waits_for(planned: PlannedImage) -> None:
    unit = planned.service("app")
    assert unit.values("Service", "LoadCredential") == (
        "api-token:/run/tundravm/credentials/api-token",
    )
    assert "${CREDENTIALS_DIRECTORY}/api-token" in (unit.exec_start or "")
    assert "credential-api-token.service" in _after(planned, "app.service")
    wait = planned.unit("credential-api-token.service")
    assert wait.get("Service", "Type") == "oneshot"
    assert "/run/tundravm/credentials/api-token" in (wait.get("Service", "ExecStart") or "")
    assert not wait.values("Service", "TimeoutStartSec")  # an operator may take a while


def test_nothing_of_a_secret_is_written_into_the_image(planned: PlannedImage) -> None:
    assert not [path for path in planned.files if path.startswith("/run/")]
    assert "/usr/lib/tmpfiles.d/50-tundravm-runtime.conf" not in planned.files
    expected = json.loads(planned.file("/etc/tdx/secrets.json").text)
    assert [s["name"] for s in expected["secrets"]] == ["api-token"]
    (target,) = expected["secrets"][0]["targets"]
    assert target == {
        "kind": "file",
        "location": "/run/tundravm/credentials/api-token",
        "mode": "0600",
    }


def test_an_optional_secret_starts_the_service_without_it() -> None:
    service = app(credentials=[token, flags])
    image = prover(services=[service])
    planned = plan(image, "production")
    unit = planned.service("app")
    assert unit.values("Service", "SetCredential") == ("flags:",)
    assert "credential-flags.service" not in _after(planned, "app.service")
    manifest = json.loads(planned.file("/etc/tundravm/runtime.json").text)
    entries = {entry["name"]: entry for entry in manifest["secrets"]}
    assert entries["flags"]["rotation"] == "none" and not entries["flags"]["required"]
    assert entries["api-token"]["units"] == ["app.service"]


@pytest.mark.parametrize(
    ("argv", "environment", "expected"),
    [
        (["--token", token], {}, ("ExecStart", "--token ${CREDENTIALS_DIRECTORY}/api-token")),
        ([], {"TOKEN_FILE": token}, ("Environment", "TOKEN_FILE=%d/api-token")),
        ([token.path], {}, "Pass the Secret itself as the argument"),
        ([token], {"TOKEN": Secret("api-token", required=False)}, "two different credentials"),
    ],
)
def test_a_secret_named_in_argv_or_environment_binds_itself(
    argv: list[object], environment: dict[str, object], expected: tuple[str, str] | str
) -> None:
    def make() -> Service:
        return Service("x", ["/usr/bin/x", *argv], environment=environment)  # type: ignore[list-item, arg-type]

    if isinstance(expected, str):
        with pytest.raises(ValidationError) as caught:
            make()
        assert expected in str(caught.value)
        return
    planned = plan(Image("x", services=[make()]), "production")
    unit = planned.service("x")
    key, text = expected
    assert text in " ".join(unit.values("Service", key))
    assert unit.values("Service", "LoadCredential") == (
        "api-token:/run/tundravm/credentials/api-token",
    )
    assert "credential-api-token.service" in _after(planned, "x.service")


def test_secret_values_are_checked() -> None:
    with pytest.raises(ValidationError, match="rotation"):
        Secret("x", rotation="reload")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="https://"):
        BrokerKey("http://kbs", audience="x")


def test_a_generated_secret_is_written_at_boot_for_the_services_that_share_it() -> None:
    shared = Generated("jwt")
    services = [Service(name, [f"/usr/bin/{name}", "--jwt", shared]) for name in ("el", "cl")]
    image = Image("pair", services=services)
    assert not image.secrets  # nothing to deliver, so no control plane
    planned = plan(image, "production")
    for name in ("el", "cl"):
        unit = planned.service(name)
        assert unit.values("Service", "LoadCredential") == ("jwt:/run/tundravm/secrets/jwt",)
        assert "tundravm-generated-jwt.service" in _after(planned, f"{name}.service")
    writer = planned.unit("tundravm-generated-jwt.service")
    assert writer.get("Service", "User") is None  # root, without capabilities
    assert writer.get("Service", "RestrictAddressFamilies") == "none"
    assert "-N32 -tx1 /dev/urandom" in (writer.get("Service", "ExecStart") or "")
    twin = Service("cl", ["/usr/bin/cl", "--jwt", Generated("jwt", bytes=64)])
    with pytest.raises(ValidationError, match="two different generated secrets"):
        plan(Image("pair", services=[services[0], twin]), "production")
    with pytest.raises(ValidationError, match="16..4096"):
        Generated("jwt", bytes=8)


def test_a_broker_is_recorded_and_waited_for_like_an_operator() -> None:
    broker = BrokerKey("https://kbs.example", audience="prover")
    released = Secret("api-token", source=broker)
    fleet = Volume("data", mount="/persistent", unlock=broker)
    service = Service(
        "app",
        ["/usr/bin/app", "--token", released.path],
        state=fleet.directory("app"),
        credentials=[released],
    )
    image = Image("fleet", services=[service])
    manifest = json.loads(plan(image, "production").file("/etc/tundravm/runtime.json").text)
    source = {"kind": "broker-key", "url": "https://kbs.example", "audience": "prover"}
    assert manifest["secrets"][0]["source"] == source
    assert manifest["volumes"][0]["unlock"] == source


# ── The control plane and the ingress ───────────────────────────────────


def test_the_control_plane_bootstrap_listens_on_the_management_port(
    planned: PlannedImage,
) -> None:
    config = planned.file("/etc/tdx/secrets.yaml").text
    assert f'listen: "0.0.0.0:{MANAGEMENT_PORT}"' in config
    assert "store_at" not in config  # the bootstrap never waits for the volume
    sshd = planned.file("/etc/ssh/sshd_config.d/50-tundravm.conf").text
    assert "PasswordAuthentication no" in sshd
    needs = planned.file("/usr/lib/systemd/system/ssh.service.d/10-tundravm-needs.conf").text
    assert "runtime-init-secrets.service" not in needs  # the key lands whenever
    # The evidence service waits for nothing an operator delivers.
    assert not [unit for unit in _after(planned, "tdxs.service") if "runtime-init" in unit]


def test_attested_tls_is_the_only_way_in(planned: PlannedImage) -> None:
    rules = planned.file("/etc/nftables.conf").text.split("chain output")[0]
    accepted = sorted(
        int(line.split("tcp dport ")[1].split()[0])
        for line in rules.splitlines()
        if "tcp dport" in line
    )
    assert accepted == [22, 443, MANAGEMENT_PORT]  # control plane, ingress, management
    relay = planned.unit("attested-tls-443.service")
    command = relay.get("Service", "ExecStart") or ""
    assert command.startswith("/usr/bin/socat OPENSSL-LISTEN:443,")
    assert "cert=${CREDENTIALS_DIRECTORY}/cert,key=${CREDENTIALS_DIRECTORY}/key" in command
    assert command.endswith(" TCP:127.0.0.1:8080")
    assert relay.values("Service", "LoadCredential") == (
        "cert:/run/tundravm/attested-tls/443/cert.pem",
        "key:/run/tundravm/attested-tls/443/key.pem",
    )
    assert relay.get("Service", "AmbientCapabilities") == "CAP_NET_BIND_SERVICE"
    assert relay.get("Service", "User") == "attested-tls-443"
    assert {"socat", "openssl"} <= set(planned.packages)
    # The key pair is made each boot by a root oneshot the relay and the endpoint follow.
    key = planned.unit("attested-tls-443-key.service")
    assert key.get("Service", "ExecStart") == "/bin/sh /usr/lib/tundravm/attested-tls-key 443"
    assert key.get("Service", "User") is None and key.get("Service", "Type") == "oneshot"
    assert "attested-tls-443-key.service" in _after(planned, "attested-tls-443.service")
    assert "attested-tls-443-key.service" in _after(planned, "tundravm-endpoint.service")
    config = planned.file("/etc/tdx/secrets.yaml").text
    assert "attested_tls:\n  - port: 443\n" in config
    assert 'certificate: "/run/tundravm/attested-tls/443/cert.pem"' in config
    manifest = json.loads(planned.file("/etc/tundravm/runtime.json").text)
    assert manifest["ingress"][0]["kind"] == "attested-tls"
    assert manifest["ingress"][0]["upstream"] == "http://127.0.0.1:8080/"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (
            lambda: AttestedTLS(443, upstream=Endpoint("0.0.0.0", 8080)),
            "not be the only path",
        ),
        (lambda: AttestedTLS(443, upstream=Endpoint("127.0.0.1", 8080, scheme="https")), "HTTP"),
        (lambda: prover(services=[app(expose=[TCP(8080)])]), "not the only path"),
        (lambda: prover(services=[app(expose=[TCP(443)])]), "also exposed"),
        (
            lambda: prover(ingress=[AttestedTLS(MANAGEMENT_PORT, upstream=upstream())]),
            "where the attested endpoint listens",
        ),
    ],
)
def test_an_ingress_that_would_not_be_the_only_path_is_refused(make: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        make()  # type: ignore[operator]


def test_tls_reads_its_certificate_as_a_credential() -> None:
    cert = Secret("tls")
    image = prover(ingress=[TLS(8443, upstream=upstream(), certificate=cert)])
    planned = plan(image, "production")
    relay = planned.unit("tls-8443.service")
    assert relay.values("Service", "LoadCredential") == ("tls:/run/tundravm/credentials/tls",)
    assert "cert=${CREDENTIALS_DIRECTORY}/tls" in (relay.get("Service", "ExecStart") or "")
    assert "credential-tls.service" in _after(planned, "tls-8443.service")
    assert not relay.values("Service", "AmbientCapabilities")  # 8443 needs no capability


def test_the_prover_shape_lints_clean_apart_from_unpinned_sources(planned: PlannedImage) -> None:
    shipped = File.text("/usr/bin/app", "#!/bin/sh\n", mode=0o755)
    assert_clean(prover(files=[shipped]), allow=("source-unpinned",))


# ── The root layout ─────────────────────────────────────────────────────


def test_the_root_layout_is_chosen_explicitly() -> None:
    assert recipe_of(prover()).mkosi.root == "initramfs"
    verity = recipe_of(prover(rootfs=RootFS.verity()))
    assert verity.mkosi.root == "verity"
    with pytest.raises(ValidationError, match="erofs"):
        RootFS.verity(filesystem="ext4")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("layout", "size", "refused"),
    [
        ("initramfs", "4G", None),
        ("initramfs", "50%", None),
        ("initramfs", "4GB", "not a tmpfs size"),
        ("verity", "4G", "volatile overlay"),
    ],
)
def test_the_initramfs_root_tmpfs_cap(layout: str, size: str, refused: str | None) -> None:
    """Lowered to /etc/fstab, which systemd-remount-fs applies; in the digest and the
    manifest's options; refused where the root is not a tmpfs."""
    filesystem = "erofs" if layout == "verity" else None
    if refused is not None:
        with pytest.raises(ValidationError, match=refused):
            RootFS(layout, filesystem, size)  # type: ignore[arg-type]
        return
    image = prover(rootfs=RootFS.initramfs(max_size=size))
    assert f"rootfs / tmpfs size={size} 0 0\n" in plan(image, "production").file("/etc/fstab").text
    assert lower(image).mkosi.root_size == size
    assert lower(image).digest() != lower(prover()).digest()


@pytest.mark.parametrize(
    ("layout", "size", "refused"),
    [
        ("initramfs", "384M", None),
        ("initramfs", "50%", "not a size"),
        ("verity", "384M", "fixed ESP"),
    ],
)
def test_the_initramfs_uki_ceiling(layout: str, size: str, refused: str | None) -> None:
    """Lowered to the recipe's ``Mkosi(uki_max_size=)``, which sizes the ESP; refused where
    the UKI carries no OS."""
    filesystem = "erofs" if layout == "verity" else None
    if refused is not None:
        with pytest.raises(ValidationError, match=refused):
            RootFS(layout, filesystem, uki_max_size=size)  # type: ignore[arg-type]
        return
    assert recipe_of(prover(rootfs=RootFS.initramfs(uki_max_size=size))).mkosi.uki_max_size == size


# ── The kernel command line ─────────────────────────────────────────────


def test_kernel_parameters_follow_the_images_own_and_enter_its_digest() -> None:
    kernel = Kernel.default(cmdline=["nospectre_v2", "mitigations=auto"])
    image = prover(rootfs=RootFS.verity(), kernel=kernel)
    (line,) = plan(image, "production").settings("Content")["KernelCommandLine"]
    assert line.split()[0] == "root=/dev/mapper/root"
    assert line.split()[-3:] == ["systemd.volatile=overlay", "nospectre_v2", "mitigations=auto"]
    digest = lower(image).digest()
    assert digest != lower(prover(rootfs=RootFS.verity())).digest()
    assert digest == lower(prover(rootfs=RootFS.verity(), kernel=kernel)).digest()


@pytest.mark.parametrize(
    ("cmdline", "message"),
    [
        ("nospectre_v2", "a list of kernel parameters"),
        (["a b"], "not one kernel parameter"),
        ([""], "not one kernel parameter"),
        (["root=/dev/sda1"], "rootfs="),
        (["roothash=ab"], "rootfs=RootFS.verity"),
        (["rd.systemd.verity=0"], "rootfs=RootFS.verity"),
        (["systemd.volatile=yes"], "rootfs=RootFS.verity"),
        (["init=/bin/sh"], "boots systemd"),
        (["systemd.unit=rescue.target"], "minimal.target"),
        (["console=tty0"], "console=ttyS0 at boot"),
    ],
)
def test_a_kernel_parameter_tundravm_sets_is_refused(cmdline: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Kernel.default(cmdline=cmdline)  # type: ignore[arg-type]


def test_a_built_kernel_refuses_modules_it_would_drop() -> None:
    source = Git("https://github.com/gregkh/linux", "v6.12.1")
    with pytest.raises(ValidationError, match="a built kernel has every driver built in"):
        Kernel("6.12.1", source, config=Path("kernel.config"), modules=["tdx_guest"])


@pytest.mark.parametrize(
    ("platform", "target", "reported"),
    [("azure", "azure", True), ("gcp", "gcp", True), ("swtpm", "qemu", False)],
)
def test_volume_tpm_on_cloud_points_a_cloud_sealed_volume_at_an_attested_release(
    platform: str, target: str, reported: bool
) -> None:
    seal = TpmSeal(platform=platform, pcrs=[7], recovery="none")  # type: ignore[arg-type]
    volume = Volume("cache", mount="/cache", unlock=seal)
    service = Service("a", ["/usr/bin/a"], state=volume.directory("a"))
    image = Image("x", services=[service], targets=(target,))  # type: ignore[arg-type]
    found = {d.code for d in plan(image, "production").lint()}
    assert ("volume-tpm-on-cloud" in found) is reported
