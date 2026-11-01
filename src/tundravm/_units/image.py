"""Unit files as an ``Image`` file of public :class:`~tundravm.Service` values.

``convert --from systemd`` reads each ``x.service`` with its ``x.service.d/*.conf``
drop-ins (in name order, an empty assignment resetting a list) and the ``x.timer`` or
``x.socket`` that starts it, and writes one ``Service``. A directive a ``Service`` keyword
holds goes there (``ExecStart=`` as argv, ``Type=notify`` as ``readiness=Notify()``, the
cgroup limits as ``resources=``, the stop as ``shutdown=``, the schedule as ``timer=``, the
socket as ``socket=``); anything else systemd knows goes into ``unit=DropIn(...)``, the
service's escape. Where the unit differs from what the image implies (``Type=exec``,
``Restart=on-failure``, a dedicated user, the ``static`` sandbox, enabled at boot) a note
says so, and a unit or directive no ``Image`` slot holds is reported as unsupported:
nothing is dropped without a line saying so.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tundravm.errors import ValidationError

from .render import HARDENING_KEYS
from .text import UnitText

__all__ = ["ImageSource", "image_source"]

_RELATIONS = {
    "After": "after",
    "Requires": "requires",
    "Wants": "wants",
    "Before": "before",
    "PartOf": "part_of",
    "BindsTo": "binds_to",
    "Conflicts": "conflicts",
}
_RESOURCES = {
    "MemoryMax": "memory_max",
    "MemoryHigh": "memory_high",
    "CPUQuota": "cpu_quota",
    "CPUWeight": "cpu_weight",
    "TasksMax": "tasks_max",
    "IOWeight": "io_weight",
}
_COUNTS = ("cpu_weight", "tasks_max", "io_weight")
_DIRECTORIES = {"StateDirectory": "state", "LogsDirectory": "logs", "RuntimeDirectory": "runtime"}
_DROPIN = {
    "Group": "group",
    "EnvironmentFile": "env_file",
    "CacheDirectory": "cache",
    "ExecReload": "exec_reload",
    "TimeoutStartSec": "timeout_start",
}
_DROPIN_LISTS = {
    "ExecStartPre": "exec_start_pre",
    "ExecStartPost": "exec_start_post",
    "ReadWritePaths": "write_paths",
}
_TARGETS = ("multi-user.target", "default.target", "minimal.target")
"""``WantedBy=`` targets a generated service is enabled into anyway."""


def _preset(entries: list[tuple[str, str]]) -> str | None:
    """The preset whose directives are exactly *entries*, if one is."""
    from tundravm.declarative.systemd import Hardening, ceiling

    from .parse import hardening_of

    if not entries:
        return None
    sandbox = hardening_of(entries)
    exact = ceiling(sandbox) if isinstance(sandbox, Hardening) else None
    if exact is None or sorted(sandbox.entries()) != sorted(entries):
        return None
    return exact[0]


def _yes(value: str | None) -> bool:
    """A systemd boolean (``yes``, ``true``, ``on``, ``1``) that is true."""
    return value is not None and value.lower() in ("yes", "true", "on", "1")


@dataclass(slots=True)
class ImageSource:
    """The services a conversion found, and what it could not carry."""

    services: list[tuple[str, Any, str | None]] = field(default_factory=list)
    """``(unit names, Service, description)`` per converted service."""
    notes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _Directives:
    """One unit's directives after its drop-ins, by section and key, in first-seen order."""

    values: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def add(self, text: str) -> None:
        for section in UnitText.parse(text).sections:
            for directive in section.directives:
                key = (section.name, directive.key)
                if not directive.value:
                    self.values[key] = []
                else:
                    self.values.setdefault(key, []).append(directive.value)

    def pop(self, section: str, key: str) -> list[str]:
        return self.values.pop((section, key), [])

    def last(self, section: str, key: str) -> str | None:
        found = self.pop(section, key)
        return found[-1] if found else None


