"""``Container``: a program in a Debian root of its own, built as a subimage with the image."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tundravm import File, Image, Profile, Service
from tundravm._image import compile as compile_image
from tundravm.builds import _container_roots
from tundravm.components import Endpoint
from tundravm.components.access import DevAccess, Login, SshAccess
from tundravm.components.container import Bind, Container, ContainerSsh
from tundravm.components.network import Egress, Firewall, Mode, Network
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
        (lambda: Container("web", ["busybox", "httpd"], packages=["busybox"]), "program path"),
        (lambda: Container("Web", HTTPD, packages=["busybox"]), "not a container name"),
        (lambda: ContainerSsh(port=22, keys=Secret("web-keys")), "only root binds below 1024"),
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
        "PrivateNetwork=yes\nPrivateUsers=yes\nRootImage=/usr/lib/tundravm/containers/web.raw\n"
    )
    assert "WorkingDirectory=/persistent/data/web\n" in read(units, "web.service")
    loader = read(units, "web-loop.service")
    assert "ExecStart=/usr/sbin/modprobe loop\n" in loader
    assert "ConditionPathExists=!/dev/loop-control\n" in loader
    assert "User=" not in loader
    assert not (units / "web-net.service").exists()


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


def test_a_networked_container_lowers_to_a_veth_the_firewall_forwards() -> None:
    planned = plan(networked())
    dropin = planned.file("/usr/lib/systemd/system/web.service.d/20-container.conf").text
    assert dropin == (
        "[Unit]\nAfter=web-loop.service web-net.service web-endpoint-8545.socket\n"
        "Requires=web-loop.service web-net.service web-endpoint-8545.socket\n\n"
        "[Service]\nMountAPIVFS=yes\nNetworkNamespacePath=/run/netns/web\nPrivateUsers=yes\n"
        "RootImage=/usr/lib/tundravm/containers/web.raw\n"
    )
    net = planned.file("/usr/lib/systemd/system/web-net.service").text
    for line in (
        "DefaultDependencies=no\nRequires=nftables.service\nAfter=nftables.service\n",
        "ExecStart=/usr/bin/ip link add ve-web type veth peer name host0 netns web\n",
        "ExecStart=/usr/bin/ip -n web route add default via 169.254.132.217\n",
        "ExecStart=/bin/sh -c 'echo 1 >/proc/sys/net/ipv4/ip_forward'\n",
    ):
        assert line in net, line
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


@pytest.mark.parametrize(
    ("image", "reported"),
    [
        (Image("box", services=[Service("web", HTTPD)]), "nothing in the image ships"),
        (Image("box", components=[web()]), ""),
        (
            Image("box", packages=["busybox"], components=[web(packages=["curl"])]),
            "nothing in its root ships",
        ),
    ],
    ids=["host-missing", "container-root-ships-it", "container-root-lacks-it"],
)
def test_a_container_s_program_is_looked_up_in_its_own_root(image: Image, reported: str) -> None:
    found = [d for d in plan(image).lint() if d.code == "service-program-missing"]
    assert [reported in d.message for d in found] == ([True] if reported else [])


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
