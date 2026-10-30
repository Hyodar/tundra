"""Building the internal ``Recipe`` from an :class:`~tundravm.image.Image`.

This is the only place that knows both the public values and the declarations
:func:`tundravm.declarative.resolve` and :func:`tundravm.declarative.lower` consume.
Everything the image implies and does not say — the TEE baseline, the firewall with
the management ingress, the evidence service, the generated users, the probe units a
``requires=`` needs — is added here and nowhere else, so one reading of this module is
the whole answer to "what did I get".
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Hardening
from tundravm.declarative.utils import Backports, DevTools, Network, SshAccess, Tdxs, TeeBase
from tundravm.errors import ValidationError

from ._component import (
    Component,
    ComponentReady,
    Endpoint,
    ServiceReady,
    ServiceRef,
    composed,
    export_providers,
)
from ._image import BASE_PROFILE, DEV_PROFILE, Image, Profile
from ._service import Service
from ._values import (
    MANAGEMENT_PORT,
    Credential,
    File,
    Notify,
    Package,
    Tree,
)

__all__ = ["OWNED_FILES", "recipe_of"]

OWNED_FILES = "/etc/tmpfiles.d/50-tundravm-owned.conf"
"""Where the ownership of ``File(owner=...)`` files is applied, at boot, by
systemd-tmpfiles: the users exist by then, which they do not while the image is built."""

_TMPFILES_HEADER = (
    "# tundravm: the owners File(owner=...) and Tree(owner=...) asked for.\n"
    "# systemd-tmpfiles applies them at boot, once the users exist.\n"
)


def recipe_of(image: Image) -> dm.Recipe:
    """The internal recipe *image* lowers to, with every implied declaration in place."""
    state = _Lowering(image)
    items = state.items()
    return dm.Recipe(
        name=image.name,
        items=items,
        variants=state.variants(),
        platform=state.platform(),
    )


@dataclass(slots=True)
class _Lowering:
    """One pass over an image, collecting what the declarations alone cannot carry."""

    image: Image
    owned: list[tuple[str, str]] = field(default_factory=list)
    curl: bool = False
    backports: bool = False

    # ── The image ───────────────────────────────────────────────────

    def platform(self) -> dm.Platform:
        """Debian, its archive and its boot set."""
        base = self.image.base
        kernel = self.image.kernel
        return dm.Debian(
            base.release,
            snapshot=base.archive,
            efi_stub=base.efi_stub,
            kernel_package=None if kernel.built else "linux-image-amd64",
            kernel_modules=() if kernel.built else tuple(kernel.modules),
        )

    def items(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """Everything the image declares, implied baseline first."""
        image = self.image
        body: list[dm.Declaration | dm.Fragment] = []
        for item in (*image.packages, *image.files, *image.users, *image.groups):
            body.extend(self.content(item))
        for component in image.components:
            body.append(self.component(component))
        for service in image.services:
            body.extend(self.service(service))
        if image.kernel.built:
            body.append(self.kernel())
        body.extend(self.mkosi())
        return (*self.baseline(), *body, *self.owned_files())

    def baseline(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """What every image gets: the TEE baseline, the firewall and the evidence service."""
        network = self.image.network
        found: list[dm.Declaration | dm.Fragment] = [
            TeeBase(network=network is None, firewall=False),
            self.firewall(),
            Tdxs(),
        ]
        if network is not None:
            found.append(
                Network(
                    dhcp=network.dhcp,
                    interfaces=tuple(network.interfaces),
                    static=network.static,
                )
            )
        if self.backports:
            found.append(Backports())
        if self.curl:
            found.append(dm.Package("curl"))
        return tuple(found)

    def firewall(self) -> dm.Firewall:
        """Inbound: the management ingress plus whatever a service exposed. Outbound:
        unfiltered, unless the image's network names an ``Egress``."""
        egress = self.image.network.egress if self.image.network is not None else None
        if egress is None:
            return dm.Firewall(allow=(MANAGEMENT_PORT,))
        return dm.Firewall(
            allow=(MANAGEMENT_PORT,),
            egress="filter",
            egress_ports=tuple(dm.Port(p.port, p.protocol) for p in egress.ports),
            egress_hosts=tuple(egress.hosts),
        )

    def kernel(self) -> dm.Kernel:
        """The kernel the build compiles, with every driver built in."""
        kernel = self.image.kernel
        assert kernel.version is not None and kernel.source is not None
        assert kernel.config is not None
        return dm.Kernel(
            kernel.version,
            kernel.source,
            base=kernel.config,
            patches=tuple(dm.KernelPatch(path) for path in kernel.patches),
        )

    def mkosi(self) -> tuple[dm.Declaration, ...]:
        """``mkosi=Mkosi(settings=, scripts=)``, recorded as settings and hooks."""
        mkosi = self.image.mkosi
        if mkosi is None:
            return ()
        settings = mkosi.settings or {}
        found: list[dm.Declaration] = [
            dm.Setting("Content", key, (value,)) for key, value in sorted(settings.items())
        ]
        for index, (stage, script) in enumerate(mkosi.scripts, start=1):
            found.append(dm.Hook(f"mkosi-{stage}-{index}", stage, script))  # type: ignore[arg-type]
        return tuple(found)

    def owned_files(self) -> tuple[dm.Declaration, ...]:
        """The one tmpfiles file that applies every ``owner=`` the image asked for."""
        if not self.owned:
            return ()
        lines = "".join(f"{line}\n" for line in sorted(dict.fromkeys(self.owned_lines())))
        return (dm.File.text(OWNED_FILES, _TMPFILES_HEADER + lines),)

    def owned_lines(self) -> Iterator[str]:
        """One ``z``/``Z`` line per owned path."""
        for path, owner in self.owned:
            user, _, group = owner.partition(":")
            kind = "Z" if path.endswith("/") else "z"
            yield f"{kind} {path.rstrip('/')} - {user} {group or user} -"

    def variants(self) -> tuple[dm.Variant, ...]:
        """The base variant the image is, then its profiles, then ``dev=``."""
        image = self.image
        found = [dm.Variant(BASE_PROFILE, outputs=tuple(image.targets))]
        for name, profile in image.profiles.items():
            found.append(self.variant(name, profile))
        if image.dev is not None:
            found.append(self.dev_variant())
        return tuple(found)

    def variant(self, name: str, profile: Profile) -> dm.Variant:
        """One ``Profile`` as the overlay it is."""
        items: list[dm.Declaration | dm.Fragment] = []
        for item in profile.add:
            items.extend(self.content(item))
        return dm.Variant(
            name,
            inherits=profile.extends or BASE_PROFILE,
            add=dm.Fragment(name, items=tuple(items)),
            changes=tuple(profile.changes),
            release=profile.production,
        )

    def dev_variant(self) -> dm.Variant:
        """``dev=DevAccess(...)`` as a non-production profile."""
        dev = self.image.dev
        assert dev is not None
        items: list[dm.Declaration | dm.Fragment] = []
        keys = dev.public_keys()
        if keys:
            items.append(SshAccess(authorized_keys=keys))
        if dev.password is not None:
            items.append(DevTools(root_password=dev.password))
        return dm.Variant(
            DEV_PROFILE,
            inherits=BASE_PROFILE,
            add=dm.Fragment(DEV_PROFILE, items=tuple(items)),
            release=False,
        )

    # ── Content ─────────────────────────────────────────────────────

    def content(self, item: object) -> Iterator[dm.Declaration | dm.Fragment]:
        """One public value as the declarations it stands for."""
        if isinstance(item, str):
            yield dm.Package(item)
        elif isinstance(item, Package):
            if item.backports:
                self.backports = True
            yield dm.Package(item.name)
        elif isinstance(item, File):
            yield from self.file(item)
        elif isinstance(item, Tree):
            yield from self.tree(item)
        elif isinstance(item, Service):
            yield from self.service(item)
        elif isinstance(item, Component):
            yield self.component(item)
        elif isinstance(item, (*dm.DECLARATION_TYPES, dm.Fragment)):
            # The escape a component has while gate 6 finishes the public spellings of
            # Repository, Unit, Mount, BootStep and the rest: an internal declaration
            # passes through unchanged, and resolution treats it like any other.
            yield cast("dm.Declaration | dm.Fragment", item)
        else:
            raise ValidationError(
                f"{item!r} is not image content.",
                hint="Pass a package name, Package, File, Tree, Service, Component, User or Group.",
            )

    def file(self, item: File) -> Iterator[dm.Declaration]:
        """A ``File`` as the declaration that ships it, plus its ownership line."""
        if item.owner is not None:
            self.owned.append((item.path, item.owner))
        yield dm.File(item.path, item.content, item.mode)

    def tree(self, item: Tree) -> Iterator[dm.Declaration]:
        """A ``Tree`` as the host directory it ships."""
        if item.owner is not None:
            self.owned.append((item.dst + "/", item.owner))
        assert isinstance(item.src, Path)
        yield dm.FileTree(item.dst, item.src, exclude=tuple(item.exclude))

    def component(self, item: Component) -> dm.Fragment:
        """A ``Component`` as the fragment it is, nested components included."""
        items: list[dm.Declaration | dm.Fragment] = []
        for child in composed(item):
            items.extend(self.content(child))
        check = getattr(item, "check", None)
        checks: tuple[dm.Check, ...] = () if check is None else (_collected(item),)
        return dm.Fragment(
            item.name,
            items=tuple(items),
            requires=tuple(dict.fromkeys(export_providers(item))),
            checks=checks,
        )

    # ── Services ────────────────────────────────────────────────────

    def service(self, item: Service) -> Iterator[dm.Declaration | dm.Fragment]:
        """A ``Service`` as its unit, the builds its argv pulls in and its probe."""
        yield from item.binaries()
        if item.timer is not None:
            yield self.timer_task(item)
            return
        yield self.unit(item)
        probe = self.probe(item)
        if probe is not None:
            yield probe
        if item.unit is not None:
            yield dm.Unit(item.unit_name, dropin=item.unit)

    def unit(self, item: Service) -> dm.Service:
        """The generated ``.service``."""
        extra: dict[str, str | tuple[str, ...]] = {}
        if item.resources is not None:
            extra.update(dict(item.resources.entries()))
        if item.shutdown is not None:
            extra.update(dict(item.shutdown.entries()))
        requires = self.required_units(item)
        shutdown = item.shutdown
        return dm.Service(
            item.unit_name,
            item.command(),
            type="notify" if isinstance(item.readiness, Notify) else "exec",
            user=item.user,
            groups=tuple(item.groups),
            working_dir=item.working_directory,
            env=dict(item.environment),
            credentials=_credentials(tuple(item.credentials)),
            state=item.state,
            logs=item.logs,
            runtime=item.runtime,
            ports=tuple(dm.Port(port.port, port.protocol) for port in item.expose),
            requires=requires,
            after=requires,
            hardening=item.hardening,
            socket=item.socket,
            timeout_stop=None if shutdown is None else shutdown.timeout,
            kill_mode=None if shutdown is None else shutdown.kill_mode,
            extra={"Service": extra} if extra else {},
        )

    def timer_task(self, item: Service) -> dm.Task:
        """A ``Service(timer=...)`` as the scheduled oneshot it is."""
        requires = self.required_units(item)
        return dm.Task(
            item.unit_name,
            item.command(),
            schedule=item.timer,
            user=item.user,
            state=item.state,
            after=requires,
            requires=requires,
            hardening=item.hardening,
        )

    def probe(self, item: Service) -> dm.Task | None:
        """The bounded unit that proves *item* ready, when its readiness needs one."""
        readiness = item.readiness
        if readiness is None or isinstance(readiness, Notify):
            return None
        from ._values import HttpProbe

        if isinstance(readiness, HttpProbe):
            self.curl = True
        return dm.Task(
            f"{item.stem}-ready",
            readiness.command(),
            description=f"Wait until {item.unit_name} is ready",
            requires=(item.unit_name,),
            after=(item.unit_name,),
            timeout=readiness.timeout,
            hardening=Hardening.preset("static"),
        )

    def required_units(self, item: Service) -> tuple[str, ...]:
        """The units ``requires=`` means: what proves each dependency ready."""
        units: list[str] = []
        for requirement in item.requires:
            for handle in _handles(requirement):
                units.append(_ready_unit(item, handle))
        return tuple(dict.fromkeys(units))


