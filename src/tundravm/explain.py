"""Dry-run description of what a lowered recipe will produce.

``describe()`` returns a JSON-serializable, deterministically ordered dict for
one variant without compiling or baking; ``render()`` turns that dict into a
compact plain-text summary and ``render_markdown()`` into a review-friendly
Markdown section (tables per kind, long package lists collapsed).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import posixpath
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, get_args

from ._options import MkosiOptions
from .compiler import PHASE_ORDER
from .declarative.model import (
    Declaration,
    Directory,
    File,
    Hook,
    Init,
    Package,
    Recipe,
    Resolved,
    Service,
    Template,
    Unit,
)
from .declarative.resolve import COMMON_LEVEL, Identity, Origin, identity
from .declarative.resolve import describe as describe_identity
from .errors import ValidationError
from .formats import md_cell, md_table
from .lockfile import LockedFetch
from .models import InitScriptEntry, Kernel, ProfileState, UnitAction, unit_name

if TYPE_CHECKING:
    from .declarative._lowered import Lowered

PREVIEW_WIDTH = 80
SHORT_DIGEST_LEN = 12
MARKDOWN_COLLAPSE_AT = 20
"""Markdown tables with more rows than this (usually packages) render collapsed."""


_IMAGE_PARENT: Any = object()
"""``describe(parent=...)`` default: the profile the lowered image builds the variant on."""


def describe(
    image: Lowered,
    *,
    profile: str | None = None,
    parent: str | None = _IMAGE_PARENT,
    fragments: Sequence[str] = (),
) -> dict[str, object]:
    """Describe what *image* will produce for variant *profile* (default: the active one).

    Keys are sorted; lists are sorted by their natural identity (path, name,
    ...) except hooks, which keep registration order within each phase.
    File and init-script contents are summarized as short sha256 digests.
    A variant built on another is described as built: merged over it. Hooks and
    sources show the pins of the lockfile *image* reads, as compile renders them.
    *parent* and *fragments* are the recipe's ``Variant.parent`` and the
    fragment names the variant resolved to.
    """
    selected = image.profile(profile)
    pins = image.build_pins()
    state = image.pinned_state(pins)
    profile_state = state.effective_profile(selected)
    kernel = image.kernel_for(selected)
    kernel_source = image.kernel_source(selected)
    kernel_pin = None if kernel_source is None else kernel_source.pin_from(pins)
    return {
        "arch": state.arch,
        "base": state.base,
        "build_options": _describe_build_options(image.mkosi_for(selected)),
        "build_packages": sorted(profile_state.build_packages),
        "build_source_mounts": [
            {"dest": dest, "src": src} for src, dest in sorted(profile_state.build_sources)
        ],
        "debloat": image.explain_debloat(profile=selected),
        "files": _describe_files(profile_state.files),
        "fragments": list(fragments),
        "groups": [
            {"gid": g.gid, "name": g.name, "system": g.system}
            for g in sorted(profile_state.groups, key=lambda item: item.name)
        ],
        "hooks": _describe_hooks(profile_state),
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
        "mirror": image.mirror,
        "packages": sorted(profile_state.packages),
        "parent": profile_state.extends if parent is _IMAGE_PARENT else parent,
        "partitions": [
            {"fs": p.fs, "mount_at": p.mount_at, "name": p.name, "size": p.size}
            for p in sorted(profile_state.partitions, key=lambda item: item.name)
        ],
        "policy": {f.name: getattr(image.policy, f.name) for f in fields(image.policy)},
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
        "reproducible": image.reproducible,
        "runtime_init": _describe_init_scripts(profile_state.init_scripts),
        "secrets": [
            {
                "name": secret.name,
                "required": secret.required,
                "targets": [{"kind": t.kind, "location": t.location} for t in secret.targets],
            }
            for secret in sorted(profile_state.secrets, key=lambda item: item.name)
        ],
        "services": [
            {
                "after": list(svc.after),
                "command": list(svc.command),
                "enabled": svc.enabled,
                "name": svc.name,
                "requires": list(svc.requires),
                "restart": svc.restart,
                "security_profile": svc.security_profile,
                "user": svc.user,
                "wants": list(svc.wants),
                **svc.extras(),
            }
            for svc in sorted(profile_state.services, key=lambda item: item.name)
        ],
        "skeleton_files": _describe_files(profile_state.skeleton_files),
        "sources": _describe_sources(image, selected, profile_state, pins),
        "targets": list(profile_state.output_targets),
        "units": _describe_units(profile_state),
        "templates": [
            {
                "mode": tmpl.mode,
                "path": tmpl.path,
                "variables": sorted(tmpl.variables),
            }
            for tmpl in sorted(profile_state.templates, key=lambda item: item.path)
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
        ],
        "variant": selected,
    }


def render(description: dict[str, object]) -> str:
    """Render a ``describe()`` result as a compact plain-text summary."""
    lines: list[str] = [
        f"Image: {description['base']} ({description['arch']})"
        f"  variant={description['variant']}"
        f"  reproducible={_yes_no(description['reproducible'])}"
    ]
    if description.get("mirror"):
        lines.append(f"Mirror: {description['mirror']}")
    kernel = description.get("kernel")
    if isinstance(kernel, dict):
        lines.append(_render_kernel(cast(dict[str, Any], kernel)))
    policy = _as_dict(description.get("policy"))
    if policy:
        lines.append("Policy: " + " ".join(f"{k}={_fmt(v)}" for k, v in sorted(policy.items())))
    build_options = _as_dict(description.get("build_options"))
    if build_options:
        lines.append(
            "Build options: " + " ".join(f"{k}={_fmt(v)}" for k, v in build_options.items())
        )
    if description.get("parent"):
        lines.append(f"Parent: {description['parent']}")
    _append_inline_list(lines, "Fragments", _as_list(description.get("fragments")))

    _append_inline_list(lines, "Packages", _as_list(description.get("packages")))
    _append_inline_list(lines, "Build packages", _as_list(description.get("build_packages")))

    sources = _as_list(description.get("sources"))
    if sources:
        lines.append(f"Sources ({len(sources)}):")
        width = _column_width(sources, "name")
        for source in sources:
            ref = f"  ref={source['ref']}" if source.get("ref") else ""
            lines.append(
                f"  {source['name']:<{width}}  {source['kind']} {source['url']}{ref}"
                f"  pinned={source['pinned'] or '-'}"
            )

    repositories = _as_list(description.get("repositories"))
    if repositories:
        lines.append(f"Repositories ({len(repositories)}):")
        for repo in repositories:
            parts = [str(repo["name"]), str(repo["url"])]
            if repo.get("suite"):
                parts.append(str(repo["suite"]))
            if repo.get("components"):
                parts.append(",".join(repo["components"]))
            parts.append(f"prio={repo['priority']}")
            if repo.get("in_image") is False:
                parts.append("build only")
            lines.append("  " + "  ".join(parts))

    _append_files(lines, "Files", _as_list(description.get("files")))
    _append_files(lines, "Skeleton files", _as_list(description.get("skeleton_files")))

    templates = _as_list(description.get("templates"))
    if templates:
        lines.append(f"Templates ({len(templates)}):")
        width = _column_width(templates, "path")
        for tmpl in templates:
            variables = ",".join(tmpl["variables"]) or "-"
            lines.append(f"  {tmpl['path']:<{width}}  {tmpl['mode']}  vars={variables}")

    groups = _as_list(description.get("groups"))
    if groups:
        lines.append(f"Groups ({len(groups)}):")
        for group in groups:
            parts = [str(group["name"])]
            if group.get("system"):
                parts.append("system")
            if group.get("gid") is not None:
                parts.append(f"gid={group['gid']}")
            lines.append("  " + "  ".join(parts))

    users = _as_list(description.get("users"))
    if users:
        lines.append(f"Users ({len(users)}):")
        for user in users:
            parts = [str(user["name"])]
            if user.get("system"):
                parts.append("system")
            for key in ("uid", "gid", "home", "shell"):
                if user.get(key) is not None:
                    parts.append(f"{key}={user[key]}")
            if user.get("groups"):
                parts.append("groups=" + ",".join(user["groups"]))
            lines.append("  " + "  ".join(parts))

    units = _as_dict(description.get("units"))
    states = [f"{action} {', '.join(names)}" for action, names in units.items() if names]
    if states:
        lines.append("Units: " + "; ".join(states))

    # Command-less services only enable a shipped unit; "Units:" lists them.
    services = [s for s in _as_list(description.get("services")) if s.get("command")]
    if services:
        lines.append(f"Services ({len(services)}):")
        width = _column_width(services, "name")
        for svc in services:
            parts = [f"{svc['name']:<{width}}"]
            if svc.get("command"):
                parts.append(_truncate(" ".join(svc["command"])))
            parts.append(f"restart={svc['restart']}")
            if svc.get("type"):
                parts.append(f"type={svc['type']}")
            if svc.get("user"):
                parts.append(f"user={svc['user']}")
            if svc.get("group"):
                parts.append(f"group={svc['group']}")
            if svc.get("working_dir"):
                parts.append(f"cwd={svc['working_dir']}")
            if svc.get("env"):
                parts.append("env=" + ",".join(svc["env"]))
            if svc.get("env_file"):
                parts.append(f"env_file={svc['env_file']}")
            for key in ("after", "requires", "wants"):
                if svc.get(key):
                    parts.append(f"{key}=" + ",".join(svc[key]))
            if svc.get("limits"):
                parts.append(
                    "limits=" + ",".join(f"{k}={v}" for k, v in sorted(svc["limits"].items()))
                )
            if svc.get("wanted_by"):
                parts.append(f"wanted_by={svc['wanted_by']}")
            if not svc.get("enabled", True):
                parts.append("disabled")
            if svc.get("security_profile", "default") != "default":
                parts.append(f"security={svc['security_profile']}")
            lines.append("  " + "  ".join(parts))

    partitions = _as_list(description.get("partitions"))
    if partitions:
        lines.append(f"Partitions ({len(partitions)}):")
        for part in partitions:
            lines.append(f"  {part['name']}  {part['size']}  {part['mount_at']}  {part['fs']}")

    secrets = _as_list(description.get("secrets"))
    if secrets:
        lines.append(f"Secrets ({len(secrets)}):")
        for secret in secrets:
            targets = " ".join(f"{t['kind']}:{t['location']}" for t in secret["targets"])
            flag = "required" if secret["required"] else "optional"
            lines.append("  " + "  ".join(p for p in (secret["name"], flag, targets) if p))

    hooks = _as_dict(description.get("hooks"))
    if hooks:
        lines.append("Hooks:")
        for phase, previews in hooks.items():
            lines.append(f"  {phase} ({len(previews)}):")
            lines.extend(f"    {preview}" for preview in previews)

    runtime_init = _as_dict(description.get("runtime_init"))
    if runtime_init.get("count"):
        priorities = ", ".join(str(p) for p in runtime_init["priorities"])
        lines.append(f"Runtime init: {runtime_init['count']} (priorities: {priorities})")

    debloat = _as_dict(description.get("debloat"))
    if debloat:
        if debloat.get("enabled"):
            lines.append(
                f"Debloat: enabled, {len(debloat.get('paths_remove', []))} paths removed,"
                f" systemd minimize={_yes_no(debloat.get('systemd_minimize'))}"
            )
        else:
            lines.append("Debloat: disabled")

    output = _as_list(description.get("targets"))
    if output:
        lines.append("Targets: " + " ".join(str(t) for t in output))
    return "\n".join(lines) + "\n"


def render_markdown(description: dict[str, object]) -> str:
    """Render a ``describe()`` result as one Markdown section for code review.

    A ``## Variant`` heading and an image line, then a ``Parent``/``Fragments`` line,
    then one table per non-empty kind: packages, files, users, services, units,
    hooks, sources and runtime init. A table longer than ``MARKDOWN_COLLAPSE_AT``
    rows is collapsed in a ``<details>`` block.
    """
    targets = " ".join(f"`{t}`" for t in _as_list(description.get("targets"))) or "none"
    blocks: list[str] = [
        f"## Variant `{description['variant']}`",
        f"`{description['base']}` ({description['arch']}) · "
        f"reproducible: {_yes_no(description['reproducible'])} · targets: {targets}",
        _markdown_lineage(description),
    ]
    packages = _as_list(description.get("packages"))
    build_packages = _as_list(description.get("build_packages"))
    package_rows = [(md_cell(name, code=True), "image") for name in packages]
    package_rows += [(md_cell(name, code=True), "build") for name in build_packages]
    _append_section(blocks, "Packages", ("Package", "Installed in"), package_rows)

    files = _as_list(description.get("files")) + _as_list(description.get("skeleton_files"))
    _append_section(
        blocks,
        "Files",
        ("Path", "Mode", "Bytes", "sha256"),
        [
            (md_cell(f["path"], code=True), md_cell(f["mode"]), str(f["bytes"]), f"`{f['sha256']}`")
            for f in files
        ],
    )
    _append_section(
        blocks,
        "Users",
        ("User", "System", "UID", "GID", "Home", "Shell", "Groups"),
        [
            (
                md_cell(u["name"], code=True),
                _yes_no(u.get("system")),
                md_cell(u.get("uid")),
                md_cell(u.get("gid")),
                md_cell(u.get("home"), code=True),
                md_cell(u.get("shell"), code=True),
                md_cell(", ".join(u.get("groups") or ())),
            )
            for u in _as_list(description.get("users"))
        ],
    )
    services = [s for s in _as_list(description.get("services")) if s.get("command")]
    _append_section(
        blocks,
        "Services",
        ("Service", "Command", "User", "Restart", "Enabled"),
        [
            (
                md_cell(svc["name"], code=True),
                md_cell(_truncate(" ".join(svc["command"])), code=True),
                md_cell(svc.get("user")),
                md_cell(svc.get("restart")),
                _yes_no(svc.get("enabled", True)),
            )
            for svc in services
        ],
    )
    units = _as_dict(description.get("units"))
    _append_section(
        blocks,
        "Units",
        ("Action", "Units"),
        [
            (action, ", ".join(md_cell(name, code=True) for name in names))
            for action, names in units.items()
            if names
        ],
    )
    hooks = _as_dict(description.get("hooks"))
    _append_section(
        blocks,
        "Hooks",
        ("Phase", "Command"),
        [
            (md_cell(phase), md_cell(preview, code=True))
            for phase, previews in hooks.items()
            for preview in previews
        ],
    )
    _append_section(
        blocks,
        "Sources",
        ("Source", "Kind", "URL", "Ref", "Pinned", "Build"),
        [
            (
                md_cell(src["name"], code=True),
                md_cell(src["kind"]),
                md_cell(src["url"]),
                md_cell(src.get("ref"), code=True),
                md_cell(src.get("pinned"), code=True),
                md_cell(src.get("build")),
            )
            for src in _as_list(description.get("sources"))
        ],
    )
    runtime_init = _as_dict(description.get("runtime_init"))
    priorities = _as_list(runtime_init.get("priorities"))
    _append_section(
        blocks,
        "Runtime init",
        ("Priority", "Scripts"),
        [
            (str(priority), str(priorities.count(priority)))
            for priority in dict.fromkeys(priorities)
        ],
        count=len(priorities),
    )
    return "\n\n".join(blocks) + "\n"


def _markdown_lineage(description: dict[str, object]) -> str:
    fragments = ", ".join(f"`{f}`" for f in _as_list(description.get("fragments"))) or "none"
    parent = description.get("parent")
    shown = f"`{parent}`" if parent else "none"
    return f"**Parent:** {shown} · **Fragments:** {fragments}"


def _append_section(
    blocks: list[str],
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    count: int | None = None,
) -> None:
    if not rows:
        return
    heading = f"{title} ({len(rows) if count is None else count})"
    table = md_table(headers, rows)
    if len(rows) > MARKDOWN_COLLAPSE_AT:
        blocks.append(f"<details><summary>{heading}</summary>\n\n{table}\n\n</details>")
    else:
        blocks.append(f"### {heading}\n\n{table}")


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


def _describe_hooks(profile_state: ProfileState) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for phase in PHASE_ORDER:
        previews = [
            _truncate(_first_command_line(" ".join(hook.command.argv)))
            for hook in profile_state.hooks
            if hook.phase == phase
        ]
        if previews:
            grouped[phase] = previews
    return grouped


def _describe_units(profile_state: ProfileState) -> dict[str, list[str]]:
    """Unit state by action: ``enable`` lists command-less enabled services."""
    enabled = [unit_name(s.name) for s in profile_state.services if s.enabled and not s.command]
    units: dict[str, list[str]] = {"enable": list(dict.fromkeys(enabled))}
    for action in get_args(UnitAction):
        units[action] = list(
            dict.fromkeys(s.unit for s in profile_state.unit_states if s.action == action)
        )
    return units


def _first_command_line(script: str) -> str:
    """First line of *script* that is not blank or a comment (falls back to the first)."""
    lines = [line.strip() for line in script.splitlines()]
    for line in lines:
        if line and not line.startswith("#"):
            return line
    return next((line for line in lines if line), "")


def _describe_init_scripts(entries: Sequence[InitScriptEntry]) -> dict[str, object]:
    unique = {(entry.priority, entry.script) for entry in entries}
    return {
        "count": len(unique),
        "priorities": sorted(priority for priority, _script in unique),
    }


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


def _render_kernel(kernel: dict[str, Any]) -> str:
    parts = [f"Kernel: {kernel.get('version') or kernel.get('config_file') or 'custom'}"]
    if kernel.get("tdx"):
        parts.append("tdx=yes")
    if kernel.get("cmdline"):
        parts.append(f"cmdline={kernel['cmdline']}")
    if kernel.get("config_file"):
        parts.append(f"pinned={kernel.get('pinned') or '-'}")
    return "  ".join(parts)


def _append_inline_list(lines: list[str], label: str, items: list[Any]) -> None:
    if items:
        lines.append(f"{label} ({len(items)}): " + " ".join(str(item) for item in items))


def _append_files(lines: list[str], label: str, files: list[Any]) -> None:
    if not files:
        return
    lines.append(f"{label} ({len(files)}):")
    width = _column_width(files, "path")
    for entry in files:
        if "symlink" in entry:
            what = f"-> {entry['symlink']}"
        elif entry.get("directory"):
            what = "directory"
        else:
            what = f"{entry['bytes']}B  sha256:{entry['sha256']}"
        lines.append(f"  {entry['path']:<{width}}  {entry['mode']}  {what}")


def _column_width(rows: list[Any], key: str) -> int:
    return max((len(str(row[key])) for row in rows), default=0)


def _truncate(text: str, width: int = PREVIEW_WIDTH) -> str:
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


def _as_list(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []


def _as_dict(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


@dataclass(frozen=True, slots=True)
class VariantDiff:
    """Declarations of variant *b* against variant *a*, matched by ``identity()``."""

    a: str
    b: str
    targets: tuple[tuple[str, ...], tuple[str, ...]]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[tuple[str, tuple[str, ...]], ...]
    """``(declaration, names of the fields that differ)`` per changed declaration."""

    @property
    def is_empty(self) -> bool:
        same_targets = self.targets[0] == self.targets[1]
        return same_targets and not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict[str, object]:
        return {
            "a": self.a,
            "b": self.b,
            "targets": {self.a: list(self.targets[0]), self.b: list(self.targets[1])},
            "added": list(self.added),
            "removed": list(self.removed),
            "changed": [{"declaration": d, "fields": list(f)} for d, f in self.changed],
        }


def diff_variants(a: Resolved, b: Resolved) -> VariantDiff:
    """Which declarations *b* adds, drops or changes relative to *a* (both resolved)."""
    left = {identity(item): item for item in a.items}
    right = {identity(item): item for item in b.items}
    return VariantDiff(
        a=a.variant,
        b=b.variant,
        targets=(tuple(a.targets), tuple(b.targets)),
        added=tuple(describe_identity(key) for key in right if key not in left),
        removed=tuple(describe_identity(key) for key in left if key not in right),
        changed=tuple(
            (describe_identity(key), _changed_fields(item, right[key]))
            for key, item in left.items()
            if key in right and item != right[key]
        ),
    )


def _changed_fields(old: Declaration, new: Declaration) -> tuple[str, ...]:
    return tuple(f.name for f in fields(old) if getattr(old, f.name) != getattr(new, f.name))


def render_variant_diff(diff: VariantDiff) -> str:
    """Plain text: a count line, then ``+``/``-``/``~`` lines per declaration."""
    counts = f"{len(diff.added)} added, {len(diff.removed)} removed, {len(diff.changed)} changed"
    lines = [f"variants {diff.a} -> {diff.b}: {counts}"]
    if diff.targets[0] != diff.targets[1]:
        lines.append(f"  target: {', '.join(diff.targets[0])} -> {', '.join(diff.targets[1])}")
    lines.extend(f"  + {item}" for item in diff.added)
    lines.extend(f"  - {item}" for item in diff.removed)
    lines.extend(f"  ~ {item}: {', '.join(changed)}" for item, changed in diff.changed)
    return "\n".join(lines)


def render_variant_diff_markdown(diff: VariantDiff, *, recipe: str) -> str:
    """A Markdown section: target line when it differs, then one table row per change."""
    lines = [f"# tundravm: `{recipe}` variants `{diff.a}` → `{diff.b}`", ""]
    if diff.targets[0] != diff.targets[1]:
        old, new = (", ".join(t) for t in diff.targets)
        lines += [f"Target: {md_cell(old, code=True)} → {md_cell(new, code=True)}", ""]
    rows = [
        *(("added", md_cell(item, code=True), md_cell(None)) for item in diff.added),
        *(("removed", md_cell(item, code=True), md_cell(None)) for item in diff.removed),
        *(
            ("changed", md_cell(item, code=True), md_cell(", ".join(changed)))
            for item, changed in diff.changed
        ),
    ]
    lines.append(
        md_table(("Change", "Declaration", "Fields"), rows) if rows else "No declaration differs."
    )
    return "\n".join(lines)


UNIT_DIRS = ("/usr/lib/systemd/system/", "/etc/systemd/system/")
STAGE_DIRS = ("mkosi.extra", "mkosi.skeleton")
WHY_PREFIXES = ("unit", "package", "hook", "init")
"""``KIND:NAME`` subject prefixes :func:`explain_why` accepts besides absolute paths."""


@dataclass(frozen=True, slots=True)
class WhyDeclaration:
    """One declaration behind a subject, with the resolution steps that touched it."""

    declaration: str
    """``identity()`` rendered: ``Service(app.service)``, ``File(extra, /etc/app.conf)``."""
    type: str
    key: tuple[str, ...]
    origins: tuple[Origin, ...]
    present: bool
    """False when a variant removed it: it is explained but nothing is emitted."""

    def to_dict(self) -> dict[str, object]:
        return {
            "declaration": self.declaration,
            "type": self.type,
            "key": list(self.key),
            "present": self.present,
            "origins": [
                {
                    "action": origin.action,
                    "variant": origin.variant,
                    "fragments": [{"name": name, "class": kind} for name, kind in origin.fragments],
                }
                for origin in self.origins
            ],
        }


@dataclass(frozen=True, slots=True)
class Why:
    """Why an emitted object exists in one variant: :func:`explain_why`'s answer.

    ``files`` are the compiled tree's files that hold it and ``generated`` the
    lines the compiler added on its behalf, both relative to the variant's
    directory in the tree (``mkosi.extra/...``, ``scripts/06-postinst.sh: ...``).
    """

    variant: str
    subject: str
    declarations: tuple[WhyDeclaration, ...]
    fragments: tuple[str, ...]
    """Every fragment the variant resolved, in expansion order (``Resolved.fragments``)."""
    files: tuple[str, ...]
    generated: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "variant": self.variant,
            "subject": self.subject,
            "declarations": [entry.to_dict() for entry in self.declarations],
            "fragments": list(self.fragments),
            "files": list(self.files),
            "generated": list(self.generated),
        }


def explain_why(
    recipe: Recipe, variant: str, subject: str, *, lowered: Lowered | None = None
) -> Why:
    """Which declarations produced *subject* in *variant*, and where it lands.

    *subject* is an absolute image path (``/usr/lib/systemd/system/app.service``)
    or ``unit:NAME``, ``package:NAME``, ``hook:NAME``, ``init:NAME``; a bare
    name is tried as each of those. The variant is compiled into a scratch
    directory to find the destination files and generated lines. *lowered*
    replaces ``lower(recipe)`` (the CLI passes the recipe file's, with its
    lockfile). An unknown subject raises ``ValidationError`` with close matches.
    """
    from .declarative._compile import emit
    from .declarative.lower import lower
    from .declarative.resolve import provenance, resolve

    recipe.variant(variant)
    resolved = resolve(recipe, variant=variant)
    trace = provenance(recipe, variant=variant)
    img = lowered if lowered is not None else lower(recipe)
    with tempfile.TemporaryDirectory(prefix="tundravm-why-") as scratch:
        root = Path(scratch)
        emit(img.select((variant,)), root)
        tree = _read_tree(root / variant if (root / variant).is_dir() else root)
    items = {identity(item): item for item in resolved.items}
    keys = _match_subject(subject, items, trace, tree)
    declarations = tuple(
        WhyDeclaration(
            declaration=describe_identity(key),
            type=key[0],
            key=tuple(key[1:]),
            origins=trace.get(key, ()),
            present=key in items,
        )
        for key in keys
    )
    files: list[str] = []
    generated: list[str] = []
    if subject.startswith("/"):
        files.extend(_stage_paths(tree, subject))
    for key in keys:
        item = items.get(key)
        if item is None:
            continue
        found, extra = _emitted_for(item, tree)
        files.extend(found)
        generated.extend(extra)
    return Why(
        variant=variant,
        subject=subject,
        declarations=declarations,
        fragments=resolved.fragments,
        files=tuple(dict.fromkeys(files)),
        generated=tuple(dict.fromkeys(generated)),
    )


def _read_tree(root: Path) -> dict[str, str]:
    """Every file under *root* (relative POSIX path -> text; a symlink is ``-> target``)."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            tree[rel] = f"-> {os.readlink(path)}"
        elif path.is_file():
            tree[rel] = path.read_bytes().decode("utf-8", errors="replace")
    return tree


