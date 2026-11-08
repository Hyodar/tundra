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
import zlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from typing import ClassVar, Literal

from tundravm._image._component import BootReady, Component, Endpoint, ServiceReady
from tundravm._image._metrics import UnitMetrics
from tundravm._image._secrets import Secret
from tundravm._image._service import Sandbox, Service, _sandbox
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
from tundravm._location import Located
from tundravm._modules import container_ssh
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Condition, Hardening, Root
from tundravm.declarative.utils import _SSH_PUBLIC_KEY as SSH_PUBLIC_KEY
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError
from tundravm.kernel_modules import CONTAINER_NETWORK_PATH
from tundravm.models import CONTAINERS_PATH

from ._network import egress_rules, pinned_hosts

__all__ = ["Bind", "Container", "ContainerSsh"]

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

UIDS = range(60578, 61184)
"""The ids a container's user takes: systemd's unused range, between the users nspawn maps
into containers and the dynamic ones, so no ``useradd --system`` and no Debian package
takes one (:func:`uid`)."""
SSHD_SANDBOX = Hardening.strict(deny_syscalls=["@privileged"])
"""The ``static`` sandbox with ``@resources`` allowed back: sshd's preauth sandbox calls
``setrlimit`` and exits when it fails."""

_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+")


def _fail(message: str, *, hint: str) -> ValidationError:
    return ValidationError(f"Container: {message}", hint=hint)


PROTECTED = ("/run/tundravm", "/proc", "/sys", "/dev", "/boot")
"""Host paths no ``Bind`` reaches into: the control plane (volume keys, credentials, other
containers' keys) and the kernel's interfaces."""
WHOLE = frozenset({"/", "/etc", "/usr", "/var", "/run", "/root", "/home"})
"""Host directories a ``Bind`` may reach into but not bind whole."""
_KEYS_HINT = "owner=True for the key attest hands over, or keys=[...] baked into the root."


def _group_readable(hardening: Sandbox) -> Hardening:
    """*hardening* writing group-readable files, for the services in the container's group."""
    return replace(_sandbox(hardening), umask="0027")


def uid(user: str) -> int:
    """The uid and gid of a container's *user*, the same on the host and in its root
    (``PrivateUsers=yes`` maps the unit's own ids to themselves): a hash of the name into
    :data:`UIDS`, so it survives a rebuild and the files it owns on a volume stay its."""
    return UIDS.start + zlib.crc32(user.encode()) % len(UIDS)


