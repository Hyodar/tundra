"""The runtime contracts (volumes, secrets, ingress) checked and lowered onto today's stack.

An image's :class:`~tundravm.storage.Volume`, :class:`~tundravm.secrets.Secret` and ingress
values become the declarations the tools and systemd already understand:

- a volume is a ``Key`` (``key-gen``: a FIFO for ``OperatorKey``/``BrokerKey``, a
  vTPM-persisted random key for ``TpmSeal``) and a LUKS2 ``Disk`` (``disk-setup``, found
  by its ``/dev/disk/by-id/`` name, formatted only while uninitialised), plus a boot step
  that creates the directories services bind, owned by them; on Azure a udev rule gives
  that name to the data disk at the volume's LUN, its position in the image's volumes;
- the secrets are entries of one ``Secrets`` block whose targets are files under
  ``/run/tundravm/credentials``, written by the attested endpoint on a session whose TLS
  key a fresh quote bound, plus one boot unit per secret that finishes once it has arrived;
- a :class:`~tundravm.secrets.Generated` secret is a root boot oneshot that writes random
  hex under ``/run/tundravm/secrets``, which every service binding it requires;
- a volume an operator unlocks, any secret or an ``AttestedTLS`` ingress brings the
  control plane: that ``Secrets`` block is the attested endpoint on the management port
  (evidence, credentials, unlock, the operator's SSH key), and
  ``SshAccess(from_delivery=True)`` lets only that key in, as root, or as the
  ``Login(owner=True)`` of an ``SshAccess`` declaring logins, where root takes none. On a
  TDX boot the endpoint takes SSH keys (root's and a login's) only from an owner the
  volume's encrypted copy confirmed, so a TDX image without an ``OperatorKey()`` volume
  gets no delivered SSH key at all (bake them with ``SshAccess(keys=)``);
- an ingress is a ``socat`` TLS relay unit with its firewall port; an ``AttestedTLS`` one
  also has a root oneshot making its key pair and certificate each boot, which the relay
  reads as credentials and the endpoint's quotes bind;
- a ``Login(keys=Secret(...))`` adds that secret, which the endpoint also writes to the
  login's ``authorized_keys.d`` file, on TDX from a confirmed owner only;
- each profile's endpoint takes, and its boot waits for, only the secrets its own units read
  (:func:`reads`), so a development profile whose observability stays in the guest never
  waits for the export's credential;
- ``/etc/tundravm/runtime.json`` records all of it for the tools that deliver.

Nothing here writes a secret or a key into the image.
"""

from __future__ import annotations

import dataclasses
import json
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tundravm._location import declared_at, display_location
from tundravm._modules import attested_tls, container_ssh
from tundravm._platforms import azure
from tundravm.declarative import model as dm
from tundravm.declarative import utils as du
from tundravm.declarative.systemd import Dynamic, Hardening, Root
from tundravm.errors import ValidationError

from ._component import Component, composed
from ._ingress import TLS, AttestedTLS
from ._secrets import (
    GENERATED_DIRECTORY,
    RUNTIME_DIRECTORY,
    BrokerKey,
    Generated,
    Secret,
)
from ._service import Service
from ._storage import TPM_PLATFORMS, OperatorKey, TpmSeal, Volume, VolumeDirectory
from ._values import MANAGEMENT_PORT

if TYPE_CHECKING:
    from tundravm.components.container import Container

__all__ = [
    "CONTROL_PLANE",
    "RUNTIME_FRAGMENT",
    "RUNTIME_MANIFEST",
    "Reads",
    "Runtime",
    "RuntimeFragment",
    "bound",
    "check_bindings",
    "check_owner_login",
    "container_ports",
    "key_files",
    "reads",
    "services_of",
]

RUNTIME_MANIFEST = "/etc/tundravm/runtime.json"
"""What the image needs at run time, for the tools that deliver it."""

CONTROL_PLANE = "tundravm"
"""The ``Secrets`` block that is the control plane: the attested endpoint."""


class RuntimeFragment(dm.Fragment):
    """The fragment the runtime contracts lower into: shipped by tundravm, so lint reads
    its units as the library's, like any :mod:`tundravm.components` fragment."""

    __slots__ = ()