def _image_paths(tree: Mapping[str, str]) -> dict[str, str]:
    """Image path (``/etc/app.conf``) -> tree path, for the files the stage trees hold."""
    found: dict[str, str] = {}
    for rel in tree:
        stage, _, rest = rel.partition("/")
        if stage in STAGE_DIRS and rest:
            found.setdefault(f"/{rest}", rel)
    return found


def _stage_paths(tree: Mapping[str, str], path: str) -> list[str]:
    norm = posixpath.normpath(path)
    return [rel for rel in tree if any(rel == f"{stage}{norm}" for stage in STAGE_DIRS)] or [
        rel for rel in tree if any(rel.startswith(f"{stage}{norm}/") for stage in STAGE_DIRS)
    ]


def _match_subject(
    subject: str,
    items: Mapping[Identity, Declaration],
    trace: Mapping[Identity, tuple[Origin, ...]],
    tree: Mapping[str, str],
) -> tuple[Identity, ...]:
    """The identities *subject* names; ``ValidationError`` with close matches when none."""
    known = list(dict.fromkeys([*items, *trace]))
    prefix, sep, name = subject.partition(":")
    if subject.startswith("/"):
        keys = _path_keys(posixpath.normpath(subject), known)
        if keys or posixpath.normpath(subject) in _image_paths(tree):
            return keys
    elif sep and prefix in WHY_PREFIXES:
        keys = _named_keys(prefix, name, known)
        if keys:
            return keys
    else:
        keys = tuple(k for kind in WHY_PREFIXES for k in _named_keys(kind, subject, known))
        if keys:
            return keys
    raise ValidationError(
        f"Nothing in the variant matches {subject!r}.",
        hint=_close_hint(subject, known, tree),
    )


