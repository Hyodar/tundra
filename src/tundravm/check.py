"""Compiler diagnostics on a lowered recipe, reported by ``lint()`` and ``tundravm lint``.

Each rule is a small function ``(image, profile_name, state) -> Iterator[Diagnostic]``
registered in ``RULES``; ``_rule_module_checks`` adds each applied module's own
``Module.check()`` findings. Rules read each profile's effective state
(``RecipeState.effective_profile``): what the compiler emits for it, i.e. the
profile merged over the default profile it extends.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .formats import annotation_path, md_cell, md_table, workflow_command
from .models import InitScriptEntry, ProfileState, unit_name

if TYPE_CHECKING:
    from ._image import Image

Level = Literal["error", "warning", "info"]
_SEVERITY: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding produced by a recipe rule."""

    level: Level
    code: str
    message: str
    hint: str | None
    profile: str
    subject: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "variant": self.profile,
            "subject": self.subject,
        }


Rule = Callable[["Image", str, ProfileState], Iterator[Diagnostic]]

_INIT_FILE_PATHS = frozenset(
    {"/usr/bin/runtime-init", "/usr/lib/systemd/system/runtime-init.service"}
)
_INIT_SERVICE = "runtime-init.service"

# Accounts created by Debian's base-passwd, present in every base image.
_BASE_USERS = frozenset(
    {
        "_apt",
        "backup",
        "bin",
        "daemon",
        "games",
        "irc",
        "list",
        "lp",
        "mail",
        "man",
        "news",
        "nobody",
        "proxy",
        "root",
        "sync",
        "sys",
        "uucp",
        "www-data",
    }
)

# Groups from Debian's base-passwd plus the ones systemd's sysusers creates.
_BASE_GROUPS = frozenset(
    {
        "adm",
        "audio",
        "backup",
        "bin",
        "cdrom",
        "daemon",
        "dialout",
        "dip",
        "disk",
        "fax",
        "floppy",
        "games",
        "input",
        "irc",
        "kmem",
        "kvm",
        "list",
        "lp",
        "mail",
        "man",
        "news",
        "nogroup",
        "operator",
        "plugdev",
        "proxy",
        "render",
        "root",
        "sasl",
        "sgx",
        "shadow",
        "src",
        "staff",
        "sudo",
        "sys",
        "systemd-journal",
        "tape",
        "tty",
        "users",
        "utmp",
        "uucp",
        "video",
        "voice",
        "www-data",
    }
)

# Binaries from Essential: yes packages that every base image ships in /usr/bin.
_ESSENTIAL_BINARIES = frozenset(
    {
        "bash",
        "cat",
        "chmod",
        "chown",
        "cp",
        "dash",
        "echo",
        "env",
        "false",
        "find",
        "grep",
        "ln",
        "ls",
        "mkdir",
        "mv",
        "printf",
        "rm",
        "sed",
        "sh",
        "sleep",
        "tee",
        "test",
        "true",
        "xargs",
    }
)

_SHIPPED_PREFIXES = ("/usr/local/bin/", "/opt/", "/usr/bin/")


def _norm(path: str) -> str:
    return posixpath.normpath("/" + path.lstrip("/"))


def _is_init_file(path: str) -> bool:
    return _norm(path) in _INIT_FILE_PATHS


def _declared_paths(state: ProfileState) -> set[str]:
    paths = {_norm(f.path) for f in state.files}
    paths.update(_norm(t.path) for t in state.templates)
    paths.update(_norm(f.path) for f in state.skeleton_files)
    return paths


def _command_text(state: ProfileState) -> str:
    argvs = [cmd.argv for cmds in state.phases.values() for cmd in cmds]
    argvs.extend(hook.command.argv for hook in state.hooks)
    return "\n".join(" ".join(argv) for argv in argvs)


