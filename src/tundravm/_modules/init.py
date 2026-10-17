"""Init — minimal runtime-init script builder.

Lowering registers bash fragments per profile
(``ProfileState.init_scripts``); compiling hands each profile's merged
fragments to an Init, which orders them (:func:`runtime_init_order`) and
generates ``/usr/bin/runtime-init`` plus ``runtime-init.service``. The lockfile
records the same order.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from textwrap import dedent

from tundravm.errors import ValidationError
from tundravm.models import ALL_STEPS, FileEntry, InitScriptEntry, ProfileState


def order_steps(nodes: Sequence[tuple[str, Iterable[str], Iterable[str]]]) -> list[str]:
    """The step names of *nodes* (``(name, after, before)``) in a deterministic run order.

    A topological sort: each step runs after its ``after`` steps and before its
    ``before`` steps; among the steps ready to run, the earliest in *nodes* goes
    first (the built-ins register first, in their ``keys``, ``disks``,
    ``secrets`` rank). References to steps not in *nodes* are ignored here (lint
    reports them); a cycle raises ``ValidationError`` naming its steps.
    """
    names = list(dict.fromkeys(name for name, _, _ in nodes))
    deps: dict[str, set[str]] = {name: set() for name in names}
    for name, after, before in nodes:
        deps[name].update(dep for dep in after if dep in deps and dep != name)
        for later in before:
            if later in deps and later != name:
                deps[later].add(name)
    placed: list[str] = []
    done: set[str] = set()
    while len(placed) < len(names):
        ready = next((n for n in names if n not in done and deps[n] <= done), None)
        if ready is None:
            cycle = _cycle([n for n in names if n not in done], deps)
            raise ValidationError(
                f"Runtime-init steps form an ordering cycle: {' -> '.join(map(repr, cycle))}.",
                hint="Drop one of the after=/before= edges between these steps.",
            )
        placed.append(ready)
        done.add(ready)
    return placed


def _cycle(remaining: list[str], deps: dict[str, set[str]]) -> list[str]:
    """One cycle among *remaining* steps (each waits on another remaining one)."""
    pending = set(remaining)
    path = [remaining[0]]
    while True:
        step = min(deps[path[-1]] & pending, key=remaining.index)
        if step in path:
            return [*path[path.index(step) :], step]
        path.append(step)


def runtime_init_order(entries: Iterable[InitScriptEntry]) -> list[InitScriptEntry]:
    """*entries* in the order ``/usr/bin/runtime-init`` runs them.

    Duplicate ``(name, script)`` fragments run once; steps follow :func:`order_steps`
    and the fragments of one step keep their registration order.
    """
    by_step: dict[str, list[InitScriptEntry]] = {}
    for entry in {(entry.name, entry.script): entry for entry in entries}.values():
        by_step.setdefault(entry.name, []).append(entry)
    nodes = [(e.name, e.after, e.before) for group in by_step.values() for e in group]
    return [entry for step in order_steps(nodes) for entry in by_step[step]]


SCRIPT_HEADER = "#!/bin/bash\nset -euo pipefail\n"
STEP_DIRECTORY = "/usr/lib/runtime-init"
"""Where the per-step scripts live (one executable per step, current dialect)."""
INIT_SERVICE = "runtime-init.service"
UNIT_DIRECTORY = "/usr/lib/systemd/system"


def step_unit(step: str) -> str:
    """The oneshot unit that runs runtime-init step *step* (current dialect)."""
    return f"runtime-init-{step}.service"


def wait_units(needs: Iterable[str], *, steps: Collection[str], per_step: bool) -> tuple[str, ...]:
    """The units something needing *needs* (step names, ``"*"`` for all) waits for.

    Nothing without runtime-init steps; ``runtime-init.service`` for the whole
    chain or under ``nethermind-v1`` (one unit runs every step); else each needed
    step's own unit.
    """
    needs = [step for step in needs if step == ALL_STEPS or step in steps]
    if not steps or not needs:
        return ()
    if not per_step or ALL_STEPS in needs:
        return (INIT_SERVICE,)
    return tuple(step_unit(step) for step in needs)


def is_init_path(path: str) -> bool:
    """Whether *path* is a file runtime-init generation writes."""
    return (
        path in ("/usr/bin/runtime-init", f"{UNIT_DIRECTORY}/{INIT_SERVICE}")
        or path.startswith(f"{STEP_DIRECTORY}/")
        or (path.startswith(f"{UNIT_DIRECTORY}/runtime-init-") and path.endswith(".service"))
    )


@dataclass(slots=True)
class Init:
    """Default runtime-init generator.

    Holds no fragments itself; ``apply()`` renders the ones it is given. With
    ``per_step`` (the current dialect) each step is its own oneshot unit
    (``runtime-init-<step>.service`` running ``/usr/lib/runtime-init/<step>``,
    ``After=``/``Requires=`` the steps it runs after) and ``runtime-init.service``
    requires them all; otherwise (``nethermind-v1``) ``/usr/bin/runtime-init``
    runs every step in order from the one ``runtime-init.service``.
    """

    @property
    def service_name(self) -> str:
        return INIT_SERVICE

    def apply(
        self,
        profile: ProfileState,
        *,
        scripts: Sequence[InitScriptEntry] | None = None,
        network_setup: bool = True,
        per_step: bool = False,
    ) -> None:
        """Generate the runtime-init scripts and units into *profile*.files.

        *scripts* replaces ``profile.init_scripts`` as the fragments to render, e.g.
        with the merged fragments of a profile that extends another. With
        *network_setup* the units require ``network-setup.service``; without it,
        they wait for ``network-online.target``.
        """
        ordered = runtime_init_order(profile.init_scripts if scripts is None else scripts)
        if not ordered:
            return
        profile.files = [f for f in profile.files if not is_init_path(f.path)]
        network = _network(network_setup)
        if not per_step:
            content = "\n".join([SCRIPT_HEADER, *(entry.script for entry in ordered)])
            self._add(profile, "/usr/bin/runtime-init", content, "0755")
            unit = render_service_unit(network_setup=network_setup)
            self._add(profile, f"{UNIT_DIRECTORY}/{INIT_SERVICE}", unit, "0644")
            return
        steps: dict[str, list[InitScriptEntry]] = {}
        for entry in ordered:
            steps.setdefault(entry.name, []).append(entry)
        nodes = [(e.name, e.after, e.before) for e in ordered]
        for step, entries in steps.items():
            script = "\n".join([SCRIPT_HEADER, *(entry.script for entry in entries)])
            self._add(profile, f"{STEP_DIRECTORY}/{step}", script, "0755")
            deps = [step_unit(dep) for dep in _direct_deps(step, nodes, steps)]
            unit = _step_unit_text(step, network, deps)
            self._add(profile, f"{UNIT_DIRECTORY}/{step_unit(step)}", unit, "0644")
        units = " ".join(step_unit(step) for step in steps)
        aggregate = f"After={units}\nRequires={units}"
        self._add(
            profile, f"{UNIT_DIRECTORY}/{INIT_SERVICE}", _aggregate(aggregate, "/bin/true"), "0644"
        )

    @staticmethod
    def _add(profile: ProfileState, path: str, content: str, mode: str) -> None:
        profile.files.append(FileEntry(path=path, content=content, mode=mode))


def render_service_unit(*, network_setup: bool = True) -> str:
    """The ``nethermind-v1`` ``runtime-init.service``, which runs ``/usr/bin/runtime-init``."""
    return _aggregate(_network(network_setup), "/usr/bin/runtime-init")


def _network(network_setup: bool) -> str:
    if network_setup:
        return "After=network.target network-setup.service\nRequires=network-setup.service"
    return "After=network-online.target\nWants=network-online.target"


def _direct_deps(
    step: str, nodes: Sequence[tuple[str, Iterable[str], Iterable[str]]], steps: Collection[str]
) -> list[str]:
    """The steps *step* runs after (its ``after`` and the steps naming it in ``before``)."""
    deps: list[str] = []
    for name, after, before in nodes:
        if name == step:
            deps.extend(after)
        elif step in before:
            deps.append(name)
    order = list(steps)
    return sorted({d for d in deps if d in steps and d != step}, key=order.index)


def _step_unit_text(step: str, network: str, deps: Sequence[str]) -> str:
    """``runtime-init-<step>.service``: *network* plus *deps* in ``After=``/``Requires=``."""
    lines = network.split("\n")
    for directive in ("After", "Requires"):
        index = next((i for i, line in enumerate(lines) if line.startswith(f"{directive}=")), None)
        if deps and index is None:
            lines.append(f"{directive}={' '.join(deps)}")
        elif deps and index is not None:
            lines[index] = " ".join([lines[index], *deps])
    head = ["[Unit]", f"Description=Runtime Init: {step}", *lines, "", "[Service]"]
    tail = ["Type=oneshot", f"ExecStart={STEP_DIRECTORY}/{step}", "RemainAfterExit=yes", ""]
    return "\n".join([*head, *tail])


def _aggregate(ordering: str, exec_start: str) -> str:
    """``runtime-init.service``: *ordering* is its ``After=``/``Requires=``/``Wants=`` lines."""
    return dedent("""\
        [Unit]
        Description=Runtime Init
        {ordering}

        [Service]
        Type=oneshot
        ExecStart={exec_start}
        RemainAfterExit=yes

        [Install]
        WantedBy=minimal.target
    """).format(ordering=ordering, exec_start=exec_start)
