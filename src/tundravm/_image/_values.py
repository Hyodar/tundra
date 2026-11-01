"""The small typed values an :class:`~tundravm.Image` is written with.

Each one stands for one concern and lowers onto the internal declarations in
:mod:`tundravm._image._lower`: ports (:class:`TCP`, :class:`UDP`), content
(:class:`Package`, :class:`File`, :class:`Tree`), the machine around the services
(:class:`Debian`, :class:`AptRepository`, :class:`Kernel`, :class:`Egress`,
:class:`Mkosi`) and the grouped service options
(:class:`Resources`, :class:`Shutdown`, :class:`HttpProbe`, :class:`TcpProbe`,
:class:`Notify`, :class:`Credential`).
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from pathlib import Path
from typing import ClassVar, Literal

from tundravm._location import Located, caller_location
from tundravm._modules.instance_key import KEY as INSTANCE_KEY
from tundravm.declarative import model as dm
from tundravm.declarative.model import Git, Http
from tundravm.declarative.utils import StaticAddress
from tundravm.errors import ValidationError

from .._keywords import Keywords, shown, subject
from ._change import Ref

__all__ = [
    "LOCKED_SNAPSHOT",
    "MANAGEMENT_PORT",
    "AptRepository",
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
    "egress_host",
]

LOCKED_SNAPSHOT = "20261018T082309Z"
"""The snapshot.debian.org archive ``Debian("trixie")`` reads, so two builds of the same
image see the same packages. The lockfile records it; ``Debian(snapshot=...)`` moves it."""

MANAGEMENT_PORT = 8745
"""The management endpoint's port, which no ingress may take (plan section 6.3).

An image with volumes or secrets runs the attested endpoint on it (``tools/cmd/
attested-endpoint``) and the firewall opens it. An image without them has nothing
listening there and no rule for it.
"""

_OWNER = re.compile(r"[a-z_][a-z0-9_-]{0,31}(:[a-z_][a-z0-9_-]{0,31})?")
_MKOSI_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_KERNEL_PARAMETER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(=[^\s\x00-\x1f\x7f]*)?")


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{subject(owner)}: {message}", hint=hint)


def _absolute(owner: object, path: object, what: str = "path") -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
        raise _fail(
            owner,
            f"{what}= {path!r} is not an absolute path.",
            hint=f'{what}="/{path.lstrip("./")}"'
            if isinstance(path, str) and "\n" not in path
            else f'{what}="/etc/app.conf"',
        )
    return posixpath.normpath(path)


# ── Ports ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TCP(Keywords):
    """An inbound TCP port: ``Service(expose=[TCP(8080)])`` opens it in the firewall."""

    port: int

    protocol: ClassVar[Literal["tcp", "udp"]] = "tcp"

    def __post_init__(self) -> None:
        _check_port(self, self.port)

    def __str__(self) -> str:
        return f"{self.port}/tcp"


@dataclass(frozen=True, slots=True)
class UDP(Keywords):
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
        raise _fail(
            owner,
            f"{port!r} is not a port number (1-65535).",
            hint=f"{type(owner).__name__}({int(port)})"
            if isinstance(port, str) and port.isdigit()
            else f"{type(owner).__name__}(8080)",
        )


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
            raise _fail(
                self,
                f"{self.name!r} is not a package name.",
                hint='Name one Debian package, lower case, as apt spells it: Package("curl").',
            )
        if not isinstance(self.backports, bool):
            raise _fail(
                self,
                f"backports= must be True or False, got {self.backports!r}.",
                hint=f'Package("{self.name}", backports=True)',
            )

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        return Ref(self, dm.Ref("Package", (self.name, "runtime")))


@dataclass(frozen=True, slots=True)
class File(Located):
    """A file in the image: ``File.text(path, text)`` or ``File.copy(src, dst)``.

    *owner* (``"app"`` or ``"app:app"``) is applied at boot by the runtime-init step
    ``owners``, before the services that wait for runtime-init start: an unprivileged build
    ships every file root-owned. Without it the file stays root's. *mode* is the image's.
    """

    path: str
    content: str | bytes | Path
    _: KW_ONLY
    mode: int | None = 0o644
    owner: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _absolute(self, self.path))
        if not isinstance(self.content, (str, bytes, Path)):
            raise _fail(
                self,
                f"content= of {self.path!r} must be text, bytes or a Path.",
                hint=f'File.text("{self.path}", "...") or File.copy("host/file", "{self.path}")',
            )
        if self.mode is not None and (
            isinstance(self.mode, bool) or not isinstance(self.mode, int) or not 0 <= self.mode
        ):
            raise _fail(
                self,
                f"mode= {self.mode!r} is not a file mode.",
                hint=f"mode=0o{self.mode}"
                if isinstance(self.mode, str) and self.mode.isdigit()
                else "mode=0o644, an octal int",
            )
        if self.mode is None and not isinstance(self.content, Path):
            raise _fail(
                self,
                f"mode=None keeps a copied file's execute bit; {self.path!r} is not copied.",
                hint="Pass mode=0o644.",
            )
        if isinstance(self.content, Path):
            object.__setattr__(self, "content", _beside_image(self.content))
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
        """The host file *src*, read when the image lowers, shipped at *dst*. A relative
        *src* is beside the file that declares it, as ``Local`` is, not the working directory.

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
        return Ref(self, dm.Ref("File", ("extra", self.path)))


