"""Building the internal ``Recipe`` from an :class:`~tundravm.Image`.

This is the only place that knows both the public values and the declarations
:func:`tundravm.declarative.resolve` and :func:`tundravm.declarative.lower` consume.
Everything the image implies and does not say — the TEE baseline, the firewall with
the management ingress, the evidence service, the generated users, the probe units a
``requires=`` needs — is added here and nowhere else, so one reading of this module is
the whole answer to "what did I get".
"""

from __future__ import annotations

import copy
import shlex
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import cast

from tundravm._location import anchored, declared_at, display_location
from tundravm._modules import tdxs as _tdxs
from tundravm._modules.execution import GATE_UNIT
from tundravm._modules.init import OWNERS_STEP
from tundravm._platforms.gcp import KERNEL_PATCH as GCP_KERNEL_PATCH
from tundravm.declarative import model as dm
from tundravm.declarative._network import EGRESS_RESOLVE_UNIT
from tundravm.declarative.systemd import Hardening, target_unit
from tundravm.declarative.utils import SSH_AUTHORIZED_KEYS_DIR, Backports
from tundravm.errors import ValidationError
from tundravm.models import inrelease_variable

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
from ._runtime import (
    RUNTIME_FRAGMENT,
    Runtime,
    RuntimeFragment,
    check_owner_login,
    container_ports,
    key_files,
    services_of,
)
from ._secrets import InstanceKey, Secret
from ._service import Binding, Service
from ._storage import VolumeDirectory
from ._values import (
    Credential,
    Egress,
    File,
    HttpProbe,
    KernelFragment,
    KernelPatch,
    Notify,
    Package,
    Tree,
    egress_host,
    mkosi_section,
)

__all__ = [
    "CONTAINER_KERNEL",
    "OWNERS_STEP",
    "VERITY_KERNEL",
    "component_classes",
    "kernel_of",
    "recipe_of",
]

IMPLIED_FRAGMENT = "tundravm-requires"
"""The fragment whose check reports ``requires-implied``; it declares nothing."""

VERITY_KERNEL = KernelFragment(
    {
        "CONFIG_MD": "y",
        "CONFIG_BLK_DEV_DM": "y",
        "CONFIG_DM_VERITY": "y",
        "CONFIG_CRYPTO_SHA256": "y",
        "CONFIG_EROFS_FS": "y",
        "CONFIG_EROFS_FS_XATTR": "y",
        "CONFIG_EROFS_FS_POSIX_ACL": "y",
        "CONFIG_OVERLAY_FS": "y",
    },
    reason="RootFS.verity(): the initrd opens an erofs root through dm-verity, then overlays it",
)
"""What ``RootFS.verity()`` asks of a built kernel, flashbots-images'
``modules/verity-root/kernel/config.d/50-verity-root``: the drivers check already wanted
``DM_VERITY``, ``EROFS_FS`` and ``OVERLAY_FS`` built in, and this also requests them with
their parents (``MD``, ``BLK_DEV_DM``), the hash the root hash is (``CRYPTO_SHA256``), and the
extended attributes and ACLs of the root's files (``EROFS_FS_XATTR``, ``EROFS_FS_POSIX_ACL``)."""


CONTAINER_KERNEL = KernelFragment(
    {"CONFIG_BLK_DEV_LOOP": "y", "CONFIG_EROFS_FS": "y"},
    reason="a Container: its unit attaches the erofs root image through a loop device",
)
"""What a ``Container`` in any profile asks of a built kernel: ``loop`` and ``erofs`` built
in, since a built kernel loads no module and the container's loader skips ``modprobe`` once
``/dev/loop-control`` exists. The drivers check already wanted both."""


def kernel_of(image: Image) -> dm.Kernel:
    """The internal kernel *image*'s ``Kernel.build(...)`` lowers to: what CI's kernel
    fixtures build, so they build exactly what an image does."""
    return _Lowering(image).kernel()


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
            uki_max_size=image.rootfs.uki_max_size,
            build_network=state.build_network(),
        ),
    )


