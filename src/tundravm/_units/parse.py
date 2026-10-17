"""Unit text -> typed declarations (docs/design/systemd-units.md 5).

:func:`convert` maps unit texts to ``Service``, ``Task`` (oneshot + timer pairs),
``Service(socket=Listen(...))`` (service + socket pairs), ``Mount`` and
``Unit(dropin=DropIn(...))``. Every candidate is checked by rendering it through the same
lowering a recipe uses: ``exact`` keeps it only when the bytes match (else the raw
``Unit(name, text)`` and the directives that blocked it), ``semantic`` when every section
holds the same directives and values (unowned directives go into ``extra=``). Each field
is written out only when its declared default would render differently, so the result
follows ``Service``'s defaults as they are at run time.
"""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from tundravm.errors import ValidationError
from tundravm.models import ProfileState

from .render import HARDENING_KEYS, STRICT_HARDENING, command_line
from .text import UnitText

if TYPE_CHECKING:
    from tundravm.declarative.model import Declaration

Mode = Literal["exact", "semantic"]
Leftover = tuple[str, str, str]
"""``(section, key, value)`` of a directive no typed field owns."""

_RELATIONS = {
    "After": "after",
    "Requires": "requires",
    "Wants": "wants",
    "Before": "before",
    "PartOf": "part_of",
    "BindsTo": "binds_to",
    "Conflicts": "conflicts",
}
_SCALARS = {
    "Type": "type",
    "ExecReload": "exec_reload",
    "User": "user",
    "Group": "group",
    "WorkingDirectory": "working_dir",
    "StateDirectory": "state",
    "LogsDirectory": "logs",
    "RuntimeDirectory": "runtime",
    "CacheDirectory": "cache",
    "Restart": "restart",
    "RestartSec": "restart_sec",
    "KillMode": "kill_mode",
    "TimeoutStartSec": "timeout_start",
    "TimeoutStopSec": "timeout_stop",
}
_FAMILIES = {"AF_UNIX AF_INET AF_INET6": "inet", "AF_UNIX": "unix", "none": "none"}
_LAYOUT = "layout (directive order, comments, blank lines or spacing)"


@dataclass(frozen=True, slots=True)
class Conversion:
    """One declaration converted from unit text.

    *units* are the unit files it covers (two for a socket or timer pair); *typed* is
    false for the raw ``Unit(name, text)`` fallback, whose *blockers* say why;
    *extra* lists the ``Section.Key`` directives a semantic conversion moved into
    ``extra=``; *absent* names the fields set to an explicit opt-out because the text has
    no such directive (``--adopt-defaults`` resets exactly those).
    """

    units: tuple[str, ...]
    declaration: Declaration
    typed: bool
    blockers: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()