@dataclass(frozen=True, slots=True)
class Tree(Located):
    """The host directory *src* shipped at *dst*, keeping each file's permission bits. A
    relative *src* is beside the file that declares it, as ``Local`` is.

    *owner* is applied to the whole tree at boot (``chown -R``), as :class:`File`'s is."""

    src: str | Path
    dst: str
    _: KW_ONLY
    exclude: Sequence[str] = ()
    owner: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.src, str):
            object.__setattr__(self, "src", Path(self.src))
        if not isinstance(self.src, Path):
            raise _fail(
                self,
                f"src= {self.src!r} is not a host path.",
                hint='Tree("app/static", "/srv/www"): a directory beside the image file.',
            )
        object.__setattr__(self, "src", _beside_image(self.src))
        object.__setattr__(self, "dst", _absolute(self, self.dst, "dst"))
        if isinstance(self.exclude, str):
            raise _fail(
                self,
                "exclude= takes a list of patterns, not one string.",
                hint=f'exclude=["{self.exclude}"]',
            )
        object.__setattr__(self, "exclude", tuple(self.exclude))
        _check_owner(self, self.owner)

    @property
    def ref(self) -> Ref:
        """What a :class:`~tundravm.Profile` change targets."""
        return Ref(self, dm.Ref("FileTree", ("extra", self.dst)))


def _beside_image(path: Path) -> Path:
    """*path* as the image file means it: a relative one is beside the file that declares
    it (the image file, or the file constructing a component), as ``Local`` resolves, so a
    build reads the same file from any working directory."""
    if path.is_absolute():
        return path
    location = caller_location().rpartition(":")[0]
    return (Path(location).parent if location else Path.cwd()) / path


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


_SNAPSHOT_ARCHIVE = re.compile(
    r"https://snapshot\.debian\.org/archive/(?P<archive>[a-z0-9-]+)(/(?P<date>[^/]+))?/?"
)
_SNAPSHOT_DATE = re.compile(r"\d{8}T\d{6}Z")
_SUITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
PACKAGE_KEYRINGS = "/usr/share/keyrings/"
"""Where a keyring a package installs lives: what a ``str`` ``AptRepository(key=)`` names."""


