"""The small typed values an :class:`~tundravm.image.Image` is written with.

Each one stands for one concern and lowers onto the internal declarations in
:mod:`tundravm.image._lower`: ports (:class:`TCP`, :class:`UDP`), content
(:class:`Package`, :class:`File`, :class:`Tree`), the machine around the services
(:class:`Debian`, :class:`Kernel`, :class:`Egress`, :class:`Mkosi`) and the grouped service options
(:class:`Resources`, :class:`Shutdown`, :class:`HttpProbe`, :class:`TcpProbe`,
:class:`Notify`, :class:`Credential`).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from pathlib import Path
from typing import ClassVar, Literal

from tundravm._location import Located
from tundravm.declarative.model import Git, Http, Ref
from tundravm.declarative.utils import StaticAddress
from tundravm.errors import ValidationError

__all__ = [
    "LOCKED_SNAPSHOT",
    "MANAGEMENT_PORT",
    "Credential",
    "Debian",
    "Egress",
    "File",
    "HttpProbe",
    "Kernel",
    "Mkosi",
    "Notify",
    "Package",
    "Resources",
    "Shutdown",
    "StaticAddress",
    "TCP",
    "TcpProbe",
    "Tree",
    "UDP",
]

LOCKED_SNAPSHOT = "20251113T083151Z"
"""The snapshot.debian.org archive ``Debian("trixie")`` reads, so two builds of the same
image see the same packages. The lockfile records it; ``Debian(snapshot=...)`` moves it."""

MANAGEMENT_PORT = 8745
"""The attested management endpoint every image reserves inbound (plan section 6.3).

The port is declared and opened in the firewall from the start, so an image's inbound
surface never changes when the attested endpoint lands (gate 3). Until then an image
with volumes or secrets runs today's bootstrap on it: ``secret-delivery`` takes the
operator's SSH public key there, once per boot.
"""

_OWNER = re.compile(r"[a-z_][a-z0-9_-]{0,31}(:[a-z_][a-z0-9_-]{0,31})?")
_MKOSI_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{type(owner).__name__}: {message}", hint=hint)


def _absolute(owner: object, path: object, what: str = "path") -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
        raise _fail(owner, f"{what} {path!r} must be an absolute path.")
    return posixpath.normpath(path)


# ── Ports ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TCP:
    """An inbound TCP port: ``Service(expose=[TCP(8080)])`` opens it in the firewall."""

    port: int

    protocol: ClassVar[Literal["tcp", "udp"]] = "tcp"

    def __post_init__(self) -> None:
        _check_port(self, self.port)

    def __str__(self) -> str:
        return f"{self.port}/tcp"


@dataclass(frozen=True, slots=True)
class UDP:
    """An inbound UDP port: ``Service(expose=[UDP(53)])`` opens it in the firewall."""

    port: int

    protocol: ClassVar[Literal["tcp", "udp"]] = "udp"

    def __post_init__(self) -> None:
        _check_port(self, self.port)

    def __str__(self) -> str:
        return f"{self.port}/udp"


type PortValue = TCP | UDP


def _check_port(owner: object, port: object) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise _fail(owner, f"{port!r} is not a port number (1-65535).")


# ── Content ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Package(Located):
    """A runtime package: ``Package("curl")``, ``Package("nftables", backports=True)``.

    Build dependencies are not packages of the image: they belong to the build
    declaration that needs them (``GoBinary``, ``CargoBinary``, ``Build``).
    """

    name: str
    _: KW_ONLY
    backports: bool = False
    """Take the package from the release's backports archive instead of the release."""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or any(c.isspace() for c in self.name):
            raise _fail(self, f"{self.name!r} is not a package name.")
        if not isinstance(self.backports, bool):
            raise _fail(self, "backports= must be True or False.")

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        return Ref("Package", (self.name, "runtime"))