RUNTIME_FRAGMENT = "tundravm-runtime"
"""Its name, which the tree's ``mkosi.conf.d`` file for its packages carries."""


def services_of(items: Iterable[object]) -> Iterator[Service]:
    """Every public :class:`Service` in *items*, components expanded."""
    for item in items:
        if isinstance(item, Service):
            yield item
        elif isinstance(item, Component):
            yield from services_of(composed(item))


def _image_items(image: object) -> tuple[object, ...]:
    """The services and components of the image and of every profile it declares, each
    profile's edits of them included."""
    from ._change import Edit
    from ._image import Image

    assert isinstance(image, Image)
    items: list[object] = [*image.services, *image.components]
    if image.observability is not None:
        items.append(image.observability)
    for profile in image.profiles.values():
        items.extend(profile.add)
        items.extend(change.edited for change in profile.changes if isinstance(change, Edit))
    return tuple(items)


def _image_services(image: object) -> tuple[Service, ...]:
    """The services of the image and of every profile it declares, each profile's edits of
    them included."""
    return tuple(services_of(_image_items(image)))


def _logins(items: Iterable[object]) -> Iterator[du.Login]:
    """Every ``Login`` of an ``SshAccess`` in *items*, components expanded."""
    from tundravm.components.access import SshAccess

    for item in items:
        if isinstance(item, SshAccess):
            yield from item.logins
        elif isinstance(item, Component):
            yield from _logins(composed(item))


def _containers(items: Iterable[object]) -> Iterator[Container]:
    """Every ``Container`` running an sshd (``ssh=``) in *items*, components expanded."""
    from tundravm.components.container import Container

    for item in items:
        if isinstance(item, Container):
            if item.ssh is not None:
                yield item
        elif isinstance(item, Component):
            yield from _containers(composed(item))


def key_files(image: object) -> tuple[tuple[str, str], ...]:
    """``(secret, path)`` for each ``Login(keys=Secret(...))`` and
    ``ContainerSsh(keys=Secret(...))``: the attested endpoint also writes that secret, a
    public key list, to the login's ``authorized_keys.d`` file or the container's keys
    directory."""
    items = _image_items(image)
    logins = [
        (login.keys.name, login.key_file)
        for login in _logins(items)
        if isinstance(login.keys, Secret)
    ]
    containers = [
        (container.ssh.keys.name, container_ssh.authorized_keys(container.name))
        for container in _containers(items)
        if container.ssh is not None and isinstance(container.ssh.keys, Secret)
    ]
    return tuple(dict.fromkeys((*logins, *containers)))


def container_ports(image: object) -> tuple[tuple[str, int], ...]:
    """``(name, port)`` of each container's sshd, whose host keys every quote binds."""
    found = {
        container.name: container.ssh.port
        for container in _containers(_image_items(image))
        if container.ssh is not None
    }
    return tuple(sorted(found.items()))


def check_owner_login(image: object, ssh: object) -> None:
    """Refuse a ``Login(owner=True)`` outside the image's own ``SshAccess``: the control plane
    hands the owner's key only to the one in ``components=``."""
    from tundravm.components.access import SshAccess

    own = {login.name for login in ssh.logins} if isinstance(ssh, SshAccess) else set()
    for login in _logins(_image_items(image)):
        if login.owner and login.name not in own:
            raise ValidationError(
                f"Login {login.name}: owner=True takes the key attest hands over, which only "
                "the image's own SshAccess receives, and this one is declared elsewhere.",
                hint="Pass its SshAccess in Image(components=[...]).",
            )


def _first[T: (Volume, Secret)](kind: str, found: dict[str, T], value: T) -> None:
    known = found.setdefault(value.name, value)
    if known != value:
        where = [display_location(declared_at(v)) for v in (known, value)]
        lines = f" (at {where[0]} and {where[1]})" if all(where) else ""
        named = f'{type(value).__name__}("{value.name}")'
        raise ValidationError(
            f"Image: two different values are {named}{lines}: {known!r} and {value!r}.",
            hint=f"Declare it once, {value.name} = {named[:-1]}, ...), and pass that value "
            "everywhere it is used.",
        )


