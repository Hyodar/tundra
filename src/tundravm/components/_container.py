"""``Container``: a program in a Debian root of its own, built with the image.

``docs/design/container-2026-11-03.md`` and ``container-net-2026-11-03.md`` are the design.
The root is the subimage ``mkosi.images/<name>/`` (a
:class:`~tundravm.declarative.model.ContainerRoot`), built from the image's snapshot and
shipped in the measured payload; the program is a :class:`~tundravm.Service` whose drop-in
runs it in that root image, in a user and a network namespace of its own. With ``expose=``
or ``egress=`` the namespace has a veth the firewall forwards (DNAT in, its egress out);
each ``requires=`` endpoint is a socket in the namespace relayed to the host's loopback.
"""

from __future__ import annotations

import hashlib
import ipaddress
import posixpath
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import field, replace

from tundravm._image._component import Component, Endpoint, ServiceReady
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import (
    TCP,
    UDP,
    Egress,
    File,
    KernelFragment,
    PortValue,
    TcpProbe,
)
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Condition, Hardening, Root
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError
from tundravm.kernel_modules import CONTAINER_NETWORK_PATH
from tundravm.models import CONTAINERS_PATH

from ._base import egress_rules

__all__ = ["Container"]

PROXY = "/usr/lib/systemd/systemd-socket-proxyd"
"""What relays a socket in the container's namespace to a host endpoint (the ``systemd``
package's, in every image)."""
IP = "/usr/bin/ip"
"""What makes the namespace and the veth (``iproute2``, which a networked container adds)."""
NETNS = "/run/netns"
"""Where ``ip netns add`` names a namespace, which the units join by path."""
PEER = "host0"
"""The veth's end inside the container, as nspawn names it."""
_RESERVED = frozenset({0, 169, 255})
"""Third octets of 169.254.0.0/16 no container takes: RFC 3927's reserved first and last
/24, and the cloud metadata server's."""
LOAD_LOOP = ("/usr/sbin/modprobe", "loop")
"""What loads ``loop`` before ``RootImage=`` attaches the root, as its own root unit: no
device asks for it, the debloated image makes no static ``/dev/loop-control`` node that would
load it on open, and nothing run by the container's unit can, since systemd attaches the
image first for every command of it (a ``+`` one too) and fails with ``226/NAMESPACE``."""
LOOP_PRESENT = Condition.path_exists("/dev/loop-control", negate=True)
"""Skips the loader where ``loop`` is already there: a built ``Kernel`` has it built in and
no ``modprobe``, and devtmpfs makes the node once the driver registers. Not
``ConditionKernelModuleLoaded=``, which systemd 258 added and 257, Debian's, ignores with a
warning, so the unit would still run ``modprobe``."""
SHELLS = frozenset({"sh", "bash", "dash", "ash"})
"""The program names ``container-shell`` reads as a shell."""

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+")


def _fail(message: str, *, hint: str) -> ValidationError:
    return ValidationError(f"Container: {message}", hint=hint)