@dataclass(frozen=True, slots=True)
class File(Located):
    """A file in the image: ``File.text(path, text)`` or ``File.copy(src, dst)``.

    *owner* (``"app"`` or ``"app:app"``) is applied at boot by systemd-tmpfiles, after the
    users exist; without it the file is root-owned, as everything the image ships is.
    """

    path: str
    content: str | bytes | Path
    _: KW_ONLY
    mode: int | None = 0o644
    owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _absolute(self, self.path))
        if not isinstance(self.content, (str, bytes, Path)):
            raise _fail(self, f"content of {self.path!r} must be text, bytes or a Path.")
        if self.mode is not None and (
            isinstance(self.mode, bool) or not isinstance(self.mode, int) or not 0 <= self.mode
        ):
            raise _fail(self, f"mode {self.mode!r} must be an octal int such as 0o644.")
        if self.mode is None and not isinstance(self.content, Path):
            raise _fail(
                self,
                f"mode=None keeps a copied file's execute bit; {self.path!r} is not copied.",
                hint="Pass mode=0o644.",
            )
        _check_owner(self, self.owner)

    @classmethod
    def text(cls, path: str, text: str, *, mode: int = 0o644, owner: str | None = None) -> File:
        """The UTF-8 text *text* at *path*."""
        if not isinstance(text, str):
            raise ValidationError(
                f"File.text({path!r}) takes text, got {type(text).__name__}.",
                hint="Use File.copy(src, dst) for a host file.",
            )
        return cls(path, text, mode=mode, owner=owner)

    @classmethod
    def copy(
        cls,
        src: str | Path,
        dst: str,
        *,
        mode: int | None = None,
        owner: str | None = None,
    ) -> File:
        """The host file *src*, read when the image lowers, shipped at *dst*.

        ``mode=None`` ships ``0755`` when *src* has an execute bit, else ``0644``.
        """
        if isinstance(src, str):
            src = Path(src)
        if not isinstance(src, Path):
            raise ValidationError(
                f"File.copy() takes a host path, got {src!r}.",
                hint="Pass File.copy('etc/app.conf', '/etc/app.conf').",
            )
        return cls(dst, src, mode=mode, owner=owner)

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        return Ref("File", ("extra", self.path))


@dataclass(frozen=True, slots=True)
class Tree(Located):
    """The host directory *src* shipped at *dst*, keeping each file's permission bits."""

    src: str | Path
    dst: str
    _: KW_ONLY
    exclude: Sequence[str] = ()
    owner: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.src, str):
            object.__setattr__(self, "src", Path(self.src))
        if not isinstance(self.src, Path):
            raise _fail(self, f"src {self.src!r} must be a host path.")
        object.__setattr__(self, "dst", _absolute(self, self.dst, "dst"))
        if isinstance(self.exclude, str):
            raise _fail(self, "exclude= takes a list of patterns, not one string.")
        object.__setattr__(self, "exclude", tuple(self.exclude))
        _check_owner(self, self.owner)

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        return Ref("FileTree", ("extra", self.dst))