def _named_keys(kind: str, name: str, known: Sequence[Identity]) -> tuple[Identity, ...]:
    if kind == "unit":
        unit = unit_name(name)
        return tuple(k for k in known if k[0] in ("Unit", "Service") and k[1] == unit)
    wanted = {"package": "Package", "hook": "Hook", "init": "Init"}[kind]
    return tuple(k for k in known if k[0] == wanted and k[1] == name)


def _path_keys(path: str, known: Sequence[Identity]) -> tuple[Identity, ...]:
    """Declarations that emit *path*: files at it, a directory holding it, or a unit file."""
    keys: list[Identity] = []
    for key in known:
        kind = key[0]
        if kind in ("File", "Template") and key[2] == path:
            keys.append(key)
        elif kind == "Directory" and (path == key[2] or path.startswith(f"{key[2]}/")):
            keys.append(key)
        elif kind in ("Unit", "Service"):
            unit = key[1]
            if any(
                path in (f"{d}{unit}", f"{d}{unit}.d") or path.startswith(f"{d}{unit}.d/")
                for d in UNIT_DIRS
            ):
                keys.append(key)
    if not keys and "runtime-init" in path:
        keys.extend(k for k in known if k[0] in ("Init", "Key", "Disk", "Secrets"))
    return tuple(keys)


