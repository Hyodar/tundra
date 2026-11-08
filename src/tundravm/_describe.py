"""What ``show IMAGE`` describes, without compiling or building.

:func:`declared` reads a public :class:`~tundravm.Image` back as it was written: its
components by class and keyword, its services with what each one runs, opens and takes,
and its profiles with what each one changes. :func:`describe` is what one profile builds:
packages, sources, files, units, firewall and build steps, each in public terms (a
service's probe is its service's, a build step is what it does). Both are
JSON-serializable and deterministically ordered.

``show`` prints them as :class:`Section` rows, and ``show --diff`` (and a profile beyond
the one it extends) prints the rows two descriptions do not share (:func:`diff_sections`).
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import inspect
import json
import re
from collections.abc import Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ._modules import instance_key
from ._modules.devtools import NETWORK_LOGIN_SCRIPT
from ._modules.init import runtime_init_order, step_words
from ._options import MkosiOptions
from .errors import TdxError
from .lockfile import LockedFetch
from .models import Kernel, ProfileState, unit_name

if TYPE_CHECKING:
    from ._image import Component, Image, Service
    from .declarative._lowered import Lowered
    from .declarative.model import Recipe, Resolved
    from .declarative.systemd import Hardening

SHORT_DIGEST_LEN = 12
PROGRAM_WIDTH = 100
MASKED = '"***"'
"""How a component keyword that holds a password prints."""

_MASKED_FIELDS = frozenset({"password"})
_FIREWALL = "/etc/nftables.conf"
_RUNTIME = "/etc/tundravm/runtime.json"


# ── the image as written ─────────────────────────────────────────────────


def declared(
    image: Image, profiles: Sequence[str], bindings: Mapping[str, object] | None = None
) -> dict[str, object]:
    """*image*'s components, services and *profiles*, as the image file wrote them.

    Components are the ones the file named (``components=``, ``network=``,
    ``observability=``), never the implied baseline; services are the image's and those its
    components compose, each with the component that holds it; content is what the image
    names itself (``packages=``, ``files=``, ``users=``, ``groups=``); logins are each
    ``SshAccess`` login's commands, each with the profiles it exists in. Within a call,
    a component, service or volume the file binds at its top level (*bindings*, by name)
    is that name, and what one exports is ``name.attribute`` (:func:`call`).
    """
    token = _REFERENCES.set(_References(bindings or {}))
    try:
        return _declared(image, profiles)
    finally:
        _REFERENCES.reset(token)


def _declared(image: Image, profiles: Sequence[str]) -> dict[str, object]:
    return {
        "components": [_component(c) for c in _written_components(image)],
        "services": [_service(s, owner) for s, owner in _written_services(image)],
        "ingress": [call(way) for way in image.ingress],
        "content": [
            _content(item) for item in (*image.packages, *image.files, *image.users, *image.groups)
        ],
        "profiles": [row for row in _profiles(image) if row["name"] in profiles],
        "logins": _logins(image),
    }


def declared_recipe(recipe: Recipe, profiles: Sequence[str]) -> dict[str, object]:
    """What :func:`declared` says of a file that binds an internal ``Recipe``: its profiles,
    and no components or services, which only an ``Image`` declares."""
    from .declarative.model import BASE_VARIANT

    rows: list[dict[str, object]] = [
        {
            "name": variant.name,
            "extends": None if variant.inherits in (None, BASE_VARIANT) else variant.inherits,
            "production": variant.release,
            "adds": [],
            "changes": [],
        }
        for variant in recipe.variants
        if variant.name in profiles
    ]
    return {
        "components": [],
        "services": [],
        "ingress": [],
        "content": [],
        "profiles": rows,
        "logins": [],
    }


def probe_units(image: Image) -> frozenset[str]:
    """The probe unit of every service *image* (or a profile of it) declares: what proves
    the service ready, which ``show`` prints as the service's ``probe``."""
    from ._image._runtime import _image_services

    return frozenset(
        unit for unit in (s.probe_unit for s in _image_services(image)) if unit is not None
    )


def _written_components(image: Image) -> list[Component]:
    found: list[Component] = [*image.components]
    found += [c for c in (image.network, image.observability) if c is not None]
    return found


def senders(image: Image) -> dict[str, str]:
    """Each component of *image* by the units that send what its ``outbound()`` names,
    which a firewall rule it asked for names: the units of the services it composes, or
    the daemon one of tundravm's own runs (``tee-base``: ``chrony.service``)."""
    from ._image import Component, Service
    from ._image._component import composed

    found = dict(_DAEMONS)

    def units(component: Component) -> list[str]:
        held: list[str] = []
        for item in composed(component):
            if isinstance(item, Service):
                held.append(item.unit_name)
            elif isinstance(item, Component):
                held += units(item)
        return held

    def walk(component: Component) -> None:
        held = units(component)
        if held:
            found[component.name] = ", ".join(dict.fromkeys(held))
        for item in composed(component):
            if isinstance(item, Component):
                walk(item)

    for component in _written_components(image):
        walk(component)
    return found


_DAEMONS = {
    "tee-base": "chrony.service",
    "network": "systemd-resolved.service",
    "tdxs": "tdxs.service",
}
"""What sends for the components wrapping a packaged daemon: NTS time, DNS over TLS,
Intel's collateral."""


def _written_services(image: Image) -> list[tuple[Service, Component | None]]:
    from ._image import Component, Service
    from ._image._component import composed

    found: list[tuple[Service, Component | None]] = [(s, None) for s in image.services]

    def walk(component: Component) -> None:
        for item in composed(component):
            if isinstance(item, Service):
                found.append((item, component))
            elif isinstance(item, Component):
                walk(item)

    for component in _written_components(image):
        walk(component)
    return found


def _component(component: Component) -> dict[str, object]:
    from ._image import Component
    from ._image._component import composed

    given = {id(v) for v in _field_values(component)}
    nested = [c for c in composed(component) if isinstance(c, Component) and id(c) not in given]
    return {
        "class": type(component).__name__,
        "name": component.name,
        "call": call(component),
        "components": [_component(c) for c in nested],
    }


def _field_values(value: object) -> list[object]:
    found: list[object] = []
    for item in dataclasses.fields(cast(Any, value)):
        held = getattr(value, item.name)
        found.extend(held if isinstance(held, tuple) else (held,))
    return found


def _service(service: Service, owner: Component | None) -> dict[str, object]:
    from ._image._storage import VolumeDirectory
    from .declarative.systemd import Hardening

    state = service.state
    sandbox = service.hardening
    assert isinstance(sandbox, Hardening)
    return {
        "name": service.name,
        "description": service.description,
        "component": None if owner is None else type(owner).__name__,
        "program": list(service.command()),
        "package": service.package,
        "sources": [call(binary) for binary in service.binaries()],
        "ports": [call(port) for port in service.expose],
        "hardening": _hardening(sandbox),
        "state": service.state_directory,
        "volume": call(state.volume) if isinstance(state, VolumeDirectory) else None,
        "credentials": [call(binding) for binding in service.bindings],
        "probe": service.probe_unit is not None,
        "readiness": None if service.readiness is None else call(service.readiness),
        "checked": _checked(service, owner),
        "timer": None if service.timer is None else call(service.timer),
        "quotes": service.quotes,
    }