def _check_owner(owner: object, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not _OWNER.fullmatch(value):
        raise _fail(
            owner,
            f"owner {value!r} must be a user name, optionally 'user:group'.",
            hint="Pass owner='app' or owner='app:app'.",
        )


# ── The machine ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Debian:
    """The distribution an image is built from: ``Debian("trixie")``, the tested baseline.

    *snapshot* is the snapshot.debian.org archive the packages come from
    (:data:`LOCKED_SNAPSHOT` by default, which the lockfile records). Another release
    (``Debian("bookworm")``) is advanced and untested.
    """

    release: str = "trixie"
    _: KW_ONLY
    snapshot: str | None = None
    efi_stub: str | None = None
    """Pin the UKI's EFI stub to that ``systemd-boot-efi`` version from *snapshot*."""

    def __post_init__(self) -> None:
        if not isinstance(self.release, str) or not self.release or "/" in self.release:
            raise _fail(self, f"{self.release!r} is not a Debian release name.")
        for what in ("snapshot", "efi_stub"):
            value = getattr(self, what)
            if value is not None and (not isinstance(value, str) or not value):
                raise _fail(self, f"{what}= must be a string or None, got {value!r}.")

    @property
    def archive(self) -> str:
        """The snapshot the image reads: *snapshot*, else :data:`LOCKED_SNAPSHOT`."""
        return self.snapshot or LOCKED_SNAPSHOT


@dataclass(frozen=True, slots=True)
class Kernel:
    """The kernel: ``Kernel.default()`` (the distribution's, tested) or ``Kernel.build(...)``.

    ``Kernel.default()`` installs Debian's ``linux-image-amd64`` and keeps the curated
    module set (``tdx_guest``, virtio, NVMe, Hyper-V, gVNIC, and the ones each target
    needs). ``Kernel.build(version, source, config=, patches=)`` compiles one instead,
    with every driver built in and no loadable modules.
    """

    version: str | None = None
    source: Git | Http | None = None
    _: KW_ONLY
    config: Path | None = None
    patches: Sequence[Path] = ()
    modules: Sequence[str] = ()
    """Extra modules of the distribution kernel to keep, beyond the curated set."""

    def __post_init__(self) -> None:
        if (self.version is None) != (self.source is None):
            raise _fail(
                self,
                "a built kernel needs both a version and a source.",
                hint="Use Kernel.build('6.12.48', Git(url, ref)) or Kernel.default().",
            )
        if self.source is not None and not isinstance(self.source, (Git, Http)):
            raise _fail(self, f"source must be Git(...) or Http(...), got {self.source!r}.")
        if self.config is not None and not isinstance(self.config, Path):
            raise _fail(self, "config= must be a pathlib.Path.")
        object.__setattr__(self, "patches", tuple(self.patches))
        object.__setattr__(self, "modules", tuple(self.modules))
        if self.built and self.config is None:
            raise _fail(
                self,
                "a built kernel needs its config.",
                hint="Pass config=Path('kernel.config').",
            )
        if not self.built and (self.config is not None or self.patches):
            raise _fail(
                self,
                "config= and patches= apply to a built kernel.",
                hint="Use Kernel.build(version, source, config=...).",
            )
        if not self.built and isinstance(self.modules, str):
            raise _fail(self, "modules= takes a list of module names.")

    @property
    def built(self) -> bool:
        """Whether the build compiles this kernel."""
        return self.version is not None

    @classmethod
    def default(cls, *, modules: Sequence[str] = ()) -> Kernel:
        """Debian's kernel with the curated module set, plus *modules*."""
        return cls(modules=modules)

    @classmethod
    def build(
        cls,
        version: str,
        source: Git | Http,
        *,
        config: Path,
        patches: Sequence[Path] = (),
    ) -> Kernel:
        """Compile *version* from *source* with *config* and *patches*."""
        return cls(version, source, config=config, patches=tuple(patches))


@dataclass(frozen=True, slots=True)
class Egress:
    """Outbound traffic the image may send; everything else is dropped.

    Without an ``Egress`` the image does not filter outbound traffic. With one, the
    output chain accepts the replies of accepted inbound traffic, loopback, the DHCP
    client, the ICMPv6 messages IPv6 needs, *ports* and *hosts*, and drops the rest.
    An image that resolves names or syncs time needs those destinations listed.
    """

    ports: Sequence[PortValue] = ()
    hosts: Sequence[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.ports, (TCP, UDP)):
            raise _fail(self, "ports= takes a list of ports.", hint="Pass ports=[TCP(443)].")
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "hosts", tuple(self.hosts))
        for port in self.ports:
            if not isinstance(port, (TCP, UDP)):
                raise _fail(self, f"ports= entry {port!r} is not TCP(...) or UDP(...).")
        for host in self.hosts:
            if not isinstance(host, str) or not host:
                raise _fail(self, f"hosts= entry {host!r} is not an address or a CIDR block.")


