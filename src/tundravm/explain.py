"""Dry-run description of what an :class:`~tundravm.image.Image` recipe will produce.

``describe()`` returns a JSON-serializable, deterministically ordered dict for
one profile without compiling or baking; ``render()`` turns that dict into a
compact plain-text summary.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import TYPE_CHECKING, Any, cast

from .compiler import PHASE_ORDER
from .models import InitScriptEntry, ProfileState

if TYPE_CHECKING:
    from .image import Image

PREVIEW_WIDTH = 80
SHORT_DIGEST_LEN = 12


def describe(image: Image, *, profile: str | None = None) -> dict[str, object]:
    """Describe what *image* will produce for *profile* (default: the active profile).

    Keys are sorted; lists are sorted by their natural identity (path, name,
    ...) except hooks, which keep registration order within each phase.
    File and init-script contents are summarized as short sha256 digests.
    """
    selected = image._resolve_operation_profile(profile)
    image._apply_profile_fallbacks((selected,))
    state = image.state
    profile_state = state.ensure_profile(selected)
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
        "files": _describe_files(profile_state.files),
        "hooks": _describe_hooks(profile_state),
        "init_scripts": _describe_init_scripts(
            list(image.init._scripts) + list(profile_state.init_scripts)
        ),
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
            }
            for svc in sorted(profile_state.services, key=lambda item: item.name)
        ],
        "skeleton_files": _describe_files(profile_state.skeleton_files),
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

    _append_inline_list(lines, "Packages", _as_list(description.get("packages")))
    _append_inline_list(lines, "Build packages", _as_list(description.get("build_packages")))

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

    services = _as_list(description.get("services"))
    if services:
        lines.append(f"Services ({len(services)}):")
        width = _column_width(services, "name")
        for svc in services:
            parts = [f"{svc['name']:<{width}}"]
            if svc.get("command"):
                parts.append(_truncate(" ".join(svc["command"])))
            parts.append(f"restart={svc['restart']}")
            if svc.get("user"):
                parts.append(f"user={svc['user']}")
            for key in ("after", "requires", "wants"):
                if svc.get(key):
                    parts.append(f"{key}=" + ",".join(svc[key]))
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


def _describe_files(entries: Sequence[Any]) -> list[dict[str, object]]:
    return [
        {
            "bytes": len(entry.content.encode()),
            "mode": entry.mode,
            "path": entry.path,
            "sha256": hashlib.sha256(entry.content.encode()).hexdigest()[:SHORT_DIGEST_LEN],
        }
        for entry in sorted(entries, key=lambda item: item.path)
    ]


def _describe_hooks(profile_state: ProfileState) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for phase in PHASE_ORDER:
        previews = [
            _truncate(
                " ".join(hook.command.argv).splitlines()[0].strip() if hook.command.argv else ""
            )
            for hook in profile_state.hooks
            if hook.phase == phase
        ]
        if previews:
            grouped[phase] = previews
    return grouped


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


__all__ = ["describe", "render"]