def _init_entries(image: Image, state: ProfileState) -> list[InitScriptEntry]:
    merged: list[InitScriptEntry] = []
    seen: set[tuple[int, str]] = set()
    for entry in state.init_scripts:
        key = (entry.priority, entry.script)
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    return merged


def _rule_service_user_missing(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    declared = {u.name for u in state.users}
    commands = _command_text(state)
    default = image.state.profiles.get(image.default_profile)
    for svc in state.services:
        user = svc.user
        if user is None or user in declared or user in _BASE_USERS:
            continue
        if re.search(rf"\b(useradd|adduser)\b[^\n;&|]*\b{re.escape(user)}\b", commands):
            continue
        hint = f"Declare User({user!r}) in variant {profile_name!r}, or drop user= to run as root."
        if (
            default is not None
            and profile_name != default.name
            and any(u.name == user for u in default.users)
        ):
            hint = (
                f"{user!r} is declared in variant {default.name!r}, but {profile_name!r} "
                "is lowered standalone and does not inherit it; declare it in "
                f"{profile_name!r} too, or derive the variant from its parent."
            )
        yield Diagnostic(
            level="error",
            code="service-user-missing",
            message=f"service {svc.name!r} runs as user {user!r}, which is never created",
            hint=hint,
            profile=profile_name,
            subject=svc.name,
        )


def _rule_user_group_undefined(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    declared = {g.name for g in state.groups}
    # useradd creates a same-named private group unless the user has a primary gid.
    declared.update(u.name for u in state.users if u.gid is None)
    commands = _command_text(state)
    for user in state.users:
        for group in user.groups:
            if group in declared or group in _BASE_GROUPS:
                continue
            if re.search(rf"\b(groupadd|addgroup)\b[^\n;&|]*\b{re.escape(group)}\b", commands):
                continue
            yield Diagnostic(
                level="error",
                code="user-group-undefined",
                message=f"user {user.name!r} joins group {group!r}, which is never created",
                hint=(
                    f"Declare Group({group!r}) in variant "
                    f"{profile_name!r}, or drop it from groups=; useradd fails on an "
                    "unknown group."
                ),
                profile=profile_name,
                subject=user.name,
            )


def _rule_file_path_duplicate(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    extra: dict[str, list[tuple[str, str | bytes, str]]] = {}
    for f in state.files:
        extra.setdefault(_norm(f.path), []).append(("file", f.content, f.mode))
    for t in state.templates:
        extra.setdefault(_norm(t.path), []).append(("template", t.rendered, t.mode))
    skeleton: dict[str, list[tuple[str, str | bytes, str]]] = {}
    for f in state.skeleton_files:
        skeleton.setdefault(_norm(f.path), []).append(("skeleton", f.content, f.mode))
    for tree, groups in (("mkosi.extra", extra), ("mkosi.skeleton", skeleton)):
        for path, entries in groups.items():
            if len({(content, mode) for _, content, mode in entries}) < 2:
                continue
            kinds = ", ".join(sorted({kind for kind, _, _ in entries}))
            yield Diagnostic(
                level="error",
                code="file-path-duplicate",
                message=(
                    f"declared {len(entries)} times in {tree} with different content "
                    f"({kinds}); only the last one ends up in the image"
                ),
                hint=(
                    "Keep a single declaration. If two fragments write this path, drop "
                    "one of them or merge the content into one File."
                ),
                profile=profile_name,
                subject=path,
            )


def _rule_file_path_relative(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    entries = [
        *(("file", f.path) for f in state.files),
        *(("template", t.path) for t in state.templates),
        *(("skeleton", f.path) for f in state.skeleton_files),
    ]
    for kind, path in entries:
        if ".." in path.split("/"):
            yield Diagnostic(
                level="error",
                code="file-path-relative",
                message=f"{kind} path contains '..' and may escape the image root",
                hint=f"Write the normalized absolute path instead, e.g. {_norm(path)!r}.",
                profile=profile_name,
                subject=path,
            )


def _rule_service_command_not_shipped(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if state.packages:
        return
    provided = _declared_paths(state)
    if state.init_scripts:
        provided.update(_INIT_FILE_PATHS)
    mentions = _command_text(state) + "\n" + "\n".join(t for _, t in state.build_sources)
    for svc in state.services:
        if not svc.command:
            continue
        binary = svc.command[0]
        if not binary.startswith(_SHIPPED_PREFIXES) or _norm(binary) in provided:
            continue
        name = posixpath.basename(binary)
        if binary.startswith("/usr/bin/") and name in _ESSENTIAL_BINARIES:
            continue
        if name and name in mentions:
            continue
        yield Diagnostic(
            level="warning",
            code="service-command-not-shipped",
            message=f"service {svc.name!r} runs {binary}, which nothing in this variant ships",
            hint=(
                f"Ship it with File({binary!r}, Path(...), mode=0o755), a Build that "
                "installs it, or the package that provides it. This heuristic only "
                f"fires when the variant installs no packages and no file or hook mentions "
                f"{name!r}; ignore it if the base image already has the binary."
            ),
            profile=profile_name,
            subject=svc.name,
        )


def _rule_variant_empty(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if profile_name == image.default_profile:
        return
    state = image.state.ensure_profile(profile_name)
    own: Iterable[object] = (
        state.packages,
        state.build_packages,
        state.build_sources,
        [cmds for cmds in state.phases.values() if cmds],
        state.repositories,
        [f for f in state.files if not _is_init_file(f.path)],
        state.skeleton_files,
        state.templates,
        state.groups,
        state.users,
        [s for s in state.services if s.name != _INIT_SERVICE],
        state.unit_states,
        state.partitions,
        state.hooks,
        state.secrets,
        state.init_scripts,
    )
    if any(own) or state.output_targets_explicit or state.debloat_explicit:
        return
    if state.extends is None:
        message = "variant declares nothing of its own and builds a bare base image"
        hint = (
            f"Standalone variants (parent=None) do not inherit from "
            f"{image.default_profile!r}. Declare its contents in its add= fragment, "
            "or remove the variant."
        )
    else:
        message = f"variant declares nothing of its own and builds the {state.extends!r} image"
        hint = (
            f"Declare what it adds to {state.extends!r} in its add= fragment, "
            "or remove the variant."
        )
    yield Diagnostic(
        level="info",
        code="variant-empty",
        message=message,
        hint=hint,
        profile=profile_name,
        subject=None,
    )


def _rule_init_priority_collision(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    by_priority: dict[int, list[InitScriptEntry]] = {}
    for entry in _init_entries(image, state):
        by_priority.setdefault(entry.priority, []).append(entry)
    for priority, entries in sorted(by_priority.items()):
        if len(entries) < 2:
            continue
        heads = "; ".join(repr(e.script.strip().splitlines()[0][:40]) for e in entries)
        yield Diagnostic(
            level="warning",
            code="init-priority-collision",
            message=(
                f"{len(entries)} init scripts share priority {priority} and run in "
                f"registration order: {heads}"
            ),
            hint=(
                "Give each step its own priority with Init(name, script, priority=N) "
                "(lower runs first) so boot order does not depend on declaration order."
            ),
            profile=profile_name,
            subject=f"priority {priority}",
        )


def _rule_debloat_removes_needed_unit(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    config = state.debloat
    if not (config.enabled and config.systemd_minimize):
        return
    keep = set(config.effective_units_keep) | {"minimal.target"}
    own_units = {unit_name(s.name) for s in state.services}
    own_units.update(posixpath.basename(p) for p in _declared_paths(state))

    def masked(unit: str) -> bool:
        if unit in keep or unit in own_units:
            return False
        return unit.startswith("systemd-") or unit.endswith(".target")

    for svc in state.services:
        unit = unit_name(svc.name)
        if unit.startswith("systemd-") and unit not in keep:
            yield Diagnostic(
                level="warning",
                code="debloat-removes-needed-unit",
                message=f"service {svc.name!r} is a systemd unit that debloat masks",
                hint=(
                    f"Add it to Debloat(keep_units_extra=({unit!r},)) so the "
                    "systemd minimization keeps it."
                ),
                profile=profile_name,
                subject=svc.name,
            )
        for dep in (*svc.requires, *svc.wants):
            if not masked(dep):
                continue
            yield Diagnostic(
                level="warning",
                code="debloat-removes-needed-unit",
                message=f"service {svc.name!r} depends on {dep!r}, which debloat masks",
                hint=(
                    f"Add it to Debloat(keep_units_extra=({dep!r},)), or drop "
                    "the dependency; a Requires= on a masked unit stops the service."
                ),
                profile=profile_name,
                subject=svc.name,
            )


def _rule_debloat_removes_declared_file(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    config = state.debloat
    if not config.enabled:
        return
    removed = list(config.effective_paths_remove) + list(config.clean_var_dirs)
    for path in sorted(_declared_paths(state)):
        for pattern in removed:
            if path == pattern or path.startswith(pattern + "/") or fnmatch.fnmatch(path, pattern):
                yield Diagnostic(
                    level="warning",
                    code="debloat-removes-declared-file",
                    message=f"debloat deletes {pattern} at finalize, after this file is placed",
                    hint=(
                        f"Add {pattern!r} to Debloat(keep_paths=(...)) or move the file "
                        "outside the removed path."
                    ),
                    profile=profile_name,
                    subject=path,
                )
                break


def _rule_source_unpinned(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if not state.source_builds:
        return
    pins = image.source_pins()
    policy = image.policy.mutable_ref_policy
    level: Level = "error" if policy == "error" else "warning" if policy == "warn" else "info"
    for name, spec in sorted(state.source_builds.items()):
        if spec.pin_from(pins) is not None:
            continue
        yield Diagnostic(
            level=level,
            code="source-unpinned",
            message=(
                f"source build {name!r} ({spec.source.kind} {spec.source.url} "
                f"@ {spec.source.requested}) is not pinned in the lockfile"
            ),
            hint=(
                "Run `tundravm lock RECIPE` to pin it, or declare an immutable source "
                "(Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...))."
            ),
            profile=profile_name,
            subject=name,
        )


def _rule_module_checks(
    image: Image, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    for module in image.applied_modules(profile_name, inherited=True):
        yield from module.check(image, profile_name)


RULES: list[Rule] = [
    _rule_service_user_missing,
    _rule_user_group_undefined,
    _rule_file_path_duplicate,
    _rule_file_path_relative,
    _rule_service_command_not_shipped,
    _rule_variant_empty,
    _rule_init_priority_collision,
    _rule_debloat_removes_needed_unit,
    _rule_debloat_removes_declared_file,
    _rule_source_unpinned,
    _rule_module_checks,
]


def _sort_key(d: Diagnostic) -> tuple[str, int, str, str, str]:
    return (d.profile, _SEVERITY[d.level], d.code, d.subject or "", d.message)


def check(image: Image, *, profiles: Sequence[str] | None = None) -> list[Diagnostic]:
    """Run every rule over *profiles* (default: the image's active profiles)."""
    names = tuple(profiles) if profiles is not None else image._active_profiles
    found: dict[Diagnostic, None] = {}
    for name in dict.fromkeys(names):
        state = image.state.effective_profile(name)
        for rule in RULES:
            found.update(dict.fromkeys(rule(image, name, state)))
    return sorted(found, key=_sort_key)


def summarize(diagnostics: Sequence[Diagnostic]) -> dict[str, int]:
    return {
        "errors": sum(d.level == "error" for d in diagnostics),
        "warnings": sum(d.level == "warning" for d in diagnostics),
        "infos": sum(d.level == "info" for d in diagnostics),
    }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def render(diagnostics: Sequence[Diagnostic]) -> str:
    """Human-readable report, one line per finding plus a summary line."""
    if not diagnostics:
        return "no findings"
    lines: list[str] = []
    for d in diagnostics:
        subject = f" {d.subject}" if d.subject else ""
        lines.append(f"{d.level} {d.code} [{d.profile}]{subject}: {d.message}")
        if d.hint:
            lines.append(f"    hint: {d.hint}")
    lines.append(render_summary(diagnostics))
    return "\n".join(lines)


def render_summary(diagnostics: Sequence[Diagnostic]) -> str:
    """``no findings`` or ``N errors, N warnings, N infos``."""
    if not diagnostics:
        return "no findings"
    counts = summarize(diagnostics)
    return ", ".join(
        (
            _plural(counts["errors"], "error"),
            _plural(counts["warnings"], "warning"),
            _plural(counts["infos"], "info"),
        )
    )


def render_github(
    diagnostics: Sequence[Diagnostic], recipe_path: str | Path, *, strict: bool = False
) -> str:
    """GitHub workflow commands, one ``::error``/``::warning``/``::notice`` per finding.

    Each line is ``::error file=RECIPE,title=<code>::[variant] subject: message (hint)``,
    so findings show inline on the pull request. With *strict*, warnings (which fail
    the run) are reported as errors. A plain summary line follows.
    """
    if not diagnostics:
        return "no findings"
    path = annotation_path(recipe_path)
    lines: list[str] = []
    for d in diagnostics:
        command = _GITHUB_COMMANDS[d.level]
        if strict and d.level == "warning":
            command = "error"
        subject = f" {d.subject}" if d.subject else ""
        hint = f" ({d.hint})" if d.hint else ""
        message = f"[{d.profile}]{subject}: {d.message}{hint}"
        lines.append(workflow_command(command, message, file=path, title=d.code))
    lines.append(render_summary(diagnostics))
    return "\n".join(lines)


def render_markdown(diagnostics: Sequence[Diagnostic]) -> str:
    """A Markdown table of findings followed by the summary line in bold."""
    if not diagnostics:
        return "**No findings.**"
    rows = [
        (
            md_cell(d.level),
            md_cell(d.code, code=True),
            md_cell(d.profile, code=True),
            md_cell(d.subject, code=True),
            md_cell(d.message),
            md_cell(d.hint),
        )
        for d in diagnostics
    ]
    table = md_table(("Level", "Code", "Variant", "Subject", "Message", "Hint"), rows)
    return f"{table}\n\n**{render_summary(diagnostics)}**"


def render_as(
    diagnostics: Sequence[Diagnostic],
    fmt: str,
    *,
    recipe_path: str | Path,
    strict: bool = False,
) -> str:
    """Render *diagnostics* in a resolved ``--format``: text, json, github or markdown."""
    if fmt == "json":
        payload = {
            "diagnostics": [d.to_dict() for d in diagnostics],
            "summary": summarize(diagnostics),
        }
        return json.dumps(payload, indent=2, sort_keys=True)
    if fmt == "github":
        return render_github(diagnostics, recipe_path, strict=strict)
    if fmt == "markdown":
        return render_markdown(diagnostics)
    return render(diagnostics)


def failing(diagnostics: Sequence[Diagnostic], *, strict: bool = False) -> bool:
    """True when a finding fails ``check``: an error, or with *strict* a warning."""
    levels = {"error", "warning"} if strict else {"error"}
    return any(d.level in levels for d in diagnostics)


_GITHUB_COMMANDS: dict[str, str] = {"error": "error", "warning": "warning", "info": "notice"}


__all__ = [
    "RULES",
    "Diagnostic",
    "Rule",
    "check",
    "failing",
    "render",
    "render_as",
    "render_github",
    "render_markdown",
    "render_summary",
    "summarize",
]