def _close_hint(subject: str, known: Sequence[Identity], tree: Mapping[str, str]) -> str:
    names = [
        *sorted(_image_paths(tree)),
        *(f"unit:{k[1]}" for k in known if k[0] in ("Unit", "Service")),
        *(f"{k[0].lower()}:{k[1]}" for k in known if k[0] in ("Package", "Hook", "Init")),
    ]
    candidates = list(dict.fromkeys(names))
    close = difflib.get_close_matches(subject, candidates, n=5, cutoff=0.5)
    if not close:
        _, _, tail = subject.rpartition("/")
        close = [c for c in candidates if tail and tail in c][:5]
    shown = ", ".join(close) if close else ", ".join(candidates[:5])
    lead = "Close matches" if close else "Emitted objects include"
    return (
        f"{lead}: {shown}. Pass an absolute image path or unit:NAME, package:NAME, "
        "hook:NAME, init:NAME."
    )


def _emitted_for(item: Declaration, tree: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """The tree files holding *item* and the lines the compiler generated for it."""
    files: list[str] = []
    generated: list[str] = []
    match item:
        case File() | Template() | Directory():
            norm = posixpath.normpath(item.path)
            prefix = f"mkosi.{item.stage}{norm}"
            files.extend(rel for rel in tree if rel == prefix or rel.startswith(f"{prefix}/"))
        case Unit() | Service():
            unit = unit_name(item.name)
            for rel, text in tree.items():
                stage, _, rest = rel.partition("/")
                if stage not in STAGE_DIRS:
                    continue
                if posixpath.basename(rel) == unit:
                    files.append(rel)
                    generated.extend(_injected_lines(item, text))
                elif f"/{unit}.d/" in f"/{rest}":
                    files.append(rel)
                    generated.append(f"drop-in {rel}")
            generated.extend(_script_lines(tree, unit))
        case Package():
            files.extend(_conf_lines(tree, item.name))
        case Hook() | Init():
            first = _first_command_line(item.script)
            name = item.name
            files.extend(
                rel
                for rel, text in tree.items()
                if (first and first in text)
                or posixpath.basename(rel).removesuffix(".sh") in (name, f"init-{name}")
            )
    return files, generated


def _injected_lines(item: Unit | Service, text: str) -> list[str]:
    """``After=``/``Requires=`` lines naming runtime-init that the declaration did not write."""
    declared = item.content if isinstance(item, Unit) and isinstance(item.content, str) else ""
    if isinstance(item, Service):
        declared = " ".join((*item.after, *item.requires))
    if "runtime-init.service" in declared or not item.after_init:
        return []
    return [
        f"{line.strip()} (after_init=True)"
        for line in text.splitlines()
        if line.split("=", 1)[0] in ("After", "Requires") and "runtime-init.service" in line
    ]


def _script_lines(tree: Mapping[str, str], unit: str) -> list[str]:
    """Enablement the scripts generate for *unit*: ``systemctl`` calls and ``.wants`` links."""
    word = re.compile(rf"(?<![\w.@-]){re.escape(unit)}(?![\w.@-])")
    lines: list[str] = []
    for rel, text in tree.items():
        if not rel.startswith("scripts/"):
            continue
        for line in text.splitlines():
            if not word.search(line):
                continue
            if "systemctl" in line:
                lines.append(f"{rel}: {line.strip()}")
            elif ".wants" in line:
                target = re.search(r"([\w.@-]+\.wants)", line)
                link = target.group(1) if target else "wants"
                lines.append(f"{rel}: {link}/{unit} link")
    return lines


def _conf_lines(tree: Mapping[str, str], package: str) -> list[str]:
    """``mkosi.conf (Packages=)`` for each config list that names *package*."""
    found: list[str] = []
    for rel, text in tree.items():
        if posixpath.basename(rel) != "mkosi.conf":
            continue
        key = ""
        for line in text.splitlines():
            if line and not line[0].isspace():
                key, _, value = line.partition("=")
            else:
                value = line
            if package in value.replace(",", " ").split():
                found.append(f"{rel} ({key}=)")
    return list(dict.fromkeys(found))


def _fragment_chain(origin: Origin) -> str:
    return " > ".join(
        name if kind in ("Fragment", "") else f"{name} ({kind})" for name, kind in origin.fragments
    )


def _origin_text(origin: Origin) -> str:
    where = "in common" if origin.variant == COMMON_LEVEL else f"in variant {origin.variant}"
    chain = _fragment_chain(origin)
    return f"{origin.action} {where}" + (f" via {chain}" if chain else "")


def render_why(why: Why) -> str:
    """Plain text: declarations with their history, destination files, generated lines."""
    lines = [f"why {why.subject} (variant {why.variant})"]
    if not why.declarations:
        lines.append("  generated by the compiler; no declaration names it")
    for entry in why.declarations:
        state = "" if entry.present else " (removed: not emitted)"
        lines.append(f"  {entry.declaration}{state}")
        lines.extend(f"    {_origin_text(origin)}" for origin in entry.origins)
    lines.append(f"files (in {why.variant}/):")
    lines.extend(f"  {path}" for path in why.files)
    if not why.files:
        lines.append("  (none)")
    if why.generated:
        lines.append("generated:")
        lines.extend(f"  {line}" for line in why.generated)
    return "\n".join(lines)


def render_why_markdown(why: Why) -> str:
    """A Markdown section: one table of declarations and origins, then files and generated."""
    lines = [f"# tundravm: why {md_cell(why.subject, code=True)} in `{why.variant}`", ""]
    rows = [
        (
            md_cell(entry.declaration, code=True),
            md_cell(origin.action),
            md_cell(origin.variant),
            md_cell(_fragment_chain(origin)),
        )
        for entry in why.declarations
        for origin in entry.origins
    ]
    if rows:
        lines.append(md_table(("Declaration", "Action", "Level", "Fragments"), rows))
    else:
        lines.append("Generated by the compiler; no declaration names it.")
    lines += ["", f"Files (in `{why.variant}/`):", ""]
    lines.extend(f"- {md_cell(path, code=True)}" for path in why.files)
    if why.generated:
        lines += ["", "Generated:", ""]
        lines.extend(f"- {md_cell(line, code=True)}" for line in why.generated)
    return "\n".join(lines)


__all__ = [
    "WHY_PREFIXES",
    "VariantDiff",
    "Why",
    "WhyDeclaration",
    "describe",
    "diff_variants",
    "explain_why",
    "render",
    "render_markdown",
    "render_variant_diff",
    "render_variant_diff_markdown",
    "render_why",
    "render_why_markdown",
]
