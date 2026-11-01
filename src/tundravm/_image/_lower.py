"""Building the internal ``Recipe`` from an :class:`~tundravm.Image`.

This is the only place that knows both the public values and the declarations
:func:`tundravm.declarative.resolve` and :func:`tundravm.declarative.lower` consume.
Everything the image implies and does not say — the TEE baseline, the firewall with
the management ingress, the evidence service, the generated users, the probe units a
``requires=`` needs — is added here and nowhere else, so one reading of this module is
the whole answer to "what did I get".
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import cast

from tundravm._location import anchored, declared_at, display_location
from tundravm.declarative import model as dm
from tundravm.declarative.systemd import Hardening
from tundravm.declarative.utils import SSH_AUTHORIZED_KEYS_DIR, Backports
from tundravm.errors import ValidationError

from ._change import Edit, Remove
from ._component import (
    BootReady,
    Component,
    ComponentReady,
    Endpoint,
    Readied,
    ServiceReady,
    ServiceRef,
    builds_with_network,
    composed,
    export_providers,
)
from ._image import BASE_PROFILE, DEV_PROFILE, Image, Profile
from ._runtime import RUNTIME_FRAGMENT, Runtime, RuntimeFragment, services_of
from ._secrets import Secret
from ._service import Binding, Service
from ._storage import VolumeDirectory
from ._values import (
    Credential,
    Egress,
    File,
    Notify,
    Package,
    Tree,
)

__all__ = ["OWNED_FILES", "component_classes", "recipe_of"]

OWNED_FILES = "/etc/tmpfiles.d/50-tundravm-owned.conf"
"""Where the ownership of ``File(owner=...)`` files is applied, at boot, by
systemd-tmpfiles: the users exist by then, which they do not while the image is built. A
profile's own files are owned by ``50-tundravm-owned-<profile>.conf`` beside it."""

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
        mkosi=dm.Mkosi(
            root=image.rootfs.layout,
            root_size=image.rootfs.max_size,
            build_network=state.build_network(),
        ),
    )


