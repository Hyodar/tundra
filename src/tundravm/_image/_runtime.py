"""The runtime contracts (volumes, secrets, ingress) checked and lowered onto today's stack.

An image's :class:`~tundravm.storage.Volume`, :class:`~tundravm.secrets.Secret` and ingress
values become the declarations tundra-tools and systemd already understand:

- a volume is a ``Key`` (``key-gen``: a FIFO for ``OperatorKey``/``BrokerKey``, a
  vTPM-persisted random key for ``TpmSeal``) and a LUKS2 ``Disk`` (``disk-setup``, found
  by its ``/dev/disk/by-id/`` name, formatted only while uninitialised), plus a boot step
  that creates the directories services bind, owned by them;
- the secrets are entries of one ``Secrets`` block whose targets are files under
  ``/run/tundravm/credentials``, written by the operator (an ``ExternalDelivery``), plus
  one boot unit per secret that finishes once it has arrived;
- a volume an operator unlocks, or any secret, brings today's control-plane bootstrap:
  that ``Secrets`` block listens on the management port for the operator's SSH public
  key, and ``SshAccess(from_delivery=True)`` lets only that key in;
- an ingress is a ``socat`` TLS relay unit with its firewall port;
- ``/etc/tundravm/runtime.json`` records all of it for the tools that deliver.

Nothing here writes a secret or a key into the image.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Dynamic, Hardening
from tundravm.errors import ValidationError

from ._component import Component, composed
from ._ingress import TLS, AttestedTLS
from ._secrets import CREDENTIALS_DIRECTORY, RUNTIME_DIRECTORY, BrokerKey, Secret
from ._service import Service
from ._storage import OperatorKey, TpmSeal, Volume, VolumeDirectory
from ._values import MANAGEMENT_PORT

__all__ = [
    "CONTROL_PLANE",
    "RUNTIME_FRAGMENT",
    "RUNTIME_MANIFEST",
    "RUNTIME_TMPFILES",
    "Runtime",
    "RuntimeFragment",
    "check_bindings",
    "services_of",
]

RUNTIME_MANIFEST = "/etc/tundravm/runtime.json"
"""What the image needs at run time, for the tools that deliver it."""

RUNTIME_TMPFILES = "/usr/lib/tmpfiles.d/50-tundravm-runtime.conf"
"""The tmpfs directories the delivery writes into, made at boot."""

CONTROL_PLANE = "tundravm"
"""The ``Secrets`` block that is today's control-plane bootstrap."""

DELIVERY_UNIT = "tundravm-credentials.service"
"""Finishes once every required secret has been delivered."""

_TMPFILES = f"""\
# tundravm: where secrets, keys and unlock passphrases are delivered (root-owned tmpfs).
# credentials/ is searchable so a unit can wait for a name; its files are mode 0600.
d {RUNTIME_DIRECTORY} 0711 root root -
d {CREDENTIALS_DIRECTORY} 0711 root root -
d {RUNTIME_DIRECTORY}/unlock 0700 root root -
d {RUNTIME_DIRECTORY}/keys 0700 root root -
"""


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


def _image_services(image: object) -> tuple[Service, ...]:
    """The services of the image and of every profile it declares."""
    from ._image import Image

    assert isinstance(image, Image)
    items: list[object] = [*image.services, *image.components]
    if image.observability is not None:
        items.append(image.observability)
    for profile in image.profiles.values():
        items.extend(profile.add)
    return tuple(services_of(items))