def image_source(texts: Mapping[str, str]) -> ImageSource:
    """The ``Service`` values unit files *texts* (name -> text) convert to."""
    out = ImageSource()
    services = sorted(name for name in texts if name.endswith(".service") and "/" not in name)
    used: set[str] = set()
    for name in services:
        stem = name.removesuffix(".service")
        group = [name]
        directives = _Directives()
        directives.add(texts[name])
        for dropin in sorted(n for n in texts if n.startswith(f"{name}.d/")):
            directives.add(texts[dropin])
            group.append(dropin)
        starters = [f"{stem}.{kind}" for kind in ("timer", "socket") if f"{stem}.{kind}" in texts]
        group += starters
        used.update(group)
        converted = _Service(name, directives, {s: texts[s] for s in starters}).convert(out)
        if converted is not None:
            service, description = converted
            out.services.append((" + ".join(group), service, description))
    orphans = sorted(n for n in set(texts) - used if ".service.d/" in n and n.endswith(".conf"))
    for unit in sorted({name.partition(".d/")[0] for name in orphans}):
        group = [name for name in orphans if name.startswith(f"{unit}.d/")]
        directives = _Directives()
        for dropin in group:
            directives.add(texts[dropin])
        converted = _packaged(unit, group[0], directives, out)
        if converted is not None:
            used.update(group)
            out.services.append((" + ".join(group), converted, None))
    for name in sorted(set(texts) - used):
        out.unsupported.append(
            f"{name}: no Image slot holds it (only a service, with its "
            "drop-ins and the timer or socket that starts it)"
        )
    return out