def _handles(requirement: object) -> tuple[ServiceReady, ...]:
    """The readiness handles one ``requires=`` entry stands for."""
    if isinstance(requirement, ServiceReady):
        return (requirement,)
    if isinstance(requirement, ServiceRef):
        return (requirement.ready,)
    if isinstance(requirement, Endpoint):
        assert requirement.ready is not None
        return (requirement.ready,)
    if isinstance(requirement, ComponentReady):
        found = tuple(
            service.ready
            for service in requirement.component.services()
            if service.readiness is not None
        )
        if not found:
            raise ValidationError(
                f"Component {requirement.name!r} declares no readiness, so nothing can "
                "wait for it.",
                hint="Give one of its services readiness=HttpProbe(...), TcpProbe(...) "
                "or Notify().",
            )
        return found
    raise ValidationError(f"{requirement!r} is not a readiness handle.")


def _ready_unit(owner: Service, handle: ServiceReady) -> str:
    """The unit that proves *handle* ready: the service itself, or its probe."""
    if handle.probe is None:
        raise ValidationError(
            f"Service {owner.name!r} requires {handle.unit!r}, which declares no readiness.",
            hint="Give it readiness=HttpProbe(...), TcpProbe(...) or Notify(); After= alone "
            "only waits for the exec, not for the program to serve.",
        )
    if isinstance(handle.probe, Notify):
        return handle.unit
    return f"{handle.unit.removesuffix('.service')}-ready.service"


def _credentials(credentials: tuple[Credential, ...]) -> dict[str, str]:
    """``credentials=`` as systemd's ``LoadCredential=`` mapping."""
    found: dict[str, str] = {}
    for credential in credentials:
        if credential.name in found:
            raise ValidationError(
                f"Credential {credential.name!r} is bound twice.",
                hint="A credential name is unique per service.",
            )
        found[credential.name] = credential.source
    return found


def _collected(component: Component) -> dm.Check:
    """A component's ``check`` method as the fragment check the resolver runs."""

    def run(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        return tuple(component.check(resolved))  # type: ignore[attr-defined]

    return run