@dataclass(frozen=True, slots=True)
class AptRepository(Keywords):
    """A second apt archive, pinned and signed: ``Debian(repositories=[AptRepository(...)])``.

    A ``https://snapshot.debian.org/archive/<archive>`` *url* is dated by *snapshot*, or by
    the image's own snapshot, so it moves when the base does; a date in the URL pins it too.
    Any other *url* takes *sha256*, the digest of its ``dists/<suite>/InRelease``: that file
    is signed and lists every index's digest, so the build fails when the archive moves.
    *key* signs it: a host keyring file (``Path``, ``.asc`` when armored), or the path of
    one a package installs under ``/usr/share/keyrings/`` in the tools tree, where the build's
    apt reads it (``debian-archive-keyring``'s are added there). Unsigned archives are refused.
    """

    url: str
    suite: str
    components: Sequence[str] = ("main",)
    _: KW_ONLY
    key: Path | str
    snapshot: str | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.components, str):
            raise _fail(
                self,
                "components= takes a list, not one string.",
                hint=f'components=["{self.components}"]',
            )
        object.__setattr__(self, "components", tuple(self.components))
        url = self.url
        if not isinstance(url, str) or not url.startswith("https://"):
            raise _fail(
                self,
                f"url= {url!r} is not an https:// address.",
                hint="Pass the archive's https:// URL; apt checks its signature with key=.",
            )
        if any(c.isspace() or c in "[]" for c in url):
            raise _fail(
                self,
                f"url {url!r} carries apt options.",
                hint="An AptRepository is signed by key= and pinned by snapshot= or sha256=.",
            )
        if not isinstance(self.suite, str) or not _SUITE.fullmatch(self.suite):
            raise _fail(
                self,
                f"suite= {self.suite!r} is not a suite name.",
                hint='Name the suite under dists/: "trixie-security", "stable".',
            )
        if not self.components or not all(
            isinstance(c, str) and _SUITE.fullmatch(c) for c in self.components
        ):
            raise _fail(
                self,
                f"components= {self.components!r} names no component.",
                hint='components=["main"]',
            )
        self._check_key()
        self._check_pin()

    def _check_key(self) -> None:
        if isinstance(self.key, Path):
            object.__setattr__(self, "key", _beside_image(self.key))
            return
        if not isinstance(self.key, str) or not self.key.startswith(PACKAGE_KEYRINGS):
            raise _fail(
                self,
                f"key {self.key!r} is neither a host keyring nor one a package installs.",
                hint="Pass Path(__file__).parent / 'vendor.asc', or "
                f"'{PACKAGE_KEYRINGS}debian-archive-keyring.gpg'.",
            )

    def _check_pin(self) -> None:
        dated = _SNAPSHOT_ARCHIVE.fullmatch(self.url)
        if dated is not None:
            if self.sha256 is not None:
                raise _fail(
                    self,
                    f"{self.url} is dated by its snapshot; sha256= would pin it twice.",
                    hint="Drop sha256=.",
                )
            for date in (dated["date"], self.snapshot):
                if date is not None and not _SNAPSHOT_DATE.fullmatch(date):
                    raise _fail(
                        self,
                        f"snapshot= {date!r} is not a snapshot.debian.org date.",
                        hint='snapshot="20261018T082309Z"',
                    )
            if dated["date"] is not None and self.snapshot is not None:
                raise _fail(
                    self,
                    f"{self.url} names its date; snapshot= would name another.",
                    hint="Drop snapshot=, or drop the date from url=.",
                )
            return
        if self.snapshot is not None:
            raise _fail(
                self,
                f"snapshot= dates a snapshot.debian.org archive, not {self.url}.",
                hint="Pass sha256= of its dists/<suite>/InRelease instead.",
            )
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise _fail(
                self,
                f"{self.url} {self.suite} is not pinned.",
                hint=f"Pass sha256= of `curl -fsSL {self.release_url} | sha256sum`.",
            )

    @property
    def release_url(self) -> str:
        """The signed ``InRelease`` file *sha256* pins."""
        return f"{self.url.rstrip('/')}/dists/{self.suite}/InRelease"

    @property
    def name(self) -> str:
        """The name its apt source and keyring are written under."""
        dated = _SNAPSHOT_ARCHIVE.fullmatch(self.url)
        place = dated["archive"] if dated is not None else self.url.removeprefix("https://")
        return re.sub(r"[^a-z0-9.]+", "-", f"{place}-{self.suite}".lower()).strip("-")

    @property
    def keyring(self) -> str:
        """The keyring's path in the image and the build's apt sandbox."""
        if isinstance(self.key, str):
            return self.key
        suffix = ".asc" if self.key.suffix == ".asc" else ".gpg"
        return f"/etc/apt/keyrings/{self.name}{suffix}"

    def address(self, snapshot: str) -> str:
        """The URL apt reads, dated by *snapshot* (the base's) when nothing else dates it."""
        dated = _SNAPSHOT_ARCHIVE.fullmatch(self.url)
        if dated is None:
            return self.url
        date = dated["date"] or self.snapshot or snapshot
        return f"https://snapshot.debian.org/archive/{dated['archive']}/{date}/"


@dataclass(frozen=True, slots=True)
class Debian(Keywords):
    """The distribution an image is built from: ``Debian("trixie")``, the tested baseline.

    *snapshot* is the snapshot.debian.org archive the packages come from
    (:data:`LOCKED_SNAPSHOT` by default, which the lockfile records). Another release
    (``Debian("bookworm")``) is advanced and untested. *repositories* adds archives beside
    it, each an :class:`AptRepository`, pinned and signed.
    """

    release: str = "trixie"
    _: KW_ONLY
    snapshot: str | None = None
    efi_stub: str | None = None
    """Pin the UKI's EFI stub to that ``systemd-boot-efi`` version from *snapshot*."""
    repositories: Sequence[AptRepository] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.release, str) or not self.release or "/" in self.release:
            raise _fail(
                self,
                f"{self.release!r} is not a Debian release name.",
                hint='Debian("trixie")',
            )
        for what in ("snapshot", "efi_stub"):
            value = getattr(self, what)
            if value is not None and (not isinstance(value, str) or not value):
                raise _fail(
                    self,
                    f"{what}= must be a string or None, got {value!r}.",
                    hint=f'Debian("trixie", {what}="...") or leave {what}= out',
                )
        if isinstance(self.repositories, (str, AptRepository)):
            raise _fail(
                self,
                "repositories= takes a list of AptRepository values.",
                hint="Debian(repositories=[AptRepository(url, suite, key=...)])",
            )
        object.__setattr__(self, "repositories", tuple(self.repositories))
        names: set[str] = set()
        for repository in self.repositories:
            if not isinstance(repository, AptRepository):
                raise _fail(
                    self,
                    f"repositories= holds {shown(repository)}, not an AptRepository.",
                    hint="Debian(repositories=[AptRepository(url, suite, key=...)])",
                )
            if repository.name in names:
                raise _fail(
                    self,
                    f"repositories= names {repository.name!r} twice.",
                    hint="List each archive once.",
                )
            names.add(repository.name)

    @property
    def archive(self) -> str:
        """The snapshot the image reads: *snapshot*, else :data:`LOCKED_SNAPSHOT`."""
        return self.snapshot or LOCKED_SNAPSHOT


