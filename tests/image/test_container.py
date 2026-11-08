"""``Container``: a program in a Debian root of its own, built as a subimage with the image."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tundravm import DebianConfig, File, Image, Kernel, Profile, Service
from tundravm._image import compile as compile_image
from tundravm.builds import _container_roots
from tundravm.components import Component, Endpoint
from tundravm.components.access import Command, DevAccess, Login, SshAccess
from tundravm.components.container import Bind, Container, ContainerSsh
from tundravm.components.network import Egress, Firewall, Host, Mode, Network
from tundravm.declarative._network import HOSTS_HEADER
from tundravm.errors import ValidationError
from tundravm.models import ContainerSpec
from tundravm.ports import TCP, UDP
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume
from tundravm.systemd import HttpProbe, TcpProbe
from tundravm.testing import plan

pytestmark = pytest.mark.usefixtures("isolated_cwd")

data = Volume("data", unlock=OperatorKey())
SSH = ContainerSsh(port=10022, keys=Secret("web-keys"))
HTTPD = ["/usr/bin/busybox", "httpd", "-f", "-p", "8081", "-h", "/srv/www"]
PAGE = File.text("/srv/www/index.html", "<h1>web</h1>\n")
RPC = Service(
    "rpc", ["/usr/bin/busybox", "httpd", "-f", "-p", "127.0.0.1:8545"], readiness=TcpProbe(8545)
)
ROOT = "/usr/lib/tundravm/containers/web.raw"
WINDOW = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[TCP(8082)])})


def web(**changes: object) -> Container:
    fields: dict[str, object] = {"packages": ["busybox"], "files": [PAGE], "expose": [TCP(8081)]}
    fields.update(changes)
    return Container("web", HTTPD, **fields)  # type: ignore[arg-type]


def tree(image: Image, out: Path) -> Path:
    compile_image(image).write(out)
    return out


def read(out: Path, path: str) -> str:
    return (out / path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: web(packages=["ubuntu:24.04"]), "an OCI image reference"),
        (lambda: web(packages=["docker.io/library/debian@sha256:ab"]), "an OCI image reference"),
        (lambda: web(packages=[]), "packages= is empty"),
        (lambda: web(expose=[UDP(53), UDP(53)]), "names UDP"),
        (lambda: web(requires=[Service("a", ["/usr/bin/a"]).endpoint(80)]), "no readiness"),
        (lambda: web(requires=[Endpoint("10.0.0.5", 8545, ready=RPC.ready)]), "not the host's"),
        (lambda: web(probe=TcpProbe(8551)), "serves= does not name"),
        (lambda: web(serves=[TCP(80)], probe=HttpProbe("http://127.0.0.1/")), "TcpProbe"),
        (lambda: web().endpoint(8551), "serves= does not"),
        (lambda: web(modes={"maintenance": Egress(metadata=True)}), "outlives the window"),
        (lambda: web(user="root"), 'user="root"'),
        (lambda: web(files=[File.text("/srv/a", "a", owner="web")]), "every file in it is root's"),
        (lambda: web(files=[File.text("/etc/hosts", "1.2.3.4 a\n")]), "Host pins render"),
        (lambda: Container("web", ["busybox", "httpd"], packages=["busybox"]), "program path"),
        (lambda: Container("Web", HTTPD, packages=["busybox"]), "not a container name"),
        (lambda: ContainerSsh(port=22, keys=Secret("web-keys")), "only root binds below 1024"),
        (lambda: ContainerSsh(port=10022), "no key to accept"),
        (lambda: ContainerSsh(port=10022, owner=True, keys=Secret("k")), "names others"),
        (lambda: Bind(data.directory("logs", group="eth")), "the container's group"),
        (lambda: Bind(data.directory("logs"), file=True), "not a file"),
        (lambda: Mode(ingress=[web().ssh]), "until it says ssh="),
        (lambda: web(ssh=ContainerSsh(port=8081, keys=Secret("k"))), "ssh= exposes already"),
        (lambda: Container("web", packages=["busybox"]), "argv= is empty"),
        (lambda: web(binds=[Bind("/run/tundravm/keys")]), "tundravm keeps from it"),
        (lambda: web(binds=[Bind("/a", at="/b"), Bind("/b")]), "two binds land on /b"),
        (
            lambda: Container("web", packages=["sl"], ssh=SSH, hardening="jit"),
            "sandbox is tundravm's",
        ),
    ],
)
def test_a_container_refuses_what_would_fetch_or_leak(
    make: Callable[[], object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message) as raised:
        make()
    assert raised.value.hint


def test_a_container_binds_host_paths_read_only_unless_writable(tmp_path: Path) -> None:
    binds = [Bind("/var/log/app"), Bind("/run/input", writable=True), Bind("/run/s", file=True)]
    out = tree(Image("box", components=[web(binds=binds)]), tmp_path / "tree")
    dropin = read(out, "payload/mkosi.extra/usr/lib/systemd/system/web.service.d/20-container.conf")
    assert "BindPaths=/run/input:/run/input\n" in dropin
    assert "BindReadOnlyPaths=/var/log/app:/var/log/app\n" in dropin
    assert "BindReadOnlyPaths=/run/s:/run/s\n" in dropin
    mounts = read(out, "mkosi.images/web/mkosi.postinst.d/10-mount-points.sh")
    assert 'mkdir -p "$BUILDROOT"/var/log/app\n' in mounts
    assert 'touch "$BUILDROOT"/run/s\n' in mounts


def test_a_container_runs_under_the_hardening_it_names() -> None:
    planned = plan(Image("box", components=[web(hardening="jit")]))
    assert planned.unit("web.service").get("Service", "MemoryDenyWriteExecute") is None


def test_a_container_lowers_to_a_subimage_its_unit_runs_from(tmp_path: Path) -> None:
    image = Image("box", components=[web(state=data.directory("web"), expose=[])])
    out = tree(image, tmp_path / "tree")
    subimage = read(out, "mkosi.images/web/mkosi.conf")
    assert "Format=portable\nOutput=web\n" in subimage
    assert "Packages=\n    busybox\n" in subimage
    assert read(out, "mkosi.images/web/mkosi.extra/srv/www/index.html") == "<h1>web</h1>\n"
    mounts = read(out, "mkosi.images/web/mkosi.postinst.d/10-mount-points.sh")
    # the read-only root holds every mount point systemd's RootImage= namespace mounts over
    for path in ("/proc", "/sys", "/dev", "/run", "/tmp", "/var/tmp", "/persistent/data/web"):
        assert f'mkdir -p "$BUILDROOT"{path}\n' in mounts, path
    payload = read(out, "payload/mkosi.conf")
    assert payload.startswith("[Config]\nDependencies=web\n")
    assert "ExtraTrees=%O/web.raw:/usr/lib/tundravm/containers/web.raw" in payload
    assert "    loop\n" in payload
    units = out / "payload/mkosi.extra/usr/lib/systemd/system"
    assert (units / "web.service.d/20-container.conf").read_text(encoding="utf-8") == (
        "[Unit]\nAfter=web-loop.service\nRequires=web-loop.service\n\n"
        "[Service]\nBindPaths=/persistent/data/web:/persistent/data/web\nMountAPIVFS=yes\n"
        "PrivateNetwork=yes\nPrivatePIDs=yes\nPrivateUsers=yes\nProtectProc=invisible\n"
        "RootImage=/usr/lib/tundravm/containers/web.raw\n"
    )
    assert "WorkingDirectory=/persistent/data/web\n" in read(units, "web.service")
    loader = read(units, "web-loop.service")
    for line in (
        "ExecStart=/usr/sbin/modprobe loop\n",
        "ConditionPathExists=!/dev/loop-control\n",
        "ProtectSystem=strict\n",
        "RestrictAddressFamilies=none\n",
        "SystemCallFilter=@system-service @module\n",
        "CapabilityBoundingSet=CAP_SYS_MODULE\n",
    ):
        assert line in loader, line
    assert "User=" not in loader and "ProtectKernelModules" not in loader
    assert not (units / "web-net.service").exists()


def test_a_built_kernel_s_container_loads_no_loop_module() -> None:
    """A built kernel has loop built in (the container asks it for =y), so nothing loads it."""
    kernel = Kernel.build(base=DebianConfig("6.12.113-1"))
    planned = plan(Image("box", components=[web()], kernel=kernel))
    assert "/usr/lib/systemd/system/web-loop.service" not in planned.files
    dropin = planned.file("/usr/lib/systemd/system/web.service.d/20-container.conf").text
    assert "loop" not in dropin


def networked() -> Image:
    container = web(
        expose=[TCP(8081), UDP(9999), TCP(8082)],
        egress=Egress(hosts=["1.1.1.1:853"]),
        modes={"maintenance": Egress(hosts=["1.0.0.1:853"])},
        requires=[RPC.endpoint(8545)],
        serves=[TCP(8551)],
        probe=TcpProbe(8551),
    )
    consumer = Service("cl", ["/usr/bin/cl", container.endpoint(8551)])
    return Image(
        "box",
        services=[RPC, consumer],
        components=[container, WINDOW],
        network=Network(egress=Egress()),
    )


def test_a_containers_pinned_hosts_resolve_in_its_own_root(tmp_path: Path) -> None:
    """Every Host of egress= and modes= is a line of the root's /etc/hosts, after Debian's
    loopback lines, whatever the mode in force; the image's own /etc/hosts stays untouched."""
    peer = Host("api.example", ["203.0.113.7", "203.0.113.8"], port=443, aliases=["rpc.example"])
    windowed = Host("deb.example", ["203.0.113.20"], port=443)
    container = web(
        egress=Egress(hosts=[peer, "10.0.0.5:8545"]),
        modes={"maintenance": Egress(hosts=[windowed, peer])},
    )
    image = Image("box", components=[container, WINDOW], network=Network(egress=Egress()))
    out = tree(image, tmp_path / "tree")
    assert read(out, "mkosi.images/web/mkosi.extra/etc/hosts") == "".join(
        f"{line}\n" for line in HOSTS_HEADER
    ) + (
        "203.0.113.7 api.example rpc.example\n"
        "203.0.113.8 api.example rpc.example\n"
        "203.0.113.20 deb.example\n"
    )
    assert not (out / "payload/mkosi.extra/etc/hosts").exists()
    conf = read(out, "payload/mkosi.extra/etc/nftables.conf")
    assert 'ip daddr 203.0.113.7 tcp dport 443 accept comment "web (api.example)"' in conf
    assert "host4_api.example" not in conf