@dataclass(slots=True)
class _Lowering:
    """One pass over an image, collecting what the declarations alone cannot carry."""

    image: Image
    owned: list[tuple[str, str]] = field(default_factory=list)
    owner_steps: tuple[dm.Ready, ...] = ()
    curl: bool = False
    curl_shipped: bool = False
    """Whether the image itself ships curl, which an HTTP probe runs."""
    world: dict[str, Service] = field(default_factory=dict)
    """The services as the profile being lowered sees them, by unit, where it edits them."""
    removed: frozenset[str] = frozenset()
    """The units of the services the profile being lowered removes, or its ancestors do."""
    backports: bool = False
    sends: tuple[tuple[str, Egress], ...] | None = None
    owned_rules: tuple[dm.EgressRule, ...] = ()
    """The rules a packaged daemon's user owns (chrony's NTP), beside what *sends* asks."""
    implied: dict[str, dict[str, tuple[str, str, str]]] = field(default_factory=dict)
    """Unit -> the ready unit an :class:`Endpoint` in its argv or environment added to its
    ``requires=``, with where it named it, the URL and the upstream's unit (for
    ``requires-implied``)."""
    runtime: Runtime = field(init=False)
    baseline_types: tuple[type, ...] = field(init=False)
    declared: frozenset[str] = field(init=False)
    """The units of every service the image, its profiles and ``dev=`` declare."""

    def __post_init__(self) -> None:
        from tundravm.components.access import SshAccess
        from tundravm.components.network import Firewall, Network
        from tundravm.components.tee import Tdxs, TeeBase

        self.baseline_types = (TeeBase, Network, Firewall, Tdxs)
        image = self.image
        evidence = self.explicit(Tdxs)
        self.runtime = Runtime(
            volumes=tuple(image.volumes),
            secrets=tuple(image.secrets),
            ingress=tuple(image.ingress),
            # A profile's services and its edits of them too: the volume step makes their
            # directories in every profile, which is harmless where the profile is not
            # selected.
            services=tuple(
                services_of(
                    (
                        *self.workload(),
                        *(i for p in image.profiles.values() for i in p.add),
                        *(
                            c.edited
                            for p in image.profiles.values()
                            for c in p.changes
                            if isinstance(c, Edit)
                        ),
                    )
                )
            ),
            tools=Tdxs().source if evidence is None else evidence.source,
            key_files=key_files(image),
            containers=container_ports(image),
            ssh=self.explicit(SshAccess),
            rootfs=image.rootfs.layout,
            switchable=bool(getattr(self.explicit(Firewall), "modes", None)),
            azure="azure" in image.targets,
        )
        check_owner_login(image, self.runtime.ssh)
        dev = () if image.dev is None else tuple(services_of((image.dev,)))
        self.declared = frozenset(s.unit_name for s in (*self.runtime.services, *dev))

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
        """Debian, its archive, its boot set and the command line: ``TeeBase``'s
        parameters, then the kernel's."""
        from tundravm.components.tee import TeeBase

        base = self.image.base
        kernel = self.image.kernel
        tee = self.explicit(TeeBase) or TeeBase()
        return dm.Debian(
            base.release,
            snapshot=base.archive,
            efi_stub=base.efi_stub,
            kernel_package=None if kernel.built else "linux-image-amd64",
            kernel_modules=() if isinstance(kernel.modules, bool) else tuple(kernel.modules),
            kernel_cmdline=(*tee.kernel_cmdline, *kernel.cmdline),
        )

    def items(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """Everything the image declares, implied baseline first."""
        from tundravm.components.access import SshAccess

        image = self.image
        body: list[dm.Declaration | dm.Fragment] = []
        for item in (*image.packages, *image.files, *image.users, *image.groups):
            body.extend(self.content(item))
        for component in image.components:
            if isinstance(component, SshAccess) and self.runtime.control_plane:
                continue  # the control plane runs it, accepting the operator's key too
            if not isinstance(component, self.baseline_types):
                body.extend(self.content(component))
        if image.observability is not None:
            body.extend(self.content(image.observability))
        for service in image.services:
            body.extend(self.content(service))
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
        body.append(dm.Fragment(IMPLIED_FRAGMENT, checks=(self.implied_check,)))
        if image.kernel.built:
            body.append(self.kernel())
        body.extend(self.repositories())
        body.extend(self.mkosi())
        return (*self.baseline(), *self.owners(body))

    def baseline(self) -> tuple[dm.Declaration | dm.Fragment, ...]:
        """What every image gets: the TEE baseline, the network, the firewall and the
        evidence service, each the image's own when it passed one in ``components=``."""
        from tundravm.components._network import egress_rules
        from tundravm.components._tee import CHRONY_NTP
        from tundravm.components.network import Firewall, Network
        from tundravm.components.tee import Tdxs, TeeBase

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
            firewall = Firewall(allow=firewall.allow, egress=egress, modes=firewall.modes)
        filtering = self.component(firewall)
        if egress is not None:
            self.sends = tuple(self.outbound(egress, (tee, tdxs, network)))
            self.owned_rules = (CHRONY_NTP,) if tee.time else ()
            rules = (*egress_rules(self.sends), *self.owned_rules)
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
        self.curl_shipped = self.curl
        return tuple(found)

    def outbound(
        self, egress: Egress, baseline: Sequence[Component]
    ) -> Iterator[tuple[str, Egress]]:
        """Who sends where: the image's *egress*, the ``outbound()`` of the *baseline*, of
        every component and of what each composes. The cloud glue reaches the metadata
        servers by its own units' egress."""
        image = self.image
        yield "image", egress
        own = [item for item in image.components if not isinstance(item, self.baseline_types)]
        observability = [] if image.observability is None else [image.observability]
        for component in (*baseline, *own, *observability):
            yield from _outbound(component)

    def profile_egress(
        self, added: Sequence[object], inherited: Sequence[object] = ()
    ) -> tuple[dm.Edit, ...]:
        """The edit of the firewall a profile needs so its output chain also accepts what
        the components it adds (*added*, beyond the *inherited* ones of the profiles it
        extends) send: none when the image does not filter egress or they ask for nothing
        new."""
        from tundravm.components._network import egress_rules

        if self.sends is None:
            return ()
        before = (*self.sends, *_sends(inherited))
        rules = egress_rules((*before, *_sends(added)))
        if rules == egress_rules(before):
            return ()
        return (dm.Ref("Firewall").edit(egress_rules=(*rules, *self.owned_rules)),)

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
        """The kernel ``Kernel.build`` compiles: its base, the hardening flag, then its own
        fragments and patches and what :meth:`kernel_needs` adds, in that order."""
        kernel = self.image.kernel
        assert kernel.version is not None and kernel.source is not None
        base = kernel.base if kernel.base is not None else kernel.config
        assert base is not None
        own: list[KernelFragment | KernelPatch] = [
            KernelFragment.read(item) if isinstance(item, Path) else item
            for item in kernel.fragments
        ]
        own += [KernelPatch(item) if isinstance(item, Path) else item for item in kernel.patches]
        needs = [*own, *self.kernel_needs()]
        fragments = []
        for item in needs:
            if isinstance(item, KernelFragment):
                with _at(item):
                    fragments.append(dm.KernelConfig(item.options, reason=item.reason))
        return dm.Kernel(
            kernel.version,
            kernel.source,
            base=base,
            fragments=tuple(fragments),
            patches=tuple(
                dm.KernelPatch(item.source) for item in needs if isinstance(item, KernelPatch)
            ),
            modules=kernel.loadable,
            hardening=kernel.hardening,
        )

    def kernel_needs(self) -> Iterator[KernelFragment | KernelPatch]:
        """What the image's components (each one's :meth:`~Component.kernel`, nested ones
        after it, in declaration order, then the observability), its root and its targets
        need of a built kernel. A component a profile adds may need nothing: every profile
        boots the one kernel."""
        image = self.image
        observability = () if image.observability is None else (image.observability,)
        for component in (*image.components, *observability):
            yield from _kernel_needs(component)
        for name, profile in image.profiles.items():
            for item in profile.add:
                if isinstance(item, Component) and tuple(_kernel_needs(item)):
                    raise ValidationError(
                        f"Image: profile {name!r} adds {item.name}, whose kernel() asks the "
                        "built kernel for options or patches every profile would boot.",
                        hint=f"Put {type(item).__name__}(...) in Image(components=[...]).",
                    )
        if image.rootfs.layout == "verity":
            yield VERITY_KERNEL
        contents = (*image.components, *(i for p in image.profiles.values() for i in p.add))
        if any(_declares_container(item) for item in contents):
            yield CONTAINER_KERNEL
        if "gcp" in image.targets:
            yield KernelPatch(GCP_KERNEL_PATCH)

    def repositories(self) -> Iterator[dm.Declaration]:
        """``Debian(repositories=)``: each archive for the build's apt only (an image never
        runs apt), its keyring, and for a digest pin the ``sync`` step that fails the build
        when the archive's ``InRelease`` moved: it fetches the file, unless an offline build
        passes the digest the lock recorded (:func:`~tundravm.models.inrelease_variable`)."""
        base = self.image.base
        for repository in base.repositories:
            yield dm.Repository(
                repository.name,
                repository.address(base.archive),
                suite=repository.suite,
                components=tuple(repository.components),
                keyring=repository.keyring,
                in_image=False,
                sha256=repository.sha256,
            )
            if isinstance(repository.key, Path):
                yield dm.File.copy(repository.keyring, repository.key, mode=0o644)
            if repository.sha256 is not None:
                url = shlex.quote(repository.release_url)
                message = shlex.quote(
                    f"AptRepository {repository.url} {repository.suite} moved: its InRelease "
                    f"is no longer sha256 {repository.sha256}"
                )
                fetched = f'$(curl -fsSL --proto =https {url} | sha256sum | cut -d " " -f 1)'
                yield dm.Hook(
                    f"apt-{repository.name}",
                    "sync",
                    f'test "${{{inrelease_variable(repository.name)}:-{fetched}}}" '
                    f"= {repository.sha256} || {{ echo {message} >&2; exit 1; }}",
                )

    def mkosi(self) -> tuple[dm.Declaration, ...]:
        """``mkosi=Mkosi(settings=, scripts=)``, recorded as settings and hooks."""
        mkosi = self.image.mkosi
        if mkosi is None:
            return ()
        settings = mkosi.settings or {}
        found: list[dm.Declaration] = [
            dm.Setting(mkosi_section(key), key, (value,)) for key, value in sorted(settings.items())
        ]
        for index, (stage, script) in enumerate(mkosi.scripts, start=1):
            found.append(dm.Hook(f"mkosi-{stage}-{index}", stage, script))  # type: ignore[arg-type]
        return tuple(found)

    def owners(
        self, items: Sequence[dm.Declaration | dm.Fragment], profile: str | None = None
    ) -> list[dm.Declaration | dm.Fragment]:
        """*items* and the :data:`OWNERS_STEP` that applies every ``owner=`` collected so far
        (the image's, or *profile*'s own), which forgets them; every service and task in
        *items* waits for it and for the image's."""
        owned, self.owned = self.owned, []
        found = list(items)
        if owned:
            name = OWNERS_STEP if profile is None else f"{OWNERS_STEP}-{profile}"
            lines = sorted(dict.fromkeys(_owned_lines(owned)))
            found.append(dm.BootStep(name, "".join(f"{line}\n" for line in lines)))
            if profile is None:
                self.owner_steps = (dm.Ready(name),)
            steps = (*self.owner_steps, *(() if profile is None else (dm.Ready(name),)))
        else:
            steps = self.owner_steps
        return [_waiting(item, steps) for item in found] if steps else found

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
        changes, regenerated, world, removed = self.changes(profile)
        items: list[dm.Declaration | dm.Fragment] = []
        self.world, self.removed, curl, self.curl = world, removed, self.curl, False
        for item in profile.add:
            items.extend(self.content(item))
        self.world, self.removed = {}, frozenset()
        items.extend(regenerated)
        if any(isinstance(service.readiness, HttpProbe) for service in world.values()):
            self.curl = True
        if self.curl and not self.curl_shipped:
            items.append(dm.Package("curl"))
        self.curl = curl
        items = self.owners(items, name)
        inherited = [item for ancestor in self.ancestors(profile) for item in ancestor.add]
        with _at(profile):
            egress = self.profile_egress(profile.add, inherited)
        return dm.Variant(
            name,
            inherits=profile.extends or BASE_PROFILE,
            add=dm.Fragment(name, items=tuple(items)),
            changes=(*changes, *egress),
            release=profile.production,
        )

    def changes(
        self, profile: Profile
    ) -> tuple[
        tuple[dm.Edit | dm.Remove, ...],
        tuple[dm.Declaration, ...],
        dict[str, Service],
        frozenset[str],
    ]:
        """*profile*'s changes on the declarations their values lower to, the declarations
        its service edits add, the services as it sees them, by unit, and the units of the
        services it and its ancestors remove.

        An edit changes what the value its profile sees lowers to: the image's value with
        the edits of the profiles it extends applied, so two edits that land on the same
        directive (``resources=`` and ``shutdown=`` both write ``[Service]`` lines) add up.
        Service edits are lowered together: every service the profile inherits is lowered
        as it sees them, so a dependent of an edited readiness waits for the new probe, and
        what differs from the inherited lowering is the profile's change. A removed service
        takes everything it lowers to with it: its unit and user, its probe, its drop-in,
        its timer, its firewall rules and its waits for credentials.
        """
        current: dict[dm.Ref, object] = {}
        world: dict[str, Service] = {}
        units = {service.unit_name for service in self.profile_services(profile)}
        gone: set[str] = set()
        for ancestor in self.ancestors(profile):
            for change in ancestor.changes:
                unit = _removed_unit(change, units) if isinstance(change, Remove) else None
                if unit is not None:
                    gone.add(unit)
                elif isinstance(change, Edit) and isinstance(change.ref.value, Service):
                    unit = change.ref.value.unit_name
                    world[unit] = replace(world.get(unit, change.ref.value), **change.fields)  # type: ignore[arg-type]
                elif isinstance(change, Edit):
                    target = change.ref.target
                    current[target] = replace(
                        current.get(target, change.ref.value), **change.fields
                    )  # type: ignore[type-var]
        inherited, inherited_gone = dict(world), frozenset(gone)
        causes: dict[str, Edit | Remove] = {}
        found: list[dm.Edit | dm.Remove] = []
        position: int | None = None
        for change in profile.changes:
            if isinstance(change, (dm.Edit, dm.Remove)):
                found.append(change)
                continue
            with _at(change):
                unit = _removed_unit(change, units) if isinstance(change, Remove) else None
                if isinstance(change, Edit) and isinstance(change.ref.value, Service):
                    unit = change.ref.value.unit_name
                    if unit not in units:
                        found.append(self.missing_edit(change))
                        continue
                if unit is not None:
                    if unit in gone or (isinstance(change, Remove) and unit in causes):
                        raise ValidationError(
                            f"The profile changes {change.ref} beside removing it.",
                            hint="Keep one change of the service: an edit or its removal.",
                        )
                    if isinstance(change, Remove):
                        gone.add(unit)
                    else:
                        self.edit_service(change, world)
                    causes[unit] = change
                    position = len(found) if position is None else position
                    continue
                if isinstance(change, Remove):
                    found.append(dm.Remove(change.ref.target))
                    continue
                target = change.ref.target
                before = current.get(target, change.ref.value)
                after = current[target] = replace(before, **change.fields)  # type: ignore[type-var]
                found.append(self.edit(change, before, after))
        removed = frozenset(gone)
        if position is None:
            return tuple(found), (), world, removed
        edits, added = self.regenerate(
            profile, (inherited, inherited_gone), (world, removed), causes
        )
        found[position:position] = edits
        return tuple(found), added, world, removed

    def profile_services(self, profile: Profile) -> tuple[Service, ...]:
        """The services *profile* inherits: the image's and its ancestors' additions."""
        return tuple(
            services_of((*self.workload(), *(i for a in self.ancestors(profile) for i in a.add)))
        )

    def edit_service(self, change: Edit, world: dict[str, Service]) -> None:
        """Apply *change* to the service it names in *world*, refusing one that changes
        none of its units."""
        value = change.ref.value
        assert isinstance(value, Service)
        before = world.get(value.unit_name, value)
        after = replace(before, **change.fields)  # type: ignore[arg-type]
        self.world = world
        try:
            unchanged = tuple(self.units(before)) == tuple(self.units(after))
        finally:
            self.world = {}
        if unchanged:
            raise ValidationError(
                f"The edit of {change.ref} changes nothing the image builds.",
                hint="Drop it, or set a keyword whose value differs.",
            )
        world[value.unit_name] = after

    def missing_edit(self, change: Edit) -> dm.Edit:
        """The internal edit of a service *change*'s profile does not inherit, which the
        resolver reports as ``edit-missing``, as it does for a file or a package."""
        value = change.ref.value
        assert isinstance(value, Service)
        before, after = (next(self.units(item)) for item in (value, change.edited))
        keys = dm.key_fields(type(after).__name__)
        fields = _differing(before, after) or {
            item.name: getattr(after, item.name)
            for item in dataclass_fields(after)
            if item.init and item.name not in keys
        }
        return dm.Edit(change.ref.target, fields)

    def regenerate(
        self,
        profile: Profile,
        inherited: tuple[dict[str, Service], frozenset[str]],
        world: tuple[dict[str, Service], frozenset[str]],
        causes: dict[str, Edit | Remove],
    ) -> tuple[list[dm.Edit | dm.Remove], tuple[dm.Declaration, ...]]:
        """What turns the units of the services *profile* inherits, lowered as *inherited*
        sees them, into those lowered as *world* does: an edit of each unit that differs, a
        removal of each that is gone, and the units that are new. Each pair is the services
        as a profile sees them and the units of those it removes."""
        services = self.profile_services(profile)
        before = {unit.ref: unit for unit, _ in self.lowered(services, *inherited)}
        after = self.lowered(services, *world)
        edits: list[dm.Edit | dm.Remove] = []
        added: list[dm.Declaration] = []
        for unit, owner in after:
            old = before.get(unit.ref)
            with _at(causes.get(owner, profile)):
                if old is None:
                    added.append(unit)
                elif old != unit:
                    edits.append(dm.Edit(unit.ref, _differing(old, unit)))
        kept = {unit.ref for unit, _ in after}
        with _at(profile):
            edits.extend(dm.Remove(ref) for ref in before if ref not in kept)
        return edits, tuple(added)

    def lowered(
        self, services: Sequence[Service], world: dict[str, Service], removed: frozenset[str]
    ) -> list[tuple[dm.Declaration, str]]:
        """The units of *services* as *world* sees them, those *removed* names left out,
        each with its service's unit."""
        self.world, self.removed = world, removed
        try:
            found: list[tuple[dm.Declaration, str]] = []
            for service in services:
                if service.unit_name in removed:
                    continue
                seen = world.get(service.unit_name, service)
                with _at(seen):
                    found.extend((unit, seen.unit_name) for unit in self.units(seen))
            return found
        finally:
            self.world, self.removed = {}, frozenset()

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
        fields = _differing(self.declaration(before), self.declaration(after))
        if not fields:
            raise ValidationError(
                f"The edit of {change.ref} changes nothing the image builds.",
                hint="Drop it, or set a keyword whose value differs.",
            )
        return dm.Edit(change.ref.target, fields)

    def declaration(self, value: object) -> dm.Declaration:
        """The one declaration a :class:`File` or :class:`Tree` lowers to."""
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
        items = self.owners(items, DEV_PROFILE)
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
        self.unmounted(item, item.path)
        if item.owner is not None:
            self.owned.append((item.path, item.owner))
        yield self.declaration(item)

    def tree(self, item: Tree) -> Iterator[dm.Declaration]:
        """A ``Tree`` as the host directory it ships."""
        self.unmounted(item, item.dst)
        if item.owner is not None:
            self.owned.append((item.dst + "/", item.owner))
        yield self.declaration(item)

    def unmounted(self, item: File | Tree, path: str) -> None:
        """Refuse *item* at *path* under a volume's mount, which hides what the image ships."""
        for volume in self.runtime.volumes:
            if path == volume.mount or path.startswith(volume.mount.rstrip("/") + "/"):
                with _at(item):
                    raise ValidationError(
                        f"{type(item).__name__} {path!r} is under Volume {volume.name!r}, "
                        f"whose mount at {volume.mount} hides what the image ships there.",
                        hint="Ship it outside the volume; a service writes the volume at run "
                        f"time, from its state=, such as {volume.name}.directory(...).",
                    )

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
        """A ``Service`` as the builds its argv pulls in, its package and its units."""
        if isinstance(item.readiness, HttpProbe):
            self.curl = True
        if item.package is not None:
            yield dm.Package(item.package)
        yield from item.binaries()
        yield from self.units(item)

    def units(self, item: Service) -> Iterator[dm.Declaration]:
        """*item*'s units: its ``.service`` and its probe, or its timer task, then its
        drop-in; a package's unit with its one drop-in, then its probe."""
        if item.package is not None:
            yield self.packaged(item)
            probe = self.probe(item)
            if probe is not None:
                yield probe
            return
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
        directives, requires, environment, groups, working, write = self.bound(item)
        shutdown = item.shutdown
        state = item.state
        return dm.Service(
            item.unit_name,
            item.command(),
            wants=self.resolution(item),
            type="notify" if isinstance(item.readiness, Notify) else "exec",
            user=item.user,
            groups=tuple(groups),
            working_dir=working,
            env=environment,
            credentials=_credentials(item.bindings),
            state=None if isinstance(state, VolumeDirectory) else state,
            write_paths=write,
            needs=_outputs(item),
            logs=item.logs,
            runtime=item.runtime,
            ports=tuple(dm.Port(port.port, port.protocol) for port in item.expose),
            requires=requires,
            after=requires,
            hardening=cast("Hardening", item.hardening),
            egress=_own_egress(item),
            socket=item.socket,
            timeout_stop=None if shutdown is None else shutdown.timeout,
            kill_mode=None if shutdown is None else shutdown.kill_mode,
            directives={"Service": directives} if directives else {},
        )

    def packaged(self, item: Service) -> dm.Unit:
        """A ``Service.packaged`` as the package's unit, enabled, with one drop-in: the
        sandbox (a preset sets every field, so it replaces the package's), the execution
        gate every service waits for, ``requires=`` and the image's ``unit=``."""
        units = (*self.required_units(item), GATE_UNIT)
        hardening = cast("Hardening", item.hardening)
        if item.unit is None:
            dropin = dm.DropIn(hardening=hardening, after=units, requires=units)
        else:
            dropin = replace(
                item.unit,
                hardening=hardening,
                after=(*item.unit.after, *units),
                requires=(*item.unit.requires, *units),
            )
        return dm.Unit(
            item.unit_name,
            enabled=True,
            dropin=dropin,
            ports=tuple(dm.Port(port.port, port.protocol) for port in item.expose),
        )

    def bound(
        self, item: Service
    ) -> tuple[
        dict[str, str | tuple[str, ...]],
        tuple[str, ...],
        dict[str, str],
        list[str],
        str | None,
        tuple[str, ...],
    ]:
        """What *item*'s secrets and volume add to its unit, timer or not: the directives
        (an optional secret's ``SetCredential=``), the units it requires (readiness, then the
        secret and volume waits), its environment, groups, working directory and the paths
        it writes."""
        directives = _directives(item)
        optional = self.runtime.optional(item)
        if optional:
            directives["SetCredential"] = optional
        requires = (*self.required_units(item), *self.runtime.waits(item))
        state = item.state
        environment = item.variables()
        groups = _groups(item)
        working = item.working_directory
        write: tuple[str, ...] = ()
        if isinstance(state, VolumeDirectory):
            environment.setdefault("STATE_DIRECTORY", state.path)
            working = working or state.path
            write = (f"-{state.path}",)
            if state.group is not None and state.group not in groups:
                groups.append(state.group)
        return directives, requires, environment, groups, working, write

    def resolution(self, item: Service) -> tuple[str, ...]:
        """``egress-resolve.service`` when *item*'s own egress names a host the image's
        filter resolves: ``Wants=`` and ``After=`` it, so the service starts once the first
        resolution filled the name's sets, and starts anyway when it finds nothing."""
        from tundravm.components.network import Firewall, Network

        egress = item.egress
        if egress is None or not any(egress_host(egress, host)[2] for host in egress.hosts):
            return ()
        network = self.image.network or self.explicit(Network)
        firewall = self.explicit(Firewall)
        filtered = (network is not None and network.egress is not None) or (
            firewall is not None and firewall.egress is not None
        )
        return (EGRESS_RESOLVE_UNIT,) if filtered else ()

    def timer_task(self, item: Service) -> dm.Task:
        """A ``Service(timer=...)`` as the scheduled oneshot it is, every keyword kept: its
        secrets and its volume are waited for as a long-lived service's are."""
        directives, requires, environment, groups, working, write = self.bound(item)
        shutdown = item.shutdown
        state = item.state
        return dm.Task(
            item.unit_name,
            item.command(),
            wants=self.resolution(item),
            schedule=item.timer,
            user=item.user,
            groups=tuple(groups),
            state=None if isinstance(state, VolumeDirectory) else state,
            working_dir=working,
            env=environment,
            write_paths=write,
            credentials=_credentials(item.bindings),
            logs=item.logs,
            runtime=item.runtime,
            after=requires,
            requires=requires,
            needs=_outputs(item),
            hardening=cast("Hardening", item.hardening),
            egress=_own_egress(item),
            timeout_stop=None if shutdown is None else shutdown.timeout,
            kill_mode=None if shutdown is None else shutdown.kill_mode,
            directives={"Service": directives} if directives else {},
        )

    def probe(self, item: Service) -> dm.Task | None:
        """The bounded unit that proves *item* ready, when its readiness needs one."""
        readiness = item.readiness
        if readiness is None or isinstance(readiness, Notify):
            return None
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
        for requirement in (*_evidence(item), *_instance_key(item), *item.requires):
            if isinstance(requirement, dm.UnitOutput):
                continue
            for handle in _handles(requirement, self.world):
                if handle.unit in self.removed:
                    raise ValidationError(
                        f"Service {item.name!r} requires {handle.unit!r}, which the profile "
                        "removes.",
                        hint=f"Remove {item.name!r} too, or edit its requires= in the profile.",
                    )
                _declared(item, handle, self.declared)
                units.append(_ready_unit(item, handle))
        for where, endpoint, handle in self.endpoint_waits(item):
            ready = _ready_unit(item, handle)
            if ready not in units:
                units.append(ready)
                waits = self.implied.setdefault(item.unit_name, {})
                waits[ready] = (where, endpoint.url, handle.unit)
        return tuple(dict.fromkeys(units))

    def endpoint_waits(self, item: Service) -> Iterator[tuple[str, Endpoint, Readied]]:
        """``(where, endpoint, handle)`` of each :class:`Endpoint` of another service in
        *item*'s argv or environment whose readiness ``requires=`` does not already name:
        the readiness *item* waits for without saying it. An upstream without one adds
        nothing (nothing proves it ready)."""
        named = {
            handle.unit
            for requirement in item.requires
            if not isinstance(requirement, dm.UnitOutput)
            for handle in _handles(requirement, self.world)
        }
        values = [("argv", word) for word in item.argv[1:]]
        values += [(f"environment {key}", value) for key, value in item.environment.items()]
        for where, value in values:
            if not isinstance(value, Endpoint) or value.ready is None:
                continue
            (handle,) = _handles(value, self.world)
            if not isinstance(handle, ServiceReady) or handle.probe is None:
                continue
            if handle.unit in (item.unit_name, *named):
                continue
            if handle.unit in self.removed:
                raise ValidationError(
                    f"Service {item.name!r} reads {value.url} in its {where}, the endpoint of "
                    f"{handle.unit!r}, which the profile removes.",
                    hint=f"Remove {item.name!r} too, or edit its {where.split()[0]} in the "
                    "profile.",
                )
            named.add(handle.unit)
            yield where, value, handle

    def implied_check(self, resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]:
        """``requires-implied``: what an endpoint in a service's argv or environment added
        to the ``requires=`` of each unit the profile holds."""
        found: list[dm.Diagnostic] = []
        for item in resolved.items:
            if not isinstance(item, (dm.Service, dm.Task)):
                continue
            unit = item.unit if isinstance(item, dm.Task) else _unit_of(item.name)
            requires = {target_unit(target) for target in item.requires}
            for ready, (where, url, upstream) in self.implied.get(unit, {}).items():
                if ready not in requires:
                    continue
                stem = upstream.removesuffix(".service")
                found.append(
                    dm.Diagnostic(
                        "requires-implied",
                        "info",
                        f"{unit} waits for {ready}: its {where} reads {url}, {stem}'s endpoint",
                        subject=item.name,
                        location=display_location(declared_at(item)),
                        hint=f"Nothing to fix; requires=[{stem}.ready] says it in the image "
                        "file and silences this.",
                    )
                )
        return tuple(found)


def _unit_of(name: str) -> str:
    return name if name.endswith(".service") else f"{name}.service"


def _removed_unit(change: Remove, units: set[str]) -> str | None:
    """The unit of the service *change* removes, ``None`` when it removes something else."""
    target = change.ref.target
    if target.kind in ("Service", "Task") and target.identity[0] in units:
        return str(target.identity[0])
    return None


def _evidence(item: Service) -> tuple[ServiceReady, ...]:
    """The evidence service's readiness, which a service that asks for quotes requires."""
    from tundravm.components.tee import EVIDENCE

    return (EVIDENCE,) if item.quotes else ()


def _instance_key(item: Service) -> tuple[BootReady, ...]:
    """The instance key's generation, which a service that binds the key requires."""
    bound = any(isinstance(binding, InstanceKey) for binding in item.bindings)
    return (InstanceKey().ready,) if bound else ()


def _groups(item: Service) -> list[str]:
    """*item*'s groups, the evidence service's first when it asks for quotes."""
    quoting = [_tdxs.Tdxs().group] if item.quotes else []
    return list(dict.fromkeys([*quoting, *item.groups]))


def _outputs(item: Service) -> tuple[dm.UnitOutput, ...]:
    """The setup tasks' outputs ``requires=`` names, which the unit waits for by condition."""
    return tuple(r for r in item.requires if isinstance(r, dm.UnitOutput))


def _handles(requirement: object, world: dict[str, Service]) -> tuple[Readied, ...]:
    """The readiness handles one ``requires=`` entry stands for, each service's as *world*
    declares it where it holds the service."""
    if isinstance(requirement, BootReady):
        return (requirement,)
    if isinstance(requirement, (ServiceReady, ServiceRef, Endpoint)):
        handle = requirement if isinstance(requirement, ServiceReady) else requirement.ready
        assert handle is not None
        return (world[handle.unit].ready if handle.unit in world else handle,)
    if isinstance(requirement, ComponentReady):
        component = requirement.component
        if type(component).readiness is Component.readiness:
            services = (world.get(s.unit_name, s) for s in component.services())
            found: tuple[Readied, ...] = tuple(s.ready for s in services if s.readiness is not None)
        else:
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


def _declared(owner: Service, handle: Readied, declared: frozenset[str]) -> None:
    """Refuse a ``requires=`` on a service's ``.ready`` the image does not declare, which
    would leave *owner* waiting on a unit nothing writes."""
    required = handle.service if isinstance(handle, ServiceReady) else None
    if required is None or handle.unit in declared:
        return
    name, other = owner.name.removesuffix(".service"), required.name.removesuffix(".service")
    raise ValidationError(
        f'Service("{name}") requires Service("{other}"), which the image does not declare.',
        hint=f'Image(services=[Service("{other}", [...]), ...]) or the component that '
        f"composes it, or drop {other}.ready from requires=.",
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


def _differing(old: dm.Declaration, new: dm.Declaration) -> dict[str, object]:
    """The fields of *new*, keys aside, whose values differ from *old*'s."""
    keys = dm.key_fields(type(new).__name__)
    return {
        item.name: getattr(new, item.name)
        for item in dataclass_fields(new)
        if item.init
        and item.name not in keys
        and getattr(new, item.name) != getattr(old, item.name)
    }


def _waiting(
    item: dm.Declaration | dm.Fragment, steps: tuple[dm.Ready, ...]
) -> dm.Declaration | dm.Fragment:
    """*item*, a service or task in it waiting for *steps* as well (``needs=``)."""
    if isinstance(item, dm.Fragment):
        items = tuple(_waiting(child, steps) for child in item.items)
        if items == item.items:
            return item
        # A Composite (a GoBinary, a CargoBinary) composes its own items, so a copy takes
        # the changed ones in place of replace().
        found = copy.copy(item)
        object.__setattr__(found, "items", items)
        return found
    if isinstance(item, (dm.Service, dm.Task)) and dm.RUNTIME_INIT not in item.needs:
        missing = tuple(step for step in steps if step not in item.needs)
        return replace(item, needs=(*item.needs, *missing)) if missing else item
    return item


def _owned_lines(owned: Sequence[tuple[str, str]]) -> Iterator[str]:
    """One ``chown`` line per owned path, ``-R`` for a tree's."""
    for path, owner in owned:
        user, _, group = owner.partition(":")
        recursive = ["-R"] if path.endswith("/") else []
        yield shlex.join(["chown", *recursive, f"{user}:{group or user}", path.rstrip("/")])


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


def _own_egress(item: Service) -> tuple[dm.EgressRule, ...]:
    """*item*'s ``egress=`` as the rules the firewall accepts for its user alone."""
    from tundravm.components._network import egress_rules

    return () if item.egress is None else egress_rules(((item.unit_name, item.egress),))


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


def _kernel_needs(component: Component) -> Iterator[KernelFragment | KernelPatch]:
    """*component*'s ``kernel()``, then that of each component it composes."""
    needs = component.kernel()
    if isinstance(needs, (str, KernelFragment, KernelPatch)) or not all(
        isinstance(item, (KernelFragment, KernelPatch)) for item in needs
    ):
        raise ValidationError(
            f"{type(component).__name__}.kernel() returned {needs!r}, not a list of "
            "KernelFragment and KernelPatch values.",
            hint='Return [KernelFragment({"CONFIG_XFS_FS": "y"})], or () when it needs nothing.',
        )
    yield from needs
    for item in composed(component):
        if isinstance(item, Component):
            yield from _kernel_needs(item)


def _declares_container(item: object) -> bool:
    """Whether *item* is or composes a ``Container`` (its root image, ``dm.ContainerRoot``)."""
    if isinstance(item, dm.ContainerRoot):
        return True
    return isinstance(item, Component) and any(map(_declares_container, composed(item)))


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