def _hardening(sandbox: Hardening) -> str:
    """A sandbox by its preset (``static``, ``relaxed``, ``none``), else by the preset it
    is nearest and what it changes: ``static, but deny_syscalls=["@privileged"]``."""
    from .declarative.systemd import PRESETS, Hardening, ceiling

    exact = ceiling(sandbox)
    if exact is not None:
        return exact[0]
    if sandbox == Hardening.none():
        return "none"
    knobs = [f.name for f in dataclasses.fields(Hardening) if f.init and f.compare]
    bases = {name: Hardening.preset(cast(Any, name)) for name in PRESETS}
    bases["relaxed"] = Hardening.relaxed()
    changes = {
        name: [k for k in knobs if getattr(base, k) != getattr(sandbox, k)]
        for name, base in bases.items()
    }
    name = min(changes, key=lambda n: len(changes[n]))
    shown = ", ".join(f"{k}={call(getattr(sandbox, k))}" for k in changes[name])
    return f"{name}, but {shown}"


def _checked(service: Service, owner: Component | None) -> str | None:
    """The row of a development boot's probe that checks *service* when no readiness of
    its own does: a container's (``container:web``), a host endpoint relayed into it
    (``container:web:endpoint:8545``)."""
    from .components._container import Container

    if not isinstance(owner, Container):
        return None
    if service.unit_name == owner.unit:
        return f"container:{owner.name}"
    for endpoint in owner.requires:
        if owner.relay(endpoint).removesuffix(".socket") == service.stem:
            return f"container:{owner.name}:endpoint:{endpoint.port}"
    return None


def _profiles(image: Image) -> list[dict[str, object]]:
    from ._image._image import BASE_PROFILE, DEV_PROFILE

    rows: list[dict[str, object]] = [
        {"name": BASE_PROFILE, "extends": None, "production": True, "adds": [], "changes": []}
    ]
    for name, profile in image.profiles.items():
        rows.append(
            {
                "name": name,
                "extends": profile.extends or BASE_PROFILE,
                "production": profile.production,
                "adds": [_content(item) for item in profile.add],
                "changes": [_change(change) for change in profile.changes],
            }
        )
    if image.dev is not None:
        rows.append(
            {
                "name": DEV_PROFILE,
                "extends": BASE_PROFILE,
                "production": False,
                "adds": [call(image.dev)],
                "changes": [],
            }
        )
    return rows


def _logins(image: Image) -> list[dict[str, object]]:
    """Each login of an ``SshAccess`` the image or a profile holds: how its keys arrive,
    its shell or program, and per command its help, params, ``runs`` template, whether
    root runs it, the firewall modes it is offered in (none: every mode) and the profiles
    it exists in."""
    from .components.access import SshAccess

    rows = _profiles(image)
    extends = {str(row["name"]): row["extends"] for row in rows}
    development = [str(row["name"]) for row in rows if not row["production"]]

    def under(root: str | None) -> list[str]:
        found = []
        for name in extends:
            chain: str | None = name
            while chain is not None and chain != root:
                chain = cast("str | None", extends.get(chain))
            if root is None or chain == root:
                found.append(name)
        return found

    held: list[tuple[SshAccess, str | None]] = [
        (item, None) for item in image.components if isinstance(item, SshAccess)
    ]
    for name, profile in image.profiles.items():
        held += [(item, name) for item in profile.add if isinstance(item, SshAccess)]
    found: list[dict[str, object]] = []
    for access, root in held:
        builds = under(root)
        for login in access.logins:
            commands = sorted((login.commands or {}).items())
            baked = len(login.keys) if isinstance(login.keys, tuple) else 0
            found.append(
                {
                    "name": login.name,
                    "keys": "the owner's key"
                    if login.owner
                    else f"keys delivered as {call(login.keys)}"
                    if login.delivered
                    else f"{baked} baked key{'s' * (baked != 1)}",
                    "shell": login.shell,
                    "help": login.help,
                    "program": login.program_path,
                    "commands": [
                        {
                            **command.payload(
                                name, [p for p in builds if not command.dev or p in development]
                            ),
                            "usage": command.usage(name),
                            "shown": command.shown(),
                        }
                        for name, command in commands
                    ],
                    "profiles": builds,
                }
            )
    return found


def _content(item: object) -> str:
    from ._image import Component, Package

    if isinstance(item, str):
        return f"Package({json.dumps(item)})"
    if isinstance(item, (Component, Package)):
        return call(item)
    return _named(item)


def _named(value: object) -> str:
    """``Service("app")``: a value by its kind and key, as a change names it."""
    key = next((getattr(value, k) for k in ("name", "path", "dst") if hasattr(value, k)), None)
    return f"{type(value).__name__}({json.dumps(key)})" if key is not None else call(value)


def _change(change: object) -> str:
    from ._image._change import Edit, Remove
    from .declarative import model as dm

    if isinstance(change, (Edit, Remove)):
        ref = change.ref
        target = (
            f"Service({json.dumps(ref.target.identity[0])})"
            if ref.value is None
            else _named(ref.value)
        )
    elif isinstance(change, (dm.Edit, dm.Remove)):
        identity = change.target.identity
        target = f"{change.target.kind}({json.dumps(identity[0] if identity else '')})"
    else:
        return call(change)
    if isinstance(change, (Edit, dm.Edit)):
        edits = ", ".join(f"{key}={call(value)}" for key, value in change.fields.items())
        return f"edits {target}: {edits}"
    return f"removes {target}"


def call(value: object) -> str:
    """*value* as the call that makes it, keywords at their defaults left out:
    ``App(version="0.1.0", port=8080)``. A ``password`` keyword prints as ``"***"``.

    Within :func:`declared`, what *value* holds is spelled as the image file wrote it
    when it can be: a component, service or volume the file names is its name, a value
    one of them exports is ``nethermind.engine_api`` or ``app.endpoint(8080)``, a
    directory ``data.directory("app")``, and a list of one a keyword also takes alone is
    that one."""
    return _call(value, nested=False)