@dataclass(frozen=True, slots=True)
class ContainerSsh(Located):
    """The container's own sshd on *port*, letting its user in by key and with nothing
    else: ``Container("bob", ssh=ContainerSsh(port=10022, owner=True))``.

    *owner* takes the owner's key, the one ``attest`` hands over, as ``Login(owner=True)``
    does: the attested endpoint writes it for the container too, on the same terms. *keys*
    is instead a ``Secret`` (a public key list delivered after attestation) or public key
    lines baked into the root, as on a ``Login``. tundravm supplies the rest: an sshd
    config allowing that one user by key only (no password, no root, no forwarding), and a
    host key the host makes each boot, owned by the container's user and read-only to it,
    which the attested endpoint binds into every quote, labelled by the container's name,
    so ``tundravm attest`` pins it for ``ssh -p PORT``. *port* is 1024 or more: sshd runs
    as the container's user, unprivileged in its namespace.
    """

    port: int
    keys: Secret | Sequence[str] = ()
    owner: bool = False

    protocol: ClassVar[Literal["tcp"]] = "tcp"
    """What its port speaks, so ``Mode(ingress=[searcher.ssh])`` reads it as a port."""

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValidationError(
                f"ContainerSsh: port must be a number, got {self.port!r}.",
                hint="ContainerSsh(port=10022, owner=True)",
            )
        if not 1024 <= self.port <= 65535:
            raise ValidationError(
                f"ContainerSsh: port {self.port} is not one the container's user may bind: "
                "sshd runs as that user, and only root binds below 1024.",
                hint="Pick a port from 1024 to 65535: ContainerSsh(port=10022, ...).",
            )
        if not isinstance(self.owner, bool):
            raise ValidationError("ContainerSsh: owner= must be True or False.", hint="owner=True")
        if self.owner and (isinstance(self.keys, Secret) or self.keys):
            raise ValidationError(
                "ContainerSsh: owner=True makes the owner's key its key, and keys= names others.",
                hint="Drop keys=, or owner=True for keys of your own.",
            )
        if isinstance(self.keys, Secret):
            return
        if isinstance(self.keys, str) or not isinstance(self.keys, Sequence):
            raise ValidationError(
                "ContainerSsh: keys= takes a Secret or a list of public keys.", hint=_KEYS_HINT
            )
        object.__setattr__(self, "keys", tuple(self.keys))
        for key in self.keys:
            if not isinstance(key, str) or not SSH_PUBLIC_KEY.fullmatch(key.strip()):
                raise ValidationError(
                    f"ContainerSsh: {key!r} is not one OpenSSH public key line.",
                    hint="Paste the .pub file's line: 'ssh-ed25519 AAAA... comment'.",
                )
        if not self.keys and not self.owner:
            raise ValidationError(
                "ContainerSsh has no key to accept, so nobody could log in.", hint=_KEYS_HINT
            )

    @property
    def delivered(self) -> bool:
        """Whether the keys arrive after attestation (a ``Secret``, or the owner's) rather
        than baked in."""
        return self.owner or isinstance(self.keys, Secret)


@dataclass(frozen=True, slots=True)
class Bind(Located):
    """A host *path* the container sees at *at* (default: the same path), read-only unless
    *writable*: ``Bind("/var/log/lighthouse")``, ``Bind(data.directory("input"),
    writable=True)``. *file* binds one file rather than a directory (a socket, a config).
    The path must exist when the container starts; the root gains its mount point. A
    volume directory is made for it: its service keeps it, the container's group traverses
    it (``0750``) and writes it when *writable* (``0770``), and the container waits for the
    volume. A secret reaches a container as ``credentials=`` on a ``Service`` does, not as
    a bind.
    """

    path: str | VolumeDirectory
    _: KW_ONLY
    at: str | None = None
    writable: bool = False
    file: bool = False
    directory: VolumeDirectory | None = field(default=None, init=False)
    """The volume directory *path* named, which the image makes for the container."""

    def __post_init__(self) -> None:
        if isinstance(self.path, VolumeDirectory):
            if self.path.group is not None:
                raise ValidationError(
                    f"Bind: {self.path.path} shares group {self.path.group}, and a bind gives "
                    "it the container's group.",
                    hint="Bind a directory without group=; services beside the container "
                    "join its group (groups=[container.group]).",
                )
            if self.file:
                raise ValidationError(
                    f"Bind: {self.path.path} is a volume directory, not a file.",
                    hint="Drop file=True, or bind the file inside it: Bind(directory / 'name').",
                )
            object.__setattr__(self, "directory", self.path)
            object.__setattr__(self, "path", self.path.path)
        for keyword in ("path", "at"):
            value = getattr(self, keyword)
            if value is None and keyword == "at":
                continue
            if not isinstance(value, str) or not value.startswith("/") or ":" in value:
                raise ValidationError(
                    f"Bind: {keyword}={value!r} is not an absolute path.",
                    hint='Bind("/var/log/app"), or at="/logs" to see it elsewhere.',
                )
            object.__setattr__(self, keyword, posixpath.normpath(value))
        for keyword in ("writable", "file"):
            if not isinstance(getattr(self, keyword), bool):
                raise ValidationError(
                    f"Bind: {keyword}= must be True or False.", hint=f"{keyword}=True"
                )

    @property
    def target(self) -> str:
        """Where the container sees it."""
        return self.at or str(self.path)

    @property
    def directive(self) -> str:
        """``BindPaths=`` for a writable bind, else ``BindReadOnlyPaths=``."""
        return "BindPaths" if self.writable else "BindReadOnlyPaths"