_COMPILER_PARAMETERS: Mapping[str, str] = {
    "root": "Pick the root with rootfs=: the UKI's initrd (the default) or RootFS.verity().",
    "roothash": "Use rootfs=RootFS.verity(): mkosi writes the root hash it computed.",
    "usrhash": "Use rootfs=RootFS.verity(): mkosi writes the root hash it computed.",
    "systemd.verity": "Use rootfs=RootFS.verity(): it opens the root through dm-verity.",
    "systemd.volatile": "Use rootfs=RootFS.verity(): it stacks a tmpfs over the read-only root.",
    "init": "Drop it: the image boots systemd, and services= say what it starts.",
    "rdinit": "Drop it: the image boots systemd, and services= say what it starts.",
    "systemd.unit": "Drop it: the image boots minimal.target, and services= say what it starts.",
    "console": "Drop it: tundravm run and tundravm.testing.boot pass console=ttyS0 at boot.",
}
"""Kernel parameters the compiler, mkosi or the run set, each with the hint naming what sets
it: ``Kernel(cmdline=)`` refuses them, with an ``rd.`` prefix too and every
``systemd.verity*``."""


@dataclass(frozen=True, slots=True)
class Kernel(Keywords):
    """The kernel: ``Kernel.default()`` (the distribution's, tested) or ``Kernel.build(...)``.

    ``Kernel.default()`` installs Debian's ``linux-image-amd64`` and keeps the curated
    module set (``tdx_guest``, virtio, NVMe, Hyper-V, gVNIC, and the ones each target
    needs). ``Kernel.build(version, source, config=, patches=)`` compiles one instead,
    with every driver built in and no loadable modules.

    *cmdline* adds kernel parameters to the UKI's measured command line, after the ones
    the image sets itself: ``Kernel.default(cmdline=["nospectre_v2"])``. Each entry is one
    parameter, ``name`` or ``name=value``. None is added by default, and the parameters the
    image owns (the root and its verity, the init, the boot target, the console) are refused.
    """

    version: str | None = None
    source: Git | Http | None = None
    _: KW_ONLY
    config: Path | None = None
    patches: Sequence[Path] = ()
    modules: Sequence[str] = ()
    """Extra modules of the distribution kernel to keep, beyond the curated set."""
    cmdline: Sequence[str] = ()
    """Kernel parameters appended to the image's own command line, in this order."""

    def __post_init__(self) -> None:
        if (self.version is None) != (self.source is None):
            raise _fail(
                self,
                "a built kernel needs both a version and a source.",
                hint="Use Kernel.build('6.12.48', Git(url, ref)) or Kernel.default().",
            )
        if self.source is not None and not isinstance(self.source, (Git, Http)):
            raise _fail(
                self,
                f"source= must be Git(...) or Http(...), got {shown(self.source)}.",
                hint='Kernel.build("6.12.48", Git(url, "v6.12.48"), config=Path("kernel.config"))',
            )
        if self.config is not None and not isinstance(self.config, Path):
            raise _fail(
                self,
                f"config= must be a pathlib.Path, got {shown(self.config)}.",
                hint='config=Path(__file__).parent / "kernel.config"',
            )
        if isinstance(self.cmdline, str):
            raise _fail(
                self,
                "cmdline= takes a list of kernel parameters, not one string.",
                hint=f"Pass cmdline={self.cmdline.split()!r}.",
            )
        if isinstance(self.modules, str):
            raise _fail(
                self,
                "modules= takes a list of module names.",
                hint=f'Kernel.default(modules=["{self.modules}"])'
                if isinstance(self.modules, str)
                else 'Kernel.default(modules=["kvm"])',
            )
        if self.config is not None:
            object.__setattr__(self, "config", _beside_image(self.config))
        object.__setattr__(
            self,
            "patches",
            tuple(_beside_image(p) if isinstance(p, Path) else p for p in self.patches),
        )
        object.__setattr__(self, "modules", tuple(self.modules))
        object.__setattr__(self, "cmdline", tuple(self.cmdline))
        for parameter in self.cmdline:
            _check_kernel_parameter(self, parameter)
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
        if self.built and self.modules:
            raise _fail(
                self,
                "modules= keeps modules of Debian's kernel; a built kernel has every driver "
                "built in and loads none.",
                hint="Drop modules=, or enable the drivers in the built kernel's config=.",
            )

    @property
    def built(self) -> bool:
        """Whether the build compiles this kernel."""
        return self.version is not None

    @classmethod
    def default(cls, *, modules: Sequence[str] = (), cmdline: Sequence[str] = ()) -> Kernel:
        """Debian's kernel with the curated module set plus *modules*, booted with *cmdline*
        after the image's own parameters."""
        return cls(modules=modules, cmdline=cmdline)

    @classmethod
    def build(
        cls,
        version: str,
        source: Git | Http,
        *,
        config: Path,
        patches: Sequence[Path] = (),
        cmdline: Sequence[str] = (),
    ) -> Kernel:
        """Compile *version* from *source* with *config* and *patches*, booted with *cmdline*
        after the image's own parameters."""
        return cls(version, source, config=config, patches=tuple(patches), cmdline=cmdline)