class Container(Component):
    """*argv* run in a Debian root of its own: *packages* from the image's snapshot and
    *files*, built with the image as a read-only root image the payload ships, so the quote
    covers it, and nothing is fetched at boot.

    ``Container("web", ["/usr/bin/busybox", "httpd", "-f", "-p", "8081"],
    packages=["busybox"], expose=[TCP(8081)])``. The keywords mean what they mean on a
    :class:`~tundravm.Service`: the program runs as *user* (default: a system user named
    after the container), under the ``static`` sandbox, in a user namespace
    (``PrivateUsers=yes``) and a network namespace of its own; *state* is one volume
    directory, which it sees at the same path. Each port of *expose* (``TCP`` or ``UDP``)
    is DNATed from the host to the container through its veth, and a firewall mode naming
    it opens and closes it. *egress* is what it alone may send: the forward chain accepts
    exactly its destinations (the metadata servers only with ``metadata=True``) and refuses
    the rest. *modes* map a ``Firewall`` mode's name to what it may send in that mode only,
    beyond *egress*: a flow it opened drops once another mode is in force. Each of
    *requires* is a host endpoint on the loopback, with readiness (``nethermind.rpc``): the
    container reaches it at the same address, through a socket in its namespace, and starts
    once it is ready. Each port of *serves* is one the host reaches it on, at its veth
    address only (:meth:`endpoint`), and *probe* (a ``TcpProbe`` of one of them) is
    what a host service that requires that endpoint waits for. With none of these its
    namespace holds ``lo`` only. Component name: *name*, which names its unit too.
    """

    name: str
    argv: Sequence[str]
    packages: Sequence[str] = ()
    files: Sequence[File] = ()
    state: VolumeDirectory | None = None
    user: str | None = None
    expose: Sequence[PortValue] = ()
    egress: Egress = field(default_factory=Egress)
    requires: Sequence[Endpoint] = ()
    modes: Mapping[str, Egress] = field(default_factory=FrozenMap)
    serves: Sequence[PortValue] = ()
    probe: TcpProbe | None = None

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        packages: Sequence[str] = (),
        files: Sequence[File] = (),
        state: VolumeDirectory | None = None,
        user: str | None = None,
        expose: Sequence[PortValue] = (),
        egress: Egress | None = None,
        requires: Sequence[Endpoint] = (),
        modes: Mapping[str, Egress] | None = None,
        serves: Sequence[PortValue] = (),
        probe: TcpProbe | None = None,
    ) -> None:
        given = {
            "name": name,
            "argv": argv,
            "packages": packages,
            "files": files,
            "state": state,
            "user": user,
            "expose": expose,
            "egress": Egress() if egress is None else egress,
            "requires": requires,
            "modes": FrozenMap() if modes is None else modes,
            "serves": serves,
            "probe": probe,
        }
        for keyword, value in given.items():
            object.__setattr__(self, keyword, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        for keyword in ("argv", "packages", "files", "expose", "requires", "serves"):
            value = getattr(self, keyword)
            if isinstance(value, (str, bytes, TCP, UDP, File, Endpoint)) or not isinstance(
                value, Sequence
            ):
                raise _fail(f"{keyword}= must be a list, got {value!r}.", hint=f"{keyword}=[...]")
            object.__setattr__(self, keyword, tuple(value))
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise _fail(
                f"{self.name!r} is not a container name.",
                hint='Use lower-case letters, digits and "-": Container("web", [...]).',
            )
        self._check_argv()
        self._check_packages()
        for item in self.files:
            if not isinstance(item, File):
                raise _fail(
                    f"files= holds {item!r}, not a File.", hint='File.text("/srv/a", "...")'
                )
            if item.owner is not None:
                raise _fail(
                    f"File {item.path!r} names owner={item.owner!r}, but a container's root "
                    "is read-only and every file in it is root's.",
                    hint="Drop owner=; what the program writes goes to its state= directory.",
                )
        if self.state is not None and not isinstance(self.state, VolumeDirectory):
            raise _fail(
                f"state= holds {self.state!r}, not a volume directory.",
                hint='state=data.directory("web"): a container root keeps nothing across boots.',
            )
        if self.user == "root":
            raise _fail(
                'user="root" runs the program as root, which owns every file it binds.',
                hint="Leave user= out: the program runs as a system user named after it.",
            )
        self._check_network()

    def _check_network(self) -> None:
        for keyword in ("expose", "serves"):
            seen: set[tuple[int, str]] = set()
            for port in getattr(self, keyword):
                if not isinstance(port, (TCP, UDP)):
                    raise _fail(
                        f"{keyword}= holds {port!r}, not TCP(...) or UDP(...).",
                        hint=f"{keyword}=[TCP(8081)]",
                    )
                if (port.port, port.protocol) in seen:
                    raise _fail(f"{keyword}= names {port!r} twice.", hint="Name each port once.")
                seen.add((port.port, port.protocol))
        if not isinstance(self.egress, Egress):
            raise _fail(f"egress= holds {self.egress!r}, not an Egress.", hint="egress=Egress()")
        self._check_modes()
        if self.probe is not None:
            probe = self.probe
            served = {port.port for port in self.serves if isinstance(port, TCP)}
            if not isinstance(probe, TcpProbe) or probe.host != "127.0.0.1":
                raise _fail(
                    f"probe= holds {probe!r}: the host probes the container at its veth "
                    "address, so only a TcpProbe of a port it serves applies.",
                    hint="serves=[TCP(8551)], probe=TcpProbe(8551)",
                )
            if probe.port not in served:
                raise _fail(
                    f"probe=TcpProbe({probe.port}) probes a port serves= does not name, "
                    "which the host cannot reach.",
                    hint=f"serves=[TCP({probe.port})]",
                )
        ports: set[int] = set()
        for item in self.requires:
            if not isinstance(item, Endpoint):
                raise _fail(
                    f"requires= holds {item!r}, not an Endpoint.",
                    hint="requires=[nethermind.rpc]: an endpoint a component exports.",
                )
            if item.ready is None or item.ready.probe is None:
                raise _fail(
                    f"requires= holds {item.address}, which carries no readiness to wait for.",
                    hint="Export it from a service with readiness=TcpProbe(...), HttpProbe(...) "
                    "or Notify(): service.endpoint(port).",
                )
            try:
                loopback = ipaddress.ip_address(item.host).is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise _fail(
                    f"requires= holds {item.address}, which is not the host's loopback: it is a "
                    "destination, not a host endpoint.",
                    hint=f'egress=Egress(hosts=["{item.address}"])',
                )
            if item.port in ports:
                raise _fail(
                    f"requires= names port {item.port} twice.", hint="Require each endpoint once."
                )
            ports.add(item.port)

    def _check_argv(self) -> None:
        if not self.argv:
            raise _fail("argv= is empty, so nothing runs.", hint='argv=["/usr/bin/app", "--flag"]')
        for word in self.argv:
            if not isinstance(word, str) or "\n" in word:
                raise _fail(f"argv= holds {word!r}, not one line of text.", hint="Pass strings.")
        if not self.argv[0].startswith("/"):
            raise _fail(
                f"argv= starts with {self.argv[0]!r}, which is not a program path.",
                hint=f'argv=["/usr/bin/{self.argv[0]}", ...]: systemd searches no $PATH.',
            )

    def _check_packages(self) -> None:
        if not self.packages:
            raise _fail(
                "packages= is empty: the root holds the Debian packages it names, and its "
                "program comes from one.",
                hint='packages=["busybox"]',
            )
        for package in self.packages:
            if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
                looks_oci = isinstance(package, str) and any(c in package for c in ":/@")
                raise _fail(
                    f"packages= holds {package!r}, "
                    + (
                        "an OCI image reference: a container root is Debian packages from "
                        "the image's snapshot, never an image pulled by tag or digest."
                        if looks_oci
                        else "which is not a Debian package name."
                    ),
                    hint='packages=["busybox"]: the packages its program needs.',
                )

    def _check_modes(self) -> None:
        if not isinstance(self.modes, Mapping):
            raise _fail(
                f"modes= holds {self.modes!r}, not a map of mode names to Egress.",
                hint='modes={"maintenance": Egress(ports=[TCP(443)])}',
            )
        object.__setattr__(self, "modes", FrozenMap(dict(self.modes)))
        for mode, egress in self.modes.items():
            if not isinstance(egress, Egress):
                raise _fail(
                    f"modes[{mode!r}] holds {egress!r}, not an Egress.",
                    hint=f'modes={{"{mode}": Egress(ports=[TCP(443)])}}',
                )
            if egress.metadata:
                raise _fail(
                    f"modes[{mode!r}] reaches the cloud metadata server in a window only, but "
                    "the token it hands out outlives the window.",
                    hint="Put Egress(metadata=True) in egress=, or drop it.",
                )

    def endpoint(self, port: int, *, path: str = "/", scheme: str = "http") -> Endpoint:
        """Where the container answers the host on *port*, a port of *serves*: its veth
        address, which nothing outside the host reaches, ready once *probe* connects
        (``lighthouse = Lighthouse(execution=searcher.endpoint(8551))``)."""
        network = self.network()
        if network is None or port not in {p.port for p in self.serves if isinstance(p, TCP)}:
            raise _fail(
                f"endpoint({port}) names a port serves= does not, so the host cannot reach it.",
                hint=f"Container({self.name!r}, ..., serves=[TCP({port})])",
            )
        ready = ServiceReady(self.unit, self.probe)
        return Endpoint(network.address, port, path, scheme, ready=ready, provider=self)

    @property
    def unit(self) -> str:
        """The container's ``.service``."""
        return f"{self.name}.service"

    @property
    def image(self) -> str:
        """Where the payload ships its root image."""
        return f"{CONTAINERS_PATH}/{self.name}.raw"

    @property
    def loader(self) -> str:
        """The root oneshot that loads ``loop`` before the unit attaches its root image,
        unless ``/dev/loop-control`` is already there."""
        return f"{self.name}-loop.service"

    @property
    def namespace(self) -> str | None:
        """The ``<name>-net.service`` that makes its namespace, ``None`` when it holds ``lo``
        only (``PrivateNetwork=yes``)."""
        if not (self.veth or self.requires):
            return None
        return f"{self.name}-net.service"

    @property
    def veth(self) -> bool:
        """Whether its namespace has a veth: it exposes a port or sends something."""
        egress = self.egress
        return (
            bool(self.expose or self.serves or self.modes or egress.ports or egress.hosts)
            or egress.metadata
        )

    def network(self) -> dm.ContainerNetwork | None:
        """Its veth: ``ve-<name>`` (``ve-<first 7>-<4 hex>`` past 12 characters) and a /30 of
        169.254.0.0/16 the name's sha256 picks, outside :data:`_RESERVED`."""
        if not self.veth:
            return None
        digest = hashlib.sha256(self.name.encode()).digest()
        octets = [n for n in range(256) if n not in _RESERVED]
        slot = int.from_bytes(digest[:4], "big") % (len(octets) * 64)
        base = f"169.254.{octets[slot // 64]}"
        quad = (slot % 64) * 4
        interface = self.name if len(self.name) <= 12 else f"{self.name[:7]}-{digest.hex()[:4]}"
        return dm.ContainerNetwork(
            f"ve-{interface}",
            f"{base}.{quad + 1}",
            f"{base}.{quad + 2}",
            egress_rules=egress_rules(((self.name, self.egress),)),
            expose=tuple(dm.Port(port.port, port.protocol) for port in self.expose),
            modes=tuple(
                (mode, rule)
                for mode, egress in self.modes.items()
                for rule in egress_rules(((f"{self.name}: {mode}", egress),))
            ),
            serves=tuple(dm.Port(port.port, port.protocol) for port in self.serves),
        )

    def kernel(self) -> tuple[KernelFragment, ...]:
        """With a veth, what a built kernel needs for it: veth and the firewall's nat."""
        if not self.veth:
            return ()
        options = ("CONFIG_VETH", "CONFIG_NF_NAT", "CONFIG_NFT_NAT", "CONFIG_NFT_MASQ")
        return (
            KernelFragment(
                dict.fromkeys(options, "y"),
                reason=f"Container({self.name!r}): its veth, DNAT in and masquerade out",
            ),
        )

    def relay(self, endpoint: Endpoint) -> str:
        """The socket in its namespace that relays *endpoint*."""
        return f"{self.name}-endpoint-{endpoint.port}.socket"

    def compose(self) -> Iterator[object]:
        yield dm.Task(
            self.loader.removesuffix(".service"),
            LOAD_LOOP,
            description=f"Load loop for {self.unit}'s root image",
            user=Root(),
            hardening=Hardening.none(),
            conditions=(LOOP_PRESENT,),
        )
        mounts = () if self.state is None else (self.state.path,)
        network = self.network()
        yield dm.ContainerRoot(
            self.name,
            tuple(self.packages),
            files=tuple(dm.File(item.path, item.content, item.mode) for item in self.files),
            directories=mounts,
            network=network,
            endpoints=tuple(endpoint.address for endpoint in self.requires),
        )
        directives: dict[str, str | tuple[str, ...]] = {
            "RootImage": self.image,
            "MountAPIVFS": "yes",
            "PrivateUsers": "yes",
        }
        units = [self.loader]
        if self.namespace is None:
            directives["PrivateNetwork"] = "yes"
        else:
            directives["NetworkNamespacePath"] = f"{NETNS}/{self.name}"
            units += [self.namespace, *(self.relay(endpoint) for endpoint in self.requires)]
            yield from self._namespace(network)
        if mounts:
            directives["BindPaths"] = tuple(f"{path}:{path}" for path in mounts)
        yield Service(
            self.name,
            list(self.argv),
            state=self.state,
            user=self.user,
            requires=list(self.requires),
            readiness=None
            if self.probe is None or network is None
            else replace(self.probe, host=network.address),
            unit=dm.DropIn(
                "20-container",
                requires=tuple(units),
                after=tuple(units),
                extra={"Service": directives},
            ),
        )
        for endpoint in self.requires:
            relay = self.relay(endpoint)
            yield Service(
                relay.removesuffix(".socket"),
                [PROXY, endpoint.address],
                socket=dm.Listen(endpoint.address),
                requires=[endpoint],
            )
            yield dm.File.text(
                f"/usr/lib/systemd/system/{relay}.d/20-container.conf",
                f"# tundravm Container {self.name}: the socket listens in its namespace\n"
                f"[Unit]\nRequires={self.namespace}\nAfter={self.namespace}\n\n"
                f"[Socket]\nNetworkNamespacePath={NETNS}/{self.name}\n",
            )

    def _namespace(self, network: dm.ContainerNetwork | None) -> Iterator[object]:
        """``<name>-net.service``, ``iproute2`` and, with a veth, the networkd file that
        leaves it alone."""
        assert self.namespace is not None
        yield dm.Package("iproute2")
        name = self.name
        start = [f"{IP} netns add {name}", f"{IP} -n {name} link set lo up"]
        stop = [f"{IP} netns delete {name}"]
        order = ""
        if network is not None:
            veth = network.interface
            start += [
                f"{IP} link add {veth} type veth peer name {PEER} netns {name}",
                f"{IP} addr add {network.host}/30 dev {veth}",
                f"{IP} link set {veth} up",
                f"{IP} -n {name} addr add {network.address}/30 dev {PEER}",
                f"{IP} -n {name} link set {PEER} up",
                f"{IP} -n {name} route add default via {network.host}",
                "/bin/sh -c 'echo 1 >/proc/sys/net/ipv4/ip_forward'",
            ]
            stop.insert(0, f"-{IP} link delete {veth}")
            order = "Requires=nftables.service\nAfter=nftables.service\n"
            yield dm.File.text(
                f"{CONTAINER_NETWORK_PATH}{veth.removeprefix('ve-')}.network",
                f"# tundravm Container {name}: {network.interface} is {self.namespace}'s\n"
                f"[Match]\nName={veth}\n\n[Link]\nUnmanaged=yes\n",
            )
        text = (
            f"# tundravm Container {name}: its network namespace"
            + (", a veth the firewall forwards and IPv4 forwarding" if network else "")
            + "\n[Unit]\n"
            f"Description=Network namespace of {self.unit}\n"
            "DefaultDependencies=no\n"
            f"{order}"
            "Conflicts=shutdown.target\nBefore=shutdown.target\n\n"
            "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            + "".join(
                f"ExecStartPre=-{command}\n"
                for command in (
                    *([f"{IP} link delete {network.interface}"] if network else []),
                    f"{IP} netns delete {name}",
                )
            )
            + "".join(f"ExecStart={command}\n" for command in start)
            + "".join(f"ExecStop={command}\n" for command in stop)
        )
        yield dm.Unit(self.namespace, text)

    rules = (
        Rule(
            "container-shell",
            "error",
            "A production profile's container runs a shell, so whoever reaches it gets a "
            "prompt in it.",
            'Run the program itself: Container("web", ["/usr/bin/app", ...]), or keep the '
            "container in a Profile(production=False).",
        ),
    )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``container-shell``: a production container whose program is a shell."""
        program = posixpath.basename(self.argv[0])
        shell = program in SHELLS or (
            program == "busybox" and len(self.argv) > 1 and self.argv[1] in SHELLS
        )
        if not resolved.production or not shell:
            return ()
        return (
            dm.Diagnostic(
                "container-shell",
                "error",
                f"Container({self.name!r}) runs {' '.join(self.argv[:2])} in the production "
                f"profile {resolved.profile!r}: a container runs its program, not a shell",
            ),
        )
