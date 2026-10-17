"""Source builds back from a compiled build script, for :func:`~.importer.import_tree`.

A current-dialect hook starts with its ``# tundravm build: <json>`` marker
(:meth:`~tundravm._source.SourceBuild.marker`): the ``Build`` comes back exactly
as declared and the generated lines after it are not imported. A
``nethermind-v1`` hook has no marker: its fetch, build command, cache key and
install steps are parsed into a ``Build(..., script=...)``, kept only when it
renders the same line back.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tundravm._source import (
    BUILD_MARKER,
    COMMIT_PATTERN,
    SOURCES_MOUNT,
    CargoBuild,
    DotnetBuild,
    GoBuild,
)
from tundravm.errors import ValidationError

from .model import Build, Git, Hook, Http, Install
from .state import source_build

_RECIPES: dict[str, type[GoBuild | CargoBuild | DotnetBuild]] = {
    "go": GoBuild,
    "cargo": CargoBuild,
    "dotnet": DotnetBuild,
}
_HISTORICAL = re.compile(
    r'^if ! \(\[ -d "\$BUILDDIR/(?P<key>[^"]+)" \] && .*?\); then (?P<fetch>.*?) && '
    r"mkosi-chroot bash -c '(?P<inner>(?:[^']|'\\'')*)' && mkdir -p \"\$BUILDDIR/(?P=key)\""
)
_STORED = re.compile(r'(?:install -D -m (\d+) |cp -r )"\$BUILDROOT/build/([^"]+)"')
_RESTORED = re.compile(
    r'(?:install -D -m \d+ "\$BUILDDIR/[^"]+"/\S+ |mkdir -p )"\$DESTDIR/([^"]*)"'
)


@dataclass(frozen=True, slots=True)
class Recovered:
    """A source build read back from lines ``[start, end)`` of a build script.

    ``lock_pin`` is the 12-hex pin a lockfile gave a mounted hook (``None``
    when unpinned or pinned inline), ``unpinned`` and ``pinned`` the hook text
    without and with it; ``lost`` says what the ``Build`` cannot carry.
    """

    build: Build
    end: int
    lock_pin: str | None = None
    unpinned: str = ""
    pinned: str = ""
    lost: str | None = None


def build_items(
    lines: Sequence[str], *, historical: bool, notes: list[str], rel: str
) -> tuple[list[Hook | Build], list[Recovered]]:
    """*lines* of a build script as ``Build``s where hooks are recognized, ``Hook``s between."""
    items: list[Hook | Build] = []
    recovered: list[Recovered] = []
    start = index = 0
    part = 1
    while index < len(lines):
        found = (
            _historical(lines, index) if historical else _marked(lines, index, notes=notes, rel=rel)
        )
        if found is None:
            index += 1
            continue
        text = "\n".join(lines[start:index])
        if text.strip():
            items.append(Hook("build" if part == 1 else f"build-{part}", "build", text))
            part += 1
        items.append(found.build)
        recovered.append(found)
        start = index = found.end
    text = "\n".join(lines[start:])
    if text.strip():
        items.append(Hook("build" if part == 1 else f"build-{part}", "build", text))
    return items, recovered


# ── current dialect: the marker ──────────────────────────────────────────


def _marked(lines: Sequence[str], index: int, *, notes: list[str], rel: str) -> Recovered | None:
    line = lines[index]
    if not line.startswith(BUILD_MARKER):
        return None
    try:
        build = _from_payload(json.loads(line.removeprefix(BUILD_MARKER)))
    except (ValueError, KeyError, TypeError, ValidationError):
        notes.append(f"{rel}: a `# tundravm build:` line is not a build declaration; kept")
        return None
    following = lines[index + 1] if index + 1 < len(lines) else ""
    build = _env_in_order(build, following)
    spec = source_build(build)
    if spec.marker() != line:
        notes.append(f"{rel}: build {build.name} does not render its marker back; kept")
        return None
    unpinned = spec.render(mounted=True)
    count = unpinned.count("\n") + 1
    if "\n".join(lines[index : index + count]) == unpinned:
        return Recovered(build, index + count, unpinned=unpinned, pinned=unpinned)
    checkout = re.search(
        rf'"\$SRCDIR/{SOURCES_MOUNT}/{re.escape(build.name)}-([0-9a-f]{{12}})-[0-9a-f]{{8}}"',
        following,
    )
    if checkout is None or not following.endswith(f'"$DESTDIR{build.install[-1].destination}"'):
        notes.append(f"{rel}: the hook after build {build.name}'s marker is not its own; kept")
        return None
    pinned = f"{line}\n{following}"
    if spec.source.inline_pin is not None:
        return Recovered(build, index + 2, unpinned=pinned, pinned=pinned)
    return Recovered(build, index + 2, checkout.group(1), unpinned, pinned)


def _from_payload(payload: Mapping[str, Any]) -> Build:
    """The ``Build`` a marker's JSON (:meth:`SourceBuild.marker`) describes."""
    found = payload["source"]
    source: Git | Http
    if found["kind"] == "git":
        source = Git(
            found["url"],
            found["ref"],
            subdir=found.get("subdir"),
            submodules=found.get("submodules", False),
        )
    else:
        source = Http(found["url"], sha256=found.get("sha256"))
    recipe_payload = dict(payload["build"])
    kind = recipe_payload.pop("kind")
    fields: dict[str, Any] = {
        k: tuple(v) if isinstance(v, list) else v for k, v in recipe_payload.items()
    }
    common: dict[str, Any] = {"cache_namespace": payload.get("cache_key")}
    if kind == "script":
        install = tuple(_install(step, None) for step in payload["install"])
        return Build(
            payload["name"],
            source,
            script=fields["script"],
            install=install,
            packages=fields.get("packages", ()),
            env=dict(fields.get("env", {})),
            **common,
        )
    recipe = _RECIPES[kind](**fields)
    install = tuple(_install(step, recipe.artifact) for step in payload["install"])
    return Build(payload["name"], source, using=recipe, install=install, **common)