def check_bindings(image: object) -> None:
    """Every secret and volume a service binds is declared, every shared directory says
    who shares it, and no service exposes what an ingress fronts."""
    from ._image import Image

    assert isinstance(image, Image)
    services = _image_services(image)
    secrets = {secret.name: secret for secret in image.secrets}
    volumes = {volume.name: volume for volume in image.volumes}
    owners: dict[str, list[str]] = {}
    for service in services:
        for secret in service.secrets:
            if secrets.get(secret.name) != secret:
                raise ValidationError(
                    f"Image: service {service.name!r} binds secret {secret.name!r}, which "
                    "secrets= does not declare.",
                    hint=f"Add it to the image: secrets=[..., {secret.name}].",
                )
        state = service.state
        if isinstance(state, VolumeDirectory):
            if volumes.get(state.volume.name) != state.volume:
                raise ValidationError(
                    f"Image: service {service.name!r} keeps its state on volume "
                    f"{state.volume.name!r}, which volumes= does not declare.",
                    hint=f"Add it to the image: volumes=[..., {state.volume.name}].",
                )
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
        if isinstance(way, TLS) and secrets.get(way.certificate.name) != way.certificate:
            raise ValidationError(
                f"Image: TLS({way.port}) takes its certificate from secret "
                f"{way.certificate.name!r}, which secrets= does not declare.",
                hint=f"Add it to the image: secrets=[..., {way.certificate.name}].",
            )
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
    ingress: tuple[AttestedTLS | TLS, ...]
    services: tuple[Service, ...]
    tools: dm.Git
    rootfs: str

    @property
    def control_plane(self) -> bool:
        """Whether the image waits for an operator: a secret, or a volume it unlocks."""
        return bool(self.secrets) or any(
            isinstance(volume.unlock, (OperatorKey, BrokerKey)) for volume in self.volumes
        )

    # ── Per service ──────────────────────────────────────────────────

    def waits(self, service: Service) -> tuple[str, ...]:
        """The boot units *service* waits for: its required secrets and its volume."""
        units = [secret.ready.unit for secret in service.secrets if secret.required]
        if isinstance(service.state, VolumeDirectory):
            units.append(service.state.ready.unit)
        return tuple(units)

    def optional(self, service: Service) -> tuple[str, ...]:
        """``SetCredential=`` lines: an optional secret reads as empty until it arrives."""
        return tuple(f"{s.name}:" for s in service.secrets if not s.required)

    # ── The image ────────────────────────────────────────────────────

    def declarations(self) -> tuple[dm.Declaration | Component, ...]:
        """Everything the contracts add to the image (a component is lowered as one)."""
        found: list[dm.Declaration | Component] = []
        if self.volumes or self.secrets:
            found.append(dm.File(RUNTIME_TMPFILES, _TMPFILES))
            found.append(dm.RuntimeTools(self.tools))
        for volume in self.volumes:
            found.extend(self.volume(volume))
        if self.control_plane:
            found.extend(self.delivery())
        for way in self.ingress:
            found.extend(self.relay(way))
        if self.volumes or self.secrets or self.ingress:
            found.append(dm.File(RUNTIME_MANIFEST, self.manifest()))
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
        """Today's control plane: the SSH-key bootstrap, root SSH, and the credential waits."""
        from tundravm.components import SshAccess

        entries = tuple(
            dm.Secret(
                secret.name,
                (dm.SecretFile(secret.location, mode=0o600),),
                required=secret.required,
            )
            for secret in self.secrets
        )
        delivery = (
            dm.ExternalDelivery("operator", supports=("file",), unit=DELIVERY_UNIT)
            if entries
            else None
        )
        yield dm.Secrets(
            CONTROL_PLANE,
            entries=entries,
            port=MANAGEMENT_PORT,
            delivery=delivery,
        )
        yield dm.Package("socat")
        yield SshAccess(from_delivery=True)
        for secret in self.secrets:
            yield _wait(
                secret.ready.unit,
                f"Wait for secret {secret.name}",
                f"until [ -e {shlex.quote(secret.location)} ]; do sleep 1; done",
            )
        if entries:
            required = [s.location for s in self.secrets if s.required]
            test = " && ".join(f"[ -e {shlex.quote(path)} ]" for path in required) or "true"
            yield _wait(
                DELIVERY_UNIT,
                "Wait for every required secret",
                f"until {test}; do sleep 1; done",
            )

    def relay(self, way: AttestedTLS | TLS) -> Iterator[dm.Declaration]:
        """The TLS listener relaying *way*'s port to its upstream."""
        yield dm.Package("socat")
        name = way.unit.removesuffix(".service")
        upstream = f"TCP:{way.upstream.address}"
        hardening = Hardening.preset(
            "static",
            capabilities=("CAP_NET_BIND_SERVICE",) if way.port < 1024 else (),
        )
        if isinstance(way, AttestedTLS):
            yield dm.Package("openssl")
            keys = "${RUNTIME_DIRECTORY}"
            listen = (
                f"OPENSSL-LISTEN:{way.port},reuseaddr,fork,verify=0,"
                f"cert={keys}/cert.pem,key={keys}/key.pem"
            )
            yield dm.Service(
                name,
                ("/usr/bin/socat", listen, upstream),
                description=f"Attested TLS on :{way.port} to {way.upstream.url}",
                exec_start_pre=(
                    "/usr/bin/openssl req -x509 -newkey ec -pkeyopt "
                    "ec_paramgen_curve:prime256v1 -nodes -days 30 -subj /CN=tundravm "
                    f"-keyout {keys}/key.pem -out {keys}/cert.pem",
                ),
                runtime=name,
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
            requires=(way.certificate.ready.unit,),
            after=(way.certificate.ready.unit,),
            ports=(dm.Port(way.port),),
            needs=(),
            hardening=hardening,
        )

    def manifest(self) -> str:
        """``/etc/tundravm/runtime.json``."""
        bound: dict[str, list[str]] = {}
        for service in self.services:
            for secret in service.secrets:
                bound.setdefault(secret.name, []).append(service.unit_name)
        for way in self.ingress:
            if isinstance(way, TLS):
                bound.setdefault(way.certificate.name, []).append(way.unit)
        secrets = [
            {
                "name": secret.name,
                "required": secret.required,
                "rotation": secret.rotation,
                "location": secret.location,
                "source": None if secret.source is None else secret.source.manifest(),
                "units": sorted(bound.get(secret.name, [])),
            }
            for secret in self.secrets
        ]
        volumes = []
        for volume in self.volumes:
            entry = volume.manifest()
            entry["device"] = volume.device
            if not isinstance(volume.unlock, TpmSeal):
                entry["pipe"] = volume.pipe
            entry["directories"] = sorted(self.directories(volume))
            volumes.append(entry)
        payload = {
            "schema": 1,
            "management_port": MANAGEMENT_PORT,
            "control_plane": (
                "secret-delivery ssh-key bootstrap, unverified first contact (gate 3: "
                "attested endpoint)"
                if self.control_plane
                else None
            ),
            "rootfs": self.rootfs,
            "volumes": volumes,
            "secrets": secrets,
            "ingress": [way.manifest() for way in self.ingress],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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