def _check_kernel_parameter(owner: Kernel, parameter: object) -> None:
    if not isinstance(parameter, str) or not _KERNEL_PARAMETER.fullmatch(parameter):
        raise _fail(
            owner,
            f"cmdline= entry {parameter!r} is not one kernel parameter.",
            hint="Pass each parameter as its own entry, name or name=value without spaces: "
            "cmdline=['nospectre_v2', 'mitigations=auto'].",
        )
    name = parameter.partition("=")[0]
    owned = name.removeprefix("rd.")
    if owned.startswith("systemd.verity"):
        owned = "systemd.verity"
    if (hint := _COMPILER_PARAMETERS.get(owned)) is not None:
        raise _fail(owner, f"cmdline= sets {name}, which tundravm sets itself.", hint=hint)


@dataclass(frozen=True, slots=True)
class Egress(Keywords):
    """Outbound traffic the image may send; everything else is dropped.

    Without an ``Egress`` the image does not filter outbound traffic. With one, the
    output chain accepts the replies of accepted inbound traffic, loopback, the DHCP
    client, the ICMPv6 messages IPv6 needs, what the baseline sends (``TeeBase``'s NTS
    time on TCP 4460 and UDP 123, its resolvers, the metadata servers of the image's
    clouds), what each component's :meth:`~tundravm.components.Component.outbound` names,
    *ports* and *hosts*, and drops the rest.

    *ports* go to any address. Each of *hosts* is an address or CIDR block (any port) or
    ``HOST:PORT`` (TCP to that port; ``[ADDRESS]:PORT`` for IPv6). nftables matches
    addresses, not names, so a host name needs its port and ``"logs.example:443"`` accepts
    TCP 443 to any address: the rule is per port, and the name is its comment.
    """

    ports: Sequence[PortValue] = ()
    hosts: Sequence[str] = ()

    def __post_init__(self) -> None:
        if isinstance(self.ports, (TCP, UDP)):
            raise _fail(self, "ports= takes a list of ports.", hint="Pass ports=[TCP(443)].")
        if isinstance(self.hosts, str):
            raise _fail(self, "hosts= takes a list of hosts.", hint="Pass hosts=['10.0.0.5'].")
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "hosts", tuple(self.hosts))
        for port in self.ports:
            if not isinstance(port, (TCP, UDP)):
                raise _fail(
                    self,
                    f"ports= holds {shown(port)}, not TCP(...) or UDP(...).",
                    hint="Egress(ports=[TCP(443)])",
                )
        for host in self.hosts:
            egress_host(self, host)


_HOST_NAME = re.compile(r"(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?)+")


def egress_host(owner: object, host: object) -> tuple[str, int | None, bool]:
    """An ``Egress(hosts=)`` entry as ``(host, port, named)``: *port* ``None`` for an address
    or CIDR block, *named* when *host* is a name the firewall cannot match."""
    text = host if isinstance(host, str) else ""
    name, port = text, None
    if text.startswith("[") and "]:" in text:
        name, _, number = text[1:].partition("]:")
        port = int(number) if number.isdigit() else 0
    elif text.count(":") == 1:
        name, _, number = text.partition(":")
        port = int(number) if number.isdigit() else 0
    try:
        ipaddress.ip_network(name, strict=False)
        named = False
    except ValueError:
        named = True
    hint = "Pass '10.0.0.5', '10.0.0.0/8', '10.0.0.5:8545' or 'logs.example:443'."
    if port is not None and not 0 < port < 65536:
        raise _fail(owner, f"hosts= entry {host!r} has no port in 1..65535.", hint=hint)
    if named and (port is None or not _HOST_NAME.fullmatch(name)):
        problem = (
            "is not an address, a CIDR block or HOST:PORT"
            if port is not None or not _HOST_NAME.fullmatch(name)
            else "is a host name without a port, which the firewall cannot match"
        )
        raise _fail(owner, f"hosts= entry {host!r} {problem}.", hint=hint)
    if port is not None and "/" in name:
        raise _fail(owner, f"hosts= entry {host!r} gives a CIDR block a port.", hint=hint)
    return name, port, named