def _install(step: Mapping[str, Any], artifact: str | None) -> Install:
    mode = None if step.get("mode") is None else int(step["mode"], 8)
    if step["kind"] == "tree":
        return Install(step["path"], step["dest"], mode=None, directory=True)
    if step["kind"] == "artifact":
        if artifact is None:
            raise ValueError("a script build has no artifact install")
        return Install(artifact, step["dest"], mode=mode, artifact=True)
    return Install(step["path"], step["dest"], mode=mode)


def _env_in_order(build: Build, hook: str) -> Build:
    """*build* with its environment in the order *hook* exports it (the marker sorts it)."""

    def position(key: str) -> int:
        found = hook.find(f"{key}=")
        return len(hook) if found < 0 else found

    if build.using is not None:
        env = dict(sorted(build.using.env.items(), key=lambda kv: position(kv[0])))
        return replace(build, using=replace(build.using, env=env))
    return replace(build, env=dict(sorted(build.env.items(), key=lambda kv: position(kv[0]))))


# ── nethermind-v1: the hook itself ───────────────────────────────────────


def _historical(lines: Sequence[str], index: int) -> Recovered | None:
    line = lines[index]
    match = _HISTORICAL.match(line)
    if match is None:
        return None
    try:
        build = _parse_historical(line, match)
    except (ValueError, ValidationError):
        return None
    if build is None or source_build(build, mark_unpinned=False).render() != line:
        return None
    lost = "the recipe kind is lost (a Go, Cargo or Dotnet recipe comes back as script=)"
    if isinstance(build.source, Git) and COMMIT_PATTERN.fullmatch(build.source.ref):
        lost += f"; the ref it was locked from is lost (pinned to {build.source.ref[:12]})"
    return Recovered(build, index + 1, lost=lost)


def _parse_historical(line: str, match: re.Match[str]) -> Build | None:
    fetch, key = match["fetch"], match["key"]
    target = re.search(r'"\$BUILDROOT/build/([^"/]+)"', fetch)
    if target is None:
        return None
    name = target.group(1)
    source = _historical_source(fetch)
    if source is None:
        return None
    inner = match["inner"].replace("'\\''", "'")
    env_match = re.match(r"export (.*?) && (?=cd /build/)", inner)
    env: dict[str, str] = {}
    if env_match:
        pairs = [word.partition("=") for word in shlex.split(env_match[1])]
        env = {k: v for k, _, v in pairs}
        if len(env) != len(pairs):
            return None
        inner = inner[env_match.end() :]
    cd = re.match(rf"cd /build/{re.escape(name)}(/[^ ]+)? && ", inner)
    if cd is None:
        return None
    if cd.group(1):
        if not isinstance(source, Git):
            return None
        source = replace(source, subdir=cd.group(1).lstrip("/"))
    workdir = f"{name}{cd.group(1) or ''}/"
    stored = [(mode, path) for mode, path in _STORED.findall(line) if path.startswith(workdir)]
    dests = _RESTORED.findall(line.rpartition("; fi && ")[2])
    if not stored or len(stored) != len(dests):
        return None
    install = tuple(
        Install(path.removeprefix(workdir), f"/{dest}", mode=None, directory=True)
        if not mode
        else Install(path.removeprefix(workdir), f"/{dest}", mode=int(mode, 8))
        for (mode, path), dest in zip(stored, dests, strict=True)
    )
    build = Build(name, source, script=inner[cd.end() :], install=install, env=env)
    spec = source_build(build, mark_unpinned=False)
    pin = spec.source.inline_pin
    if key != spec._historical_key(pin):
        cache_key = key.removesuffix(f"-{pin[:12]}") if pin and key.endswith(pin[:12]) else key
        build = replace(build, cache_namespace=cache_key)
    return build


def _historical_source(fetch: str) -> Git | Http | None:
    clone = re.match(r"git clone --depth=1 (--recurse-submodules --shallow-submodules )?-b ", fetch)
    if clone:
        ref, url = shlex.split(fetch[clone.end() :])[:2]
        return Git(url, ref, submodules=bool(clone.group(1)))
    pinned = re.search(r"fetch -q --depth=1 (\S+) ([0-9a-f]{40}) && ", fetch)
    if fetch.startswith("git init -q ") and pinned:
        url = shlex.split(pinned.group(1))[0]
        return Git(url, pinned.group(2), submodules="submodule update" in fetch)
    download = re.search(r"curl -fsSL (\S+) -o ", fetch)
    if download:
        digest = re.search(r'echo "([0-9a-f]{64})  "', fetch)
        return Http(shlex.split(download.group(1))[0], sha256=digest.group(1) if digest else None)
    return None