class Container(Component):
    """*argv* run in a Debian root of its own: *packages* from the image's snapshot and
    *files*, built with the image as a read-only root image the payload ships, so the quote
    covers it, and nothing is fetched at boot.

    ``Container("web", ["/usr/bin/busybox", "httpd", "-f", "-p", "8081"],
    packages=["busybox"], expose=[TCP(8081)])``. The keywords mean what they mean on a
    :class:`~tundravm.Service`: the program runs as *user* (default: the container's name),
    an account of its own with ids pinned from its name (:func:`uid`), which the root's
    ``passwd`` and ``group`` name too, under *hardening* (a ``Service``'s: the ``static``
    preset by default, ``"jit"`` for a runtime that writes code), in a user namespace
    (``PrivateUsers=yes``) and a network namespace of its own; *state* is one volume
    directory, which it sees at the same path. Each port of *expose* (``TCP`` or ``UDP``)
    is DNATed from the host to the container through its veth, and a firewall mode naming
    it opens and closes it. *egress* is what it alone may send: the forward chain accepts
    exactly its destinations (the metadata servers only with ``metadata=True``) and refuses
    the rest. *modes* map a ``Firewall`` mode's name to what it may send in that mode only,
    beyond *egress*: a flow it opened drops once another mode is in force. Every ``Host``
    of *egress* and *modes* is a line of the root's ``/etc/hosts``, in every mode: the root
    resolves nothing else, so a pinned peer is the only kind it reaches by name. Each of
    *requires* is a host endpoint on the loopback, with readiness (``nethermind.rpc``): the
    container reaches it at the same address, through a socket in its namespace, and starts
    once it is ready. Each port of *serves* is one the host reaches it on, at its veth
    address only (:meth:`endpoint`), and *probe* (a ``TcpProbe`` of one of them) is
    what a host service that requires that endpoint waits for. With none of these its
    namespace holds ``lo`` only. *binds* (:class:`Bind`) show host paths in it, read-only
    unless marked writable. *ssh* (:class:`ContainerSsh`) runs an sshd in it letting
    *user* in by key: as its program without *argv*, else beside it as
    ``<name>-ssh.service`` in its network namespace; its port is exposed as *expose*'s are.
    Component name: *name*, which names its unit too.
    """

    name: str
    argv: Sequence[str] = ()
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
    ssh: ContainerSsh | None = None
    hardening: Sandbox = "static"
    binds: Sequence[Bind] = ()

    def __init__(
        self,
        name: str,
        argv: Sequence[str] = (),
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
        ssh: ContainerSsh | None = None,
        hardening: Sandbox = "static",
        binds: Sequence[Bind] = (),
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
            "ssh": ssh,
            "hardening": hardening,
            "binds": binds,
        }
        for keyword, value in given.items():
            object.__setattr__(self, keyword, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        for keyword in ("argv", "packages", "files", "expose", "requires", "serves", "binds"):
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
        if self.ssh is not None and not isinstance(self.ssh, ContainerSsh):
            raise _fail(
                f"ssh= holds {self.ssh!r}, not a ContainerSsh.",
                hint="ssh=ContainerSsh(port=10022, owner=True)",
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
            if posixpath.normpath(item.path) == "/etc/hosts":
                raise _fail(
                    "files= writes /etc/hosts, which the container's Host pins render.",
                    hint="Pin each peer in egress= or modes=: "
                    'Egress(hosts=[Host("api.example", ["203.0.113.7"], port=443)]).',
                )
        if self.state is not None and not isinstance(self.state, VolumeDirectory):
            raise _fail(
                f"state= holds {self.state!r}, not a volume directory.",
                hint='state=data.directory("web"): a container root keeps nothing across boots.',
            )
        self._check_user()
        self._check_binds()
        _sandbox(self.hardening)
        if not self.argv and self.hardening != "static":
            raise _fail(
                "hardening= sandboxes the program, and without argv= the program is sshd, "
                "whose sandbox is tundravm's.",
                hint="Drop hardening=, or give argv= the program it is for.",
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
        if self.ssh is not None and (self.ssh.port, "tcp") in {
            (p.port, p.protocol) for p in self.expose
        }:
            raise _fail(
                f"expose= names port {self.ssh.port}, which ssh= exposes already.",
                hint=f"Drop TCP({self.ssh.port}) from expose=.",
            )
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
            if self.ssh is not None:
                return
            raise _fail("argv= is empty, so nothing runs.", hint='argv=["/usr/bin/app", "--flag"]')
        for word in self.argv:
            if not isinstance(word, str) or "\n" in word:
                raise _fail(f"argv= holds {word!r}, not one line of text.", hint="Pass strings.")
        if not self.argv[0].startswith("/"):
            raise _fail(
                f"argv= starts with {self.argv[0]!r}, which is not a program path.",
                hint=f'argv=["/usr/bin/{self.argv[0]}", ...]: systemd searches no $PATH.',
            )

    def _check_user(self) -> None:
        if self.user == "root":
            raise _fail(
                'user="root" runs the program as root, which owns every file it binds.',
                hint="Leave user= out: the program runs as an account named after it.",
            )
        if self.user is not None and (
            not isinstance(self.user, str) or not _USER.fullmatch(self.user)
        ):
            raise _fail(
                f"user={self.user!r} is not a user name.",
                hint='user="web": lower-case letters, digits, "-" and "_", at most 32.',
            )
        if self.user is None and len(self.name) > 32:
            raise _fail(
                f"the container's user would be {self.name!r}, longer than a user name.",
                hint='Pass user= a name of at most 32 characters: user="web".',
            )

    def _check_binds(self) -> None:
        for item in self.binds:
            if not isinstance(item, Bind):
                raise _fail(f"binds= holds {item!r}, not a Bind.", hint='binds=[Bind("/srv/a")]')
            path = str(item.path)
            if path in WHOLE or any(path == p or path.startswith(f"{p}/") for p in PROTECTED):
                raise _fail(
                    f"Bind({item.path!r}) shows the container a host path tundravm keeps from "
                    "it: the control plane, the kernel's interfaces, or a whole system tree.",
                    hint="Bind the one directory or file the program needs, such as "
                    "/var/log/app; a secret goes to it as credentials=, not a bind.",
                )
        targets = [item.target for item in self._binds(sshd=True)]
        if twice := sorted({t for t in targets if targets.count(t) > 1}):
            raise _fail(
                f"two binds land on {twice[0]} in the container.",
                hint="Give each Bind its own at=; state= and ssh= keep their own paths.",
            )
        for target in targets:
            if (
                target == "/"
                or target.startswith(("/proc/", "/sys/", "/dev/"))
                or (target in ("/proc", "/sys", "/dev"))
            ):
                raise _fail(
                    f"a bind lands on {target}, which the container's namespace mounts itself.",
                    hint='Bind it elsewhere: at="/srv/name".',
                )

    def _binds(self, *, sshd: bool) -> tuple[Bind, ...]:
        """Every bind of a unit running in the root: *state* writable, *binds*, and for
        sshd (*sshd*) the host key directory and the delivered keys, read-only."""
        found: list[Bind] = []
        if self.state is not None:
            found.append(Bind(self.state.path, writable=True))
        found.extend(self.binds)
        ssh = self.ssh
        if sshd and ssh is not None:
            found.append(Bind(container_ssh.host_directory(self.name), at=container_ssh.HOST_KEYS))
            if ssh.delivered:
                where = container_ssh.keys_directory(self.name)
                found.append(Bind(where, at=container_ssh.AUTHORIZED_KEYS))
        return tuple(found)

    def _volumes(self) -> list[BootReady]:
        """The readiness of each volume a bound directory is on, once."""
        found = [item.directory.ready for item in self.binds if item.directory is not None]
        return list(dict.fromkeys(found))

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
    def exposed(self) -> tuple[PortValue, ...]:
        """The ports DNATed to it: *expose*'s, and its sshd's."""
        ssh = () if self.ssh is None else (TCP(self.ssh.port),)
        return (*self.expose, *ssh)

    @property
    def veth(self) -> bool:
        """Whether its namespace has a veth: it exposes a port or sends something."""
        egress = self.egress
        return (
            bool(self.exposed or self.serves or self.modes or egress.ports or egress.hosts)
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
            expose=tuple(dm.Port(port.port, port.protocol) for port in self.exposed),
            modes=tuple(
                (mode, rule)
                for mode, egress in self.modes.items()
                for rule in egress_rules(((f"{self.name}: {mode}", egress),))
            ),
            serves=tuple(dm.Port(port.port, port.protocol) for port in self.serves),
        )

    @property
    def run_user(self) -> str:
        """The account the program runs as: *user*, else the container's name."""
        return self.user or self.name

    @property
    def group(self) -> str:
        """The group a host service joins to read what the container writes
        (``Service(..., groups=[searcher.group])``): its account's own, whose gid :func:`uid`
        pins. The container writes group-readable files (``UMask=0027``), so a service in it
        reads them without running as the container's uid."""
        return self.run_user

    @property
    def sshd_unit(self) -> str | None:
        """The unit running sshd: the container's own without *argv*, ``<name>-ssh``
        beside it with one; ``None`` without *ssh*."""
        if self.ssh is None:
            return None
        return f"{self.name}-ssh.service" if self.argv else self.unit

    def _account(self) -> Iterator[dm.Declaration]:
        """The container's own group and user on the host, their ids pinned."""
        user = self.run_user
        yield dm.Group(user, gid=uid(user))
        owner = f"Container({self.name!r})"
        yield dm.User(user, uid=uid(user), primary_group=user, owner=owner)

    def _host_key(self) -> Iterator[dm.Declaration]:
        """``ssh-keygen`` and the root oneshot making this boot's host key."""
        ids = uid(self.run_user)
        path = container_ssh.script_path(self.name)
        yield dm.Package(container_ssh.HOST_PACKAGE)
        yield dm.File(path, container_ssh.script(self.name, ids, ids), mode=0o755)
        yield dm.Task(
            container_ssh.unit(self.name).removesuffix(".service"),
            ("/bin/sh", path),
            description=f"Generate this boot's SSH host key for container {self.name}",
            user=Root(),
            timeout="60s",
            needs=(),
            hardening=Hardening.strict(
                protect_system="full", network="none", capabilities=["CAP_CHOWN"]
            ),
            directives={"Service": {"StandardOutput": "journal+console"}},
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
            description=f"Load loop for container {self.name}'s root image",
            user=Root(),
            hardening=Hardening.none(),
            conditions=(LOOP_PRESENT,),
        )
        yield from self._account()
        network = self.network()
        yield self._root(network)
        units = [self.loader]
        if self.namespace is not None:
            units += [self.namespace, *(self.relay(endpoint) for endpoint in self.requires)]
            yield from self._namespace(network)
        sshd = list(container_ssh.SSHD)
        yield Service(
            self.name,
            list(self.argv) or sshd,
            state=self.state,
            user=self.run_user,
            hardening=_group_readable(self.hardening if self.argv else SSHD_SANDBOX),
            requires=[*self.requires, *self._volumes()],
            readiness=None
            if self.probe is None or network is None
            else replace(self.probe, host=network.address),
            unit=self._dropin(self.unit, units),
            description=f"Container {self.name}",
        )
        if self.ssh is not None:
            yield from self._host_key()
            if self.argv:
                yield Service(
                    f"{self.name}-ssh",
                    sshd,
                    user=self.run_user,
                    requires=self._volumes(),
                    hardening=_group_readable(SSHD_SANDBOX),
                    unit=self._dropin(f"{self.name}-ssh.service", units),
                    description=f"SSH into container {self.name}",
                )
        for endpoint in self.requires:
            relay = self.relay(endpoint)
            yield Service(
                relay.removesuffix(".socket"),
                [PROXY, endpoint.address],
                socket=dm.Listen(endpoint.address),
                requires=[endpoint],
                description=f"Relay {endpoint.address} into container {self.name}",
            )
            yield dm.File.text(
                f"/usr/lib/systemd/system/{relay}.d/20-container.conf",
                f"# tundravm Container {self.name}: the socket listens in its namespace\n"
                f"[Unit]\nRequires={self.namespace}\nAfter={self.namespace}\n\n"
                f"[Socket]\nNetworkNamespacePath={NETNS}/{self.name}\n",
            )

    def _root(self, network: dm.ContainerNetwork | None) -> dm.ContainerRoot:
        """The root image: its packages and files, sshd's with *ssh*, its network, the
        peers its egress pins and the user with the ids the host pins."""
        binds = self._binds(sshd=True)
        mounts = tuple(item.target for item in binds if not item.file)
        points = tuple(item.target for item in binds if item.file)
        files = [dm.File(item.path, item.content, item.mode) for item in self.files]
        packages = list(self.packages)
        ssh = self.ssh
        if ssh is not None:
            packages.append(container_ssh.PACKAGE)
            config = container_ssh.config(self.run_user, ssh.port)
            files.append(dm.File(container_ssh.CONFIG, config, 0o644))
            if not ssh.delivered:
                assert not isinstance(ssh.keys, Secret)
                keys = "".join(f"{key.strip()}\n" for key in ssh.keys)
                files.append(dm.File(f"{container_ssh.AUTHORIZED_KEYS}/authorized_keys", keys))
        ids = uid(self.run_user)
        home = "/" if self.state is None else self.state.path
        account = dm.User(
            self.run_user, uid=ids, primary_group=ids, home=home, shell=container_ssh.SHELL
        )
        return dm.ContainerRoot(
            self.name,
            tuple(dict.fromkeys(packages)),
            files=tuple(files),
            directories=mounts,
            mount_files=points,
            network=network,
            endpoints=tuple(endpoint.address for endpoint in self.requires),
            account=account,
            ssh_port=None if ssh is None else ssh.port,
            hosts=pinned_hosts(((self.name, self.egress), *self.modes.items())),
        )

    def _dropin(self, unit: str, units: Sequence[str]) -> dm.DropIn:
        """The drop-in running *unit* in the root image after *units*: its namespaces, its
        state bound, and for sshd the host key (and the delivered keys), all as :meth:`_binds`."""
        directives: dict[str, str | tuple[str, ...]] = {
            "RootImage": self.image,
            "MountAPIVFS": "yes",
            "PrivatePIDs": "yes",
            "PrivateUsers": "yes",
            "ProtectProc": "invisible",
        }
        extra: dict[str, dict[str, str | tuple[str, ...]]] = {}
        needs = list(units)
        if self.namespace is None:
            directives["PrivateNetwork"] = "yes"
        else:
            directives["NetworkNamespacePath"] = f"{NETNS}/{self.name}"
        sshd = self.ssh is not None and unit == self.sshd_unit
        for item in self._binds(sshd=sshd):
            found = directives.setdefault(item.directive, ())
            assert isinstance(found, tuple)
            directives[item.directive] = (*found, f"{item.path}:{item.target}")
        if sshd:
            needs.append(container_ssh.unit(self.name))
            if unit != self.unit:
                needs.append(self.unit)
        extra["Service"] = directives
        return dm.DropIn("20-container", requires=tuple(needs), after=tuple(needs), extra=extra)

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
            f"Description=Network namespace of container {name}\n"
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
        if not self.argv:
            return ()
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

    @property
    def metrics(self) -> UnitMetrics:
        """Its unit's series as PromQL selectors, for a ``RecordingRule``:
        ``f"max({searcher.metrics.active})"``."""
        return UnitMetrics(self.unit)