@dataclass(frozen=True, slots=True)
class Mkosi(Keywords):
    """mkosi settings and scripts the image adds verbatim, recorded in the manifest.

    *settings* maps an mkosi 27.1 key to its value (``{"Locale": "C.UTF-8"}``), each written
    in the section mkosi reads it from; *scripts* are ``(stage, script)`` pairs run at that
    mkosi stage (``"prepare"``, ``"build"``, ``"postinst"``, ``"finalize"``,
    ``"postoutput"``). A key the compiler writes itself, or one that would contradict an
    invariant the image advertises, is refused at construction, not silently applied:
    :data:`RESERVED_SETTINGS` names the slot that sets each, or says tundravm sets it.
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
                raise _fail(
                    self,
                    f"settings= names {key!r}, which is not an mkosi setting name.",
                    hint='Spell the key as mkosi.conf does: settings={"ToolsTreePackages": "..."}.',
                )
            name = _mkosi_setting(key)
            if name is None:
                raise _fail(
                    self,
                    f"settings= sets {key}=, which mkosi 27.1 does not know.",
                    hint="Check the spelling against mkosi's documentation of its settings.",
                )
            if (hint := RESERVED_SETTINGS.get(name[1])) is not None:
                raise _fail(
                    self, f"settings= sets {key}=, which tundravm writes itself.", hint=hint
                )
            if not isinstance(value, str) or "\n" in value:
                raise _fail(
                    self,
                    f"settings[{key!r}] must be one line of text.",
                    hint="Join several values with spaces on one line.",
                )
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

_SETS_IT = "Drop it: tundravm sets it, so a build reproduces."
_SCRIPTS = "Pass Mkosi(scripts=[(stage, script)]): tundravm writes the stage directories."
_FILES = "Pass Image(files=[File(...)]) or Image(files=[Tree(...)])."
_RELEASE = 'Pass Image(base=Debian("trixie")).'
_SNAPSHOT = "Pass Debian(snapshot=...)."
_VERITY = "Pass Image(rootfs=RootFS.verity())."

RESERVED_SETTINGS: dict[str, str] = {
    "Architecture": "Drop it: tundravm builds x86-64, the one architecture TDX runs.",
    "Bootable": "Drop it: every image is a measured UKI.",
    "BuildPackages": "Pass packages= to the GoBinary, CargoBinary or Build that needs them.",
    "BuildScripts": _SCRIPTS,
    "BuildSources": "Declare the source on the GoBinary, CargoBinary or Build that reads it.",
    "CleanPackageMetadata": _SETS_IT,
    "CleanScripts": _SCRIPTS,
    "CompressOutput": _SETS_IT,
    "ConfigureScripts": "Drop it: a configure script rewrites the configuration after `mkosi "
    "summary` resolved it, so the build would not be what the manifest records.",
    "Dependencies": "Pass Image(targets=[...]): each target is a subimage tundravm writes.",
    "Distribution": _RELEASE,
    "Environment": "Drop it: the build environment comes from the lock, so a value here would "
    "change what was measured without changing the lock.",
    "ExtraTrees": _FILES,
    "FinalizeScripts": _SCRIPTS,
    "Format": "Pass Image(rootfs=...) and Image(targets=...): the format follows from them.",
    "History": "Drop it: tundravm writes the tree afresh per build, with History=no.",
    "ImageId": "Pass Image(name): it names the tree's images.",
    "KernelCommandLine": "Pass Kernel(cmdline=[...]): the image writes and measures its own "
    "command line, and appends these parameters to it.",
    "KernelModules": "Pass Kernel(modules=[...]).",
    "KernelModulesExclude": "Pass Kernel(modules=[...]): tundravm keeps the curated set.",
    "KernelModulesInclude": "Pass Kernel(modules=[...]).",
    "ManifestFormat": _SETS_IT,
    "MinimumVersion": "Drop it: the tree requires the mkosi tundravm pins.",
    "Mirror": "Drop it: tundravm reads snapshot.debian.org at Debian(snapshot=...).",
    "Output": "Pass Image(name): it names the tree's images.",
    "OutputDirectory": "Drop it: `tundravm build` publishes under build/.",
    "PackageCacheDirectory": _SETS_IT,
    "Packages": "Pass Image(packages=[...]).",
    "PassEnvironment": "Drop it: the host's environment never reaches the build.",
    "PostInstallationScripts": _SCRIPTS,
    "PostOutputScripts": _SCRIPTS,
    "PrepareScripts": _SCRIPTS,
    "Profiles": "Pass Image(profiles={name: Profile(...)}).",
    "Release": _RELEASE,
    "RepartDirectories": _VERITY,
    "SandboxTrees": _SETS_IT,
    "Seed": "Drop it: tundravm sets a fixed seed, so the partition UUIDs reproduce.",
    "SkeletonTrees": _FILES,
    "Snapshot": _SNAPSHOT,
    "SourceDateEpoch": _SETS_IT,
    "SplitArtifacts": _VERITY,
    "SyncScripts": _SCRIPTS,
    "ToolsTree": "Drop it: tundravm always builds mkosi's default tools tree.",
    "ToolsTreeDistribution": _RELEASE,
    "ToolsTreeMirror": "Drop it: tundravm reads snapshot.debian.org at Debian(snapshot=...).",
    "ToolsTreeRelease": _RELEASE,
    "ToolsTreeSnapshot": _SNAPSHOT,
    "WithNetwork": "Drop it: the build runs offline; a component whose builds fetch "
    "dependencies says class X(Component, build_network=True).",
}
"""mkosi keys :class:`Mkosi` refuses, each with the public slot that sets it, or what
tundravm does instead."""


def _mkosi_setting(key: str) -> tuple[str, str] | None:
    """``(section, name)`` of the mkosi 27.1 setting *key* names (a compatibility name
    included); ``None`` when mkosi has no such setting."""
    from tundravm._mkosi import setting

    found = setting(key)
    return None if found is None else (found.section, found.name)


def mkosi_section(key: str) -> str:
    """The section of mkosi's ``mkosi.conf`` a :class:`Mkosi` *key* is written in."""
    found = _mkosi_setting(key)
    if found is None:
        raise ValidationError(
            f"Mkosi: settings= names {key!r}, which mkosi 27.1 does not know.",
            hint="Check the spelling against mkosi's documentation of its settings.",
        )
    return found[0]