@dataclass(frozen=True, slots=True)
class Mkosi:
    """mkosi settings and scripts the image adds verbatim, recorded in the manifest.

    *settings* maps an mkosi key to its value (``{"Packages": "strace"}``); *scripts* are
    ``(stage, script)`` pairs run at that mkosi stage (``"prepare"``, ``"build"``,
    ``"postinst"``, ``"finalize"``, ``"postoutput"``). Anything that would contradict an
    invariant the image advertises is refused at construction, not silently applied:
    :data:`RESERVED_SETTINGS` says which and why.
    """

    _: KW_ONLY
    settings: Mapping[str, str] | None = None
    scripts: Sequence[tuple[str, str]] = ()

    def __post_init__(self) -> None:
        settings = dict(self.settings or {})
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "scripts", tuple(tuple(s) for s in self.scripts))
        for key, value in settings.items():
            if not isinstance(key, str) or not _MKOSI_KEY.fullmatch(key):
                raise _fail(self, f"{key!r} is not an mkosi setting name.")
            if (reason := RESERVED_SETTINGS.get(key)) is not None:
                raise _fail(
                    self,
                    f"settings= sets {key}=, which the image guarantees: {reason}.",
                    hint="Drop it, or state the guarantee through the typed slot that owns it.",
                )
            if not isinstance(value, str) or "\n" in value:
                raise _fail(self, f"settings[{key!r}] must be one line of text.")
        for entry in self.scripts:
            if len(entry) != 2 or not all(isinstance(part, str) for part in entry):
                raise _fail(
                    self,
                    f"scripts= entry {entry!r} is not a (stage, script) pair.",
                    hint="Pass scripts=[('postinst', 'echo hello')].",
                )
            if entry[0] not in SCRIPT_STAGES:
                raise _fail(
                    self,
                    f"scripts= stage {entry[0]!r} is not an mkosi stage.",
                    hint=f"Use one of: {', '.join(SCRIPT_STAGES)}.",
                )


SCRIPT_STAGES = ("prepare", "build", "postinst", "finalize", "postoutput")
"""The mkosi stages :class:`Mkosi` scripts run at."""

RESERVED_SETTINGS: dict[str, str] = {
    "ConfigureScripts": "the tree's configure script is what makes the build reproducible "
    "and is generated from the image",
    "Environment": "the build environment is passed through from the lock, so a setting "
    "here would change what was measured without changing the lock",
    "Bootable": "every image is a measured UKI",
    "Format": "the payload's format follows from the root layout and the targets",
    "Profiles": "profiles are the image's own, named by Profile(...)",
    "Dependencies": "the subimage graph is the compiler's",
}
"""mkosi keys :class:`Mkosi` refuses, with the invariant each would contradict."""


# ── Grouped service options ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Resources:
    """What a service may use: ``Resources(memory_max="8G", tasks_max=4096)``.

    Each field is one cgroup directive; unset fields render nothing.
    """

    _: KW_ONLY
    memory_max: str | None = None
    memory_high: str | None = None
    cpu_quota: str | None = None
    cpu_weight: int | None = None
    tasks_max: int | None = None
    io_weight: int | None = None

    def __post_init__(self) -> None:
        for what in ("memory_max", "memory_high", "cpu_quota"):
            value = getattr(self, what)
            if value is not None and (not isinstance(value, str) or not value):
                raise _fail(self, f"{what}= must be a string such as '8G', got {value!r}.")
        for what in ("cpu_weight", "tasks_max", "io_weight"):
            value = getattr(self, what)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise _fail(self, f"{what}= must be an int, got {value!r}.")
        if not self.entries():
            raise _fail(self, "sets no limit.", hint="Pass Resources(memory_max='8G').")

    def entries(self) -> tuple[tuple[str, str], ...]:
        """The ``[Service]`` directives this value renders."""
        pairs = (
            ("MemoryMax", self.memory_max),
            ("MemoryHigh", self.memory_high),
            ("CPUQuota", self.cpu_quota),
            ("CPUWeight", self.cpu_weight),
            ("TasksMax", self.tasks_max),
            ("IOWeight", self.io_weight),
        )
        return tuple((key, str(value)) for key, value in pairs if value is not None)


@dataclass(frozen=True, slots=True)
class Shutdown:
    """How a service is stopped: ``Shutdown(timeout="120s", signal="SIGINT")``."""

    _: KW_ONLY
    timeout: str | None = None
    signal: str | None = None
    kill_mode: Literal["control-group", "mixed", "process", "none"] | None = None
    final_kill: bool = True
    """Whether systemd sends ``SIGKILL`` after *timeout*; ``False`` waits instead."""

    def __post_init__(self) -> None:
        if self.timeout is not None and (not isinstance(self.timeout, str) or not self.timeout):
            raise _fail(self, f"timeout {self.timeout!r} is not a systemd time span.")
        if self.signal is not None and (
            not isinstance(self.signal, str) or not self.signal.startswith("SIG")
        ):
            raise _fail(self, f"signal {self.signal!r} is not a signal name such as 'SIGINT'.")
        if self.kill_mode is not None and self.kill_mode not in (
            "control-group",
            "mixed",
            "process",
            "none",
        ):
            raise _fail(self, f"kill_mode {self.kill_mode!r} is not a systemd kill mode.")
        if not isinstance(self.final_kill, bool):
            raise _fail(self, "final_kill= must be True or False.")

    def entries(self) -> tuple[tuple[str, str], ...]:
        """The ``[Service]`` directives beyond ``TimeoutStopSec=`` and ``KillMode=``."""
        out: list[tuple[str, str]] = []
        if self.signal is not None:
            out.append(("KillSignal", self.signal))
        if not self.final_kill:
            out.append(("SendSIGKILL", "no"))
        return tuple(out)