@dataclass(slots=True)
class _Service:
    """One service unit, read into ``Service`` keywords and a ``DropIn``."""

    name: str
    directives: _Directives
    starters: Mapping[str, str]
    kw: dict[str, Any] = field(default_factory=dict)
    dropin: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)

    @property
    def stem(self) -> str:
        return self.name.removesuffix(".service")

    def convert(self, out: ImageSource) -> tuple[Any, str | None] | None:
        from tundravm import Service
        from tundravm.systemd import DropIn

        d = self.directives
        description = d.last("Unit", "Description")
        argv = self.argv()
        self.timer()
        self.socket()
        self.relations()
        self.service_section()
        self.install()
        for (section, key), values in d.values.items():
            if section in ("Unit", "Service") and values:
                self.extra.setdefault(section, {})[key] = (
                    values[0] if len(values) == 1 else tuple(values)
                )
            elif values:
                self.unsupported.append(f"{section}.{key}")
        if self.kw.get("timer") is not None:
            self.scheduled()
        try:
            if self.dropin or self.extra:
                extra: dict[str, Any] = {"extra": self.extra} if self.extra else {}
                self.kw["unit"] = DropIn(**self.dropin, **extra)
            service = Service(self.stem, argv, **self.kw) if argv else None
        except ValidationError as error:
            service = None
            self.unsupported.append(str(error).splitlines()[0])
        out.notes += [f"{self.name}: {note}" for note in self.notes]
        out.unsupported += [f"{self.name}: {item}" for item in self.unsupported]
        if service is None:
            out.unsupported.append(f"{self.name}: not converted")
            return None
        return service, description

    def argv(self) -> list[str]:
        values = self.directives.pop("Service", "ExecStart")
        if len(values) != 1:
            self.unsupported.append(
                f"Service.ExecStart: a Service runs one command, the unit has {len(values)}"
            )
            return []
        try:
            words = shlex.split(values[0])
        except ValueError as error:
            self.unsupported.append(f"Service.ExecStart: {error}")
            return []
        return [word.replace("$$", "$").replace("%%", "%") for word in words]

    def timer(self) -> None:
        from tundravm.systemd import Calendar, Every

        text = self.starters.get(f"{self.stem}.timer")
        if text is None:
            return
        timer = _Directives()
        timer.add(text)
        calendar = timer.last("Timer", "OnCalendar")
        interval = timer.last("Timer", "OnUnitActiveSec")
        first = timer.last("Timer", "OnBootSec")
        jitter = timer.last("Timer", "RandomizedDelaySec")
        persistent = timer.last("Timer", "Persistent")
        timer.pop("Unit", "Description")
        timer.pop("Install", "WantedBy")
        try:
            if calendar is not None:
                self.kw["timer"] = Calendar(calendar, persistent=_yes(persistent), jitter=jitter)
                if first is not None:
                    self.unsupported.append("Timer.OnBootSec: a Calendar timer runs at its times")
            elif interval is not None:
                self.kw["timer"] = Every(interval, first=first, jitter=jitter)
            else:
                self.unsupported.append("the timer has neither OnCalendar= nor OnUnitActiveSec=")
        except ValidationError as error:
            self.unsupported.append(str(error).splitlines()[0])
        self.unsupported += [f"{s}.{k} (timer)" for (s, k), v in timer.values.items() if v]

    def socket(self) -> None:
        from .parse import _Candidate, _listen

        text = self.starters.get(f"{self.stem}.socket")
        if text is None:
            return
        found = _Candidate(cls=None)
        try:
            self.kw["socket"] = _listen(UnitText.parse(text), found)
        except (ValueError, TypeError, ValidationError) as error:
            self.unsupported.append(f"the socket: {str(error).splitlines()[0]}")
        self.unsupported += [f"{blocker} (socket)" for blocker in found.blockers]

    def relations(self) -> None:
        starters = set(self.starters)
        for key, name in _RELATIONS.items():
            words = [
                word
                for value in self.directives.pop("Unit", key)
                for word in value.split()
                if word not in starters
            ]
            if words:
                self.dropin[name] = tuple(dict.fromkeys(words))
        if self.dropin.keys() & {"after", "requires"}:
            self.notes.append(
                "unit names in After=/Requires= are kept as a DropIn; requires=[other.ready] "
                "waits for another Service to be ready"
            )

    def service_section(self) -> None:
        from tundravm.secrets import Credential
        from tundravm.systemd import Notify, Resources, Shutdown

        d = self.directives
        kind = d.last("Service", "Type") or "simple"
        if kind == "notify":
            self.kw["readiness"] = Notify()
        elif kind == "simple":
            self.notes.append(
                "Type=simple becomes Type=exec: the start fails when the program cannot be run"
            )
        elif kind == "oneshot" and f"{self.stem}.timer" not in self.starters:
            self.notes.append("Type=oneshot becomes Type=exec, restarted when it fails")
        elif kind not in ("exec", "oneshot"):
            self.unsupported.append(f"Service.Type={kind}: a Service is Type=exec or notify")
        user = d.last("Service", "User")
        if _yes(d.last("Service", "DynamicUser")):
            self.notes.append(f"DynamicUser=yes becomes the generated system user {self.stem!r}")
        elif user is not None and user != self.stem:
            self.kw["user"] = user
            self.notes.append(f"User={user} must exist in the image (a package's, or users=)")
        elif user is None:
            self.notes.append(f"it runs as the generated system user {self.stem!r}, not root")
        groups = [
            word for value in d.pop("Service", "SupplementaryGroups") for word in value.split()
        ]
        if groups:
            self.kw["groups"] = groups
        working = d.last("Service", "WorkingDirectory")
        if working is not None and working.startswith("/"):
            self.kw["working_directory"] = working
        elif working is not None:
            self.unsupported.append(f"Service.WorkingDirectory={working}: not an absolute path")
        environment: dict[str, str] = {}
        for value in d.pop("Service", "Environment"):
            try:
                pairs = [word.partition("=") for word in shlex.split(value)]
            except ValueError:
                pairs = []
            if not pairs or any(not sep or not key for key, sep, _ in pairs):
                self.unsupported.append(f"Service.Environment={value}")
                continue
            environment.update((key, item) for key, _, item in pairs)
        if environment:
            self.kw["environment"] = environment
        credentials = []
        for value in d.pop("Service", "LoadCredential"):
            name, _, path = value.partition(":")
            credentials.append(Credential(name, path))
        if credentials:
            self.kw["credentials"] = credentials
        for key, name in _DIRECTORIES.items():
            values = [word for value in d.pop("Service", key) for word in value.split()]
            if len(values) == 1:
                self.kw[name] = values[0]
            elif values:
                self.unsupported.append(f"Service.{key}={' '.join(values)}: a Service keeps one")
        for key, name in _DROPIN.items():
            if (single := d.last("Service", key)) is not None:
                self.dropin[name] = single
        for key, name in _DROPIN_LISTS.items():
            values = d.pop("Service", key)
            if values:
                self.dropin[name] = tuple(values)
        restart = d.last("Service", "Restart")
        if restart is None and f"{self.stem}.timer" not in self.starters:
            self.notes.append("it restarts on failure, 5 s apart; the unit had no Restart=")
        elif restart is not None and restart != "on-failure":
            self.dropin["restart"] = restart
        delay = d.last("Service", "RestartSec")
        if delay is not None and delay not in ("5s", "5"):
            self.dropin["restart_sec"] = delay
        resources: dict[str, Any] = {
            name: int(limit) if name in _COUNTS and limit.isdigit() else limit
            for key, name in _RESOURCES.items()
            if (limit := d.last("Service", key)) is not None
        }
        if resources:
            self.kw["resources"] = Resources(**resources)
        stop: dict[str, Any] = {
            "timeout": d.last("Service", "TimeoutStopSec"),
            "kill_mode": d.last("Service", "KillMode"),
            "signal": d.last("Service", "KillSignal"),
        }
        if (kill := d.last("Service", "SendSIGKILL")) is not None and not _yes(kill):
            stop["final_kill"] = False
        stop = {key: setting for key, setting in stop.items() if setting is not None}
        if stop:
            self.kw["shutdown"] = Shutdown(**stop)
        limits = {
            key.removeprefix("Limit"): values[-1]
            for (section, key), values in list(d.values.items())
            if section == "Service" and key.startswith("Limit") and values
        }
        for key in limits:
            d.pop("Service", f"Limit{key}")
        if limits:
            self.dropin["limits"] = limits
        sandbox = sorted(
            {key for (section, key) in d.values if section == "Service"} & set(HARDENING_KEYS)
        )
        entries = [(key, value) for key in sandbox for value in d.pop("Service", key) or [""]]
        preset = _preset(entries)
        if preset is not None:
            if preset != "static":
                self.kw["hardening"] = preset
            same = " ('node' and 'python' render the same)" if preset == "jit" else ""
            self.notes.append(f"the unit's sandbox is exactly the {preset!r} preset{same}")
            return
        self.notes.append(
            "the sandbox is the 'static' preset"
            + (f", in place of the unit's {', '.join(sandbox)}" if sandbox else "")
            + ": name another (hardening='python') for a runtime that needs one"
        )

    def install(self) -> None:
        d = self.directives
        wanted = [word for value in d.pop("Install", "WantedBy") for word in value.split()]
        if not wanted and f"{self.stem}.timer" not in self.starters:
            self.notes.append("it is enabled at boot; the unit had no WantedBy=")
        for target in wanted:
            if target not in _TARGETS:
                self.unsupported.append(f"Install.WantedBy={target}: a Service starts at boot")

    def scheduled(self) -> None:
        """A timer service carries argv, user=, state=, requires= and its timer only."""
        keep = {"timer", "user", "state", "requires", "hardening"}
        for key in sorted(set(self.kw) - keep):
            self.unsupported.append(f"{key}=: a timer's service carries argv, user= and state=")
            del self.kw[key]
        for key in sorted(self.dropin):
            self.unsupported.append(f"{key} (drop-in): a timer's service takes no unit=")
        self.dropin.clear()
        self.extra.clear()