# ── Grouped service options ─────────────────────────────────────────────

_RESOURCE_EXAMPLE = {"memory_max": "8G", "memory_high": "6G", "cpu_quota": "200%"}


@dataclass(frozen=True, slots=True)
class Resources(Keywords):
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
                raise _fail(
                    self,
                    f"{what}= must be a string, got {value!r}.",
                    hint=f'Resources({what}="{value}G")'
                    if what.startswith("memory") and isinstance(value, int)
                    else f'Resources({what}="{_RESOURCE_EXAMPLE[what]}")',
                )
        for what in ("cpu_weight", "tasks_max", "io_weight"):
            value = getattr(self, what)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise _fail(
                    self,
                    f"{what}= must be an int, got {value!r}.",
                    hint=f"Resources({what}=100)",
                )
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
class Shutdown(Keywords):
    """How a service is stopped: ``Shutdown(timeout="120s", signal="SIGINT")``."""

    _: KW_ONLY
    timeout: str | None = None
    signal: str | None = None
    kill_mode: Literal["control-group", "mixed", "process", "none"] | None = None
    final_kill: bool = True
    """Whether systemd sends ``SIGKILL`` after *timeout*; ``False`` waits instead."""

    def __post_init__(self) -> None:
        if self.timeout is not None and (not isinstance(self.timeout, str) or not self.timeout):
            raise _fail(
                self,
                f"timeout= {self.timeout!r} is not a systemd time span.",
                hint='Shutdown(timeout="30s")',
            )
        if self.signal is not None and (
            not isinstance(self.signal, str) or not self.signal.startswith("SIG")
        ):
            raise _fail(
                self,
                f"signal= {self.signal!r} is not a signal name.",
                hint='Shutdown(signal="SIGINT")',
            )
        if self.kill_mode is not None and self.kill_mode not in (
            "control-group",
            "mixed",
            "process",
            "none",
        ):
            raise _fail(
                self,
                f"kill_mode= {self.kill_mode!r} is not a systemd kill mode.",
                hint='Shutdown(kill_mode="mixed"): one of control-group, mixed, process, none.',
            )
        if not isinstance(self.final_kill, bool):
            raise _fail(
                self,
                f"final_kill= must be True or False, got {self.final_kill!r}.",
                hint="Shutdown(final_kill=False)",
            )

    def entries(self) -> tuple[tuple[str, str], ...]:
        """The ``[Service]`` directives beyond ``TimeoutStopSec=`` and ``KillMode=``."""
        out: list[tuple[str, str]] = []
        if self.signal is not None:
            out.append(("KillSignal", self.signal))
        if not self.final_kill:
            out.append(("SendSIGKILL", "no"))
        return tuple(out)


@dataclass(frozen=True, slots=True)
class HttpProbe(Keywords):
    """Readiness: the service answers *url* with a 2xx, polled every *interval* until
    *timeout*, both systemd time spans.

    The probe is a bounded unit of its own, so ``requires=[other.ready]`` means the
    dependency answered, not that its binary was exec'd. It needs ``curl``, which an
    image with an ``HttpProbe`` installs.
    """

    url: str
    _: KW_ONLY
    timeout: str = "90s"
    interval: str = "500ms"

    def __post_init__(self) -> None:
        if not isinstance(self.url, str) or not self.url.startswith(("http://", "https://")):
            raise _fail(
                self,
                f"url= {self.url!r} is not an http:// or https:// URL.",
                hint='HttpProbe("http://127.0.0.1:8080/health")',
            )
        if "'" in self.url or any(c.isspace() for c in self.url):
            raise _fail(
                self,
                f"url= {self.url!r} holds spaces or quotes.",
                hint='HttpProbe("http://127.0.0.1:8080/health"): one word, escaped as a URL.',
            )
        _check_span(self, self.timeout, self.interval)

    def command(self) -> tuple[str, ...]:
        """The argv of the probe unit."""
        loop = (
            f"until curl -fsS --max-time 5 '{self.url}' >/dev/null 2>&1; "
            f"do sleep {_sleep(self.interval)}; done"
        )
        return ("/bin/bash", "-c", loop)


