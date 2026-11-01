"""The runtime contracts (volumes, secrets, ingress) checked and lowered onto today's stack.

An image's :class:`~tundravm.storage.Volume`, :class:`~tundravm.secrets.Secret` and ingress
values become the declarations the tools and systemd already understand:

- a volume is a ``Key`` (``key-gen``: a FIFO for ``OperatorKey``/``BrokerKey``, a
  vTPM-persisted random key for ``TpmSeal``) and a LUKS2 ``Disk`` (``disk-setup``, found
  by its ``/dev/disk/by-id/`` name, formatted only while uninitialised), plus a boot step
  that creates the directories services bind, owned by them;
- the secrets are entries of one ``Secrets`` block whose targets are files under
  ``/run/tundravm/credentials``, written by the attested endpoint on a session whose TLS
  key a fresh quote bound, plus one boot unit per secret that finishes once it has arrived;
- a :class:`~tundravm.secrets.Generated` secret is a root boot oneshot that writes random
  hex under ``/run/tundravm/secrets``, which every service binding it requires;
- a volume an operator unlocks, or any secret, brings the control plane: that
  ``Secrets`` block is the attested endpoint on the management port (evidence,
  credentials, unlock, the operator's SSH key), and ``SshAccess(from_delivery=True)`` lets
  only that key in;
- an ingress is a ``socat`` TLS relay unit with its firewall port;
- ``/etc/tundravm/runtime.json`` records all of it for the tools that deliver.

Nothing here writes a secret or a key into the image.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tundravm._location import declared_at, display_location
from tundravm.declarative import model as dm
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

__all__ = [
    "CONTROL_PLANE",
    "RUNTIME_FRAGMENT",
    "RUNTIME_MANIFEST",
    "Runtime",
    "RuntimeFragment",
    "bound",
    "check_bindings",
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


def _image_services(image: object) -> tuple[Service, ...]:
    """The services of the image and of every profile it declares, each profile's edits of
    them included."""
    from ._change import Edit
    from ._image import Image

    assert isinstance(image, Image)
    items: list[object] = [*image.services, *image.components]
    if image.observability is not None:
        items.append(image.observability)
    for profile in image.profiles.values():
        items.extend(profile.add)
        items.extend(change.edited for change in profile.changes if isinstance(change, Edit))
    return tuple(services_of(items))


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
    for way in image.ingress:
        if isinstance(way, TLS):
            _first("secret", secrets, way.certificate)
    return tuple(volumes.values()), tuple(secrets.values())


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
    ingress: tuple[AttestedTLS | TLS, ...]
    services: tuple[Service, ...]
    tools: dm.Git | dm.Local
    rootfs: str

    @property
    def control_plane(self) -> bool:
        """Whether the image waits for an operator: a secret, or a volume it unlocks."""
        return bool(self.secrets) or any(
            isinstance(volume.unlock, (OperatorKey, BrokerKey)) for volume in self.volumes
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
        """``SetCredential=`` lines: an optional secret reads as empty until it arrives."""
        return tuple(f"{s.name}:" for s in service.secrets if not s.required)

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
        if self.volumes or self.secrets:
            found.append(dm.RuntimeTools(self.tools))
        for volume in self.volumes:
            found.extend(self.volume(volume))
        found.extend(_generate(item) for item in self.generated())
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
        """The control plane: the attested endpoint, root SSH, and the credential waits."""
        from tundravm.components import SshAccess

        entries = tuple(
            dm.Secret(
                secret.name,
                (dm.SecretFile(secret.location, mode=0o600),),
                required=secret.required,
            )
            for secret in self.secrets
        )
        yield dm.Secrets(CONTROL_PLANE, entries=entries, port=MANAGEMENT_PORT, ssh_key_path=None)
        yield SshAccess(from_delivery=True)
        for secret in self.secrets:
            yield _wait(
                secret.ready.unit,
                f"Wait for secret {secret.name}",
                f"until [ -e {shlex.quote(secret.location)} ]; do sleep 1; done",
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
                "units": sorted(set(bound.get(secret.name, []))),
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
                "attested endpoint: TLS bound to the quote" if self.control_plane else None
            ),
            "rootfs": self.rootfs,
            "volumes": volumes,
            "secrets": secrets,
            "ingress": [way.manifest() for way in self.ingress],
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


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