def _packaged(unit: str, first: str, d: _Directives, out: ImageSource) -> Any:
    """Drop-ins over a unit the set does not hold: a package's unit, as
    ``Service.packaged(unit, unit=DropIn(...))`` under a preset sandbox."""
    from tundravm import Service
    from tundravm.systemd import DropIn

    stem = unit.removesuffix(".service")
    notes = [f"no {unit} in the set: the unit of package {stem!r} (package= names another)"]
    unsupported: list[str] = []
    dropin: dict[str, Any] = {"name": first.partition(".d/")[2].removesuffix(".conf")}
    for key, name in _RELATIONS.items():
        words = [word for value in d.pop("Unit", key) for word in value.split()]
        if words:
            dropin[name] = tuple(dict.fromkeys(words))
    environment: dict[str, str] = {}
    for value in d.pop("Service", "Environment"):
        try:
            pairs = [word.partition("=") for word in shlex.split(value)]
        except ValueError:
            pairs = []
        if not pairs or any(not sep or not key for key, sep, _ in pairs):
            unsupported.append(f"Service.Environment={value}")
            continue
        environment.update((key, item) for key, _, item in pairs)
    if environment:
        dropin["env"] = environment
    for key in ("ExecStart", "Type"):
        if d.pop("Service", key):
            unsupported.append(f"Service.{key}=: a packaged unit keeps its package's")
    keys = {key for (section, key) in d.values if section == "Service"}
    sandbox = sorted(keys & set(HARDENING_KEYS))
    entries = [(key, value) for key in sandbox for value in d.pop("Service", key) or [""]]
    preset = _preset(entries) or "static"
    replaced = f", in place of the drop-ins' {', '.join(sandbox)}" if sandbox else ""
    notes.append(f"it runs under the {preset!r} sandbox{replaced if preset == 'static' else ''}")
    extra: dict[str, dict[str, Any]] = {}
    for (section, key), values in d.values.items():
        if section in ("Unit", "Service") and values:
            extra.setdefault(section, {})[key] = values[0] if len(values) == 1 else tuple(values)
        elif values and (section, key) != ("Install", "WantedBy"):
            unsupported.append(f"{section}.{key}")
    if extra:
        dropin["extra"] = extra
    try:
        service = Service.packaged(
            unit,
            hardening=preset,  # type: ignore[arg-type]
            unit=DropIn(**dropin) if len(dropin) > 1 else None,
        )
    except ValidationError as error:
        service = None
        unsupported.append(str(error).splitlines()[0])
    out.notes += [f"{unit}: {note}" for note in notes]
    out.unsupported += [f"{unit}: {item}" for item in unsupported]
    if service is None:
        out.unsupported.append(f"{unit}: not converted")
    return service
