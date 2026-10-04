"""Dry-run description of what an :class:`~tundravm.image.Image` recipe will produce.

``describe()`` returns a JSON-serializable, deterministically ordered dict for
one profile without compiling or baking; ``render()`` turns that dict into a
compact plain-text summary and ``render_markdown()`` into a review-friendly
Markdown section (tables per kind, long package lists collapsed).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import TYPE_CHECKING, Any, cast, get_args

from .compiler import PHASE_ORDER
from .formats import md_cell, md_table
from .models import InitScriptEntry, ProfileState, UnitAction, unit_name

if TYPE_CHECKING:
    from .image import Image

PREVIEW_WIDTH = 80
SHORT_DIGEST_LEN = 12
MARKDOWN_COLLAPSE_AT = 20
"""Markdown tables with more rows than this (usually packages) render collapsed."""


def describe(image: Image, *, profile: str | None = None) -> dict[str, object]:
    """Describe what *image* will produce for *profile* (default: the active profile).

    Keys are sorted; lists are sorted by their natural identity (path, name,
    ...) except hooks, which keep registration order within each phase.
    File and init-script contents are summarized as short sha256 digests.
    A profile that extends another is described as built: merged over it.
    """
    selected = image._resolve_operation_profile(profile)
    state = image.state
    profile_state = state.effective_profile(selected)
    extends = profile_state.extends
    kernel = image.kernel
    return {
        "arch": state.arch,
        "base": state.base,
        "build_packages": sorted(profile_state.build_packages),
        "build_sources": [
            {"host_path": host_path, "target": target}
            for host_path, target in sorted(profile_state.build_sources)
        ],
        "debloat": image.explain_debloat(profile=selected),
        "extends": extends,
        "extends_modules": [] if extends is None else _module_names(image, extends),
        "files": _describe_files(profile_state.files),
        "groups": [
            {"gid": g.gid, "name": g.name, "system": g.system}
            for g in sorted(profile_state.groups, key=lambda item: item.name)
        ],
        "hooks": _describe_hooks(profile_state),
        "init_scripts": _describe_init_scripts(profile_state.init_scripts),
        "kernel": None
        if kernel is None
        else {
            "cmdline": kernel.cmdline,
            "config_file": None if kernel.config_file is None else str(kernel.config_file),
            "source_repo": kernel.source_repo,
            "tdx": kernel.tdx,
            "version": kernel.version,
        },
        "mirror": image.mirror,
        "modules": _module_names(image, selected),
        "output_targets": list(profile_state.output_targets),
        "packages": sorted(profile_state.packages),
        "partitions": [
            {"fs": p.fs, "mount": p.mount, "name": p.name, "size": p.size}
            for p in sorted(profile_state.partitions, key=lambda item: item.name)
        ],
        "policy": {f.name: getattr(image.policy, f.name) for f in fields(image.policy)},
        "profile": selected,
        "repositories": [
            {
                "components": list(repo.components),
                "keyring": repo.keyring,
                "name": repo.name,
                "priority": repo.priority,
                "suite": repo.suite,
                "url": repo.url,
            }
            for repo in sorted(
                profile_state.repositories,
                key=lambda item: (item.priority, item.name, item.url),
            )
        ],
        "reproducible": image.reproducible,
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
        "sources": _describe_sources(image, profile_state),
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
    }


def render(description: dict[str, object]) -> str:
    """Render a ``describe()`` result as a compact plain-text summary."""
    lines: list[str] = [
        f"Image: {description['base']} ({description['arch']})"
        f"  profile={description['profile']}"
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
    if description.get("extends"):
        inherited = _as_list(description.get("extends_modules"))
        modules = f" (modules: {', '.join(inherited)})" if inherited else ""
        lines.append(f"Extends: {description['extends']}{modules}")
    _append_inline_list(lines, "Modules", _as_list(description.get("modules")))

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
            lines.append(f"  {part['name']}  {part['size']}  {part['mount']}  {part['fs']}")

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

    init_scripts = _as_dict(description.get("init_scripts"))
    if init_scripts.get("count"):
        priorities = ", ".join(str(p) for p in init_scripts["priorities"])
        lines.append(f"Init scripts: {init_scripts['count']} (priorities: {priorities})")

    debloat = _as_dict(description.get("debloat"))
    if debloat:
        if debloat.get("enabled"):
            lines.append(
                f"Debloat: enabled, {len(debloat.get('paths_remove', []))} paths removed,"
                f" systemd minimize={_yes_no(debloat.get('systemd_minimize'))}"
            )
        else:
            lines.append("Debloat: disabled")

    output_targets = _as_list(description.get("output_targets"))
    if output_targets:
        lines.append("Output targets: " + " ".join(str(t) for t in output_targets))
    return "\n".join(lines) + "\n"


def render_markdown(description: dict[str, object]) -> str:
    """Render a ``describe()`` result as one Markdown section for code review.

    A ``## Profile`` heading and an image line, then an ``Extends``/``Modules`` line,
    then one table per non-empty kind: packages, files, users, services, units,
    hooks, sources and init scripts. A table longer than ``MARKDOWN_COLLAPSE_AT``
    rows is collapsed in a ``<details>`` block.
    """
    targets = " ".join(f"`{t}`" for t in _as_list(description.get("output_targets"))) or "none"
    blocks: list[str] = [
        f"## Profile `{description['profile']}`",
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
    init_scripts = _as_dict(description.get("init_scripts"))
    priorities = _as_list(init_scripts.get("priorities"))
    _append_section(
        blocks,
        "Init scripts",
        ("Priority", "Scripts"),
        [
            (str(priority), str(priorities.count(priority)))
            for priority in dict.fromkeys(priorities)
        ],
        count=len(priorities),
    )
    return "\n\n".join(blocks) + "\n"


def _markdown_lineage(description: dict[str, object]) -> str:
    modules = ", ".join(f"`{m}`" for m in _as_list(description.get("modules"))) or "none"
    extends = description.get("extends")
    if not extends:
        return f"**Extends:** none · **Modules:** {modules}"
    inherited = ", ".join(f"`{m}`" for m in _as_list(description.get("extends_modules")))
    via = f" (modules: {inherited})" if inherited else ""
    return f"**Extends:** `{extends}`{via} · **Modules:** {modules}"


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
    return [
        {
            "bytes": len(entry.data),
            "mode": entry.mode,
            "path": entry.path,
            "sha256": hashlib.sha256(entry.data).hexdigest()[:SHORT_DIGEST_LEN],
        }
        for entry in sorted(entries, key=lambda item: item.path)
    ]


def _describe_sources(image: Image, profile_state: ProfileState) -> list[dict[str, object]]:
    pins = image.source_pins() if profile_state.source_builds else {}
    described: list[dict[str, object]] = []
    for name, spec in sorted(profile_state.source_builds.items()):
        pin = spec.pin_from(pins)
        described.append(
            {
                "build": spec.build.kind,
                "install_to": spec.install_to,
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


def _module_names(image: Image, profile: str) -> list[str]:
    return [type(module).__name__ for module in image.applied_modules(profile)]


def _describe_init_scripts(entries: Sequence[InitScriptEntry]) -> dict[str, object]:
    unique = {(entry.priority, entry.script) for entry in entries}
    return {
        "count": len(unique),
        "priorities": sorted(priority for priority, _script in unique),
    }


def _render_kernel(kernel: dict[str, Any]) -> str:
    parts = [f"Kernel: {kernel.get('version') or kernel.get('config_file') or 'custom'}"]
    if kernel.get("tdx"):
        parts.append("tdx=yes")
    if kernel.get("cmdline"):
        parts.append(f"cmdline={kernel['cmdline']}")
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
        lines.append(
            f"  {entry['path']:<{width}}  {entry['mode']}  {entry['bytes']}B"
            f"  sha256:{entry['sha256']}"
        )


def _column_width(rows: list[Any], key: str) -> int:
    return max((len(str(row[key])) for row in rows), default=0)


def _truncate(text: str, width: int = PREVIEW_WIDTH) -> str:
    if len(text) <= width:
        return text
    return text[: width - 3].rstrip() + "..."


def _yes_no(value: object) -> str:
    return "yes" if value else "no"


def _fmt(value: object) -> str:
    return _yes_no(value) if isinstance(value, bool) else str(value)


def _as_list(value: object) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return list(value)
    return []


def _as_dict(value: object) -> dict[str, Any]:
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


__all__ = ["describe", "render", "render_markdown"]