@dataclass(frozen=True, slots=True)
class TcpProbe(Keywords):
    """Readiness: *host* accepts a TCP connection on *port*, polled every *interval* until
    *timeout*, both systemd time spans.

    The probe is a bounded unit of its own and uses bash's ``/dev/tcp``, which Debian
    always has: it installs nothing.
    """

    port: int
    _: KW_ONLY
    host: str = "127.0.0.1"
    timeout: str = "90s"
    interval: str = "500ms"

    def __post_init__(self) -> None:
        _check_port(self, self.port)
        if not isinstance(self.host, str) or not self.host or any(c in self.host for c in " '/"):
            raise _fail(
                self,
                f"host= {self.host!r} is not a host name or address.",
                hint=f'TcpProbe({self.port!r}, host="127.0.0.1")',
            )
        _check_span(self, self.timeout, self.interval)

    def command(self) -> tuple[str, ...]:
        """The argv of the probe unit."""
        loop = (
            f"until (exec 3<>/dev/tcp/{self.host}/{self.port}) 2>/dev/null; "
            f"do sleep {_sleep(self.interval)}; done"
        )
        return ("/bin/bash", "-c", loop)


@dataclass(frozen=True, slots=True)
class Notify(Keywords):
    """Readiness: the program itself reports ``READY=1`` on ``$NOTIFY_SOCKET``.

    The unit becomes ``Type=notify``, so systemd holds everything that requires it until
    the program says it is serving. It is the cheapest readiness there is, and the only
    one that needs no probe; the program has to implement ``sd_notify`` for it.
    """


type Readiness = HttpProbe | TcpProbe | Notify


_SPAN = re.compile(r"(?:\s*\d+(?:\.\d*)?\s*[A-Za-z\u00b5\u03bc]*)+\s*")
_SPAN_PART = re.compile(r"(\d+(?:\.\d*)?)\s*([A-Za-z\u00b5\u03bc]*)")
_SPAN_UNITS = {
    **dict.fromkeys(("ns", "nsec"), 1e-9),
    **dict.fromkeys(("us", "usec", "\u00b5s", "\u03bcs"), 1e-6),
    **dict.fromkeys(("ms", "msec"), 1e-3),
    **dict.fromkeys(("", "s", "sec", "second", "seconds"), 1.0),
    **dict.fromkeys(("m", "min", "minute", "minutes"), 60.0),
    **dict.fromkeys(("h", "hr", "hour", "hours"), 3600.0),
    **dict.fromkeys(("d", "day", "days"), 86400.0),
    **dict.fromkeys(("w", "week", "weeks"), 604800.0),
    **dict.fromkeys(("M", "month", "months"), 2629800.0),
    **dict.fromkeys(("y", "year", "years"), 31557600.0),
}


def _seconds(span: object) -> float | None:
    """*span*, a systemd time span (``"500ms"``, ``"1min 30s"``), in seconds, or ``None``."""
    if not isinstance(span, str) or not _SPAN.fullmatch(span):
        return None
    total = 0.0
    for number, unit in _SPAN_PART.findall(span):
        if unit not in _SPAN_UNITS:
            return None
        total += float(number) * _SPAN_UNITS[unit]
    return total


def _sleep(span: str) -> str:
    """*span* as the seconds ``sleep`` takes."""
    return f"{_seconds(span):.6f}".rstrip("0").rstrip(".")


def _check_span(owner: object, timeout: object, interval: object) -> None:
    for keyword, value, example in (("timeout", timeout, "90s"), ("interval", interval, "500ms")):
        seconds = _seconds(value)
        if seconds is None or seconds <= 0:
            raise _fail(
                owner,
                f"{keyword}= {value!r} is not a positive systemd time span.",
                hint=f'{type(owner).__name__}(..., {keyword}="{example}"): a span such as '
                "500ms, 30s, 10min or 1h.",
            )


@dataclass(frozen=True, slots=True)
class Credential(Keywords):
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
        if self.source == INSTANCE_KEY and type(self) is Credential:
            raise _fail(
                self,
                f"{INSTANCE_KEY} is the instance key: a Credential naming it skips the "
                "unit that generates it.",
                hint="Pass InstanceKey() (from tundravm.secrets) instead.",
            )
