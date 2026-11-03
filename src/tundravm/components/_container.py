"""``Container``: a program in a Debian root of its own, built with the image, with no network.

``docs/design/container-2026-11-03.md`` is the design. The root is the subimage
``mkosi.images/<name>/`` (a :class:`~tundravm.declarative.model.ContainerRoot`), built from
the image's snapshot and shipped in the measured payload; the program is a
:class:`~tundravm.Service` whose drop-in runs it in that root image, in a user and a network
namespace of its own; each exposed port is a socket on the host relayed into that
namespace by ``systemd-socket-proxyd``.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator, Sequence
from dataclasses import field

from tundravm._image._component import Component
from tundravm._image._service import Service
from tundravm._image._storage import VolumeDirectory
from tundravm._image._values import TCP, UDP, Egress, File, PortValue
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Condition, Hardening, Root
from tundravm.diagnostics import Rule
from tundravm.errors import ValidationError
from tundravm.models import CONTAINERS_PATH

__all__ = ["Container"]

PROXY = "/usr/lib/systemd/systemd-socket-proxyd"
"""What relays a host port into the container's network namespace (the ``systemd``
package's, in every image)."""
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
    (``PrivateUsers=yes``) and a network namespace holding only ``lo``; *state* is one
    volume directory, which it sees at the same path; each port of *expose* (TCP) is a
    socket on the host, open in the firewall, relayed into its namespace. *egress* is what
    it may send to: ``Egress()``, nothing, which the empty namespace enforces; a non-empty
    one is not built yet. Component name: *name*, which names its unit too.
    """

    name: str
    argv: Sequence[str]
    packages: Sequence[str] = ()
    files: Sequence[File] = ()
    state: VolumeDirectory | None = None
    user: str | None = None
    expose: Sequence[PortValue] = ()
    egress: Egress = field(default_factory=Egress)

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
        }
        for keyword, value in given.items():
            object.__setattr__(self, keyword, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        for keyword in ("argv", "packages", "files", "expose"):
            value = getattr(self, keyword)
            if isinstance(value, (str, bytes, TCP, UDP, File)) or not isinstance(value, Sequence):
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
        for port in self.expose:
            if not isinstance(port, TCP):
                raise _fail(
                    f"expose= holds {port!r}: a container's ports are relayed by "
                    "systemd-socket-proxyd, which carries TCP only.",
                    hint="expose=[TCP(8081)]",
                )
        if not isinstance(self.egress, Egress):
            raise _fail(f"egress= holds {self.egress!r}, not an Egress.", hint="egress=Egress()")
        if self.egress.ports or self.egress.hosts:
            raise _fail(
                "a non-empty egress= needs a veth through the host's forward chain, which "
                "this release does not build: a container reaches nothing.",
                hint="Drop egress= (the container sends nothing), or run the program as a "
                "Service, which the image's Egress filters.",
            )

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
        yield dm.ContainerRoot(
            self.name,
            tuple(self.packages),
            files=tuple(dm.File(item.path, item.content, item.mode) for item in self.files),
            directories=mounts,
        )
        directives: dict[str, str | tuple[str, ...]] = {
            "RootImage": self.image,
            "MountAPIVFS": "yes",
            "PrivateUsers": "yes",
            "PrivateNetwork": "yes",
        }
        if mounts:
            directives["BindPaths"] = tuple(f"{path}:{path}" for path in mounts)
        yield Service(
            self.name,
            list(self.argv),
            state=self.state,
            user=self.user,
            unit=dm.DropIn(
                "20-container",
                requires=(self.loader,),
                after=(self.loader,),
                extra={"Service": directives},
            ),
        )
        for port in self.expose:
            yield Service(
                f"{self.name}-forward-{port.port}",
                [PROXY, f"127.0.0.1:{port.port}"],
                socket=dm.Listen(port.port),
                expose=[port],
                unit=dm.DropIn(
                    "20-container",
                    requires=(self.unit,),
                    after=(self.unit,),
                    extra={
                        "Unit": {"JoinsNamespaceOf": self.unit},
                        "Service": {"PrivateNetwork": "yes"},
                    },
                ),
            )

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