@dataclass(frozen=True, slots=True)
class HttpProbe:
    """Readiness: the service answers *url* with a 2xx, polled until *timeout*.

    The probe is a bounded unit of its own, so ``requires=[other.ready]`` means the
    dependency answered, not that its binary was exec'd. It needs ``curl``, which an
    image with an ``HttpProbe`` installs.
    """

    url: str
    _: KW_ONLY
    timeout: str = "90s"
    interval: str = "0.5"

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith(("http://", "https://")):
            raise _fail(self, f"url {self.url!r} must be an http:// or https:// URL.")
        if "'" in self.url or any(c.isspace() for c in self.url):
            raise _fail(self, f"url {self.url!r} must be one word without quotes.")
        _check_span(self, self.timeout, self.interval)

    def command(self) -> tuple[str, ...]:
        """The argv of the probe unit."""
        loop = (
            f"until curl -fsS --max-time 5 '{self.url}' >/dev/null 2>&1; "
            f"do sleep {self.interval}; done"
        )
        return ("/bin/bash", "-c", loop)


@dataclass(frozen=True, slots=True)
class TcpProbe:
    """Readiness: *host* accepts a TCP connection on *port*, polled until *timeout*.

    The probe is a bounded unit of its own and uses bash's ``/dev/tcp``, which Debian
    always has: it installs nothing.
    """

    port: int
    _: KW_ONLY
    host: str = "127.0.0.1"
    timeout: str = "90s"
    interval: str = "0.5"

    def __post_init__(self) -> None:
        _check_port(self, self.port)
        if not isinstance(self.host, str) or not self.host or any(c in self.host for c in " '/"):
            raise _fail(self, f"host {self.host!r} is not a host name or address.")
        _check_span(self, self.timeout, self.interval)

    def command(self) -> tuple[str, ...]:
        """The argv of the probe unit."""
        loop = (
            f"until (exec 3<>/dev/tcp/{self.host}/{self.port}) 2>/dev/null; "
            f"do sleep {self.interval}; done"
        )
        return ("/bin/bash", "-c", loop)


@dataclass(frozen=True, slots=True)
class Notify:
    """Readiness: the program itself reports ``READY=1`` on ``$NOTIFY_SOCKET``.

    The unit becomes ``Type=notify``, so systemd holds everything that requires it until
    the program says it is serving. It is the cheapest readiness there is, and the only
    one that needs no probe; the program has to implement ``sd_notify`` for it.
    """


type Readiness = HttpProbe | TcpProbe | Notify


def _check_span(owner: object, timeout: object, interval: object) -> None:
    if not isinstance(timeout, str) or not timeout:
        raise _fail(owner, f"timeout {timeout!r} is not a systemd time span such as '90s'.")
    if not isinstance(interval, str) or not re.fullmatch(r"\d+(\.\d+)?", interval):
        raise _fail(owner, f"interval {interval!r} must be a number of seconds, e.g. '0.5'.")


@dataclass(frozen=True, slots=True)
class Credential:
    """A systemd credential a service reads: ``Credential("jwt", "/etc/app/jwt")``.

    The service gets ``LoadCredential=<name>:<source>`` and reads the value under
    ``$CREDENTIALS_DIRECTORY``, never from its environment or its command line.
    """

    name: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise _fail(self, f"{self.name!r} is not a credential name.", hint="Use 'api-token'.")
        object.__setattr__(self, "source", _absolute(self, self.source, "source"))