@dataclass(slots=True)
class _Lowering:
    """One pass over an image, collecting what the declarations alone cannot carry."""

    image: Image
    owned: list[tuple[str, str]] = field(default_factory=list)
    curl: bool = False
    backports: bool = False
    sends: tuple[tuple[str, Egress], ...] | None = None
    runtime: Runtime = field(init=False)
    baseline_types: tuple[type, ...] = field(init=False)

    def __post_init__(self) -> None:
        from tundravm.components import Firewall, Network, Tdxs, TeeBase

        self.baseline_types = (TeeBase, Network, Firewall, Tdxs)
        image = self.image
        evidence = self.explicit(Tdxs)
        self.runtime = Runtime(
            volumes=tuple(image.volumes),
            secrets=tuple(image.secrets),
            ingress=tuple(image.ingress),
            # A profile's services too: the volume step makes their directories in
            # every profile, which is harmless where the profile is not selected.
            services=tuple(
                services_of(
                    (*self.workload(), *(i for p in image.profiles.values() for i in p.add))
                )
            ),
            tools=Tdxs().source if evidence is None else evidence.source,
            rootfs=image.rootfs.layout,
        )

    def workload(self) -> tuple[object, ...]:
        """What the image runs beyond its baseline: services, components, observability."""
        image = self.image
        found: list[object] = [*image.services, *image.components]
        if image.observability is not None:
            found.append(image.observability)
        return tuple(found)

    def explicit[T](self, kind: type[T]) -> T | None:
        """The baseline component of *kind* the image passed in ``components=``."""
        found = [item for item in self.image.components if isinstance(item, kind)]
        if len(found) > 1:
            raise ValidationError(
                f"Image: components= holds {len(found)} {kind.__name__} components.",
                hint=f"Pass one {kind.__name__}(...): it replaces the image's default.",
            )
        return found[0] if found else None

    def build_network(self) -> bool:
        """Whether a component's builds fetch dependencies while the image is built."""
        components = [item for item in self.workload() if isinstance(item, Component)]
        for profile in self.image.profiles.values():
            components.extend(item for item in profile.add if isinstance(item, Component))
        return any(builds_with_network(item) for item in components)

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
            kernel_cmdline=tuple(kernel.cmdline),
        )

    def items(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """Everything the image declares, implied baseline first."""
        image = self.image
        body: list[dm.Declaration | dm.Fragment] = []
        for item in (*image.packages, *image.files, *image.users, *image.groups):
            body.extend(self.content(item))
        for component in image.components:
            if not isinstance(component, self.baseline_types):
                body.append(self.component(component))
        if image.observability is not None:
            body.append(self.component(image.observability))
        for service in image.services:
            body.extend(self.service(service))
        for group in self.shared_groups():
            body.append(dm.Group(group))
        runtime = [
            lowered
            for declaration in self.runtime.declarations()
            for lowered in self.content(declaration)
        ]
        if runtime:
            body.append(
                RuntimeFragment(
                    RUNTIME_FRAGMENT, items=tuple(runtime), checks=(self.runtime.check,)
                )
            )
        if image.kernel.built:
            body.append(self.kernel())
        body.extend(self.mkosi())
        return (*self.baseline(), *body, *self.owned_files())

    def baseline(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """What every image gets: the TEE baseline, the network, the firewall and the
        evidence service, each the image's own when it passed one in ``components=``."""
        from tundravm.components import Firewall, Network, Tdxs, TeeBase
        from tundravm.components._base import egress_rules

        network = self.image.network
        if network is not None and self.explicit(Network) is not None:
            raise ValidationError(
                "Image: network= and components= both hold a Network.",
                hint="Keep network=Network(...).",
            )
        network = network or self.explicit(Network) or Network()
        firewall = self.explicit(Firewall)
        if firewall is not None and network.egress is not None and firewall.egress is not None:
            raise ValidationError(
                "Image: the Network and the Firewall both set egress=.",
                hint="Set it once: network=Network(egress=Egress(...)).",
            )
        tee = self.explicit(TeeBase) or TeeBase()
        tdxs = self.explicit(Tdxs) or Tdxs()
        egress = network.egress if firewall is None or firewall.egress is None else firewall.egress
        if firewall is None:
            firewall = Firewall(egress=egress)
        elif egress is not None:
            firewall = Firewall(allow=firewall.allow, egress=egress)
        filtering = self.component(firewall)
        if egress is not None:
            self.sends = tuple(self.outbound(egress, (tee, tdxs, network)))
            rules = egress_rules(self.sends)
            with _at(firewall):
                filtering = _with_rules(filtering, rules)
        found: list[dm.Declaration | dm.Fragment] = [
            self.component(tee),
            filtering,
            self.component(tdxs),
            self.component(network),
        ]
        if self.backports:
            found.append(Backports())
        if self.curl:
            found.append(dm.Package("curl"))
        return tuple(found)

    def outbound(
        self, egress: Egress, baseline: Sequence[Component]
    ) -> Iterator[tuple[str, Egress]]:
        """Who sends where: the image's *egress*, the ``outbound()`` of the *baseline*, of
        every component and of what each composes, then the metadata servers of the
        image's clouds."""
        image = self.image
        yield "image", egress
        own = [item for item in image.components if not isinstance(item, self.baseline_types)]
        observability = [] if image.observability is None else [image.observability]
        for component in (*baseline, *own, *observability):
            yield from _outbound(component)
        for target in image.targets:
            if target in _CLOUD_EGRESS:
                yield target, Egress(hosts=_CLOUD_EGRESS[target])

    def profile_egress(
        self, added: Sequence[object], inherited: Sequence[object] = ()
    ) -> tuple[dm.Edit, ...]:
        """The edit of the firewall a profile needs so its output chain also accepts what
        the components it adds (*added*, beyond the *inherited* ones of the profiles it
        extends) send: none when the image does not filter egress or they ask for nothing
        new."""
        from tundravm.components._base import egress_rules

        if self.sends is None:
            return ()
        before = (*self.sends, *_sends(inherited))
        rules = egress_rules((*before, *_sends(added)))
        if rules == egress_rules(before):
            return ()
        return (dm.Ref("Firewall").edit(egress_rules=rules),)

    def shared_groups(self) -> tuple[str, ...]:
        """The groups shared volume directories name, declared once each."""
        groups = {
            service.state.group
            for service in self.runtime.services
            if isinstance(service.state, VolumeDirectory) and service.state.group is not None
        }
        declared = {group.name for group in self.image.groups}
        return tuple(sorted(groups - declared))

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

    def owned_files(self, profile: str | None = None) -> tuple[dm.Declaration, ...]:
        """The tmpfiles file that applies every ``owner=`` collected so far (the image's, or
        *profile*'s own), and forgets them."""
        owned, self.owned = self.owned, []
        if not owned:
            return ()
        path = OWNED_FILES if profile is None else f"{OWNED_FILES[:-5]}-{profile}.conf"
        lines = "".join(f"{line}\n" for line in sorted(dict.fromkeys(_owned_lines(owned))))
        return (dm.File.text(path, _TMPFILES_HEADER + lines),)

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
        items.extend(self.owned_files(name))
        inherited = [item for ancestor in self.ancestors(profile) for item in ancestor.add]
        with _at(profile):
            egress = self.profile_egress(profile.add, inherited)
        return dm.Variant(
            name,
            inherits=profile.extends or BASE_PROFILE,
            add=dm.Fragment(name, items=tuple(items)),
            changes=(*self.changes(profile), *egress),
            release=profile.production,
        )

    def changes(self, profile: Profile) -> tuple[dm.Edit | dm.Remove, ...]:
        """*profile*'s changes on the declarations their values lower to.

        An edit changes what the value its profile sees lowers to: the image's value with
        the edits of the profiles it extends applied, so two edits that land on the same
        directive (``resources=`` and ``shutdown=`` both write ``[Service]`` lines) add up.
        """
        current: dict[dm.Ref, object] = {}
        for ancestor in self.ancestors(profile):
            for change in ancestor.changes:
                if isinstance(change, Edit):
                    target = change.ref.target
                    current[target] = replace(
                        current.get(target, change.ref.value), **change.fields
                    )  # type: ignore[type-var]
        found: list[dm.Edit | dm.Remove] = []
        for change in profile.changes:
            if isinstance(change, (dm.Edit, dm.Remove)):
                found.append(change)
                continue
            with _at(change):
                if isinstance(change, Remove):
                    found.append(dm.Remove(change.ref.target))
                    continue
                target = change.ref.target
                before = current.get(target, change.ref.value)
                after = current[target] = replace(before, **change.fields)  # type: ignore[type-var]
                found.append(self.edit(change, before, after))
        return tuple(found)

    def ancestors(self, profile: Profile) -> tuple[Profile, ...]:
        """The profiles *profile* extends, the image's nearest first."""
        found: list[Profile] = []
        parent = self.image.profiles.get(profile.extends or BASE_PROFILE)
        while parent is not None and parent not in found:
            found.insert(0, parent)
            parent = self.image.profiles.get(parent.extends or BASE_PROFILE)
        return tuple(found)

    def edit(self, change: Edit, before: object, after: object) -> dm.Edit:
        """The internal edit that turns what *before* lowers to into what *after* does."""
        old, new = self.declaration(before), self.declaration(after)
        keys = dm.key_fields(change.ref.target.kind)
        fields = {
            item.name: getattr(new, item.name)
            for item in dataclass_fields(new)
            if item.init
            and item.name not in keys
            and getattr(new, item.name) != getattr(old, item.name)
        }
        if not fields:
            raise ValidationError(
                f"The edit of {change.ref} changes nothing the image builds.",
                hint="Drop it, or set a keyword whose value differs.",
            )
        return dm.Edit(change.ref.target, fields)

    def declaration(self, value: object) -> dm.Declaration:
        """The one declaration a public value an edit names lowers to."""
        if isinstance(value, Service):
            return self.timer_task(value) if value.timer is not None else self.unit(value)
        if isinstance(value, File):
            return dm.File(value.path, value.content, value.mode)
        assert isinstance(value, Tree)
        assert isinstance(value.src, Path)
        return dm.FileTree(value.dst, value.src, exclude=tuple(value.exclude))

    def dev_variant(self) -> dm.Variant:
        """``dev=DevAccess(...)`` as a non-production profile.

        When the image already runs SSH for its control plane, the dev keys join it
        (``authorized_keys.d/root``) instead of a second ``sshd``."""
        dev = self.image.dev
        assert dev is not None
        items: list[dm.Declaration | dm.Fragment] = []
        if self.runtime.control_plane:
            keys = dev.public_keys()
            if keys:
                text = "".join(f"{key}\n" for key in keys)
                items.append(dm.File.text(f"{SSH_AUTHORIZED_KEYS_DIR}/root", text))
            for tool in dev.tools():
                assert isinstance(tool, dm.Fragment)
                items.append(tool)
        else:
            items.append(self.component(dev))
        items.extend(self.owned_files(DEV_PROFILE))
        with _at(dev):
            egress = self.profile_egress([dev])
        return dm.Variant(
            DEV_PROFILE,
            inherits=BASE_PROFILE,
            add=dm.Fragment(DEV_PROFILE, items=tuple(items)),
            changes=egress,
            release=False,
        )

    # ── Content ─────────────────────────────────────────────────────

    def content(self, item: object) -> Iterator[dm.Declaration | dm.Fragment]:
        """One public value as the declarations it stands for, located where it was written."""
        with _at(item):
            found = tuple(self._content(item))
        yield from found

    def _content(self, item: object) -> Iterator[dm.Declaration | dm.Fragment]:
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
        yield self.declaration(item)

    def tree(self, item: Tree) -> Iterator[dm.Declaration]:
        """A ``Tree`` as the host directory it ships."""
        if item.owner is not None:
            self.owned.append((item.dst + "/", item.owner))
        yield self.declaration(item)

    def component(self, item: Component) -> dm.Fragment:
        """A ``Component`` as the fragment it is, nested components included."""
        items: list[dm.Declaration | dm.Fragment] = []
        for child in composed(item):
            items.extend(self.content(child))
        check = getattr(item, "check", None)
        checks: tuple[dm.Check, ...] = () if check is None else (_collected(item),)
        return _fragment_type(type(item))(
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
        else:
            yield self.unit(item)
            probe = self.probe(item)
            if probe is not None:
                yield probe
        if item.unit is not None:
            yield dm.Unit(item.unit_name, dropin=item.unit)

    def unit(self, item: Service) -> dm.Service:
        """The generated ``.service``."""
        directives = _directives(item)
        optional = self.runtime.optional(item)
        if optional:
            directives["SetCredential"] = optional
        requires = (*self.required_units(item), *self.runtime.waits(item))
        shutdown = item.shutdown
        state = item.state
        environment = dict(item.environment)
        groups = list(item.groups)
        working = item.working_directory
        write: tuple[str, ...] = ()
        if isinstance(state, VolumeDirectory):
            environment.setdefault("STATE_DIRECTORY", state.path)
            working = working or state.path
            write = (f"-{state.path}",)
            if state.group is not None and state.group not in groups:
                groups.append(state.group)
        return dm.Service(
            item.unit_name,
            item.command(),
            type="notify" if isinstance(item.readiness, Notify) else "exec",
            user=item.user,
            groups=tuple(groups),
            working_dir=working,
            env=environment,
            credentials=_credentials(tuple(item.credentials)),
            state=None if isinstance(state, VolumeDirectory) else state,
            write_paths=write,
            needs=_outputs(item),
            logs=item.logs,
            runtime=item.runtime,
            ports=tuple(dm.Port(port.port, port.protocol) for port in item.expose),
            requires=requires,
            after=requires,
            hardening=item.hardening,
            socket=item.socket,
            timeout_stop=None if shutdown is None else shutdown.timeout,
            kill_mode=None if shutdown is None else shutdown.kill_mode,
            directives={"Service": directives} if directives else {},
        )

    def timer_task(self, item: Service) -> dm.Task:
        """A ``Service(timer=...)`` as the scheduled oneshot it is, every keyword kept."""
        assert not isinstance(item.state, VolumeDirectory)  # refused with timer=
        requires = self.required_units(item)
        shutdown = item.shutdown
        directives = _directives(item)
        return dm.Task(
            item.unit_name,
            item.command(),
            schedule=item.timer,
            user=item.user,
            groups=tuple(item.groups),
            state=item.state,
            working_dir=item.working_directory,
            env=dict(item.environment),
            logs=item.logs,
            runtime=item.runtime,
            after=requires,
            requires=requires,
            needs=_outputs(item),
            hardening=item.hardening,
            timeout_stop=None if shutdown is None else shutdown.timeout,
            kill_mode=None if shutdown is None else shutdown.kill_mode,
            directives={"Service": directives} if directives else {},
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
            needs=(),
            hardening=Hardening.preset("static"),
        )

    def required_units(self, item: Service) -> tuple[str, ...]:
        """The units ``requires=`` means: what proves each dependency ready."""
        units: list[str] = []
        for requirement in item.requires:
            if isinstance(requirement, dm.UnitOutput):
                continue
            for handle in _handles(requirement):
                units.append(_ready_unit(item, handle))
        return tuple(dict.fromkeys(units))


def _outputs(item: Service) -> tuple[dm.UnitOutput, ...]:
    """The setup tasks' outputs ``requires=`` names, which the unit waits for by condition."""
    return tuple(r for r in item.requires if isinstance(r, dm.UnitOutput))


def _handles(requirement: object) -> tuple[Readied, ...]:
    """The readiness handles one ``requires=`` entry stands for."""
    if isinstance(requirement, (ServiceReady, BootReady)):
        return (requirement,)
    if isinstance(requirement, ServiceRef):
        return (requirement.ready,)
    if isinstance(requirement, Endpoint):
        assert requirement.ready is not None
        return (requirement.ready,)
    if isinstance(requirement, ComponentReady):
        found = requirement.handles()
        if not found:
            raise ValidationError(
                f"Component {requirement.name!r} declares no readiness, so nothing can "
                "wait for it.",
                hint="Give one of its services readiness=HttpProbe(...), TcpProbe(...) "
                "or Notify().",
            )
        return found
    raise ValidationError(
        f"{requirement!r} is not a readiness handle.",
        hint="Pass other.ready, component.ready or an exported Endpoint.",
    )


def _ready_unit(owner: Service, handle: Readied) -> str:
    """The unit that proves *handle* ready: the service itself, its probe, or the boot
    step."""
    if isinstance(handle, BootReady):
        return handle.unit
    if handle.probe is None:
        raise ValidationError(
            f"Service {owner.name!r} requires {handle.unit!r}, which declares no readiness.",
            hint="Give it readiness=HttpProbe(...), TcpProbe(...) or Notify(); After= alone "
            "only waits for the exec, not for the program to serve.",
        )
    if isinstance(handle.probe, Notify):
        return handle.unit
    return f"{handle.unit.removesuffix('.service')}-ready.service"


def _owned_lines(owned: Sequence[tuple[str, str]]) -> Iterator[str]:
    """One ``z``/``Z`` line per owned path."""
    for path, owner in owned:
        user, _, group = owner.partition(":")
        kind = "Z" if path.endswith("/") else "z"
        yield f"{kind} {path.rstrip('/')} - {user} {group or user} -"


def _directives(item: Service) -> dict[str, str | tuple[str, ...]]:
    """The ``[Service]`` lines ``resources=`` and ``shutdown=`` render beyond typed fields."""
    found: dict[str, str | tuple[str, ...]] = {}
    for value in (item.resources, item.shutdown):
        if value is not None:
            found.update(dict(value.entries()))
    return found


def _credentials(bindings: tuple[Binding, ...]) -> dict[str, str]:
    """``credentials=`` as systemd's ``LoadCredential=`` mapping."""
    found: dict[str, str] = {}
    credentials: tuple[Credential, ...] = tuple(
        item.credential if isinstance(item, Secret) else item for item in bindings
    )
    for credential in credentials:
        if credential.name in found:
            raise ValidationError(
                f"Credential {credential.name!r} is bound twice.",
                hint="A credential name is unique per service.",
            )
        found[credential.name] = credential.source
    return found


_FRAGMENT_TYPES: dict[type, type[dm.Fragment]] = {}


def _fragment_type(cls: type) -> type[dm.Fragment]:
    """A ``Fragment`` subclass named after the component class, so provenance keeps it.

    ``explore`` and ``why`` print "via app (App)": the fragment's name and the class that
    composed it. It lives in the component's module, so lint tells the shipped
    :mod:`tundravm.components` from the image's own. One subclass per component class.
    """
    found = _FRAGMENT_TYPES.get(cls)
    if found is None:
        namespace = {"__slots__": (), "__module__": cls.__module__, "__component_class__": cls}
        found = type(cls.__name__, (dm.Fragment,), namespace)
        _FRAGMENT_TYPES[cls] = found
    return found


def component_classes(recipe: dm.Recipe) -> tuple[type[Component], ...]:
    """The class of each component *recipe* holds, nested and profile-added ones included,
    in the order they appear."""
    found: dict[type[Component], None] = {}
    pending: list[object] = [*recipe.items, *(variant.add for variant in recipe.variants)]
    while pending:
        item = pending.pop(0)
        if isinstance(item, dm.Fragment):
            cls = getattr(type(item), "__component_class__", None)
            if cls is not None:
                found[cls] = None
            pending.extend(item.items)
    return tuple(found)


_CLOUD_EGRESS: dict[str, tuple[str, ...]] = {
    "azure": ("168.63.129.16", "169.254.169.254"),
    "gcp": ("169.254.169.254",),
}
"""What each cloud's glue talks to: Azure's wireserver and IMDS, GCE's metadata server
(its DNS resolver, too)."""


def _outbound(component: Component) -> Iterator[tuple[str, Egress]]:
    """*component*'s ``outbound()`` under its name, then that of each component it composes."""
    sends = component.outbound()
    if not isinstance(sends, Egress):
        raise ValidationError(
            f"{type(component).__name__}.outbound() returned {sends!r}, not an Egress.",
            hint="Return Egress(ports=[...], hosts=[...]), or Egress() when it sends nothing.",
        )
    yield component.name, sends
    for item in composed(component):
        if isinstance(item, Component):
            yield from _outbound(item)


def _sends(items: Sequence[object]) -> Iterator[tuple[str, Egress]]:
    """Who among *items* (a profile's content) sends where: each component's ``outbound()``."""
    for item in items:
        if isinstance(item, Component):
            yield from _outbound(item)


def _with_rules(fragment: dm.Fragment, rules: tuple[dm.EgressRule, ...]) -> dm.Fragment:
    """*fragment* with its ``Firewall`` filtering by *rules*."""
    items = tuple(
        replace(item, egress_rules=rules) if isinstance(item, dm.Firewall) else item
        for item in fragment.items
    )
    return replace(fragment, items=items)


@contextmanager
def _at(item: object) -> Iterator[None]:
    """Locate everything built from *item* at the line *item* was written on."""
    location = declared_at(item)
    if not location:
        yield
        return
    with anchored(location):
        yield


def _collected(component: Component) -> dm.Check:
    """A component's ``check`` method as the fragment check the resolver runs, with a
    ``rule-undeclared`` error for each code it reports that its class does not declare."""
    cls = type(component)
    declared = {rule.code for rule in cls.rules}

    def run(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        found = tuple(component.check(resolved))  # type: ignore[attr-defined]
        undeclared = dict.fromkeys(d.code for d in found if d.code not in declared)
        location = display_location(declared_at(component))
        return found + tuple(
            dm.Diagnostic(
                "rule-undeclared",
                "error",
                f"{cls.__name__} reports {code!r}, which its class does not declare",
                subject=component.name,
                location=location,
                hint=f"Declare it on {cls.__name__}: rules = (Rule({code!r}, SEVERITY, "
                "SUMMARY, FIX),), with Rule from tundravm.components.",
            )
            for code in undeclared
        )

    return run
