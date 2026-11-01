"""The public :class:`Image` and :class:`Profile`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import TYPE_CHECKING

from tundravm._location import Located
from tundravm._source import FrozenMap
from tundravm.declarative import model as dm
from tundravm.declarative.model import TARGETS, Group, Target, User
from tundravm.errors import ValidationError

from ._change import Change, Edit, Remove
from ._component import Component
from ._ingress import TLS, AttestedTLS, Ingress
from ._rootfs import RootFS
from ._secrets import Secret
from ._service import Service
from ._storage import TpmSeal, Volume
from ._values import MANAGEMENT_PORT, Debian, File, Kernel, Mkosi, Package, Tree

if TYPE_CHECKING:
    from tundravm.components import DevAccess, Network, Observability

__all__ = ["Content", "Image", "Profile"]

type Content = str | Package | File | Tree | Service | Component | User | Group
"""What an image's (or a profile's) content may be."""

_CONTENT: tuple[type, ...] = (str, Package, File, Tree, Service, Component, User, Group)


def _fail(owner: object, message: str, *, hint: str | None = None) -> ValidationError:
    return ValidationError(f"{type(owner).__name__}: {message}", hint=hint)


def _tuple(owner: object, name: str, value: object) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _fail(owner, f"{name}= must be a list, got {value!r}.")
    return tuple(value)


def _check_content(owner: object, name: str, items: Sequence[object]) -> None:
    for item in items:
        if not isinstance(item, _CONTENT):
            raise _fail(
                owner,
                f"{name}= holds {item!r}, which is not image content.",
                hint="Pass a package name, Package, File, Tree, Service, Component, User or Group.",
            )


@dataclass(frozen=True, slots=True)
class Profile(Located):
    """A named variation on the image: what it adds, and what it changes.

    *extends* is the profile it starts from (``None``: the image itself, which is the
    ``production`` profile); a profile that extends another resolves as the chain it is.
    tundravm resolves every profile's whole image, then writes what all of them build once
    and what each builds beyond it in its own mkosi profile overlay; a declaration one
    profile removes leaves the shared payload for the overlays of the profiles that keep
    it, ``production``'s included. *add* is more content. *changes* edit or remove what the
    image declares, by reference: ``service.ref.edit(environment={...})``,
    ``component.service("x").ref.remove()``. An edit takes the keywords of the value's own
    constructor; removing a service removes its unit, its user, its firewall rule and the
    dependencies that are now unused. There is no string-keyed ``set``: a change names a
    declaration that exists, or the image does not resolve.

    ``production=False`` marks the profile's artifacts non-production, so a release never
    ships them by accident.
    """

    _: KW_ONLY
    extends: str | None = None
    add: Sequence[Content] = ()
    changes: Sequence[Change] = ()
    production: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "add", _tuple(self, "add", self.add))
        object.__setattr__(self, "changes", _tuple(self, "changes", self.changes))
        _check_content(self, "add", self.add)
        if self.extends is not None and (not isinstance(self.extends, str) or not self.extends):
            raise _fail(self, f"extends= must be a profile name or None, got {self.extends!r}.")
        if not isinstance(self.production, bool):
            raise _fail(self, "production= must be True or False.")
        for change in self.changes:
            if not isinstance(change, (Edit, Remove, dm.Edit, dm.Remove)):
                raise _fail(
                    self,
                    f"changes= holds {change!r}, which is not an edit or a removal.",
                    hint="Use value.ref.edit(FIELD=...) or value.ref.remove().",
                )


@dataclass(frozen=True, slots=True)
class Image(Located):
    """A confidential VM image: what it runs, and nothing about how it is built.

    Every slot is optional and holds small typed values; the slots are tuples after
    construction and the value is frozen, so an image can be shared, compared and
    planned without being copied.

    **Implied, with no further words, and the one escape hatch for each:**

    - *Debian trixie from the locked snapshot* (:data:`~tundravm._image._values.LOCKED_SNAPSHOT`),
      the one tested baseline. Escape: ``base=Debian("trixie", snapshot="...")``.
    - *A measured UKI* with the curated module set: ``tdx_guest``, virtio, NVMe, Hyper-V,
      gVNIC, plus whatever each target needs. Escape: ``kernel=Kernel.default(modules=...)``
      to keep more of Debian's, ``kernel=Kernel.build(...)`` to compile your own.
    - *The TEE baseline*: NTS time from two authenticated servers, a volatile journal, the
      sysctl hardening set, a static resolver, DHCP on ``en*``/``eth*``, and no apt timers
      or console getty. Escape: ``network=Network(...)`` for the network; every other piece
      is an ordinary declaration, so a ``Profile`` change removes or edits it by reference.
    - *A firewall that drops inbound traffic* except the ports services declare with
      ``expose=`` and the management port
      :data:`~tundravm._image._values.MANAGEMENT_PORT` (8745), which every image reserves;
      ``secret-delivery`` listens on it in an image with an operator- or broker-unlocked
      volume, or with secrets. Escape: ``expose=`` to open a port,
      ``network=Network(egress=Egress(...))`` to filter outbound too.
    - *One generated system user per service*, the preset sandbox and ``Restart=on-failure``.
      Escape: ``Service(user=...)``, ``hardening=``, ``unit=DropIn(...)``.
    - *The evidence service* (``tdxs`` from tundra-tools, socket-activated on
      ``/var/tdxs.sock``), which
      produces the quote an attestation is checked against. Escape:
      ``components=[Tdxs(source=Git(mirror, ref))]`` to build it from a mirror or a
      vendored checkout, or to validate peers too; a ``Profile`` change removing it by
      reference, for an image that is deliberately not attestable.
    - *The baseline components* ``TeeBase()``, ``Network()``, ``Firewall()`` and ``Tdxs()``
      (:mod:`tundravm.components`): passing one in ``components=`` replaces the default.
    - *The Azure and GCP provisioning glue*, inert off-platform
      (``ConditionVirtualization=``), so one measured payload boots on either cloud.
      Escape: ``targets=``.
    - *A raw bootable disk for QEMU* as the default artifact. Escape: ``targets=``.

    **The runtime contracts**. The image collects :attr:`volumes` and :attr:`secrets` from
    what binds them, so a volume or a secret is written once, where it is used:

    - LUKS2 volumes that unlock at boot (``OperatorKey()``, ``TpmSeal(...)`` or
      ``BrokerKey(...)``): a service binds a directory on one with
      ``state=volume.directory("x")`` and waits for it.
    - Secrets delivered after attestation and read as systemd credentials
      (``Service(credentials=[jwt])``); nothing is written into the image.
    - ``ingress=[AttestedTLS(port, upstream=...)]`` (or ``TLS``): the only path from
      outside to a service that listens on the loopback.
    - ``rootfs=RootFS.verity()``: a dm-verity erofs root instead of the initrd.
    - ``observability=Observability(logs=..., metrics=...)``: the authenticated export path.

    An image with an ``OperatorKey`` or ``BrokerKey`` volume, or with secrets, also gets
    today's control-plane bootstrap: ``secret-delivery`` takes the operator's SSH public
    key on the management port, root SSH accepts only that key, and the operator writes
    the passphrase and the secrets over it. Gate 3's attested endpoint replaces the write
    path on the same port; the image's runtime manifest
    (``/etc/tundravm/runtime.json``) records every volume, secret and ingress it needs.

    The starter is the whole contract::

        image = Image(
            "demo",
            packages=["busybox"],
            files=[File.text("/srv/www/index.html", "<h1>demo</h1>\\n")],
            services=[
                Service(
                    "app",
                    ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
                    expose=[TCP(8080)],
                ),
            ],
        )
    """

    name: str
    _: KW_ONLY
    base: Debian = Debian("trixie")
    packages: Sequence[str | Package] = ()
    files: Sequence[File | Tree] = ()
    services: Sequence[Service] = ()
    components: Sequence[Component] = ()
    ingress: Sequence[Ingress] = ()
    network: Network | None = None
    users: Sequence[User] = ()
    groups: Sequence[Group] = ()
    kernel: Kernel = field(default_factory=Kernel.default)
    rootfs: RootFS = field(default_factory=RootFS.initramfs)
    observability: Observability | None = None
    targets: Sequence[Target] = ("qemu",)
    dev: DevAccess | None = None
    profiles: Mapping[str, Profile] = field(default_factory=FrozenMap)
    mkosi: Mkosi | None = None
    volumes: tuple[Volume, ...] = field(init=False, default=())
    """Every volume a service's ``state=`` sits on, in order of first use."""
    secrets: tuple[Secret, ...] = field(init=False, default=())
    """Every secret a service binds or a ``TLS`` ingress takes, in order of first use."""

    def __post_init__(self) -> None:
        for name in (
            "packages",
            "files",
            "services",
            "components",
            "ingress",
            "users",
            "groups",
            "targets",
        ):
            object.__setattr__(self, name, _tuple(self, name, getattr(self, name)))
        object.__setattr__(self, "profiles", FrozenMap(dict(self.profiles)))
        if (
            not isinstance(self.name, str)
            or not self.name
            or any(c.isspace() or c == "/" for c in self.name)
        ):
            raise _fail(self, f"{self.name!r} is not an image name.")
        self._check_slots()
        self._check_services()
        self._check_profiles()
        from ._runtime import bound, check_bindings

        volumes, secrets = bound(self)
        object.__setattr__(self, "volumes", volumes)
        object.__setattr__(self, "secrets", secrets)
        self._check_runtime()
        check_bindings(self)

    def _check_slots(self) -> None:
        slots: tuple[tuple[str, Sequence[object], type | tuple[type, ...], str], ...] = (
            ("packages", self.packages, (str, Package), "a package name or Package"),
            ("files", self.files, (File, Tree), "a File or Tree"),
            ("users", self.users, User, "a User"),
            ("groups", self.groups, Group, "a Group"),
        )
        for slot, items, kinds, what in slots:
            for item in items:
                if not isinstance(item, kinds):
                    raise _fail(self, f"{slot}= holds {item!r}, not {what}.")
        if not isinstance(self.base, Debian):
            raise _fail(self, f"base= holds {self.base!r}, not a Debian.")
        if not isinstance(self.kernel, Kernel):
            raise _fail(self, f"kernel= holds {self.kernel!r}, not a Kernel.")
        from tundravm.components import DevAccess, Network, Observability

        if self.network is not None and not isinstance(self.network, Network):
            raise _fail(self, f"network= holds {self.network!r}, not a Network.")
        if self.dev is not None and not isinstance(self.dev, DevAccess):
            raise _fail(self, f"dev= holds {self.dev!r}, not a DevAccess.")
        if self.observability is not None and not isinstance(self.observability, Observability):
            raise _fail(self, f"observability= holds {self.observability!r}, not Observability.")
        if not isinstance(self.rootfs, RootFS):
            raise _fail(
                self,
                f"rootfs= holds {self.rootfs!r}, not a RootFS.",
                hint="Pass RootFS.initramfs() or RootFS.verity().",
            )
        if self.mkosi is not None and not isinstance(self.mkosi, Mkosi):
            raise _fail(self, f"mkosi= holds {self.mkosi!r}, not a Mkosi.")
        if not self.targets:
            raise _fail(self, "targets= names no target.", hint="Pass targets=('qemu',).")
        for target in self.targets:
            if target not in TARGETS:
                raise _fail(
                    self,
                    f"targets= names {target!r}.",
                    hint=f"Known targets: {', '.join(TARGETS)}.",
                )
        if len(set(self.targets)) != len(self.targets):
            raise _fail(self, "targets= names a target twice.")

    def _check_services(self) -> None:
        for item in self.services:
            if isinstance(item, Component):
                raise _fail(
                    self,
                    f"services= holds the component {type(item).__name__}.",
                    hint="Pass it in components=; a component is resolved before validation "
                    "and may declare more than one service.",
                )
            if not isinstance(item, Service):
                raise _fail(self, f"services= holds {item!r}, not a Service.")
        for entry in self.components:
            if not isinstance(entry, Component):
                raise _fail(
                    self,
                    f"components= holds {entry!r}, not a Component.",
                    hint="Subclass Component and give it a compose() method.",
                )

    def _check_runtime(self) -> None:
        """Volumes, secrets and ingress: types, unique names, and what the targets allow."""
        from tundravm.components import DevAccess

        for entry in self.components:
            if isinstance(entry, DevAccess):
                raise _fail(
                    self,
                    "components= holds DevAccess, which would put debug access in production.",
                    hint="Pass it as dev=DevAccess(...): it then lives in the non-production "
                    "'dev' profile.",
                )
        mounts: dict[str, str] = {}
        for volume in self.volumes:
            for other, name in mounts.items():
                if (
                    volume.mount == other
                    or volume.mount.startswith(other + "/")
                    or other.startswith(volume.mount + "/")
                ):
                    raise _fail(
                        self,
                        f"volumes {name!r} and {volume.name!r} mount at {other} and "
                        f"{volume.mount}, one inside the other.",
                    )
            mounts[volume.mount] = volume.name
            if isinstance(volume.unlock, TpmSeal):
                refusal = volume.unlock.refusal(tuple(self.targets))
                if refusal is not None:
                    raise _fail(
                        self,
                        f"volume {volume.name!r} cannot be TPM-sealed for targets "
                        f"{', '.join(self.targets)}: {refusal}.",
                        hint="One payload boots on every target, so seal to the vTPM of the "
                        "only target you trust, or use unlock=OperatorKey().",
                    )
        ports: set[int] = set()
        for way in self.ingress:
            if not isinstance(way, (AttestedTLS, TLS)):
                raise _fail(
                    self,
                    f"ingress= holds {way!r}, not AttestedTLS or TLS.",
                    hint="Plain TCP exposure is Service(expose=[TCP(port)]).",
                )
            if way.port in ports:
                raise _fail(self, f"ingress port {way.port} is declared twice.")
            if way.port == MANAGEMENT_PORT:
                raise _fail(self, f"ingress port {way.port} is the management ingress.")
            ports.add(way.port)

    def _check_profiles(self) -> None:
        for name, profile in self.profiles.items():
            if not isinstance(name, str) or not name:
                raise _fail(self, f"profiles= is keyed by {name!r}, not a profile name.")
            if not isinstance(profile, Profile):
                raise _fail(self, f"profile {name!r} holds {profile!r}, not a Profile.")
            if name == BASE_PROFILE:
                raise _fail(
                    self,
                    f"{BASE_PROFILE!r} is the image itself.",
                    hint="Name the profile something else, or change the image.",
                )
            if profile.extends is not None and profile.extends not in (
                BASE_PROFILE,
                *self.profiles,
            ):
                known = ", ".join(sorted({BASE_PROFILE, *self.profiles}))
                raise _fail(
                    self,
                    f"profile {name!r} extends {profile.extends!r}, which is not declared.",
                    hint=f"Declared profiles: {known}.",
                )
            if profile.extends == name:
                raise _fail(self, f"profile {name!r} extends itself.")
        if self.dev is not None and DEV_PROFILE in self.profiles:
            raise _fail(
                self,
                f"dev=DevAccess(...) and a {DEV_PROFILE!r} profile both declare one.",
                hint="Keep dev=, or write the profile yourself.",
            )


BASE_PROFILE = "production"
"""The profile the image itself is: what ``Profile(extends=None)`` starts from."""

DEV_PROFILE = "dev"
"""The non-production profile ``dev=DevAccess(...)`` adds."""