def semantic(text: str) -> dict[str, list[tuple[str, str]]]:
    """Each section's directives as a sorted list (order, comments and layout ignored)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for section in UnitText.parse(text).sections:
        out.setdefault(section.name, []).extend((d.key, d.value) for d in section.directives)
    return {name: sorted(entries) for name, entries in sorted(out.items()) if entries}


def _differences(text: str, rendered: str) -> list[str]:
    """``Section.Key`` of every directive one side has and the other lacks."""
    left, right = semantic(text), semantic(rendered)
    found: set[str] = set()
    for name in {*left, *right}:
        a, b = left.get(name, []), right.get(name, [])
        found |= {f"{name}.{key}" for key, value in a if (key, value) not in b}
        found |= {f"{name}.{key}" for key, value in b if (key, value) not in a}
    return sorted(found)


def command(value: str) -> str | tuple[str, ...]:
    """An ``ExecStart=`` value as the field takes it: the line itself when it needs no
    unescaping, else the argv systemd reads (``$$`` and ``%%`` undoubled)."""
    try:
        words = shlex.split(value)
    except ValueError:
        return value
    if "$" not in value and "%" not in value and command_line(words) == value:
        return value
    return tuple(word.replace("$$", "$").replace("%%", "%") for word in words)


def _environment(value: str) -> list[tuple[str, str]] | None:
    try:
        words = shlex.split(value, posix=True)
    except ValueError:
        return None
    pairs = [word.partition("=") for word in words]
    if not pairs or any(not sep or not key for key, sep, _ in pairs):
        return None
    return [(key, item) for key, _, item in pairs]


@dataclass
class _Fields:
    """The typed fields a unit text implies, and what no field owns."""

    kw: dict[str, Any] = field(default_factory=dict)
    exec_start: list[str] = field(default_factory=list)
    hardening: list[tuple[str, str]] = field(default_factory=list)
    leftovers: list[Leftover] = field(default_factory=list)
    absent: set[str] = field(default_factory=set)


def _fields(text: UnitText, own: str = "Service") -> _Fields:
    """Read ``[Unit]``, ``[Service]`` and ``[Install]`` into ``Service``/``DropIn`` fields."""
    out = _Fields()
    kw = out.kw
    env: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    for section in text.sections:
        for d in section.directives:
            name, key, value = section.name, d.key, d.value
            if name == "Unit" and key == "Description" and "description" not in kw:
                kw["description"] = value
            elif name == "Unit" and key in _RELATIONS:
                lists.setdefault(_RELATIONS[key], []).extend(value.split())
            elif name == "Install" and key == "WantedBy" and "wanted_by" not in kw:
                kw["wanted_by"] = value
            elif name != own:
                out.leftovers.append((name, key, value))
            elif key in _SCALARS and _SCALARS[key] not in kw and value:
                kw[_SCALARS[key]] = value
            elif key == "DynamicUser" and value == "yes" and "user" not in kw:
                from tundravm.declarative.systemd import Dynamic

                kw["user"] = Dynamic()
            elif key == "ExecStart":
                out.exec_start.append(value)
            elif key in ("ExecStartPre", "ExecStartPost") and value:
                lists.setdefault("exec_start_pre" if key.endswith("Pre") else "exec_start_post", [])
                lists["exec_start_pre" if key.endswith("Pre") else "exec_start_post"].append(value)
            elif key in ("SupplementaryGroups", "ReadWritePaths", "EnvironmentFile") and value:
                target = {"SupplementaryGroups": "groups", "ReadWritePaths": "write_paths"}
                words = [value] if key == "EnvironmentFile" else value.split()
                lists.setdefault(target.get(key, "env_file"), []).extend(words)
            elif key == "Environment" and (pairs := _environment(value)) is not None:
                env.update(pairs)
            elif key == "LoadCredential" and ":" in value:
                cred, _, path = value.partition(":")
                kw.setdefault("credentials", {})[cred] = path
            elif key.startswith("Limit") and value:
                kw.setdefault("limits", {})[key.removeprefix("Limit")] = value
            elif key in HARDENING_KEYS:
                out.hardening.append((key, value))
            else:
                out.leftovers.append((name, key, value))
    for name, values in lists.items():
        kw[name] = tuple(values)
    if "env_file" in kw:
        files = kw["env_file"]
        kw["env_file"] = files[0] if len(files) == 1 else files
    if env:
        kw["env"] = env
    return out


def hardening_of(entries: list[tuple[str, str]]) -> Any:
    """The ``Hardening`` whose entries are *entries* (``"strict"``: the historical nine
    lines of ``security="strict"``); the caller checks the rendering."""
    from tundravm.declarative.systemd import Hardening

    if tuple(entries) == STRICT_HARDENING:
        return "strict"
    if not entries:
        return Hardening.none()
    got: dict[str, list[str]] = {}
    for key, value in entries:
        got.setdefault(key, []).append(value)

    def on(key: str) -> bool:
        return got.get(key, [""])[0] == "yes"

    filters = got.get("SystemCallFilter", [])
    allow = [v for v in filters if not v.startswith("~")]
    deny = [v[1:] for v in filters if v.startswith("~")]
    caps = got.get("CapabilityBoundingSet")
    return Hardening(
        protect_system=got.get("ProtectSystem", ["no"])[0],  # type: ignore[arg-type]
        protect_home=on("ProtectHome"),
        private_tmp=on("PrivateTmp"),
        private_devices=on("PrivateDevices"),
        devices=tuple(v.removesuffix(" rw") for v in got.get("DeviceAllow", [])),
        no_new_privileges=on("NoNewPrivileges"),
        capabilities=None if caps is None else tuple(caps[0].split()),
        jit=not on("MemoryDenyWriteExecute"),
        network=_FAMILIES.get(got.get("RestrictAddressFamilies", ["any"])[0], "any"),  # type: ignore[arg-type]
        syscalls=tuple(allow[0].split()) if allow else (),
        deny_syscalls=tuple(deny[0].split()) if deny else (),
        kernel=on("ProtectKernelModules"),
        sandbox=on("ProtectHostname"),
        umask=got.get("UMask", [None])[0],
    )


def _lowered(decl: Declaration) -> ProfileState:
    """*decl* lowered on its own into an empty profile."""
    from tundravm.declarative import state
    from tundravm.declarative.model import Mount, Service, Task

    profile = ProfileState(name="parse")
    match decl:
        case Service():
            state._service(profile, decl)
            state._finish_service(profile, decl)
        case Task():
            state._task(profile, decl, users=(), historical=False)
        case Mount():
            state._mount(profile, decl, steps=(), per_step=False)
        case _:
            raise TypeError(f"cannot render {type(decl).__name__}")
    return profile


def enables(decl: Declaration) -> frozenset[str]:
    """The units lowering *decl* enables (``x.socket`` for a socket-activated service)."""
    from tundravm.models import unit_name

    return frozenset(unit_name(s.name) for s in _lowered(decl).services if s.enabled)


def rendered(decl: Declaration, *, init: bool = False) -> dict[str, str]:
    """Unit file name (``x.service``, ``x.service.d/20-y.conf``) -> the text *decl*
    renders, through the lowering a recipe uses. With *init* a unit that waits for
    runtime-init (``needs=``) is ordered after ``runtime-init.service``, as in a variant
    with a runtime-init chain. Any other side effect (a declared user) is a ``"!"`` entry."""
    from tundravm.compiler.emit_mkosi import _systemd_unit_content
    from tundravm.declarative import state
    from tundravm.declarative.model import File, Unit

    if isinstance(decl, File) and isinstance(decl.content, str):
        return {decl.path.removeprefix("/usr/lib/systemd/system/"): decl.content}
    if isinstance(decl, Unit):
        if decl.dropin is not None and decl.content is None:
            name = f"{decl.name}.d/{decl.dropin.name}.conf"
            return {name: state._dropin(decl, decl.dropin, None)}
        if isinstance(decl.content, str):
            return {decl.name: decl.content}
    profile = _lowered(decl)
    out: dict[str, str] = {}
    for spec in profile.services:
        if not spec.command:
            continue
        if init and spec.needs:
            after = tuple(dict.fromkeys((state.INIT_SERVICE, *spec.after)))
            requires = tuple(dict.fromkeys((state.INIT_SERVICE, *spec.requires)))
            spec = dataclasses.replace(spec, after=after, requires=requires)
        out[f"{spec.name.removesuffix('.service')}.service"] = _systemd_unit_content(spec)
    prefix = f"{state.UNIT_DIRECTORY}/"
    for entry in profile.files:
        key = entry.path.removeprefix(prefix) if entry.path.startswith(prefix) else entry.path
        out[key] = entry.content if isinstance(entry.content, str) else "!"
    if profile.users or profile.groups or profile.phases:
        out["!"] = "side effects"
    return out


INIT = "runtime-init.service"


@dataclass
class _Candidate:
    """Keyword sets to try in order for one declaration class (the first that renders
    the text wins), what went into ``extra=``, the opt-outs, and early blockers."""

    cls: Any
    tries: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    absent: set[str] = field(default_factory=set)
    wrap: Callable[[Any], Declaration] | None = None


def _extra(leftovers: list[Leftover], mode: Mode, out: _Candidate, kw: dict[str, Any]) -> None:
    """Unowned directives: ``extra=`` in semantic mode, blockers in exact mode."""
    if mode == "exact":
        out.blockers += [f"{section}.{key}" for section, key, _ in leftovers]
        return
    extra: dict[str, dict[str, list[str]]] = {}
    for section, key, value in leftovers:
        extra.setdefault(section, {}).setdefault(key, []).append(value)
        out.extra.append(f"{section}.{key}")
    if extra:
        kw["extra"] = {
            s: {k: v[0] if len(v) == 1 else tuple(v) for k, v in keys.items()}
            for s, keys in extra.items()
        }


def _needs(kw: dict[str, Any], init: bool, absent: set[str]) -> None:
    """``needs=``: runtime-init first in ``After=`` and ``Requires=`` is the default
    ``needs``; under *init* a unit without it opts out (``needs=()``)."""
    if not init:
        return
    after, requires = list(kw.get("after", ())), list(kw.get("requires", ()))
    if after[:1] == [INIT] and requires[:1] == [INIT]:
        kw["after"], kw["requires"] = tuple(after[1:]), tuple(requires[1:])
    else:
        kw["needs"] = ()
        absent.add("needs")


def _tries(kw: dict[str, Any], *implied: str) -> list[dict[str, Any]]:
    """*kw*, first with ``after=`` minus what the *implied* relations already order."""
    after = kw.get("after", ())
    covered = {name for what in implied for name in kw.get(what, ())}
    short = tuple(name for name in after if name not in covered)
    return [{**kw, "after": short}, kw] if short != after else [kw]


def _exec(values: list[str], out: _Candidate, *, reset: bool = False) -> Any:
    expected = 2 if reset else 1
    if len(values) != expected or not values[-1] or (reset and values[0]):
        out.blockers.append("Service.ExecStart")
        return None
    return command(values[-1])


def _service(name: str, text: UnitText, mode: Mode, init: bool) -> _Candidate:
    from tundravm.declarative.model import Service

    out = _Candidate(Service)
    f = _fields(text)
    kw: dict[str, Any] = {**f.kw, "name": name.removesuffix(".service")}
    kw["exec_start"] = _exec(f.exec_start, out)
    profile = hardening_of(f.hardening)
    kw.update({"security": "strict"} if profile == "strict" else {"hardening": profile})
    for key, value in (("type", None), ("restart", "no"), ("restart_sec", None), ("user", None)):
        if key not in kw:
            kw[key] = value
            out.absent.add(key)
    if not f.hardening:
        out.absent.add("hardening")
    if "wanted_by" not in kw:
        kw["enabled"] = False
    _needs(kw, init, out.absent)
    _extra(f.leftovers, mode, out, kw)
    out.tries = _tries(kw, "requires", "wants", "binds_to")
    if kw.get("description") == kw["name"]:
        bare = {k: v for k, v in kw.items() if k != "description"}
        out.tries = [*_tries(bare, "requires", "wants", "binds_to"), *out.tries]
    return out


def _listen(text: UnitText, out: _Candidate) -> Any:
    from tundravm.declarative.model import Listen

    got: dict[str, str] = {}
    for section in text.sections:
        for d in section.directives:
            owned = (section.name, d.key) in (("Unit", "Description"), ("Install", "WantedBy"))
            if section.name == "Socket" and d.key not in got:
                got[d.key] = d.value
            elif not owned:
                out.blockers.append(f"{section.name}.{d.key}")
    kind: Literal["stream", "datagram"] = "stream" if "ListenStream" in got else "datagram"
    spelled = got.pop("ListenStream", "") or got.pop("ListenDatagram", "")
    address: int | str = int(spelled) if spelled.isdigit() else spelled
    mode = got.pop("SocketMode", "0660")
    accept = got.pop("Accept", "no") == "yes"
    user, group = got.pop("SocketUser", None), got.pop("SocketGroup", None)
    out.blockers += [f"Socket.{key}" for key in got]
    return Listen(
        address,
        kind=kind,
        accept=accept,
        user=user,
        group=group,
        mode=None if mode == "0660" else int(mode, 8),
    )


def _socket_pair(
    service: str, socket: str, texts: Mapping[str, str], mode: Mode, init: bool
) -> _Candidate:
    out = _service(service, UnitText.parse(texts[service]), mode, init)
    try:
        listen = _listen(UnitText.parse(texts[socket]), out)
    except (ValueError, TypeError) as error:
        out.blockers.append(f"invalid: {error}")
        return out
    for kw in out.tries:
        for what in ("after", "requires"):
            kw[what] = tuple(name for name in kw.get(what, ()) if name != socket)
        kw["socket"] = listen
    return out


def _schedule(text: UnitText, out: _Candidate) -> Any:
    from tundravm.declarative.model import Calendar, Every

    got: dict[str, str] = {}
    for section in text.sections:
        for d in section.directives:
            owned = (section.name, d.key) in (("Unit", "Description"), ("Install", "WantedBy"))
            if section.name == "Timer" and d.key not in got:
                got[d.key] = d.value
            elif not owned:
                out.blockers.append(f"{section.name}.{d.key}")
    jitter = got.pop("RandomizedDelaySec", None)
    if "OnCalendar" in got:
        spec = got.pop("OnCalendar")
        persistent = got.pop("Persistent", "no") == "yes"
        out.blockers += [f"Timer.{key}" for key in got]
        return Calendar(spec, persistent=persistent, jitter=jitter)
    interval = got.pop("OnUnitActiveSec", "")
    first = got.pop("OnBootSec", None)
    out.blockers += [f"Timer.{key}" for key in got]
    return Every(interval, first=None if first == interval else first, jitter=jitter)


_TASK_FIELDS = {"description", "user", "group", "state", "after", "requires", "wants"}


def _task(name: str, texts: Mapping[str, str], timer: str | None, init: bool) -> _Candidate:
    from tundravm.declarative.model import Task

    out = _Candidate(Task)
    f = _fields(UnitText.parse(texts[name]))
    kw: dict[str, Any] = {"name": name.removesuffix(".service")}
    kw["command"] = _exec(f.exec_start, out)
    got = dict(f.kw)
    if got.pop("type", None) != "oneshot":
        out.blockers.append("Service.Type")
    remain = [v for s, k, v in f.leftovers if (s, k) == ("Service", "RemainAfterExit")]
    if remain != ([] if timer else ["yes"]):
        out.blockers.append("Service.RemainAfterExit")
    out.blockers += [f"{s}.{k}" for s, k, _ in f.leftovers if k != "RemainAfterExit"]
    state = got.get("state")
    if got.pop("working_dir", None) != (f"/var/lib/{state}" if state else None):
        out.blockers.append("Service.WorkingDirectory")
    if "user" not in got:
        out.blockers.append("Service.User")
    kw["timeout"] = got.pop("timeout_start", None)
    if kw["timeout"] is None:
        out.absent.add("timeout")
    if not timer:
        kw["wanted_by"] = got.pop("wanted_by", None)
    out.blockers += [f"Service.{k}" for k in got if k not in _TASK_FIELDS]
    kw.update((k, v) for k, v in got.items() if k in _TASK_FIELDS)
    profile = hardening_of(f.hardening)
    kw["hardening"] = profile if profile != "strict" else None
    if not f.hardening:
        out.absent.add("hardening")
    if timer:
        try:
            kw["schedule"] = _schedule(UnitText.parse(texts[timer]), out)
        except (ValueError, TypeError) as error:
            out.blockers.append(f"invalid: {error}")
    _needs(kw, init, out.absent)
    out.tries = _tries(kw, "requires", "wants")
    return out


def _mount(name: str, text: UnitText) -> _Candidate:
    from tundravm.declarative.model import Mount

    out = _Candidate(Mount)
    f = _fields(text, own="")
    got = {k: v for s, k, v in f.leftovers if s == "Mount"}
    out.blockers += [f"{s}.{k}" for s, k, _ in f.leftovers if s != "Mount"]
    kw: dict[str, Any] = {"where": got.pop("Where", ""), "what": got.pop("What", "")}
    kw["type"] = got.pop("Type", None)
    kw["options"] = tuple(got.pop("Options", "").split(",")) if "Options" in got else ()
    out.blockers += [f"Mount.{key}" for key in got]
    for key in ("description", "after", "requires", "wanted_by"):
        if key in f.kw:
            kw[key] = f.kw.pop(key)
    out.blockers += [f"Unit.{key}" for key in f.kw]
    kw["enabled"] = "wanted_by" in kw
    out.tries = _tries(kw, "requires")
    return out


_NOT_DROPIN = {"type": "Type", "kill_mode": "KillMode", "wanted_by": "WantedBy"}


def _dropin(name: str, text: UnitText, mode: Mode) -> _Candidate:
    from tundravm.declarative.model import DropIn, Unit

    unit, _, file = name.partition(".d/")
    out = _Candidate(DropIn)
    f = _fields(text)
    kw: dict[str, Any] = {"name": file.removesuffix(".conf"), **f.kw}
    for key, directive in _NOT_DROPIN.items():
        if key in kw:
            section = "Install" if key == "wanted_by" else "Service"
            f.leftovers.append((section, directive, kw.pop(key)))
    if f.exec_start:
        kw["exec_start"] = _exec(f.exec_start, out, reset=True)
    if f.hardening:
        profile = hardening_of(f.hardening)
        kw["hardening"] = None if profile == "strict" else profile
    _extra(f.leftovers, mode, out, kw)
    out.tries = [kw]
    out.wrap = lambda dropin: Unit(unit, dropin=dropin)
    return out


_FAILURES: tuple[type[Exception], ...] = (ValueError, TypeError, KeyError, ValidationError)


def _build(cand: _Candidate, kw: dict[str, Any]) -> Declaration:
    made = cand.cls(**kw)
    return cand.wrap(made) if cand.wrap else made


def _check(decl: Declaration, texts: Mapping[str, str], mode: Mode, init: bool) -> list[str]:
    """Why *decl* does not render *texts* (empty when it does, in *mode*)."""
    out = rendered(decl, init=init)
    if set(out) != set(texts):
        return [f"renders {name}" for name in sorted(set(out) - set(texts))] or [
            f"does not render {name}" for name in sorted(set(texts) - set(out))
        ]
    blockers: list[str] = []
    for name, text in texts.items():
        if out[name] != text:
            blockers += _differences(text, out[name]) or ([_LAYOUT] if mode == "exact" else [])
    return blockers


def _settle(
    cand: _Candidate, texts: Mapping[str, str], mode: Mode, init: bool
) -> tuple[Declaration | None, list[str]]:
    """The first try that renders *texts*, each field reset to its declared default when
    that renders the same bytes; else the blockers."""
    if cand.blockers:
        return None, sorted(set(cand.blockers))
    blockers: list[str] = []
    for kw in cand.tries:
        try:
            decl = _build(cand, kw)
            found = _check(decl, texts, mode, init)
        except _FAILURES as error:
            found = [f"invalid: {error}"]
        if not found:
            return _minimize(cand, kw, decl, init), []
        blockers = blockers or found
    return None, sorted(set(blockers))


def _minimize(cand: _Candidate, kw: dict[str, Any], decl: Declaration, init: bool) -> Declaration:
    target = rendered(decl, init=init)
    defaults = {f.name: f for f in dataclasses.fields(cand.cls)}
    for name in list(kw):
        spec = defaults.get(name)
        if spec is None:
            continue
        if spec.default is not dataclasses.MISSING:
            value: Any = spec.default
        elif spec.default_factory is not dataclasses.MISSING:
            value = spec.default_factory()
        else:
            continue
        if kw[name] == value and type(kw[name]) is type(value):
            continue
        trial = {**kw, name: value}
        try:
            candidate = _build(cand, trial)
            same = rendered(candidate, init=init) == target
        except _FAILURES:
            continue
        if same:
            kw, decl = trial, candidate
    return decl


def _pairs(names: list[str], texts: Mapping[str, str]) -> list[tuple[str, ...]]:
    """Group *names*: ``x.socket`` with ``x.service``/``x@.service``, ``x.timer`` with a
    oneshot ``x.service``; everything else alone, in the order given."""
    groups: list[tuple[str, ...]] = []
    used: set[str] = set()
    for name in names:
        stem, _, suffix = name.rpartition(".")
        partner = None
        if suffix == "socket" and "/" not in name:
            partner = next((s for s in (f"{stem}.service", f"{stem}@.service") if s in texts), None)
        elif suffix == "timer" and f"{stem}.service" in texts:
            partner = f"{stem}.service"
        if partner and partner not in used:
            groups.append((partner, name))
            used |= {partner, name}
    for name in names:
        if name not in used:
            groups.append((name,))
    order = {name: index for index, name in enumerate(names)}
    return sorted(groups, key=lambda group: min(order[n] for n in group))


def _candidates(
    group: tuple[str, ...], texts: Mapping[str, str], mode: Mode, init: bool
) -> list[_Candidate]:
    """The candidate shapes for *group*, most specific first."""
    first = group[0]
    if len(group) == 2 and group[1].endswith(".timer"):
        return [_task(first, texts, group[1], init)]
    if len(group) == 2:
        return [_socket_pair(first, group[1], texts, mode, init)]
    if ".d/" in first and first.endswith(".conf"):
        return [_dropin(first, UnitText.parse(texts[first]), mode)]
    if first.endswith(".mount"):
        return [_mount(first, UnitText.parse(texts[first]))]
    if first.endswith(".service"):
        text = UnitText.parse(texts[first])
        found = [_service(first, text, mode, init)]
        if "oneshot" in text.values("Service", "Type"):
            found.insert(0, _task(first, texts, None, init))
        return found
    return []


def convert(
    texts: Mapping[str, str], *, mode: Mode = "exact", init: bool = False
) -> list[Conversion]:
    """Typed declarations for the unit files *texts* (name -> text), pairing sockets and
    timers with their services; a unit no shape renders comes back as ``Unit(name, text)``.
    *init*: units are rendered as in a variant with a runtime-init chain."""
    out: list[Conversion] = []
    for group in _pairs(list(texts), texts):
        blockers: list[str] = []
        for cand in _candidates(group, texts, mode, init):
            decl, found = _settle(cand, {n: texts[n] for n in group}, mode, init)
            if decl is not None:
                absent = tuple(sorted(cand.absent))
                out.append(Conversion(group, decl, True, (), tuple(cand.extra), absent))
                break
            blockers = found
        else:
            if len(group) > 1:
                out += convert({n: texts[n] for n in group[1:]}, mode=mode, init=init)
                out += convert({group[0]: texts[group[0]]}, mode=mode, init=init)
                continue
            reason = tuple(blockers) or (f"no typed shape for {group[0]}",)
            out.append(Conversion(group, raw(group[0], texts[group[0]]), False, reason))
    return out


def raw(name: str, text: str) -> Declaration:
    """The verbatim declaration of unit file *name*: ``Unit(name, text)``, or the
    ``File`` a drop-in is."""
    from tundravm.declarative.model import File, Unit

    if ".d/" in name:
        return File(f"/usr/lib/systemd/system/{name}", text)
    return Unit(name, text)


def parse(text: str, *, name: str, mode: Mode = "exact", init: bool = False) -> Declaration:
    """The declaration unit *text* (unit file *name*) converts to; see :func:`convert`."""
    return convert({name: text}, mode=mode, init=init)[0].declaration


def with_defaults(decl: Declaration, names: tuple[str, ...]) -> Declaration:
    """*decl* with the fields *names* reset to their declared defaults (``--adopt-defaults``
    on a conversion's :attr:`Conversion.absent`)."""
    reset: dict[str, Any] = {}
    for spec in dataclasses.fields(decl):
        if spec.name not in names:
            continue
        if spec.default is not dataclasses.MISSING:
            reset[spec.name] = spec.default
        elif spec.default_factory is not dataclasses.MISSING:
            reset[spec.name] = spec.default_factory()
    return dataclasses.replace(decl, **reset)