def bound(image: object) -> tuple[tuple[Volume, ...], tuple[Secret, ...]]:
    """The volumes the image's services keep state on and the secrets they bind (each
    ``TLS`` certificate too), in order of first use: what ``image.volumes`` and
    ``image.secrets`` are."""
    from ._image import Image

    assert isinstance(image, Image)
    volumes: dict[str, Volume] = {}
    secrets: dict[str, Secret] = {}
    for service in _image_services(image):
        if isinstance(service.state, VolumeDirectory):
            _first("volume", volumes, service.state.volume)
        for secret in service.secrets:
            _first("secret", secrets, secret)
    for login in _logins(_image_items(image)):
        if isinstance(login.keys, Secret):
            _first("secret", secrets, login.keys)
    for container in _containers(_image_items(image)):
        if container.ssh is not None and isinstance(container.ssh.keys, Secret):
            _first("secret", secrets, container.ssh.keys)
    for way in image.ingress:
        if isinstance(way, TLS):
            _first("secret", secrets, way.certificate)
    return tuple(volumes.values()), tuple(secrets.values())


@dataclass(frozen=True, slots=True)
class Reads:
    """What one profile's units read of the image's secrets: the secrets, in the image's
    order, and the services that bind them."""

    secrets: tuple[Secret, ...] = ()
    services: tuple[Service, ...] = ()


def reads(image: object, items: Iterable[object], services: Iterable[Service]) -> Reads:
    """The secrets of ``image.secrets`` one profile reads: what its *services* bind, the keys
    a ``Login`` or a container's sshd among its *items* takes, and each ``TLS``
    certificate. A secret nothing there reads stays out: the profile's endpoint does not
    take it and its boot does not wait for it."""
    from ._image import Image

    assert isinstance(image, Image)
    items, services = tuple(items), tuple(services)
    names = {secret.name for service in services for secret in service.secrets}
    names.update(login.keys.name for login in _logins(items) if isinstance(login.keys, Secret))
    names.update(
        container.ssh.keys.name
        for container in _containers(items)
        if container.ssh is not None and isinstance(container.ssh.keys, Secret)
    )
    names.update(way.certificate.name for way in image.ingress if isinstance(way, TLS))
    return Reads(
        tuple(secret for secret in image.secrets if secret.name in names),
        tuple(service for service in services if service.secrets),
    )


def check_bindings(image: object) -> None:
    """Every shared directory says who shares it, and no service exposes what an ingress
    fronts."""
    from ._image import Image

    assert isinstance(image, Image)
    services = _image_services(image)
    owners: dict[str, list[str]] = {}
    for service in services:
        state = service.state
        if isinstance(state, VolumeDirectory):
            owners.setdefault(state.path, []).append(service.name)
            if len(set(owners[state.path])) > 1 and state.group is None:
                raise ValidationError(
                    f"Image: {state.path} is the state of {' and '.join(owners[state.path])}, "
                    "but says nothing about sharing it.",
                    hint="Give each service its own directory, or share one through a group: "
                    "volume.directory(name, group='eth').",
                )
    exposed = {
        (port.port, port.protocol): service.name for service in services for port in service.expose
    }
    for way in image.ingress:
        owner = exposed.get((way.upstream.port, "tcp"))
        if owner is not None:
            raise ValidationError(
                f"Image: {type(way).__name__}({way.port}) fronts {way.upstream.url}, but "
                f"service {owner!r} also exposes port {way.upstream.port}, so the ingress "
                "is not the only path to it.",
                hint=f"Drop expose=[TCP({way.upstream.port})] from {owner!r}.",
            )
        if (way.port, "tcp") in exposed:
            raise ValidationError(
                f"Image: ingress port {way.port} is also exposed by service "
                f"{exposed[(way.port, 'tcp')]!r}.",
                hint="Drop the port from the service's expose=: the ingress opens it.",
            )