def _call(value: object, *, nested: bool = True) -> str:
    from ._image._storage import VolumeDirectory

    if isinstance(value, str):
        return json.dumps(value)
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, Path):
        return json.dumps(str(value))
    references = _REFERENCES.get() if nested else None
    named = None if references is None else references.spelling(value)
    if named is not None:
        return named
    if isinstance(value, VolumeDirectory):
        words: list[str] = [json.dumps(value.name)]
        default = 0o750 if value.group is None else 0o2750
        words += [] if value.mode == default else [f"mode={value.mode:#o}"]
        words += [] if value.group is None else [f"group={json.dumps(value.group)}"]
        return f"{_call(value.volume)}.directory({', '.join(words)})"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_call(k)}: {_call(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (tuple, list, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return "[" + ", ".join(_call(item) for item in items) + "]"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        if not cast(Any, value).__dataclass_params__.repr:
            text = repr(value)
            return text if '"' in text else text.replace("'", '"')
        positional = _positional(type(value))
        words = []
        for item in dataclasses.fields(value):
            if not item.init or not item.compare or item.name.startswith("_"):
                continue
            held = getattr(value, item.name)
            if _at_default(item, held) or _derived(value, item, held):
                continue
            if isinstance(held, tuple) and len(held) == 1 and _alone(value, item, held[0]):
                held = held[0]
            text = MASKED if item.name in _MASKED_FIELDS else _call(held)
            words.append(text if item.name in positional else f"{item.name}={text}")
        return f"{type(value).__name__}({', '.join(words)})"
    return repr(value)


@functools.cache
def _positional(kind: type) -> frozenset[str]:
    """The parameters *kind* requires and takes by position."""
    try:
        parameters = inspect.signature(kind).parameters.values()
    except (TypeError, ValueError):
        return frozenset()
    return frozenset(
        p.name
        for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    )


def _alone(owner: object, item: dataclasses.Field[Any], value: object) -> bool:
    """Whether *owner*'s keyword takes *value* alone for the list of it it holds
    (``login=Login(...)``)."""
    try:
        return bool(dataclasses.replace(cast(Any, owner), **{item.name: value}) == owner)
    except (TdxError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class _References:
    """The components, services and volumes an image file binds to its top-level *names*,
    which :func:`call` spells by name, with what each exports."""

    names: Mapping[str, object]

    @functools.cached_property
    def bound(self) -> dict[int, str]:
        from ._image import Component, Service
        from ._image._storage import Volume

        found: dict[int, str] = {}
        for name, value in self.names.items():
            if isinstance(value, (Component, Service, Volume)):
                found.setdefault(id(value), name)
        return found

    @functools.cached_property
    def exports(self) -> list[tuple[str, object]]:
        """``(name.attribute, value)`` of each property a bound component or service
        exports that is a value of its own: an endpoint, a login entry, a readiness, and
        one level down a group's (``firewall.commands.status``)."""
        found: list[tuple[str, object]] = []
        for owner, name in self._owners():
            for spelled, value in _properties(owner, name):
                found.append((spelled, value))
                found += _properties(value, spelled)
        return found

    def _owners(self) -> list[tuple[object, str]]:
        from ._image import Component, Service

        return [
            (value, name)
            for name, value in self.names.items()
            if isinstance(value, (Component, Service)) and self.bound.get(id(value)) == name
        ]

    def spelling(self, value: object) -> str | None:
        """*value* by the name the file binds it to or the export it is, else ``None``."""
        from ._image._component import Endpoint

        if id(value) in self.bound:
            return self.bound[id(value)]
        if not _exported(value):
            return None
        for spelled, exported in self.exports:
            if _same(exported, value):
                return spelled
        if isinstance(value, Endpoint):
            return self._endpoint(value)
        return None

    def _endpoint(self, value: Any) -> str | None:
        """``app.endpoint(8080)``: the endpoint a bound owner's ``endpoint()`` makes."""
        for owner, name in self._owners():
            make = getattr(owner, "endpoint", None)
            if not callable(make):
                continue
            try:
                made = make(value.port, path=value.path, scheme=value.scheme)
            except (TdxError, TypeError, ValueError):
                continue
            if _same(made, value):
                words = [str(value.port)]
                words += [] if value.path == "/" else [f"path={json.dumps(value.path)}"]
                words += [] if value.scheme == "http" else [f"scheme={json.dumps(value.scheme)}"]
                return f"{name}.endpoint({', '.join(words)})"
        return None


_REFERENCES: ContextVar[_References | None] = ContextVar("references", default=None)


def _properties(owner: object, name: str) -> list[tuple[str, object]]:
    """``(name.attribute, value)`` of each public property of *owner* whose value is one
    of its own (:func:`_exported`), neither a component nor a service."""
    from ._image import Component, Service

    found: list[tuple[str, object]] = []
    seen: set[str] = set()
    for kind in type(owner).__mro__:
        for attribute, member in vars(kind).items():
            if attribute in seen or attribute.startswith("_"):
                continue
            seen.add(attribute)
            if not isinstance(member, property):
                continue
            try:
                value = getattr(owner, attribute)
            except (TdxError, AttributeError, TypeError, ValueError):
                continue
            if _exported(value) and not isinstance(value, (Component, Service)):
                found.append((f"{name}.{attribute}", value))
    return found


def _exported(value: object) -> bool:
    """Whether *value* is a value of its own an owner may export: a dataclass instance
    that is not empty (an export equal to ``Kind()`` names nothing)."""
    if not dataclasses.is_dataclass(value) or isinstance(value, type):
        return False
    try:
        return bool(type(value)() != value)
    except (TdxError, TypeError, ValueError):
        return True


def _same(left: object, right: object) -> bool:
    """Whether *left* and *right* are one value, the fields equality skips compared too
    (an endpoint's readiness and provider)."""
    return type(left) is type(right) and all(
        getattr(left, item.name) == getattr(right, item.name)
        for item in dataclasses.fields(cast(Any, left))
    )


def _derived(owner: object, item: dataclasses.Field[Any], value: object) -> bool:
    """Whether *value* is what *owner* derives from the keyword's default (``Volume("data")``'s
    mount): the call then says nothing by passing it."""
    if item.default is dataclasses.MISSING:
        return False
    try:
        return bool(dataclasses.replace(cast(Any, owner), **{item.name: item.default}) == owner)
    except (TdxError, TypeError, ValueError):
        return False


def _at_default(item: dataclasses.Field[Any], value: object) -> bool:
    if item.default is not dataclasses.MISSING:
        return bool(value == item.default)
    if item.default_factory is not dataclasses.MISSING:
        return bool(value == item.default_factory())
    return False


# ── what one profile builds ──────────────────────────────────────────────


def describe(
    image: Lowered,
    *,
    profile: str | None = None,
    resolved: Resolved | None = None,
    probes: Collection[str] = (),
    senders: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """What *image* builds for *profile* (default: the active one), merged over the profile
    it extends.

    Lists are sorted by their natural identity (path, name, ...). File contents are
    summarized as short sha256 digests; sources show the pins of the lockfile *image*
    reads. *probes* are the probe units of the image's services (:func:`probe_units`),
    which are their service's ``probe``, not units or users of their own. *resolved* (the
    profile resolved) names each build step, which is then said by what it does, and counts
    its declarations. *senders* (:func:`senders`) name the units behind each component a
    firewall rule names.
    """
    selected = image.profile(profile)
    pins = image.build_pins()
    state = image.pinned_state(pins)
    profile_state = state.effective_profile(selected)
    kernel = image.kernel
    kernel_source = image.kernel_source(selected)
    kernel_pin = None if kernel_source is None else kernel_source.pin_from(pins)
    probe_users = {unit.removesuffix(".service") for unit in probes}
    return {
        "profile": selected,
        "declarations": None if resolved is None else len(resolved.items),
        "extends": profile_state.extends,
        "targets": list(image.targets.get(selected, profile_state.output_targets)),
        "kernel": None
        if kernel is None
        else {
            "cmdline": kernel.cmdline,
            "config_file": None if kernel.config_file is None else str(kernel.config_file),
            "pinned": kernel_pin[:7] if kernel_pin else None,
            "source": _describe_kernel_source(kernel),
            "tdx": kernel.tdx,
            "version": kernel.version,
        },
        "build_options": _describe_build_options(image.mkosi),
        "packages": sorted(profile_state.packages),
        "build_packages": sorted(profile_state.build_packages),
        "sources": _describe_sources(image, selected, profile_state, pins),
        "repositories": [
            {
                "components": list(repo.components),
                "keyring": repo.keyring,
                "name": repo.name,
                "priority": repo.priority,
                "suite": repo.suite,
                "url": repo.url,
                **({} if repo.in_image else {"in_image": False}),
            }
            for repo in sorted(
                profile_state.repositories,
                key=lambda item: (item.priority, item.name, item.url),
            )
        ],
        "files": _describe_files(profile_state.files),
        "skeleton_files": _describe_files(profile_state.skeleton_files),
        "templates": [
            {"mode": tmpl.mode, "path": tmpl.path, "variables": sorted(tmpl.variables)}
            for tmpl in sorted(profile_state.templates, key=lambda item: item.path)
        ],
        "partitions": [
            {"fs": p.fs, "mount_at": p.mount_at, "name": p.name, "size": p.size}
            for p in sorted(profile_state.partitions, key=lambda item: item.name)
        ],
        "users": [
            {
                "gid": u.gid,
                "groups": list(u.groups),
                "home": u.home,
                "name": u.name,
                "shell": u.shell,
                "system": u.system,
                "uid": u.uid,
            }
            for u in sorted(profile_state.users, key=lambda item: item.name)
            if u.name not in probe_users
        ],
        "groups": [
            {"gid": g.gid, "name": g.name, "system": g.system}
            for g in sorted(profile_state.groups, key=lambda item: item.name)
        ],
        "units": _describe_units(profile_state, frozenset(probes)),
        "firewall": _describe_firewall(profile_state, senders or {}),
        "metrics": _describe_metrics(profile_state),
        "control_plane": _describe_control_plane(profile_state),
        "build_steps": _describe_build_steps(profile_state, resolved),
        "boot_steps": [
            step_words(entry.name) for entry in runtime_init_order(profile_state.init_scripts)
        ],
        "instance_key": sorted(
            unit_name(svc.name)
            for svc in profile_state.services
            if svc.credentials.get(instance_key.CREDENTIAL) == instance_key.KEY
        ),
        "debloat": image.explain_debloat(profile=selected),
    }


def _describe_files(entries: Sequence[Any]) -> list[dict[str, object]]:
    described: list[dict[str, object]] = []
    for entry in sorted(entries, key=lambda item: item.path):
        row: dict[str, object] = {
            "bytes": len(entry.data),
            "mode": entry.mode,
            "path": entry.path,
            "sha256": hashlib.sha256(entry.data).hexdigest()[:SHORT_DIGEST_LEN],
        }
        if entry.kind == "symlink":
            row["symlink"] = entry.data.decode()
        elif entry.kind == "directory":
            row["directory"] = True
        described.append(row)
    return described


def _describe_sources(
    image: Lowered, profile: str, profile_state: ProfileState, pins: Mapping[str, LockedFetch]
) -> list[dict[str, object]]:
    described: list[dict[str, object]] = []
    for name, spec in sorted(profile_state.source_builds.items()):
        pin = image.keyed(profile, spec).pin_from(pins)
        described.append(
            {
                "build": spec.build.kind,
                "install": [install.dest for install in spec.install],
                "kind": spec.source.kind,
                "name": name,
                "pinned": pin[:7] if pin else None,
                "ref": spec.source.requested if spec.source.kind == "git" else None,
                "url": spec.source.url,
            }
        )
    return described


def _describe_units(profile_state: ProfileState, probes: frozenset[str]) -> dict[str, list[str]]:
    """Every unit by what boot does with it: ``enabled``, ``not_enabled`` (started by a
    socket, a timer or another unit), ``disabled`` and ``masked``. Probe units are their
    service's and left out."""
    names = [
        (unit_name(s.name), s.enabled)
        for s in profile_state.services
        if unit_name(s.name) not in probes
    ]
    states = profile_state.unit_states
    return {
        "enabled": list(dict.fromkeys(name for name, on in names if on)),
        "not_enabled": list(dict.fromkeys(name for name, on in names if not on)),
        "disabled": list(dict.fromkeys(s.unit for s in states if s.action == "disable")),
        "masked": list(dict.fromkeys(s.unit for s in states if s.action == "mask")),
    }


def _describe_firewall(
    profile_state: ProfileState, senders: Mapping[str, str]
) -> dict[str, object] | None:
    """What ``/etc/nftables.conf`` accepts: inbound, each port DNATed to a container among
    it; outbound when its output chain drops what no rule accepts (``None`` there:
    unfiltered); and what each container may send (``containers``). Each rule names who
    asked: a unit (a component by the units *senders* name for it), a mode, or the
    ``Firewall``'s own ``allow=``; an ``Egress(deny=)`` drop is marked ``deny``. A host
    name stands for the sets of its addresses."""
    conf = next((f for f in profile_state.files if f.path == _FIREWALL), None)
    if conf is None:
        return None
    units = {unit_name(s.name) for s in profile_state.services}
    inbound: list[dict[str, str]] = []
    outbound: list[dict[str, str]] = []
    sent: list[dict[str, str]] = []
    forwarded: dict[str, str] = {}
    filtered = False
    chain = ""
    for line in conf.data.decode("utf-8", errors="replace").splitlines():
        words = line.split()
        if words[:1] == ["chain"] and len(words) > 1:
            chain = words[1]
            continue
        if chain == "output" and "policy" in words and "drop;" in words:
            filtered = True
        if "comment" not in words:
            continue
        who, pinned = _who(line.split("comment", 1)[1].strip().strip('"'))
        if chain == "prerouting" and "dnat" in words:
            forwarded[_rule(words[: words.index("dnat")])] = who
            continue
        verdict = next((word for word in ("accept", "drop") if word in words), None)
        if verdict is None or who.startswith("DHCP") or who == "cloud metadata":
            continue
        rule = {"rule": _rule(words[: words.index(verdict)], pinned), "for": who}
        if verdict == "drop":
            rule["deny"] = "yes"
        hook = chain.rpartition("_")[2]
        if chain == "forward" and words[0] == "iifname":
            name, _, mode = who.partition(": ")
            rule["for"] = f"{mode}: {_unit(name, units)}" if mode else _unit(name, units)
            sent.append(rule)
        elif hook == "output":
            rule["for"] = ", ".join(
                dict.fromkeys(senders.get(name) or _unit(name, units) for name in who.split(", "))
            )
            outbound.append(rule)
        elif hook == "input":
            mode, _, names = who.rpartition(": ")
            if names == "allow":
                rule["for"] = f"{mode} mode" if mode else "Firewall(allow=)"
            inbound.append(rule)
    for row in inbound:
        mode, _, names = row["for"].rpartition(": ")
        container = forwarded.get(row["rule"])
        if mode and container in names.split(", "):
            del forwarded[row["rule"]]
            row["rule"] += f" {_FORWARDED} {container}"
            row["for"] = f"{mode}: {_unit(container, units)}"
    inbound += [
        {"rule": f"{port} {_FORWARDED} {name}", "for": _unit(name, units)}
        for port, name in forwarded.items()
    ]
    return {
        "inbound": inbound,
        "outbound": _merged(outbound) if filtered else None,
        "containers": _merged(sent),
    }


_FORWARDED = "\N{RIGHTWARDS ARROW}"
"""Between a port and the container the host DNATs it to."""


def _who(comment: str) -> tuple[str, str | None]:
    """A rule's comment as who asked and the name its address is pinned to:
    ``image (api.example)`` is ``("image", "api.example")``."""
    found = re.fullmatch(r"(.*?) \(([^()]+)\)", comment)
    return (comment, None) if found is None else (found.group(1), found.group(2))


def _unit(name: str, units: Collection[str]) -> str:
    """A container (or a service) by its unit, one of *units*."""
    return unit_name(name) if unit_name(name) in units else name


def _merged(rules: list[dict[str, str]]) -> list[dict[str, str]]:
    """*rules* once each, the addresses of one pinned name on one rule:
    ``api.example:443/tcp (203.0.113.7, 203.0.113.8)``."""
    found: dict[tuple[str, str, str], list[str]] = {}
    for row in rules:
        rule, _, address = row["rule"].partition(" (")
        held = found.setdefault((rule, row["for"], row.get("deny", "")), [])
        if address and address.rstrip(")") not in held:
            held.append(address.rstrip(")"))
    return [
        {"rule": f"{rule} ({', '.join(held)})" if held else rule, "for": who}
        | ({"deny": deny} if deny else {})
        for (rule, who, deny), held in found.items()
    ]


def _describe_metrics(profile_state: ProfileState) -> dict[str, object] | None:
    """Where the metrics export writes and the series its ``keep`` relabel lets through,
    the image's identity among them, from its configuration; ``None`` without one."""
    from .components._observability import METRICS_CONFIG

    found = next((f for f in profile_state.files if f.path == METRICS_CONFIG), None)
    if found is None:
        return None
    remote = json.loads(found.data)["remote_write"][0]
    kept = next(r for r in remote["write_relabel_configs"] if r["action"] == "keep")
    return {"url": remote["url"], "series": kept["regex"].split("|")}


def _describe_control_plane(profile_state: ProfileState) -> dict[str, object] | None:
    """The attested endpoint and who hands it each value, from the profile's runtime
    manifest: a volume's key and a secret come from the operator or a broker (its URL and
    audience); a ``TpmSeal`` volume needs neither. ``None`` when no endpoint runs."""
    found = next((f for f in profile_state.files if f.path == _RUNTIME), None)
    runtime = None if found is None else json.loads(found.data)
    if not isinstance(runtime, dict) or not runtime.get("control_plane"):
        return None

    def given_by(source: object) -> dict[str, object]:
        if isinstance(source, dict) and source.get("kind") == "broker-key":
            return {"by": "broker", "url": source["url"], "audience": source["audience"]}
        return {"by": "operator"}

    return {
        "port": runtime["management_port"],
        "values": [
            {"value": f"volume {volume['name']}", **given_by(volume["unlock"])}
            for volume in runtime["volumes"]
            if volume["unlock"].get("kind") != "tpm-seal"
        ]
        + [
            {"value": f"secret {secret['name']}", **given_by(secret["source"])}
            for secret in runtime["secrets"]
        ],
    }


def _rule(words: list[str], pinned: str | None = None) -> str:
    """``tcp dport 8080`` as ``8080/tcp``, ``ip daddr 1.1.1.1 tcp dport 443`` as
    ``1.1.1.1:443/tcp``, a host name's set (``ip daddr @host4_api.example``) as the name,
    an address *pinned* to a name as ``api.example:443/tcp (203.0.113.7)``; anything else
    as written."""
    found: dict[str, str] = {}
    for key in ("daddr", "dport"):
        if key in words and words.index(key) + 1 < len(words):
            found[key] = words[words.index(key) + 1]
    protocol = next((w for w in words if w in ("tcp", "udp")), None)
    if not found:
        return " ".join(words)
    port = f"{found['dport']}/{protocol}" if "dport" in found else ""
    if "daddr" not in found:
        return port
    address = found["daddr"]
    if address.startswith(("@host4_", "@host6_")):
        address = address[len("@host4_") :]
    elif pinned is not None:
        return f"{pinned}:{port} ({address})" if port else f"{pinned} ({address})"
    return f"{address}:{port}" if port else address


_STEPS = {
    "devtools-root-login": "sets the dev root password",
    "devtools-serial-console": "enables the serial console",
    "efi-stub": "installs the pinned EFI stub",
    "tee-base-resolv-conf": "points /etc/resolv.conf at systemd-resolved's stub",
    "metrics-export-rules": "checks the recording rules with promtool",
}

_LOGIN_STEPS = {
    "keys": "makes the directory {login}'s delivered keys go in",
    "sudoers": "checks {login}'s sudoers file with visudo",
}
"""The build steps of a ``Login`` (``ssh-login-NAME-keys``), by what each does."""

_WHEN = {
    "sync": "before the build",
    "prepare": "before the packages are installed",
    "build": "in the build",
    "postinst": "once the packages are installed",
    "finalize": "once the tree is complete",
    "postoutput": "once the image is written",
    "clean": "when the build is cleaned",
}
"""When a build step of each phase runs."""


def _describe_build_steps(profile_state: ProfileState, resolved: Resolved | None) -> list[str]:
    """Each build step by what it does, in the order the build runs them."""
    from ._platforms import gcp
    from .compiler import PHASE_ORDER
    from .declarative import model as dm
    from .declarative.state import STRIP_IMAGE_VERSION

    names = {
        item.script: item.name
        for item in (resolved.items if resolved is not None else ())
        if isinstance(item, dm.Hook)
    }
    sources = {spec.marker(): name for name, spec in profile_state.source_builds.items()}
    steps: list[str] = []
    built: list[str] = []
    for phase in PHASE_ORDER:
        for hook in profile_state.hooks:
            if hook.phase != phase:
                continue
            script = hook.command.argv[0]
            marker = script.split("\n", 1)[0]
            if marker in sources:
                built.append(sources[marker])
                continue
            if built:
                steps.append(_built(built))
                built = []
            steps.append(_step(names.get(script), script, phase, STRIP_IMAGE_VERSION, gcp))
        if built:
            steps.append(_built(built))
            built = []
    return steps


def _built(names: list[str]) -> str:
    *rest, last = sorted(names)
    listed = f"{', '.join(rest)} and {last}" if rest else last
    return f"builds {listed} from source"


def _step(name: str | None, script: str, phase: str, strip: str, gcp: Any) -> str:
    if script == strip:
        return "removes IMAGE_VERSION from os-release, so a rebuild matches"
    if script == gcp.NVME_HOST_IDS:
        return "drops the NVMe host ids, which GCP sets per instance"
    when = _WHEN.get(phase, f"in the {phase} phase")
    if name is None:
        return f"runs a script {when}"
    if name in _STEPS:
        step = _STEPS[name]
        return f"{step}, which logs in over SSH too" if NETWORK_LOGIN_SCRIPT in script else step
    login = re.fullmatch(r"ssh-login-(.+)-(keys|sudoers)", name)
    if login is not None:
        return _LOGIN_STEPS[login.group(2)].format(login=login.group(1))
    if name.startswith("apt-"):
        return f"checks the InRelease of {name.removeprefix('apt-')}"
    if name.startswith("mkosi-"):
        return f"runs Mkosi(scripts=) {phase} script {name.rsplit('-', 1)[-1]}"
    return f"runs {name} {when}"


def _describe_build_options(options: MkosiOptions) -> dict[str, object]:
    """``MkosiOptions`` fields that differ from the defaults; the init script as a digest."""
    described: dict[str, object] = {}
    for name, value in options.non_defaults().items():
        if name == "init_script" and isinstance(value, str):
            digest = hashlib.sha256(value.encode()).hexdigest()[:SHORT_DIGEST_LEN]
            described[name] = f"sha256:{digest}"
        elif isinstance(value, Mapping):
            described[name] = dict(sorted(value.items()))
        elif isinstance(value, tuple):
            described[name] = list(value)
        else:
            described[name] = value
    return described


def _describe_kernel_source(kernel: Kernel) -> dict[str, object]:
    if kernel.source_archive is not None:
        return {"sha256": kernel.source_sha256, "url": kernel.source_archive}
    return {
        "ref": kernel.source_ref or f"v{kernel.version}",
        "repo": kernel.source_repo,
        "subdir": kernel.source_subdir,
        "submodules": kernel.source_submodules,
    }


# ── rows: what show prints, and what --diff compares ─────────────────────


type Item = str | tuple[str, ...]
"""One line of a row: text, or columns ``show`` aligns."""


@dataclass(frozen=True, slots=True)
class Row:
    """A line of a section, keyed by *head*: alone, with its *items* inline
    (``Packages (3): a b c``), or with them as the indented lines under it (*counted*:
    ``Files (2):``)."""

    head: str
    items: tuple[Item, ...] = ()
    inline: bool = False
    counted: bool = False


@dataclass(frozen=True, slots=True)
class Section:
    """A titled group of rows; *counted* puts the number of rows in the title."""

    title: str
    rows: tuple[Row, ...]
    counted: bool = False


def declared_sections(report: Mapping[str, Any]) -> list[Section]:
    """The image as written: components, services, profiles."""
    sections = [
        Section("Components", tuple(_component_row(c) for c in report["components"]), True),
        Section("Services", tuple(_service_row(s) for s in report["services"]), True),
        Section("Ingress", tuple(Row(way) for way in report.get("ingress", ())), True),
        Section("Content", tuple(Row(item) for item in report.get("content", ())), True),
        Section("Profiles", tuple(_profile_rows(report["profiles"])), True),
        Section("Logins", tuple(_login_row(row) for row in report.get("logins", ())), True),
    ]
    return [section for section in sections if section.rows]


def _login_row(row: Mapping[str, Any]) -> Row:
    """A login's head (``searcher  the owner's key, /usr/libexec/tundravm/cli/searcher``),
    the listing its program prints, each command limited to some profiles saying which,
    then each command's ``runs:`` line."""
    from .declarative._login import annotated, table

    if row["shell"]:
        return Row(f"{row['name']}  {row['keys']}, shell {row['shell']}")
    head = f"{row['name']}: {row['help']}" if row["help"] else row["name"]
    listed = []
    for command in row["commands"]:
        notes = ["as root"] if command["sudo"] else []
        if command["modes"]:
            notes.append(" or ".join(command["modes"]) + " only")
        if command["profiles"] != row["profiles"]:
            notes.append("profiles " + (", ".join(command["profiles"]) or "none"))
        listed.append((command["usage"], annotated(command["help"], notes)))
    items: list[Item] = [head, f"ssh {row['name']}@HOST COMMAND", *table(listed)]
    items += [(command["name"], f"runs: {command['shown']}") for command in row["commands"]]
    return Row(f"{row['name']}  {row['keys']}, {row['program']}", tuple(items))


def _component_row(row: Mapping[str, Any]) -> Row:
    return Row(row["call"], tuple(f"composes {c['call']}" for c in row["components"]))


def _service_row(row: Mapping[str, Any]) -> Row:
    items: list[Item] = []
    if row["package"]:
        items.append(("program", f"the unit Debian package {row['package']} ships"))
    else:
        items.append(("program", _Cut(" ".join(row["program"]))))
    items += [("source", source) for source in row["sources"]]
    if row["ports"]:
        items.append(("ports", ", ".join(row["ports"])))
    items.append(("hardening", row["hardening"]))
    if row["volume"]:
        items.append(("volume", f"{row['state']} on {row['volume']}"))
    elif row["state"]:
        items.append(("state", f"{row['state']}, volatile"))
    if row["credentials"]:
        items.append(("credentials", ", ".join(row["credentials"])))
    if row["timer"]:
        items.append(("timer", row["timer"]))
    probe = "yes, " + str(row["readiness"]) if row["probe"] else "no"
    if row["readiness"] and not row["probe"]:
        probe = f"no, {row['readiness']} says when it is ready"
    elif row.get("checked") and not row["probe"]:
        probe = f"no, a development boot checks {row['checked']}"
    items.append(("probe", probe))
    if row.get("quotes"):
        items.append(("quotes", "yes"))
    head = row["name"] + (f"  {row['component']}" if row["component"] else "")
    head += f"  {row['description']}" if row.get("description") else ""
    return Row(head, tuple(items))


def _profile_rows(profiles: Sequence[Mapping[str, Any]]) -> list[Row]:
    width = max((len(row["name"]) for row in profiles), default=0)
    rows: list[Row] = []
    for row in profiles:
        what = "the image" if row["extends"] is None else f"extends {row['extends']}"
        what += "" if row["production"] else ", non-production"
        items = [f"adds {item}" for item in row["adds"]] + list(row["changes"])
        rows.append(Row(f"{row['name']:<{width}}  {what}", tuple(items)))
    return rows


def resolved_sections(
    resolved: Mapping[str, Mapping[str, Any]],
    extends: Mapping[str, str | None],
    *,
    beyond: bool = True,
) -> list[Section]:
    """What each profile builds; with *beyond*, a profile that extends one shown too lists
    only what differs from it."""
    sections: list[Section] = []
    for name, description in resolved.items():
        rows = tuple(resolved_rows(description))
        parent = extends.get(name)
        if beyond and parent is not None and parent in resolved and parent != name:
            parent_rows = tuple(resolved_rows(resolved[parent]))
            changed = diff_rows(parent_rows, rows) or [Row("nothing")]
            sections.append(Section(f"Built into {name}, beyond {parent}", tuple(changed)))
        else:
            sections.append(Section(f"Built into {name}", rows))
    return sections


def resolved_rows(description: Mapping[str, Any]) -> list[Row]:
    """One profile's derived facts as rows (:func:`describe`); an empty list is left out."""
    rows: list[Row] = []

    def listing(head: str, items: Sequence[Item], *, inline: bool = False) -> None:
        if items:
            rows.append(Row(head, tuple(items), inline, counted=True))

    get = description.get
    listing("Targets", get("targets", ()), inline=True)
    kernel = get("kernel")
    if isinstance(kernel, Mapping):
        rows.append(Row(_render_kernel(kernel)))
    options = dict(get("build_options") or {})
    cmdline = options.pop("kernel_cmdline", None)
    if cmdline:
        shown = " ".join(cmdline) if isinstance(cmdline, list) else str(cmdline)
        rows.append(Row(f"Kernel command line: {shown}"))
    if options:
        rows.append(Row("Build options: " + " ".join(f"{k}={_fmt(v)}" for k, v in options.items())))
    listing("Packages", get("packages", ()), inline=True)
    listing("Build packages", get("build_packages", ()), inline=True)
    listing("Sources", [_source_item(s) for s in get("sources", ())])
    listing("Repositories", [_repository_item(r) for r in get("repositories", ())])
    listing("Files", [_file_item(f) for f in get("files", ())])
    listing("Skeleton files", [_file_item(f) for f in get("skeleton_files", ())])
    listing(
        "Templates",
        [
            (t["path"], t["mode"], "vars=" + (",".join(t["variables"]) or "-"))
            for t in get("templates", ())
        ],
    )
    listing(
        "Partitions",
        [(p["name"], p["size"], p["mount_at"], p["fs"]) for p in get("partitions", ())],
    )
    listing("Users", [u["name"] for u in get("users", ())], inline=True)
    listing("Groups", [g["name"] for g in get("groups", ())], inline=True)
    units = get("units") or {}
    for key, label in UNIT_ROWS:
        listing(label, units.get(key, ()), inline=True)
    firewall = get("firewall")
    if isinstance(firewall, Mapping):
        listing("Firewall", _firewall_items(firewall))
    metrics = get("metrics")
    if isinstance(metrics, Mapping):
        rows.append(Row(f"Metrics export to {metrics['url']}"))
        listing("Exported series", metrics["series"], inline=True)
    control = get("control_plane")
    if isinstance(control, Mapping):
        listing("Control plane", _control_plane_items(control))
    if "instance_key" in description:
        takers = ", ".join(description["instance_key"]) or "no service"
        rows.append(Row(f"Instance key: {instance_key.CURVE}, fresh each boot; taken by {takers}"))
    listing("Build steps", get("build_steps", ()))
    listing("Boot steps", get("boot_steps", ()))
    debloat = get("debloat") or {}
    if debloat.get("enabled"):
        rows.append(
            Row(
                f"Debloat: enabled, {len(debloat.get('paths_remove', []))} paths removed,"
                f" systemd minimize={_yes_no(debloat.get('systemd_minimize'))}"
            )
        )
    elif debloat:
        rows.append(Row("Debloat: disabled"))
    return rows


UNIT_ROWS = (
    ("enabled", "Enabled units"),
    ("not_enabled", "Units started on demand"),
    ("disabled", "Disabled units"),
    ("masked", "Masked units"),
)
"""The units rows, by :func:`describe`'s ``units`` key."""


def _source_item(source: Mapping[str, Any]) -> Item:
    return (
        str(source["name"]),
        f"{source['kind']} {source['url']}",
        f"ref={source['ref']}" if source.get("ref") else "",
        f"pinned={source['pinned'] or '-'}",
    )


def _repository_item(repo: Mapping[str, Any]) -> Item:
    parts = [str(repo["name"]), str(repo["url"])]
    if repo.get("suite"):
        parts.append(str(repo["suite"]))
    if repo.get("components"):
        parts.append(",".join(repo["components"]))
    parts.append(f"prio={repo['priority']}")
    if repo.get("in_image") is False:
        parts.append("build only")
    return tuple(parts)


def _file_item(entry: Mapping[str, Any]) -> Item:
    if "symlink" in entry:
        what = f"-> {entry['symlink']}"
    elif entry.get("directory"):
        what = "directory"
    else:
        what = f"{entry['bytes']}B  sha256:{entry['sha256']}"
    return (str(entry["path"]), str(entry["mode"]), what)


def _firewall_items(firewall: Mapping[str, Any]) -> list[Item]:
    items: list[Item] = [("in", rule["rule"], rule["for"]) for rule in firewall["inbound"]]
    if not firewall["inbound"]:
        items.append(("in", "nothing beyond replies and DHCP"))
    outbound = firewall.get("outbound")
    if outbound is None:
        items.append(("out", "unfiltered"))
    else:
        items += _grouped(outbound)
        if not outbound:
            items.append(("out", "nothing beyond replies and DHCP"))
    return items + _grouped(firewall.get("containers", ()))


def _grouped(rules: Sequence[Mapping[str, str]]) -> list[Item]:
    """``out`` rows (``deny`` for drops), one per who asked, its rules joined up to
    :data:`PROGRAM_WIDTH` characters a row."""
    grouped: dict[tuple[str, str], list[list[str]]] = {}
    for rule in rules:
        rows = grouped.setdefault(("deny" if rule.get("deny") else "out", rule["for"]), [[]])
        if rows[-1] and len(", ".join([*rows[-1], rule["rule"]])) > PROGRAM_WIDTH:
            rows.append([])
        rows[-1].append(rule["rule"])
    return [(way, ", ".join(row), who) for (way, who), rows in grouped.items() for row in rows]


def _control_plane_items(control: Mapping[str, Any]) -> list[Item]:
    items: list[Item] = [("endpoint", f"{control['port']}/tcp, TLS bound to the quote")]
    for row in control["values"]:
        if row["by"] == "broker":
            items.append((row["value"], f"broker {row['url']}, audience {row['audience']}"))
        else:
            items.append((row["value"], "the operator, over the endpoint"))
    return items


def _render_kernel(kernel: Mapping[str, Any]) -> str:
    parts = [f"Kernel: {kernel.get('version') or kernel.get('config_file') or 'custom'}"]
    if kernel.get("tdx"):
        parts.append("tdx=yes")
    if kernel.get("cmdline"):
        parts.append(f"cmdline={kernel['cmdline']}")
    if kernel.get("config_file"):
        parts.append(f"pinned={kernel.get('pinned') or '-'}")
    return "  ".join(parts)


# ── rendering and diffing rows ───────────────────────────────────────────


def render_sections(sections: Sequence[Section]) -> str:
    """The sections as ``show`` prints them, a blank line between two."""
    blocks: list[str] = []
    for section in sections:
        title = (
            f"{section.title} ({len(section.rows)}):" if section.counted else f"{section.title}:"
        )
        lines = [title]
        for row in section.rows:
            lines.extend(_row_lines(row))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _row_lines(row: Row, prefix: str = "") -> list[str]:
    if row.inline:
        return [f"  {prefix}{row.head} ({len(row.items)}): " + " ".join(map(_text, row.items))]
    if not row.items:
        return [f"  {prefix}{row.head}"]
    head = f"{row.head} ({len(row.items)}):" if row.counted else row.head
    return [f"  {prefix}{head}", *(f"    {prefix}{line}" for line in _aligned(row.items))]


def _aligned(items: Sequence[Item]) -> list[str]:
    widths: dict[int, int] = {}
    for item in items:
        if isinstance(item, str):
            continue
        for index, cell in enumerate(item[:-1]):
            widths[index] = max(widths.get(index, 0), len(_shown(cell)))
    lines: list[str] = []
    for item in items:
        if isinstance(item, str):
            lines.append(item)
            continue
        cells = [f"{_shown(cell):<{widths[i]}}" for i, cell in enumerate(item[:-1])]
        lines.append("  ".join([*cells, *map(_shown, item[-1:])]).rstrip())
    return lines


class _Cut(str):
    """A cell ``show`` cuts to :data:`PROGRAM_WIDTH` characters: a program line."""

    __slots__ = ()


def _shown(cell: str, width: int = PROGRAM_WIDTH) -> str:
    return _truncate(cell, width) if isinstance(cell, _Cut) else cell


def _text(item: Item, width: int = PROGRAM_WIDTH) -> str:
    return item if isinstance(item, str) else "  ".join(_shown(cell, width) for cell in item)


_AFTER = 20
"""How much of two program lines ``show --diff`` keeps past where they first differ."""


def _cut_after(gone: Sequence[Item], came: Sequence[Item]) -> int:
    """Where a changed row cuts its program lines: past the first character an old one and
    its new one (the cells before it alike) differ in, so the difference shows."""
    width = PROGRAM_WIDTH
    for old in gone:
        for new in came:
            if isinstance(old, str) or isinstance(new, str) or old[:-1] != new[:-1]:
                continue
            if isinstance(old[-1], _Cut) and isinstance(new[-1], _Cut):
                at = next(
                    (
                        i
                        for i, pair in enumerate(zip(old[-1], new[-1], strict=False))
                        if len(set(pair)) > 1
                    ),
                    min(len(old[-1]), len(new[-1])),
                )
                width = max(width, at + _AFTER)
    return width


def diff_rows(old: Sequence[Row], new: Sequence[Row]) -> list[Row]:
    """The rows of *new* that *old* does not hold as they are, each with only the items that
    differ: ``+ x`` added, ``- x`` removed; a row only one side holds is ``+``/``-`` whole."""
    before = {row.head: row for row in old}
    after = {row.head: row for row in new}
    changed: list[Row] = []
    for row in old:
        if row.head not in after:
            changed.append(_whole(row, "-"))
    for row in new:
        known = before.get(row.head)
        if known is None:
            changed.append(_whole(row, "+"))
            continue
        if known == row:
            continue
        gone = [i for i in known.items if i not in row.items]
        came = [i for i in row.items if i not in known.items]
        if not gone and not came:
            continue
        width = _cut_after(gone, came)
        removed = [f"- {_text(i, width)}" for i in gone]
        added = [f"+ {_text(i, width)}" for i in came]
        if row.inline:
            words = [f"-{_text(i)}" for i in known.items if i not in row.items]
            words += [f"+{_text(i)}" for i in row.items if i not in known.items]
            changed.append(Row(f"{row.head}: {' '.join(words)}"))
        else:
            changed.append(Row(row.head, tuple(removed + added)))
    return changed


def _whole(row: Row, sign: str) -> Row:
    if row.inline:
        return Row(f"{sign} {row.head} ({len(row.items)}): " + " ".join(map(_text, row.items)))
    return Row(f"{sign} {row.head}", tuple(f"{sign} {_text(i)}" for i in row.items))


def diff_sections(old: Sequence[Section], new: Sequence[Section]) -> list[Section]:
    """Section by section (by title), the rows that differ; a section with none is left
    out."""
    before = {section.title: section for section in old}
    titles = [s.title for s in old] + [s.title for s in new if s.title not in before]
    after = {section.title: section for section in new}
    found: list[Section] = []
    for title in titles:
        rows = diff_rows(
            before[title].rows if title in before else (),
            after[title].rows if title in after else (),
        )
        if rows:
            found.append(Section(title, tuple(rows)))
    return found


def section_payload(sections: Sequence[Section]) -> list[dict[str, object]]:
    """*sections* as JSON: each title and its rows as the lines ``show`` prints."""
    return [
        {
            "title": section.title,
            "lines": [line[2:] for row in section.rows for line in _row_lines(row)],
        }
        for section in sections
    ]


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _fmt(value: object) -> str:
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, Mapping):
        return ",".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)