def test_a_networked_container_lowers_to_a_veth_the_firewall_forwards() -> None:
    planned = plan(networked())
    dropin = planned.file("/usr/lib/systemd/system/web.service.d/20-container.conf").text
    assert dropin == (
        "[Unit]\nAfter=web-loop.service web-net.service web-endpoint-8545.socket\n"
        "Requires=web-loop.service web-net.service web-endpoint-8545.socket\n\n"
        "[Service]\nMountAPIVFS=yes\nNetworkNamespacePath=/run/netns/web\nPrivatePIDs=yes\n"
        "PrivateUsers=yes\nProtectProc=invisible\nRootImage=/usr/lib/tundravm/containers/web.raw\n"
    )
    net = planned.file("/usr/lib/systemd/system/web-net.service").text
    for line in (
        "DefaultDependencies=no\nRequires=nftables.service\nAfter=nftables.service\n",
        "ExecStart=/usr/bin/ip link add ve-web type veth peer name host0 netns web\n",
        "ExecStart=/usr/bin/ip -n web route add default via 169.254.132.217\n",
        "CapabilityBoundingSet=CAP_NET_ADMIN CAP_SYS_ADMIN\n",
        "RestrictAddressFamilies=AF_UNIX AF_NETLINK\n",
        "SystemCallFilter=@system-service @mount\n",
    ):
        assert line in net, line
    assert "/bin/sh" not in net and "ProtectSystem" not in net
    forward = planned.file("/etc/sysctl.d/60-container-web.conf").text
    assert forward.endswith("\nnet.ipv4.ip_forward = 1\n")
    relay = planned.file("/usr/lib/systemd/system/web-endpoint-8545.socket.d/20-container.conf")
    assert "[Socket]\nNetworkNamespacePath=/run/netns/web\n" in relay.text
    assert "Unmanaged=yes" in planned.file("/etc/systemd/network/10-tundravm-ve-web.network").text
    assert (
        "/tcp/169.254.132.218/8551"
        in planned.file("/usr/lib/systemd/system/web-ready.service").text
    )
    conf = planned.file("/etc/nftables.conf").text
    for line in (
        'iifname "ve-web" reject with icmpx type admin-prohibited comment "web: not the host"',
        'iifname "ve-web" ip daddr { 168.63.129.16, 169.254.169.254 } meta l4proto tcp reject '
        'with tcp reset comment "web: cloud metadata"',
        'iifname "ve-web" ip daddr 1.1.1.1 tcp dport 853 accept comment "web"',
        'iifname "ve-web" ip daddr 1.0.0.1 tcp dport 853 meta mark set 0x00000002 '
        'meta mark @current_mode ct mark set meta mark accept comment "web: maintenance"',
        'oifname "ve-web" ct status dnat udp dport 9999 accept comment "web"',
        'oifname "ve-web" ct status dnat jump mode_input',
        'tcp dport 8082 ct mark set 0x00000002 accept comment "maintenance: web"',
        'udp dport 9999 dnat ip to 169.254.132.218 comment "web"',
        'ip saddr 169.254.132.218 oifname != "ve-web" masquerade comment "web"',
        'oifname "ve-web" ip daddr 169.254.132.218 tcp dport 8551 accept comment "web: serves"',
    ):
        assert f"\t\t{line}\n" in conf, line
    assert "dport 8082 accept" not in conf


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (
            lambda: Image(
                "box",
                services=[Service("a", ["/usr/bin/a"], expose=[TCP(8081)])],
                components=[web()],
            ),
            "which a.service takes too",
        ),
        (
            lambda: Image("box", components=[web(modes={"debug": Egress(ports=[TCP(443)])})]),
            "names 'debug'",
        ),
    ],
)
def test_a_networked_container_refuses_what_the_firewall_cannot_render(
    image: Callable[[], Image], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        plan(image()).file("/etc/nftables.conf")


def test_a_profile_container_is_that_profile_s_dependency_only(tmp_path: Path) -> None:
    image = Image("box", profiles={"tenant": Profile(add=[web()])})
    out = tree(image, tmp_path / "tree")
    assert (out / "mkosi.images/web/mkosi.conf").is_file()
    assert "Dependencies=web" not in read(out, "payload/mkosi.conf")
    overlay = read(out, "payload/mkosi.profiles/tenant/mkosi.conf")
    assert overlay.startswith("[Config]\nDependencies=web\n")
    assert "ExtraTrees=%O/web.raw:/usr/lib/tundravm/containers/web.raw" in overlay


@pytest.mark.parametrize(
    ("argv", "profile", "reported"),
    [
        (["/bin/sh", "-c", "httpd"], "production", True),
        (["/usr/bin/busybox", "sh"], "production", True),
        (["/bin/sh", "-c", "httpd"], "dev", False),
        (HTTPD, "production", False),
    ],
)
def test_container_shell_refuses_a_production_shell(
    argv: list[str], profile: str, reported: bool
) -> None:
    container = Container("web", argv, packages=["busybox"])
    image = Image("box", components=[container], dev=DevAccess(password="tdx"))
    found = {d.code: d.severity for d in plan(image, profile).lint() if d.profile == profile}
    assert (found.get("container-shell") == "error") is reported


SERVE = File.text("/usr/bin/serve", "#!/bin/sh\nexec busybox httpd -f\n", mode=0o755)


@pytest.mark.parametrize(
    ("container", "missing"),
    [
        (web(), False),
        (Container("web", ["/usr/bin/serve"], packages=["busybox"], files=[SERVE]), False),
        (web(packages=["curl"]), True),
    ],
    ids=["its-package-ships-it", "its-files-ship-it", "nothing-in-its-root-ships-it"],
)
def test_a_container_s_program_is_looked_up_in_its_own_root(
    container: Container, missing: bool
) -> None:
    image = Image("box", packages=["busybox"], components=[container])
    found = [d for d in plan(image).lint() if d.code == "service-program-missing"]
    assert [
        ("nothing in its root ships" in d.message, "packages=[" in (d.hint or "")) for d in found
    ] == ([(True, True)] if missing else [])


@pytest.mark.parametrize(
    ("container", "rows"),
    [
        (
            web(expose=[]),
            ["container:web\nprobe_container web.service " + ROOT + " 192.0.2.1 9"],
        ),
        (
            web(
                expose=[UDP(9999)],
                egress=Egress(hosts=["1.1.1.1:853"]),
                modes={"maintenance": Egress(hosts=["1.0.0.1:853"])},
                requires=[RPC.endpoint(8545)],
                serves=[TCP(8551)],
            ),
            [
                "container:web\nprobe_container web.service " + ROOT + " veth 169.254.132.218",
                "container:web:egress\nprobe_container_egress web.service 1.1.1.1:853 "
                "1.0.0.1:853 169.254.169.254:80",
                "container:web:endpoint:8545\nprobe_container_endpoint web.service 127.0.0.1 "
                "8545 web-endpoint-8545.socket",
                "container:web:serves:8551\nprobe_container_serves 169.254.132.218 8551",
                "container:web:udp:9999\nprobe_container_udp web.service 9999",
            ],
        ),
    ],
    ids=["lo-only", "veth"],
)
def test_the_probe_checks_the_container_in_a_development_profile(
    container: Container, rows: list[str]
) -> None:
    image = Image(
        "box", services=[RPC], components=[container, WINDOW], dev=DevAccess(password="tdx")
    )
    script = plan(image, "dev").file("/usr/lib/tundravm/probe").text
    for row in rows:
        assert f"check={row}\n" in script, row


def test_the_manifest_records_each_container_root(tmp_path: Path) -> None:
    output = tmp_path / "mkosi.output"
    output.mkdir()
    (output / "web.raw").write_bytes(b"root")
    (output / "web.manifest").write_text("{}", encoding="utf-8")
    spec = ContainerSpec("web", ("busybox",))
    state = SimpleNamespace(effective_profile=lambda name: SimpleNamespace(containers=[spec]))
    artifact: Any = SimpleNamespace(path=output / "qemu.raw")
    img: Any = SimpleNamespace(state=state)
    out = tmp_path / "build"
    out.mkdir()
    found = _container_roots(img, "default", [artifact], out, set())
    assert found["web"]["path"] == "containers/web.raw"
    assert found["web"]["packages"] == "containers/web.manifest"
    assert found["web"]["size"] == 4
    assert (out / "containers/web.raw").read_bytes() == b"root"


SEARCHER = Login(
    "searcher", keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK0 me@host"], shell="/bin/sh"
)


@pytest.mark.parametrize(
    ("beside", "message"),
    [
        (
            Container("searcher", HTTPD, packages=["busybox"]),
            "Login('searcher') and Container('searcher')",
        ),
        (web(user="searcher"), "both declare the account 'searcher'"),
        (Service("searcher", ["/usr/bin/true"]), "the account Login('searcher') declares"),
        (Service("app", ["/usr/bin/true"], user="searcher"), "could change the unit's process"),
        (Service("api", ["/usr/bin/true"]), "the account Container('web') declares"),
    ],
)
def test_an_account_belongs_to_one_declaration(beside: Any, message: str) -> None:
    owned = message.endswith("Container('web') declares")
    first = web(user="api") if owned else SshAccess(login=SEARCHER)
    services = [beside] if isinstance(beside, Service) else []
    components = [first] if services else [first, beside]
    with pytest.raises(ValidationError, match=re.escape(message)) as raised:
        compile_image(Image("box", components=components, services=services))
    assert raised.value.hint


def neighbours() -> Any:
    tenant = Login("tenant", owner=True, commands={"status": Command(runs=["/usr/bin/true"])})
    container = web(state=data.directory("web"), expose=[])
    reader = Service(
        "reader",
        ["/usr/bin/reader", data.directory("web") / "bot.log"],
        groups=[container.group],
        state=data.directory("out", group="tenant"),
    )
    image = Image("box", components=[container, SshAccess(login=tenant)], services=[reader])
    return plan(image, "production")


@pytest.mark.parametrize(
    ("where", "line"),
    [
        ("/usr/lib/systemd/system/web.service.d/20-container.conf", "PrivatePIDs=yes"),
        ("/usr/lib/systemd/system/web.service.d/20-container.conf", "ProtectProc=invisible"),
        ("/usr/lib/systemd/system/web.service", "UMask=0027"),
        ("/usr/lib/systemd/system/reader.service", "SupplementaryGroups=web tenant"),
        ("/usr/lib/systemd/system/reader.service", "UMask=0027"),
        ("volume-data", "/usr/bin/install -d -o reader -g tenant -m 2750 /persistent/data/out"),
        ("volume-data", "/usr/bin/install -d -o web -g web -m 0750 /persistent/data/web"),
    ],
)
def test_a_host_service_beside_a_container_reads_it_through_a_group(where: str, line: str) -> None:
    """The tenant sees no host process and writes what its group reads; a host service joins
    that group instead of taking the tenant's uid, and a group= directory it writes is
    readable by the group (a Login's own here) without root."""
    planned = neighbours()
    if where.startswith("/"):
        assert line in planned.file(where).text.splitlines()
    else:
        (step,) = [step for step in planned.init_steps if step.name == where]
        assert line in step.script.splitlines()
    assert set(planned.groups) & {"tenant", "web"} == {"web"}


KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl me@host"
MENU = {"status": Command(runs=["/usr/bin/true"])}


@pytest.mark.parametrize(
    ("login", "volume", "outcome"),
    [
        (Login("tenant", owner=True, commands=MENU), True, "    owner: true\n"),
        (None, True, "    owner: true\n"),
        (Login("tenant", keys=[KEY], commands=MENU), True, "hands it to no account"),
        (None, False, "no attested endpoint"),
    ],
)
def test_a_containers_sshd_takes_the_owners_key(
    login: Login | None, volume: bool, outcome: str
) -> None:
    """ContainerSsh(owner=True) takes the key attest hands over, as Login(owner=True) does:
    the endpoint writes it to the container's keys directory too, and no secret is asked."""
    container = Container(
        "web",
        packages=["busybox"],
        ssh=ContainerSsh(port=10022, owner=True),
        state=data.directory("web") if volume else None,
    )
    access = [] if login is None else [SshAccess(login=login)]
    image = Image("box", components=[container, *access])
    if not outcome.startswith("    "):
        with pytest.raises(ValidationError, match=outcome):
            plan(image, "production")
        return
    planned = plan(image, "production")
    config = planned.file("/etc/tdx/secrets.yaml").text
    assert '  - name: "web"\n' in config and outcome in config
    dropin = planned.file("/usr/lib/systemd/system/web.service.d/20-container.conf").text
    assert "/run/tundravm/containers/web/keys:/etc/ssh/tundravm/keys" in dropin
    assert "credentials: []\n" in config


@pytest.mark.parametrize(
    ("ingress", "rule"),
    [
        ([ContainerSsh(port=10022, owner=True)], "tcp dport 10022 ct mark set 0x00000002 accept"),
        ([TCP(10022)], "tcp dport 10022 ct mark set 0x00000002 accept"),
    ],
)
def test_a_mode_opens_a_containers_sshd_by_naming_it(ingress: list[Any], rule: str) -> None:
    assert Mode(ingress=ingress).ingress == (TCP(10022),)
    container = web(state=data.directory("web"), ssh=ContainerSsh(port=10022, owner=True))
    window = Firewall(modes={"production": Mode(), "maintenance": Mode(ingress=[container.ssh])})
    image = Image("box", components=[container, window, SshAccess(from_delivery=True)])
    assert rule in plan(image, "production").file("/etc/nftables.conf").text


@pytest.mark.parametrize(
    ("bind", "line"),
    [
        (
            Bind(data.directory("input"), writable=True),
            "-o feeder -g web -m 2770 /persistent/data/input",
        ),
        (Bind(data.directory("input")), "-o feeder -g web -m 2750 /persistent/data/input"),
        (Bind(data.directory("logs")), "-o feeder -g web -m 2750 /persistent/data/logs"),
        (
            Bind(data.directory("drop"), writable=True),
            "-o web -g web -m 2770 /persistent/data/drop",
        ),
    ],
)
def test_a_bound_volume_directory_is_one_the_containers_group_traverses(
    bind: Bind, line: str
) -> None:
    """The directory keeps its service as owner and takes the container's group, with the
    mode the bind needs: no raw mode= on the directory."""
    feeder = Service(
        "feeder", ["/usr/bin/feeder"], state=data.directory("input"), logs=data.directory("logs")
    )
    image = Image("box", components=[web(binds=[bind])], services=[feeder])
    planned = plan(image, "production")
    (step,) = [step for step in planned.init_steps if step.name == "volume-data"]
    assert f"/usr/bin/install -d {line}" in step.script.splitlines()
    assert (
        "runtime-init-volume-data.service"
        in planned.file("/usr/lib/systemd/system/web.service").text
    )
    dropin = planned.file("/usr/lib/systemd/system/web.service.d/20-container.conf").text
    path = bind.target
    assert f"{bind.directive}={path}:{path}" in dropin


class Node(Component):
    """A node exporting its RPC, as Nethermind does."""

    @property
    def json_rpc(self) -> Endpoint:
        return self.service("node").endpoint(8545)

    def compose(self) -> Iterator[object]:
        yield Service("node", ["/usr/bin/node"], readiness=TcpProbe(8545))


@pytest.mark.parametrize(
    ("name", "endpoint", "relay", "user"),
    [
        ("bob", Node().json_rpc, "bob-node-json-rpc", "bob-node-json-rpc"),
        ("bob", RPC.endpoint(8545), "bob-endpoint-8545", "bob-endpoint-8545"),
        ("a" * 20, Node().json_rpc, f"{'a' * 20}-node-json-rpc", None),
    ],
)
def test_a_relay_is_named_after_the_export_it_carries(
    name: str, endpoint: Endpoint, relay: str, user: str | None
) -> None:
    container = Container(name, ["/usr/bin/app"], packages=["busybox"], requires=[endpoint])
    image = Image("box", components=[container, Node()], services=[RPC])
    unit = plan(image).file(f"/usr/lib/systemd/system/{relay}.service").text
    found = next(line for line in unit.splitlines() if line.startswith("User="))
    assert found == f"User={user}" if user else len(found) == len("User=") + 32