@dataclass(slots=True)
class Runtime:
    """The runtime contracts of one image, ready to lower."""

    volumes: tuple[Volume, ...]
    secrets: tuple[Secret, ...]
    """Every secret some profile reads: with one, every profile runs the endpoint."""
    ingress: tuple[AttestedTLS | TLS, ...]
    services: tuple[Service, ...]
    tools: dm.Git | dm.Local
    rootfs: str
    key_files: tuple[tuple[str, str], ...] = ()
    """``(secret, path)``: a login's or a container's delivered keys, written to its
    ``authorized_keys`` file (``0644``: sshd reads it as the login) beside the secret's own
    location."""
    containers: tuple[tuple[str, int], ...] = ()
    """``(name, port)`` of each container's sshd, whose host keys the quote binds."""
    ssh: Component | None = None
    """The image's own ``SshAccess``, which the control plane runs with ``from_delivery``
    on instead of a second one."""
    switchable: bool = False
    """The firewall has modes, which an operator switches over the attested endpoint."""
    azure: bool = False
    """The image targets Azure: each volume is the data disk at its LUN, its position."""
    read: Reads = Reads()
    """What the image's own profile reads, which its endpoint takes, its boot waits for and
    its manifest records; every other profile edits them to its own (:meth:`changes`)."""

    @property
    def owner_login(self) -> str | None:
        """The ``Login(owner=True)`` of the image's ``SshAccess``, which takes the owner's key
        instead of root."""
        from tundravm.components.access import SshAccess

        if not isinstance(self.ssh, SshAccess):
            return None
        return next((login.name for login in self.ssh.logins if login.owner), None)

    def terminal(self, development: bool) -> bool:
        """Whether the owner login's delivered key may have a terminal: in a *development*
        build, when the login has ``dev=True`` commands."""
        from tundravm.components.access import SshAccess

        if not development or not isinstance(self.ssh, SshAccess):
            return False
        return any(login.owner and login.developed for login in self.ssh.logins)

    @property
    def root_delivery(self) -> bool:
        """Whether root takes the delivered key: unless the image's ``SshAccess`` declares a
        login and does not ask for it with ``from_delivery=True``."""
        from tundravm.components.access import SshAccess

        if not isinstance(self.ssh, SshAccess) or not self.ssh.logins:
            return True
        return bool(self.ssh.from_delivery)

    @property
    def control_plane(self) -> bool:
        """Whether the image runs the attested endpoint: a secret, a volume an operator
        unlocks, an attested TLS ingress whose certificate its quotes bind, or firewall
        modes to switch."""
        return (
            bool(self.secrets)
            or bool(self.attested)
            or self.switchable
            or any(isinstance(volume.unlock, (OperatorKey, BrokerKey)) for volume in self.volumes)
        )

    @property
    def attested(self) -> tuple[AttestedTLS, ...]:
        """The attested TLS ingress, in port order: the quote binds each one's key."""
        return tuple(
            sorted(
                (way for way in self.ingress if isinstance(way, AttestedTLS)),
                key=lambda way: way.port,
            )
        )

    def check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``volume-tpm-on-cloud``: a volume sealed to a cloud's vTPM, whose trust boundary
        is the platform's, where an operator or a broker could release the key instead."""
        found: list[dm.Diagnostic] = []
        for volume in self.volumes:
            unlock = volume.unlock
            if not isinstance(unlock, TpmSeal) or unlock.platform not in resolved.targets:
                continue
            found.append(
                dm.Diagnostic(
                    "volume-tpm-on-cloud",
                    "info",
                    f"volume {volume.name!r} unlocks with a key sealed to "
                    f"{TPM_PLATFORMS[unlock.platform]}, and today's key-gen binds it to no PCR",
                    subject=volume.name,
                    location=display_location(declared_at(volume)),
                    hint="To release the key only to an instance that attested, unlock it "
                    "with OperatorKey() (an operator sends it each boot) or BrokerKey(url, "
                    "audience=...) (a key broker does, unattended).",
                )
            )
        return tuple(found)

    # ── Per service ──────────────────────────────────────────────────

    def waits(self, service: Service) -> tuple[str, ...]:
        """The boot units *service* waits for: its required secrets, the generated ones and
        its volume."""
        units = [secret.ready.unit for secret in service.secrets if secret.required]
        units += [item.unit for item in service.bindings if isinstance(item, Generated)]
        if isinstance(service.state, VolumeDirectory):
            units.append(service.state.ready.unit)
        return tuple(units)

    def optional(self, service: Service) -> tuple[str, ...]:
        """``SetCredential=`` lines: an optional secret reads as one newline until it
        arrives. systemd refuses an empty ``SetCredential=`` value (``Invalid syntax``), and
        without the fallback a missing ``LoadCredential=`` file fails the start."""
        return tuple(f"{s.name}:\\n" for s in service.secrets if not s.required)

    # ── The image ────────────────────────────────────────────────────

    def generated(self) -> tuple[Generated, ...]:
        """The generated secrets the services bind, one per name, in order of first use."""
        found: dict[str, Generated] = {}
        for service in self.services:
            for item in service.bindings:
                if not isinstance(item, Generated):
                    continue
                known = found.setdefault(item.name, item)
                if known != item:
                    raise ValidationError(
                        f"Image: two different generated secrets are called {item.name!r}: "
                        f"{known!r} and {item!r}.",
                        hint="Declare the Generated once and pass that value to every "
                        "service that reads it.",
                    )
        return tuple(found.values())

    def declarations(self) -> tuple[dm.Declaration | Component, ...]:
        """Everything the contracts add to the image (a component is lowered as one)."""
        found: list[dm.Declaration | Component] = []
        if self.owner_login is not None and not self.control_plane:
            raise ValidationError(
                f"Login {self.owner_login}: owner=True takes the key attest hands over, and "
                "this image runs no attested endpoint to take it.",
                hint="Give the image a Volume(..., unlock=OperatorKey()), whose owner it is, "
                "or keys= of its own.",
            )
        if self.volumes or self.control_plane:
            found.append(dm.RuntimeTools(self.tools))
        for volume in self.volumes:
            found.extend(self.volume(volume))
        if self.azure and self.volumes:
            rules = azure.volume_rules([volume.serial for volume in self.volumes])
            found.append(dm.File(azure.VOLUME_RULES_PATH, rules))
        found.extend(_generate(item) for item in self.generated())
        if self.control_plane:
            found.extend(self.delivery())
        if self.attested:
            found.append(dm.File(attested_tls.SCRIPT_PATH, attested_tls.SCRIPT, mode=0o755))
        for way in self.ingress:
            found.extend(self.relay(way))
        if self.volumes or self.secrets or self.ingress:
            found.append(dm.File(RUNTIME_MANIFEST, self.manifest(self.read)))
        return tuple(found)

    def volume(self, volume: Volume) -> Iterator[dm.Declaration]:
        """The key, the LUKS2 disk and the step that prepares the directories."""
        unlock = volume.unlock
        output = f"{RUNTIME_DIRECTORY}/keys/{volume.name}"
        if isinstance(unlock, TpmSeal):
            key = dm.Key(f"{volume.name}-key", output=output, persist_in_tpm=True)
        else:
            key = dm.Key(
                f"{volume.name}-key",
                output=output,
                strategy="pipe",
                pipe=volume.pipe,
                persist_in_tpm=False,
            )
        yield key
        yield dm.Disk(
            volume.name,
            mount=volume.mount,
            device=volume.device,
            key=key,
            format="on_initialize",
            directories=(),
            owner=isinstance(unlock, OperatorKey),
        )
        lines = [f"# tundravm: the directories services keep on volume {volume.name}"]
        for path, (owner, group, mode) in sorted(self.directories(volume).items()):
            lines.append(
                f"/usr/bin/install -d -o {owner} -g {group} -m {mode:04o} {shlex.quote(path)}"
            )
        yield dm.BootStep(volume.step, "\n".join(lines) + "\n", after=(dm.Ready("disks"),))

    def directories(self, volume: Volume) -> dict[str, tuple[str, str, int]]:
        """``path -> (owner, group, mode)`` of each directory a service binds on *volume*."""
        found: dict[str, tuple[str, str, int]] = {}
        for service in self.services:
            state = service.state
            if not isinstance(state, VolumeDirectory) or state.volume != volume:
                continue
            user = service.user or service.stem
            if state.group is not None:
                found[state.path] = ("root", state.group, state.mode)
            else:
                found[state.path] = (user, user, state.mode)
        return found

    def delivery(self) -> Iterator[dm.Declaration | Component]:
        """The control plane: the attested endpoint, SSH for the delivered key (root's, or the
        owner login's), and the credential waits of the required secrets; an optional secret
        has none, so the boot completes without it."""
        from tundravm.components.access import SshAccess

        yield dm.Secrets(
            CONTROL_PLANE,
            entries=self.entries(self.read.secrets),
            port=MANAGEMENT_PORT,
            ssh_directory="/root/.ssh" if self.root_delivery else None,
            ssh_key_path=None,
            ssh_login=self.owner_login,
            attested_tls=tuple(way.port for way in self.attested),
            containers=self.containers,
        )
        if isinstance(self.ssh, SshAccess):
            yield dataclasses.replace(self.ssh, from_delivery=self.root_delivery)
        else:
            yield SshAccess(from_delivery=True)
        yield from self.waits_for(self.read.secrets)

    def entries(self, secrets: Iterable[Secret]) -> tuple[dm.Secret, ...]:
        """The endpoint's entry of each of *secrets*: its credential file, and the
        ``authorized_keys`` files of the logins and containers it is the keys of."""
        return tuple(
            dm.Secret(
                secret.name,
                (
                    dm.SecretFile(secret.location, mode=0o600),
                    *(dm.SecretFile(p, mode=0o644) for s, p in self.key_files if s == secret.name),
                ),
                required=secret.required,
            )
            for secret in secrets
        )

    def waits_for(self, secrets: Iterable[Secret]) -> Iterator[dm.Task]:
        """The boot unit of each required one of *secrets*, which finishes once it arrived."""
        for secret in secrets:
            if secret.required:
                yield _wait(
                    secret.ready.unit,
                    f"Wait for secret {secret.name}",
                    f"until [ -e {shlex.quote(secret.location)} ]; do sleep 1; done",
                )

    def changes(
        self, parent: Reads, own: Reads, local: tuple[bool, bool] = (False, False)
    ) -> tuple[tuple[dm.Edit | dm.Remove, ...], tuple[dm.Task, ...]]:
        """What turns a profile's runtime from the one that reads *parent* into the one that
        reads *own* (*local*: whether each is a development one): the edits of the
        endpoint's entries, of its terminal for the owner login's key and of the manifest,
        the removal of each wait it no longer needs, and the waits it adds."""
        terminal = self.terminal(local[1])
        if not self.control_plane or (parent == own and terminal == self.terminal(local[0])):
            return (), ()
        found: list[dm.Edit | dm.Remove] = []
        fields: dict[str, object] = {}
        entries = self.entries(own.secrets)
        if entries != self.entries(parent.secrets):
            fields["entries"] = entries
        if terminal != self.terminal(local[0]):
            fields["ssh_terminal"] = terminal
        if fields:
            found.append(dm.Secrets.ref(CONTROL_PLANE).edit(**fields))
        manifest = self.manifest(own)
        if manifest != self.manifest(parent):
            found.append(dm.File.ref(RUNTIME_MANIFEST).edit(content=manifest))
        before = {task.name: task for task in self.waits_for(parent.secrets)}
        after = {task.name: task for task in self.waits_for(own.secrets)}
        found.extend(task.ref.remove() for name, task in before.items() if name not in after)
        return tuple(found), tuple(task for name, task in after.items() if name not in before)

    def relay(self, way: AttestedTLS | TLS) -> Iterator[dm.Declaration]:
        """The TLS listener relaying *way*'s port to its upstream, started once the
        upstream's readiness holds; an attested one with the root oneshot that makes its
        key pair each boot (:mod:`._modules.attested_tls`), whose script
        :meth:`declarations` ships once."""
        yield dm.Package("socat")
        name = way.unit.removesuffix(".service")
        upstream = f"TCP:{way.upstream.address}"
        ready = _upstream_ready(way)
        hardening = Hardening.preset(
            "static",
            capabilities=("CAP_NET_BIND_SERVICE",) if way.port < 1024 else (),
        )
        if isinstance(way, AttestedTLS):
            yield dm.Package(attested_tls.PACKAGE)
            key_unit = attested_tls.unit(way.port)
            yield dm.Task(
                key_unit.removesuffix(".service"),
                ("/bin/sh", attested_tls.SCRIPT_PATH, str(way.port)),
                description=f"Generate this boot's TLS key for :{way.port}",
                user=Root(),
                timeout="60s",
                needs=(),
                hardening=Hardening.strict(protect_system="full", network="none"),
                directives={"Service": {"StandardOutput": "journal+console"}},
            )
            listen = (
                f"OPENSSL-LISTEN:{way.port},reuseaddr,fork,verify=0,"
                "cert=${CREDENTIALS_DIRECTORY}/cert,key=${CREDENTIALS_DIRECTORY}/key"
            )
            yield dm.Service(
                name,
                ("/usr/bin/socat", listen, upstream),
                description=f"Attested TLS on :{way.port} to {way.upstream.url}",
                credentials={
                    "cert": attested_tls.certificate(way.port),
                    "key": attested_tls.key(way.port),
                },
                requires=(key_unit, *ready),
                after=(key_unit, *ready),
                ports=(dm.Port(way.port),),
                needs=(),
                hardening=hardening,
            )
            return
        listen = f"OPENSSL-LISTEN:{way.port},reuseaddr,fork,verify=0,cert={way.certificate.path}"
        yield dm.Service(
            name,
            ("/usr/bin/socat", listen, upstream),
            description=f"TLS on :{way.port} to {way.upstream.url}",
            credentials={way.certificate.name: way.certificate.location},
            requires=(way.certificate.ready.unit, *ready),
            after=(way.certificate.ready.unit, *ready),
            ports=(dm.Port(way.port),),
            needs=(),
            hardening=hardening,
        )

    def manifest(self, read: Reads) -> str:
        """``/etc/tundravm/runtime.json`` of the profile that reads *read*."""
        bound: dict[str, list[str]] = {}
        for service in read.services:
            for secret in service.secrets:
                bound.setdefault(secret.name, []).append(service.unit_name)
        for way in self.ingress:
            if isinstance(way, TLS):
                bound.setdefault(way.certificate.name, []).append(way.unit)
        secrets = [
            {
                "name": secret.name,
                "required": secret.required,
                "location": secret.location,
                "source": None if secret.source is None else secret.source.manifest(),
                "units": sorted(set(bound.get(secret.name, []))),
            }
            for secret in read.secrets
        ]
        volumes = []
        for lun, volume in enumerate(self.volumes):
            entry = volume.manifest()
            entry["device"] = volume.device
            if self.azure:
                entry["lun"] = lun
            if not isinstance(volume.unlock, TpmSeal):
                entry["pipe"] = volume.pipe
            entry["directories"] = sorted(self.directories(volume))
            volumes.append(entry)
        payload = {
            "schema": 1,
            "management_port": MANAGEMENT_PORT,
            "control_plane": (
                "attested endpoint: TLS bound to the quote" if self.control_plane else None
            ),
            "rootfs": self.rootfs,
            "volumes": volumes,
            "secrets": secrets,
            "ingress": [way.manifest() for way in self.ingress],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _upstream_ready(way: AttestedTLS | TLS) -> tuple[str, ...]:
    """The unit that proves *way*'s upstream ready, which the relay requires and follows:
    the service's probe, or the service itself under ``Notify``; none when the upstream
    declares no readiness."""
    ready = way.upstream.ready
    service = None if ready is None else ready.service
    if service is None or service.readiness is None:
        return ()
    return (service.probe_unit or service.unit_name,)


def _generate(secret: Generated) -> dm.Task:
    """The root oneshot that writes *secret* once per boot, on tmpfs only."""
    path = shlex.quote(secret.source)
    script = (
        f"mkdir -p -m 0711 {RUNTIME_DIRECTORY} && mkdir -p -m 0700 {GENERATED_DIRECTORY} && "
        f"if [ ! -s {path} ]; then "
        f"od -An -v -N{secret.bytes} -tx1 /dev/urandom | tr -d ' \\n' > {path}.tmp && "
        f"[ -s {path}.tmp ] && mv {path}.tmp {path}; fi"
    )
    return dm.Task(
        secret.unit.removesuffix(".service"),
        ("/bin/sh", "-c", script),
        description=f"Generate this boot's secret {secret.name}",
        user=Root(),
        timeout="60s",
        needs=(),
        hardening=Hardening.strict(protect_system="full", network="none"),
    )


def _wait(unit: str, description: str, script: str) -> dm.Task:
    """A boot oneshot that finishes once *script*'s loop ends; it never times out."""
    return dm.Task(
        unit.removesuffix(".service"),
        ("/bin/sh", "-c", script),
        description=description,
        user=Dynamic(),
        timeout=None,
        needs=(),
        hardening=Hardening.preset("static"),
    )
