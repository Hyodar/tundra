"""Compiler diagnostics on a lowered recipe, reported by ``lint()`` and ``tundravm check``.

``RULES`` registers every code ``tundravm check`` reports, wherever it comes from
(resolution, a component, a module, the compiler, unit text, the lock, the host,
``--tree``), as a :class:`Rule`: its severity, what it checks and how to fix it.
``tundravm check --explain CODE`` prints one, and ``docs/reference/lint.md`` lists them.
A compiler rule's ``checks`` are small functions
``(image, profile_name, state) -> Iterator[Diagnostic]``; ``_rule_module_checks`` adds
each applied module's own ``Module.check()`` findings. They read each profile's effective
state (``RecipeState.effective_profile``): what the compiler emits for it, i.e. the
profile merged over the default profile it extends.
"""

from __future__ import annotations

import difflib
import fnmatch
import posixpath
import re
import textwrap
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._modules.init import is_init_path
from ._modules.secret_delivery import SecretDelivery
from ._secret_scan import Finding, is_unit_path, scan_file, scan_unit
from ._source import DebFile, KernelFile, KernelSource, LocalSource, NamedSource
from .compiler.emit_mkosi import _systemd_unit_content
from .diagnostics import Diagnostic, Severity
from .errors import ValidationError
from .formats import annotation_path, workflow_command
from .kernel_modules import (
    NO_MODULES,
    ModuleSelection,
    debloat_paths_remove,
    installs_kernel_package,
    module_selection,
    removes_path,
)
from .models import MODULES_PATH, ProfileState, ServiceSpec, debloat_units_keep, unit_name

if TYPE_CHECKING:
    from .declarative._lowered import Lowered
    from .declarative.model import Recipe, Service, Task

_SEVERITY: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


RuleCheck = Callable[["Lowered", str, ProfileState], Iterator[Diagnostic]]
"""A compiler pass: the findings of one profile of the lowered image."""


@dataclass(frozen=True, slots=True)
class Rule:
    """One code ``tundravm check`` reports: what it checks, and how to fix it.

    *severity* is the level it reports at by default (the *summary* names when it
    differs). *summary* is one line: what it checks and why it matters; *fix* is what
    clears it. *checks* are the compiler passes that report it; a code resolution, a
    component, the lock, the host or ``--tree`` reports has none.
    """

    code: str
    severity: Severity
    summary: str
    fix: str
    checks: tuple[RuleCheck, ...] = ()


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
    return is_init_path(_norm(path))


def _declared_paths(state: ProfileState) -> set[str]:
    paths = {_norm(f.path) for f in state.files}
    paths.update(_norm(t.path) for t in state.templates)
    paths.update(_norm(f.path) for f in state.skeleton_files)
    return paths


def _command_text(state: ProfileState) -> str:
    argvs = [cmd.argv for cmds in state.phases.values() for cmd in cmds]
    argvs.extend(hook.command.argv for hook in state.hooks)
    return "\n".join(" ".join(argv) for argv in argvs)


def _rule_service_user_missing(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    declared = {u.name for u in state.users}
    commands = _command_text(state)
    for svc in state.services:
        user = svc.user
        if user is None or user in declared or user in _BASE_USERS:
            continue
        if re.search(rf"\b(useradd|adduser)\b[^\n;&|]*\b{re.escape(user)}\b", commands):
            continue
        hint = (
            f"Declare User({user!r}) in profile {profile_name!r}, or drop user= to run as "
            "a system user named after the service."
        )
        yield Diagnostic(
            severity="error",
            code="service-user-missing",
            message=f"service {svc.name!r} runs as user {user!r}, which is never created",
            hint=hint,
            profile=profile_name,
            subject=svc.name,
        )


def _rule_user_group_undefined(
    image: Lowered, profile_name: str, state: ProfileState
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
                severity="error",
                code="user-group-undefined",
                message=f"user {user.name!r} joins group {group!r}, which is never created",
                hint=(
                    f"Declare Group({group!r}) in profile "
                    f"{profile_name!r}, or drop it from groups=; useradd fails on an "
                    "unknown group."
                ),
                profile=profile_name,
                subject=user.name,
            )


def _rule_file_path_duplicate(
    image: Lowered, profile_name: str, state: ProfileState
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
                severity="error",
                code="file-path-duplicate",
                message=(
                    f"declared {len(entries)} times in {tree} with different content "
                    f"({kinds}); only the last one ends up in the image"
                ),
                hint=(
                    "Keep a single declaration. If two components write this path, drop "
                    "one of them or merge the content into one File."
                ),
                profile=profile_name,
                subject=path,
            )


def _rule_file_path_relative(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    entries = [
        *(("file", f.path) for f in state.files),
        *(("template", t.path) for t in state.templates),
        *(("skeleton", f.path) for f in state.skeleton_files),
    ]
    for kind, path in entries:
        if ".." in path.split("/"):
            yield Diagnostic(
                severity="error",
                code="file-path-relative",
                message=f"{kind} path contains '..' and may escape the image root",
                hint=f"Write the normalized absolute path instead, e.g. {_norm(path)!r}.",
                profile=profile_name,
                subject=path,
            )


def _rule_service_command_not_shipped(
    image: Lowered, profile_name: str, state: ProfileState
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
            severity="warning",
            code="service-command-not-shipped",
            message=f"service {svc.name!r} runs {binary}, which nothing in this profile ships",
            hint=(
                f"Ship it with File({binary!r}, Path(...), mode=0o755), a Build that "
                "installs it, or the package that provides it. This heuristic only "
                f"fires when the profile installs no packages and no file or hook mentions "
                f"{name!r}; ignore it if the base image already has the binary."
            ),
            profile=profile_name,
            subject=svc.name,
        )


def _rule_profile_empty(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if profile_name == image.default_profile:
        return
    if image.targets.get(profile_name) != image.targets.get(image.default_profile):
        return  # it builds other wrappers around the payload
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
    message = f"profile declares nothing of its own and builds the {state.extends!r} image"
    hint = f"Declare what it adds to {state.extends!r} in its add= or changes=, or remove it."
    yield Diagnostic(
        severity="info",
        code="profile-empty",
        message=message,
        hint=hint,
        profile=profile_name,
    )


def _rule_debloat_removes_needed_unit(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    config = state.debloat
    if not (config.enabled and config.systemd_minimize):
        return
    keep = set(debloat_units_keep(state)) | {"minimal.target"}
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
                severity="warning",
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
            if not masked(dep) or (svc.enabled and dep.endswith(".target")):
                continue  # the debloat keeps the targets enabled units reference
            yield Diagnostic(
                severity="warning",
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
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    config = state.debloat
    if not config.enabled:
        return
    declared = _declared_paths(state)
    removed = [*debloat_paths_remove(config, NO_MODULES, declared), *config.clean_var_dirs]
    for path in sorted(declared):
        for pattern in removed:
            if removes_path(pattern, path):
                yield Diagnostic(
                    severity="warning",
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
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    sources = image.select(profile_name).lock_sources()
    if not sources:
        return
    pins = image.source_pins()
    policy = image.policy.mutable_ref_policy
    severity: Severity = "error" if policy == "error" else "warning" if policy == "warn" else "info"
    for spec in sources.values():
        if spec.pin_from(pins) is not None:
            continue
        message, hint = _unpinned_advice(spec)
        yield Diagnostic(
            severity=severity,
            code="source-unpinned",
            message=message,
            hint=hint,
            profile=profile_name,
            subject=spec.name,
        )


def _unpinned_advice(spec: NamedSource) -> tuple[str, str]:
    """The ``source-unpinned`` message and hint for the unpinned lockable source *spec*."""
    source = spec.source
    fetch = " (`tundravm build` then checks it out on the host)"
    if isinstance(spec, KernelSource):
        return (
            f"kernel {spec.version} ({source.kind} {source.url} @ {source.requested}) "
            "is not pinned in the lockfile",
            f"Run `tundravm lock IMAGE` to pin it as {spec.name!r}{fetch}, or give "
            "Kernel a Git source with a 40-hex commit ref.",
        )
    if isinstance(spec, KernelFile):
        return (
            f"the kernel's {spec.name} ({source.kind} {source.url}) is not pinned in the lockfile",
            f"Run `tundravm lock IMAGE` to pin its sha256 as {spec.name!r}{fetch}.",
        )
    if isinstance(spec, DebFile):
        return (
            f"the EFI stub package ({source.kind} {source.url}) is not pinned in the lockfile",
            f"Run `tundravm lock IMAGE` to pin its sha256 as {spec.name!r} "
            "(`tundravm build` then downloads it on the host). "
            "Unpinned, the postinst hook downloads it in the build sandbox.",
        )
    if isinstance(source, LocalSource):
        return (
            f"source build {spec.name!r} ({source.describe()}) is not pinned in the lockfile",
            "Run `tundravm lock IMAGE` to pin its content digest (`tundravm build` then "
            "copies the directory into its workspace).",
        )
    return (
        f"source build {spec.name!r} ({source.kind} {source.url} @ {source.requested}) "
        "is not pinned in the lockfile",
        f"Run `tundravm lock IMAGE` to pin it{fetch}, or declare an immutable source "
        "(Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)). "
        "Locking reports a dead upstream ref, with every other source it cannot "
        "resolve, before anything is written.",
    )


def _rule_kernel_missing(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if not image.mkosi.bootable or image.kernel is not None:
        return
    if installs_kernel_package(state.packages):
        return
    yield Diagnostic(
        severity="error",
        code="kernel-missing",
        message=(
            "profile builds a bootable UKI but installs no kernel, so mkosi stops with "
            "'A kernel must be installed in the image to build a UKI'"
        ),
        hint=(
            'Keep Debian(kernel_package="linux-image-amd64") for the distribution kernel, '
            'declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) '
            "with Debian(boot=None) for a non-bootable image."
        ),
        profile=profile_name,
    )


def _removes(patterns: Iterable[str], path: str) -> str | None:
    """The first of *patterns* (debloat removal paths or globs) that deletes *path*."""
    for pattern in patterns:
        if path == pattern or path.startswith(pattern.rstrip("/") + "/"):
            return pattern
        if fnmatch.fnmatch(path, pattern):
            return pattern
    return None


def _rule_built_kernel_modules(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    """A built kernel's modules: ``kernel_modules`` it cannot have, or a debloat deleting them."""
    kernel = image.kernel
    if kernel is None or not kernel.built:
        return
    loadable = kernel.composed and kernel.modules
    selected = image.mkosi.kernel_modules
    if selected and not loadable:
        yield Diagnostic(
            severity="warning",
            code="kernel-modules-ignored",
            message=(
                "Debian(kernel_modules=...) selects modules, but the built kernel has no "
                "loadable modules (Kernel(modules=False) or a whole config=)"
            ),
            hint="Drop kernel_modules and build those drivers in (KernelConfig), or pass "
            "Kernel(modules=True).",
            profile=profile_name,
            subject="kernel_modules",
        )
    config = state.debloat
    if loadable and config.enabled and config.keep_modules is not True:
        # The implicit debloat keeps them (module_selection); a declared list may not.
        kept = ModuleSelection(modular=True)
        pattern = _removes(debloat_paths_remove(config, kept), MODULES_PATH)
        if pattern is not None and config.keep_modules is None:
            yield Diagnostic(
                severity="error",
                code="debloat-removes-modules",
                message=(
                    f"debloat removes {pattern} but the built kernel installs its loadable "
                    "modules there (Kernel(modules=True))"
                ),
                hint="Debloat(keep_modules=True) keeps them; or build them in "
                "(Kernel(modules=False)).",
                profile=profile_name,
                subject=MODULES_PATH,
            )


def _rule_debloat_removes_modules(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    config = state.debloat
    if not config.enabled or config.keep_modules is False:
        return
    # A built Kernel installs only its vmlinuz: nothing under /usr/lib/modules loads.
    selection = module_selection(
        state,
        image.mkosi.kernel_modules,
        built_kernel=image.kernel is not None,
    )
    if not selection.modular:
        return
    # What finalize removes: the implicit default already keeps the modules mkosi selected.
    pattern = _removes(debloat_paths_remove(config, selection), MODULES_PATH)
    if pattern is None:
        return
    yield Diagnostic(
        severity="error",
        code="debloat-removes-modules",
        message=(
            f"debloat removes {pattern} but the profile boots the distribution's modular "
            "kernel, whose drivers (virtio, tdx_guest, cloud NICs) then never load"
        ),
        hint=(
            "Debloat(keep_modules=True) keeps them; Debloat(keep_modules=False) removes them "
            "on purpose; or declare Kernel(...) built with everything the image needs."
        ),
        profile=profile_name,
        subject=MODULES_PATH,
    )


_EFI_STUB_DEB = re.compile(r"systemd-boot-efi_\S*_amd64\.deb")


def _rule_recipe_arch_unsupported(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    if image.arch != "aarch64":
        return
    if _EFI_STUB_DEB.search(_command_text(state)):
        yield Diagnostic(
            severity="error",
            code="recipe-arch-unsupported",
            message=(
                "the platform's arch is aarch64 but EfiStub installs the amd64 systemd-boot-efi "
                "package, whose x86-64 EFI stub cannot boot an arm64 UKI"
            ),
            hint=(
                "EfiStub downloads systemd-boot-efi_<version>_amd64.deb only: drop EfiStub "
                "from aarch64 recipes, or build for x86_64."
            ),
            profile=profile_name,
            subject="EfiStub",
        )
    if "qemu" in state.output_targets:
        yield Diagnostic(
            severity="error",
            code="recipe-arch-unsupported",
            message=(
                "the platform's arch is aarch64 but the profile targets qemu, whose deploy adapter "
                "boots images with qemu-system-x86_64"
            ),
            hint=(
                "The qemu adapter runs qemu-system-x86_64 only, and TDX guests are x86-64: "
                "set Debian(..., arch='x86_64')."
            ),
            profile=profile_name,
            subject="qemu",
        )


SECRET_HINT = (
    "Deliver it at boot instead of baking it into the image, where every copy and every "
    "registry holding it exposes it. The built-in Secrets() delivery provisions only an "
    "SSH key: put the value in a Secret with a SecretFile or SecretEnv target under "
    "Secrets(delivery=ExternalDelivery(NAME)), naming what writes it at boot (a cloud "
    "secret store, a custom unit). If it is not a secret, pass allow_secret=True to the "
    "declaration."
)
"""The secret lints' hint for a variant without an ``ExternalDelivery`` (:func:`secret_hint`)."""

_TARGET_CLASSES = {"file": "SecretFile", "env": "SecretEnv"}


def secret_hint(image: Lowered, profile_name: str) -> str:
    """The secret lints' hint: the targets an ``ExternalDelivery`` of the variant writes.

    :data:`SECRET_HINT` when the variant declares none.
    """
    for module in image.applied_modules(profile_name, inherited=True):
        if isinstance(module, SecretDelivery) and module.external:
            targets = " or ".join(_TARGET_CLASSES[kind] for kind in module.provider_supports)
            return (
                "Deliver it at boot instead of baking it into the image, where every copy "
                "and every registry holding it exposes it: add a Secret with a "
                f"{targets} target to the Secrets that ExternalDelivery({module.provider!r}) "
                "delivers. If it is not a secret, pass allow_secret=True to the declaration."
            )
    return SECRET_HINT


def _secret_allowed(image: Lowered, profile_name: str, subject: str) -> bool:
    """Whether a declaration with ``allow_secret=True`` exempts *subject* in *profile_name*."""
    allowed = image.secret_allowed.get(profile_name, frozenset())
    return subject in allowed or any(
        entry.endswith("/") and subject.startswith(entry) for entry in allowed
    )


def _shipped_files(state: ProfileState) -> Iterator[tuple[str, str | bytes]]:
    for entry in (*state.files, *state.skeleton_files):
        if entry.kind == "file":
            yield _norm(entry.path), entry.content
    for template in state.templates:
        yield _norm(template.path), template.rendered


def _secret_message(what: str, findings: Sequence[Finding]) -> str:
    more = len(findings) - 1
    extra = f", and {more} more finding{'s' if more > 1 else ''}" if more else ""
    return f"{what} holds {findings[0].where()}{extra}; it is baked into the image"


def _rule_secret_in_file(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    for path, content in _shipped_files(state):
        if is_unit_path(path) or _secret_allowed(image, profile_name, path):
            continue
        findings = scan_file(path, content)
        if findings:
            yield Diagnostic(
                severity="error",
                code="secret-in-file",
                message=_secret_message("the file", findings),
                hint=secret_hint(image, profile_name),
                profile=profile_name,
                subject=path,
            )


_SERVICE_FIELDS = {
    "Description=": "description",
    "After=": "after",
    "Requires=": "requires",
    "Wants=": "wants",
    "ExecStartPre=": "exec_start_pre",
    "ExecStart=": "exec_start",
    "User=": "user",
    "Group=": "group",
    "WorkingDirectory=": "working_dir",
    "EnvironmentFile=": "env_file",
    "Environment=": "env",
    "KillMode=": "kill_mode",
    "TimeoutStopSec=": "timeout_stop",
    "WantedBy=": "wanted_by",
}
"""The ``Service`` field each directive of its rendered unit comes from."""


def _service_field(directive: str) -> str:
    """The ``Service`` field a rendered unit's *directive* (``ExecStart=``) comes from."""
    if directive.startswith("Limit"):
        return "limits"
    return _SERVICE_FIELDS.get(directive, "")


def _service_findings(svc: ServiceSpec) -> tuple[Finding, ...]:
    """:func:`scan_unit` on the unit *svc* renders to, each finding naming its field.

    The line is left out: it is the generated unit's, which the declaration does not show.
    """
    return tuple(
        Finding(found.kind, field=_service_field(found.field))
        for found in scan_unit(_systemd_unit_content(svc))
    )


def _rule_secret_in_env(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    for svc in state.services:
        name = unit_name(svc.name)
        if (
            not svc.command
            or svc.name.endswith(".target")
            or _secret_allowed(image, profile_name, name)
        ):
            continue
        found = _service_findings(svc)
        if found:
            yield Diagnostic(
                severity="error",
                code="secret-in-env",
                message=_secret_message(f"service {name!r}", found),
                hint=secret_hint(image, profile_name),
                profile=profile_name,
                subject=name,
            )
    for path, content in _shipped_files(state):
        if not is_unit_path(path) or _secret_allowed(image, profile_name, path):
            continue
        findings = scan_unit(content)
        if findings:
            yield Diagnostic(
                severity="error",
                code="secret-in-env",
                message=_secret_message("the unit file", findings),
                hint=secret_hint(image, profile_name),
                profile=profile_name,
                subject=path,
            )


# ── systemd unit text (every unit a variant ships) ──────────────────────
#
# ``tundravm._units.lint`` holds the rules; these wrappers feed them the variant's
# units (shipped files and generated services) and the facts they check against.


def _rule_unit_text(image: Lowered, profile_name: str, state: ProfileState) -> Iterator[Diagnostic]:
    from ._units.lint import Facts, ShippedUnit, unit_findings

    units: list[ShippedUnit] = []
    for path, content in _shipped_files(state):
        if is_unit_path(path) and not path.endswith("/"):
            text = content.decode("utf-8", "replace") if isinstance(content, bytes) else content
            subject = path if posixpath.dirname(path).endswith(".d") else posixpath.basename(path)
            units.append(ShippedUnit(subject, path, text))
    shipped = {unit.name for unit in units}
    for svc in state.services:
        name = unit_name(svc.name)
        if svc.command and name not in shipped:
            path = f"/usr/lib/systemd/system/{name}"
            units.append(ShippedUnit(svc.name, path, _systemd_unit_content(svc), generated=True))
    commands = _command_text(state)
    users = {u.name for u in state.users} | _BASE_USERS
    groups = {g.name for g in state.groups} | {u.name for u in state.users if u.gid is None}
    groups |= _BASE_GROUPS

    def made(name: str, known: set[str], tools: str) -> bool:
        # A Debian package creates its daemon's account (prometheus-node-exporter: prometheus).
        packaged = any(package.startswith(name.lstrip("_")) for package in state.packages)
        return (
            name in known
            or packaged
            or bool(re.search(rf"\b({tools})\b[^\n;&|]*\b{re.escape(name)}\b", commands))
        )

    facts = Facts(
        variant=profile_name,
        enabled=frozenset(unit_name(s.name) for s in state.services if s.enabled),
        has_user=lambda name: made(name, users, "useradd|adduser"),
        has_group=lambda name: made(name, groups, "groupadd|addgroup"),
        packages=bool(state.packages),
        style=True,
        outputs=frozenset(state.output_targets),
    )
    yield from unit_findings(units, facts)


def unit_declaration_check(
    recipe: Recipe, *, variants: Sequence[str] | None = None
) -> list[Diagnostic]:
    """``unit-raw``, ``unit-as-file``, ``unit-extra``, ``hardening-*`` and
    ``service-runs-as-root`` findings of *recipe*'s own declarations.

    The first two are style codes; the others describe typed units.
    """
    from .declarative.model import File, Service, Task, Unit

    selected = set(variants) if variants is not None else {v.name for v in recipe.variants}
    groups = [("", (*recipe.platform.contribution(), *recipe.items))]
    groups += [(v.name, (v.add,)) for v in recipe.variants if v.name in selected]
    generated = {
        unit_name(item.name)
        for _, items in groups
        for item in _own_declarations(items)
        if isinstance(item, (Service, Task))
    }
    found: list[Diagnostic] = []
    for variant, items in groups:
        for item in _own_declarations(items):
            if isinstance(item, (Service, Task)):
                found += [_located(d, item) for d in _typed_unit_findings(item, variant)]
            if isinstance(item, Service):
                covered = _keyword_covered(item.extra, {})
                if covered:
                    found.append(_located(_unit_extra(item.name, covered, variant, False), item))
            elif isinstance(item, Unit) and item.dropin is not None:
                if unit_name(item.name) in generated:
                    dropin = item.dropin
                    covered = _keyword_covered(dropin.extra, _dropin_keywords(dropin))
                    if covered:
                        found.append(_located(_unit_extra(item.name, covered, variant, True), item))
            if isinstance(item, Unit) and item.content is not None:
                if unit_name(item.name).endswith(".service"):
                    found.append(_located(_unit_raw(item.name, variant), item))
            elif isinstance(item, File) and is_unit_path(_norm(item.path)):
                found.append(_located(_unit_as_file(item.path, variant), item))
    return found


def _typed_unit_findings(item: Service | Task, variant: str) -> Iterator[Diagnostic]:
    """``service-runs-as-root``, ``hardening-disabled`` and ``hardening-relaxed`` of a typed
    ``Service``/``Task``."""
    from ._units.render import STRICT_HARDENING
    from .declarative.model import Service as TypedService
    from .declarative.systemd import Hardening, Root

    name = unit_name(item.name)
    if item.user == "root" or isinstance(item.user, Root):
        spelled = "user='root'" if item.user == "root" else "user=Root()"
        yield Diagnostic(
            code="service-runs-as-root",
            severity="info",
            message=f"unit {name!r} runs as root ({spelled})",
            subject=name,
            profile=variant,
            hint="Drop user= to run as a system user named after the unit (declared for you); "
            "grant one capability with Hardening.strict(capabilities=(...)) if it needs one.",
        )
    sandbox = item.sandbox if isinstance(item, TypedService) else item.hardening
    entries = STRICT_HARDENING if sandbox is None else sandbox.entries()
    if not entries:
        yield Diagnostic(
            code="hardening-disabled",
            severity="warning",
            message=f"unit {name!r} runs without a sandbox (Hardening.none())",
            subject=name,
            profile=variant,
            hint="Use hardening=Hardening.relaxed() or relax one knob of Hardening.strict(...) "
            "(jit=True, devices=(...), capabilities=(...)) instead of dropping them all.",
        )
        return
    keys = {key for key, _ in entries}
    if "DevicePolicy" in keys:
        keys.add("PrivateDevices")
    strict = dict(Hardening.strict().entries())
    dropped = [k for k in strict if k not in keys or (k, strict[k]) not in entries]
    dropped = [k for k in dropped if not (k == "PrivateDevices" and "DevicePolicy" in keys)]
    if dropped:
        legacy = " (security= is deprecated: use hardening=)" if sandbox is None else ""
        yield Diagnostic(
            code="hardening-relaxed",
            severity="info",
            message=f"unit {name!r} relaxes the strict sandbox: {', '.join(dropped)}{legacy}",
            subject=name,
            profile=variant,
            hint="Each relaxed knob widens what a compromised process can do; keep the ones "
            "the program does not need (docs/reference/api.md: Hardening).",
        )


_KEYWORDS: Mapping[str, str] = {
    **{
        f"Service.{key}": f"resources=Resources({field}=...)"
        for key, field in (
            ("MemoryMax", "memory_max"),
            ("MemoryHigh", "memory_high"),
            ("CPUQuota", "cpu_quota"),
            ("CPUWeight", "cpu_weight"),
            ("TasksMax", "tasks_max"),
            ("IOWeight", "io_weight"),
        )
    },
    "Service.KillSignal": "shutdown=Shutdown(signal=...)",
    "Service.SendSIGKILL": "shutdown=Shutdown(final_kill=False)",
}
"""Directives raw unit text may set that a public ``Service`` keyword renders."""

_DROPIN_KEYWORDS: Mapping[str, str] = {
    "exec_start": "the argv",
    "user": "user=",
    "groups": "groups=",
    "working_dir": "working_directory=",
    "env": "environment=",
    "credentials": "credentials=",
    "state": "state=",
    "logs": "logs=",
    "runtime": "runtime=",
    "timeout_stop": "shutdown=Shutdown(timeout=...)",
    "hardening": "hardening=",
}
"""``DropIn`` fields a public ``Service`` keyword renders on the unit it generates."""


def _dropin_keywords(dropin: object) -> dict[str, str]:
    """The ``DropIn`` fields *dropin* sets that a ``Service`` keyword covers, as ``field=``."""
    defaults = type(dropin).__dataclass_fields__  # type: ignore[attr-defined]
    found: dict[str, str] = {}
    for name, keyword in _DROPIN_KEYWORDS.items():
        value = getattr(dropin, name)
        if value not in (None, (), defaults[name].default) and value != {}:
            found[f"{name}="] = keyword
    return found


def _keyword_covered(
    extra: Mapping[str, Mapping[str, object]], typed: Mapping[str, str]
) -> dict[str, str]:
    """What *extra* and the *typed* fields set that a ``Service`` keyword covers."""
    found = dict(typed)
    for section, values in extra.items():
        for key in values:
            keyword = _KEYWORDS.get(f"{section}.{key}")
            if keyword is not None:
                found[f"{key}="] = keyword
    return found


def _unit_extra(name: str, covered: Mapping[str, str], variant: str, dropin: bool) -> Diagnostic:
    unit = unit_name(name)
    where = "through unit=DropIn(...)" if dropin else "as raw unit directives"
    keywords = tuple(dict.fromkeys(covered.values()))
    which = "a Service keyword covers" if len(keywords) == 1 else "Service keywords cover"
    stem = unit.removesuffix(".service")
    return Diagnostic(
        code="unit-extra",
        severity="info",
        message=f"unit {unit!r} sets {', '.join(covered)} {where}, which {which}",
        subject=unit,
        profile=variant,
        hint=(
            f"Use {', '.join(keywords)} on Service({stem!r}, ...) instead, so the setting stays "
            "typed and validated; unit=DropIn(...) is for what no keyword covers."
        ),
    )


def _unit_raw(name: str, variant: str) -> Diagnostic:
    return Diagnostic(
        code="unit-raw",
        severity="info",
        message=f"unit {name!r} is hand-written unit text",
        subject=name,
        profile=variant,
        hint=(
            f"Declare Service({posixpath.splitext(name)[0]!r}, command, ...) instead: the "
            "compiler renders the unit, its ordering after needs= and its enablement."
        ),
    )


def _unit_as_file(path: str, variant: str) -> Diagnostic:
    return Diagnostic(
        code="unit-as-file",
        severity="info",
        message=f"the unit file {path!r} is shipped as a File",
        subject=path,
        profile=variant,
        hint="Ship it as Unit(name, text) so enabled=, needs= and the unit lint apply to it.",
    )


def _rule_module_checks(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    for module in image.applied_modules(profile_name, inherited=True):
        yield from module.check(image, profile_name)


_UNIT_TEXT = (_rule_unit_text,)
_MODULES = (_rule_module_checks,)
_LOCK_AGAIN = (
    "Review the change, then run `tundravm lock IMAGE` to record it and commit the lockfile; "
    "`tundravm lock --check` shows the same drift without writing."
)

_REGISTRY: tuple[Rule, ...] = (
    # ── resolution: the declarations of each profile ──
    Rule(
        "edit-invalid",
        "error",
        "A profile's edit sets a field to a value the declaration refuses.",
        "Fix the field values in the profile's changes=; the message carries the "
        "declaration's own error.",
    ),
    Rule(
        "edit-missing",
        "error",
        "A profile edits a declaration it does not inherit, so the edit changes nothing.",
        "Edit only what the profile inherits from the profile it extends (or the image), "
        "or declare the value in its add= instead.",
    ),
    Rule(
        "remove-missing",
        "error",
        "A profile removes a declaration it does not inherit, so the removal does nothing.",
        "Remove only what the profile inherits from the profile it extends (or the image), "
        "or drop the removal from its changes=.",
    ),
    Rule(
        "identity-collision",
        "error",
        "Two declarations with one identity (a path, a unit, an account) hold different "
        "values, so the image could only keep one of them.",
        "Declare it once. To change an inherited declaration in a profile, edit it in the "
        "profile's changes= instead of declaring it again.",
    ),
    Rule(
        "fragment-conflict",
        "error",
        "Two different component configurations share one name, so their declarations "
        "and exports would mix.",
        "Use one configuration of the component, or give the second one its own name.",
    ),
    Rule(
        "fragment-requires-missing",
        "error",
        "A component needs another component that the profile does not include.",
        "Add the required component to components= (or to the profile's add=).",
    ),
    Rule(
        "target-inconsistent",
        "error",
        "A profile targets outputs that drop the cloud platform integration it inherits, "
        "so its UKI carries glue for a cloud it never boots on.",
        "Keep every inherited cloud target in the profile's outputs, or extend a profile "
        "that has no cloud integration.",
    ),
    Rule(
        "unit-collision",
        "error",
        "Two declarations write the same unit file, so one would silently replace the other.",
        "Rename one of them. Change a packaged unit with Unit(name, dropin=...), and a "
        "Service's own unit through its fields.",
    ),
    Rule(
        "port-unreachable",
        "warning",
        "A service listens on a port the firewall drops, so nothing outside reaches it.",
        "Open it with Firewall(allow=(PORT,)), or Firewall(collect=True) to open every port "
        "a service declares.",
    ),
    Rule(
        "network-missing",
        "warning",
        "The profile serves ports or syncs time but brings up no network interface.",
        "Add Network() (TeeBase(network=True) does), or ship a .network file.",
    ),
    Rule(
        "step-undefined",
        "error",
        "A boot step orders itself after or before a step the profile does not declare.",
        "Declare that step in the profile, or drop it from after=/before=.",
    ),
    Rule(
        "step-cycle",
        "error",
        "The boot steps' after= and before= form a cycle, so runtime-init has no order to "
        "run them in.",
        "Drop one after=/before= edge of the cycle the message names.",
    ),
    Rule(
        "needs-undefined",
        "error",
        "A unit needs a boot step or boot task the profile does not declare, so it would "
        "wait forever.",
        "Declare the step or the Task in the profile, or drop it from needs=.",
    ),
    Rule(
        "needs-without-init",
        "warning",
        "A unit waits for runtime-init, but the profile declares no runtime-init step, so "
        "it waits for nothing.",
        "Declare the boot step the unit waits for, or drop needs= so it starts without waiting.",
    ),
    Rule(
        "hook-after-undefined",
        "error",
        "A build hook runs after a hook the profile does not declare.",
        "Declare that hook in the profile, or drop it from after=.",
    ),
    Rule(
        "hook-order",
        "error",
        "A build hook is ordered after a hook of a later phase, which only runs after it.",
        "Drop the later hook from after=, or move this hook to that phase or a later one.",
    ),
    Rule(
        "disk-key-undefined",
        "error",
        "A disk is encrypted with a key that no Key in the profile declares, so it cannot "
        "be unlocked at boot.",
        "Declare Key(NAME, ...) in the same profile, or point the Disk at a declared key.",
        _MODULES,
    ),
    Rule(
        "secret-store-undefined",
        "error",
        "The secrets are stored on a disk no Disk in the profile declares (a warning when "
        "a module reports it).",
        "Declare Disk(NAME, ...) in the same profile, pass Secrets(store=) one of the "
        "declared disks, or store=None.",
        _MODULES,
    ),
    Rule(
        "export-stale",
        "error",
        "A component holds an export of a provider that this profile configures "
        "differently; a warning when the profile only edits declarations inside the "
        "provider, which exports do not follow.",
        "Pass the consumer the export of the component this profile includes, or change "
        "the provider through its own arguments instead of editing what it composed.",
    ),
    Rule(
        "executable-stale",
        "error",
        "A service runs a path a Build installed before a profile edited or removed it, so "
        "the command names a file the image no longer has.",
        "Point the service's command at what the edited Build installs, or edit the "
        "command in the same profile.",
    ),
    Rule(
        "external-delivery-unwired",
        "warning",
        "Nothing in the image can tell when an ExternalDelivery has written the secrets, "
        "so services start without them.",
        "Give the delivery unit= (the unit that writes them) or ready_path= (a file it "
        "writes when done), and wait for it with needs=[delivery.ready].",
    ),
    # ── components ──
    Rule(
        "tee-base-clock",
        "warning",
        "TeeBase(time=False) leaves the guest on the host's clock, so the host decides "
        "certificate expiry and attestation freshness.",
        "Keep TeeBase(time=True), the default: authenticated NTS time.",
    ),
    Rule(
        "network-no-route",
        "warning",
        "A static address without a gateway reaches its own subnet only, so NTS time and "
        "every remote endpoint fail.",
        'Give it a gateway: Network(static=StaticAddress("10.0.0.5/24", gateway="10.0.0.1")).',
    ),
    Rule(
        "firewall-ssh-open",
        "warning",
        "The firewall opens port 22 by hand, outside the SSH access that accepts only a key.",
        "Drop 22 from Firewall(allow=) and use SshAccess(...) or dev=DevAccess(...), which "
        "open it only with a key to accept.",
    ),
    Rule(
        "ssh-keys-baked",
        "warning",
        "SSH keys are baked into a cloud image, so every instance of it lets their holders "
        "in as root.",
        "Use SshAccess(from_delivery=True) so the control plane delivers the key at boot, "
        "or keep baked keys in a non-production profile with dev=DevAccess(...).",
    ),
    Rule(
        "dev-access-rsa-key",
        "info",
        "A development profile accepts an RSA SSH key where every current client makes ed25519.",
        "Make an ed25519 key (ssh-keygen -t ed25519) and pass its .pub instead.",
    ),
    Rule(
        "tdxs-validator-unchecked",
        "warning",
        "Tdxs validates peers' quotes with no expected measurements, so it accepts any "
        "genuine TD whatever it runs.",
        "Pass Tdxs(expected_measurements=...), or build it with Tdxs.from_policy(POLICY).",
    ),
    Rule(
        "observability-plaintext",
        "warning",
        "An Observability sink is http://, so the host and every hop read what the guest logs.",
        "Point the sink at an https:// URL.",
    ),
    Rule(
        "nethermind-volatile-state",
        "warning",
        "Nethermind keeps its database on the volatile root, so it resyncs from genesis at "
        "every boot.",
        "Pass state=volume.directory('nethermind') on a Volume.",
    ),
    Rule(
        "raiko-listens-publicly",
        "warning",
        "Raiko listens beyond the loopback, so requests reach the prover without going "
        "through its AttestedTLS ingress.",
        "Keep Raiko(listen='127.0.0.1:PORT'), the default, and reach it through the ingress.",
    ),
    # ── modules ──
    Rule(
        "disk-key-path-mismatch",
        "warning",
        "A disk reads its key from a path other than the one its Key writes, so the unlock "
        "finds no key.",
        "Set Key(NAME, output=PATH) to the path the Disk reads, or point the Disk at the "
        "key's output.",
        _MODULES,
    ),
    Rule(
        "disk-device-guessed",
        "error",
        "A disk that is formatted names no device, and automatic selection can pick the "
        "boot disk and format it (a warning under Policy(storage_safety='warn')).",
        "Name a stable device (device='/dev/disk/by-id/virtio-SERIAL'), or format='never' "
        "for a disk prepared beforehand.",
        _MODULES,
    ),
    Rule(
        "disk-device-unstable",
        "warning",
        "A disk is formatted through a kernel name (/dev/sdb), which follows probe order "
        "and can point at another disk on the next boot.",
        "Name the disk by a stable link: /dev/disk/by-id/ (a virtio serial, a SCSI WWN) or "
        "/dev/disk/by-path/.",
        _MODULES,
    ),
    Rule(
        "secret-target-unsupported",
        "error",
        "A secret goes to a target its delivery does not write at boot, so it never arrives.",
        "Name what writes it with Secrets(delivery=ExternalDelivery(NAME, supports=...)), "
        "add the target kind to that delivery's supports=, or remove the target.",
        _MODULES,
    ),
    Rule(
        "secret-env-service-unknown",
        "error",
        "A SecretEnv delivers to a service the profile neither generates, ships nor enables.",
        "Declare the Service or Unit in the same profile, enable a packaged unit with "
        "Unit(NAME, enabled=True), or fix the SecretEnv's service=.",
        _MODULES,
    ),
    Rule(
        "key-pipe-outside-run",
        "info",
        "A key reads its pipe outside /run, where a FIFO survives reboots and can be "
        "replaced by a regular file.",
        "Place the pipe under /run, which is a tmpfs.",
        _MODULES,
    ),
    # ── the compiler: each profile's lowered state ──
    Rule(
        "service-user-missing",
        "error",
        "A service (or a unit's User=/Group=) runs as an account that nothing creates, so "
        "systemd refuses to start it.",
        "Declare the User or Group in the same profile, or drop user= to run as a system "
        "user named after the service.",
        (_rule_service_user_missing, _rule_unit_text),
    ),
    Rule(
        "user-group-undefined",
        "error",
        "A user joins a group that nothing creates, so useradd fails during the build.",
        "Declare the Group in the same profile, or drop it from the user's groups=.",
        (_rule_user_group_undefined,),
    ),
    Rule(
        "file-path-duplicate",
        "error",
        "One path is declared several times with different content, and only the last "
        "one ends up in the image.",
        "Keep a single declaration; if two components write the path, drop one or merge "
        "their content into one File.",
        (_rule_file_path_duplicate,),
    ),
    Rule(
        "file-path-relative",
        "error",
        "A file path contains '..' and may escape the image root.",
        "Write the normalized absolute path instead.",
        (_rule_file_path_relative,),
    ),
    Rule(
        "service-command-not-shipped",
        "warning",
        "A service runs a binary that nothing in a package-free profile ships (a "
        "heuristic: no file, build or hook mentions it).",
        "Ship it with File(PATH, Path(...), mode=0o755), a Build that installs it, or the "
        "package that provides it; ignore it when the base image already has it.",
        (_rule_service_command_not_shipped,),
    ),
    Rule(
        "profile-empty",
        "info",
        "A profile declares nothing of its own, so it builds the same image as the one it extends.",
        "Declare what it adds in its add= or changes=, or remove it.",
        (_rule_profile_empty,),
    ),
    Rule(
        "debloat-removes-needed-unit",
        "warning",
        "The systemd debloat masks a unit a service is, or depends on, so the service "
        "stops or never starts.",
        "Keep it with Debloat(keep_units_extra=(UNIT,)), or drop the dependency.",
        (_rule_debloat_removes_needed_unit,),
    ),
    Rule(
        "debloat-removes-declared-file",
        "warning",
        "The debloat deletes a path at finalize, after a declared file was placed there, "
        "so the file never reaches the image.",
        "Keep the path with Debloat(keep_paths=(...)), or move the file outside it.",
        (_rule_debloat_removes_declared_file,),
    ),
    Rule(
        "source-unpinned",
        "warning",
        "A source build, built kernel or downloaded package has no pin in the lockfile, so "
        "a moving ref builds different bytes (the image's policy can make it an error or "
        "an info).",
        "Run `tundravm lock IMAGE` to pin every source at once (`tundravm build` then "
        "fetches each on the host), or declare an immutable source: Git(url, ref) with a "
        "40-hex commit ref, or Http(url, sha256=...). Locking names a dead upstream ref, "
        "with every other source it cannot resolve, before anything is written.",
        (_rule_source_unpinned,),
    ),
    Rule(
        "kernel-missing",
        "error",
        "A bootable image installs no kernel, so mkosi cannot build its UKI.",
        'Keep Debian(kernel_package="linux-image-amd64") for the distribution kernel, '
        'declare Kernel(...) to build one, or Setting("Content", "Bootable", ("no",)) with '
        "Debian(boot=None) for a non-bootable image.",
        (_rule_kernel_missing,),
    ),
    Rule(
        "kernel-modules-ignored",
        "warning",
        "Kernel modules are selected, but the built kernel has no loadable modules, so the "
        "selection does nothing.",
        "Build those drivers into the kernel config and drop the selection, or build the "
        "kernel with loadable modules.",
        (_rule_built_kernel_modules,),
    ),
    Rule(
        "debloat-removes-modules",
        "error",
        "The debloat removes /usr/lib/modules while the kernel loads its drivers (virtio, "
        "tdx_guest, cloud NICs) from there, so they never load.",
        "Debloat(keep_modules=True) keeps them; Debloat(keep_modules=False) removes them on "
        "purpose; or build a Kernel with everything the image needs built in.",
        (_rule_debloat_removes_modules, _rule_built_kernel_modules),
    ),
    Rule(
        "recipe-arch-unsupported",
        "error",
        "An aarch64 image uses what only boots x86-64: EfiStub's amd64 stub or the qemu "
        "target. TDX guests are x86-64.",
        "Build for x86_64 (Debian(..., arch='x86_64')), or drop EfiStub and the qemu target "
        "from the aarch64 image.",
        (_rule_recipe_arch_unsupported,),
    ),
    Rule(
        "secret-in-file",
        "error",
        "A shipped file holds what looks like a secret, baked into every copy of the image "
        "and every registry that holds it.",
        "Deliver it at boot: a Secret with a SecretFile target, written by the profile's "
        "ExternalDelivery. If it is not a secret, pass allow_secret=True to the "
        "declaration.",
        (_rule_secret_in_file,),
    ),
    Rule(
        "secret-in-env",
        "error",
        "A unit's environment or command line holds what looks like a secret, baked into "
        "every copy of the image.",
        "Deliver it at boot: a Secret with a SecretEnv target, written by the profile's "
        "ExternalDelivery. If it is not a secret, pass allow_secret=True to the "
        "declaration.",
        (_rule_secret_in_env,),
    ),
    # ── systemd unit text: every unit a profile ships, raw or generated ──
    Rule(
        "unit-section-invalid",
        "error",
        "A unit has a section its type does not allow, which systemd ignores.",
        "Move its directives to [Unit], [Install] or the unit type's own section.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-directive-unknown",
        "warning",
        "A unit sets a directive systemd 257 does not know, which systemd ignores.",
        "Check the spelling against systemd.directives(7); X- keys are left alone.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-value-invalid",
        "error",
        "A unit gives a directive a value it does not accept, so systemd keeps the default.",
        "Use a value the directive accepts; the message names what is wrong with it.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-exec-relative",
        "warning",
        "A unit command (ExecStart= and the other Exec directives) names its program by a "
        "relative name, and systemd's search path is not $PATH.",
        "Spell the absolute path (/usr/bin/NAME or /usr/sbin/NAME).",
        _UNIT_TEXT,
    ),
    Rule(
        "state-dir-mkdir",
        "info",
        "ExecStartPre= creates a state directory that systemd can create and own for the "
        "service user.",
        "Use StateDirectory=, LogsDirectory=, RuntimeDirectory= or CacheDirectory= instead.",
        _UNIT_TEXT,
    ),
    Rule(
        "relation-conflict",
        "error",
        "A unit's relations contradict each other: it pulls in and conflicts with a unit, "
        "is ordered both before and after one, or names itself.",
        "Drop one of the relations.",
        _UNIT_TEXT,
    ),
    Rule(
        "requires-without-after",
        "info",
        "A unit Requires= another without After=, so both start in parallel.",
        "Add After= for the same unit, so this one starts once it is up.",
        _UNIT_TEXT,
    ),
    Rule(
        "socket-service-unpaired",
        "error",
        "A socket activates a service the profile neither ships nor enables (a warning "
        "when the profile installs packages, which may ship it).",
        "Ship the service next to the socket, or name the one it starts with Service=.",
        _UNIT_TEXT,
    ),
    Rule(
        "timer-unit-missing",
        "error",
        "A timer activates a unit the profile neither ships nor enables (a warning when the "
        "profile installs packages, which may ship it).",
        "Ship the unit next to the timer, or name the one it starts with Unit=.",
        _UNIT_TEXT,
    ),
    Rule(
        "timer-service-enabled",
        "warning",
        "A service a timer starts is also enabled at boot, so it runs off schedule.",
        "Leave it out of [Install] and do not enable it; enable the timer alone.",
        _UNIT_TEXT,
    ),
    Rule(
        "socket-service-started",
        "info",
        "A service a socket starts on demand is also enabled at boot.",
        "Leave it out of [Install] and do not enable it; enable the socket alone.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-enable-noop",
        "warning",
        "A unit is enabled but has no [Install] section, so systemctl enable does nothing.",
        "Add [Install] WantedBy=, or stop enabling it.",
        _UNIT_TEXT,
    ),
    Rule(
        "cloud-glue-unconditional",
        "warning",
        "A unit that only works on one cloud has no ConditionVirtualization= for it, while "
        "the profile's UKI also boots elsewhere, where it runs and fails.",
        "Add conditions=(Condition.virtualization(ID),) to the Service or Task (or "
        "ConditionVirtualization=ID to the unit text) so it stays inert elsewhere.",
        _UNIT_TEXT,
    ),
    # ── the image file's own declarations ──
    Rule(
        "service-runs-as-root",
        "info",
        "A Service or Task runs as root, so a compromise owns the whole guest.",
        "Drop user= to run as a system user named after the unit (declared for you); "
        "grant one capability with Hardening.strict(capabilities=(...)) if it needs one.",
    ),
    Rule(
        "hardening-disabled",
        "warning",
        "A unit runs without a sandbox (Hardening.none()).",
        "Use Hardening.relaxed(), or relax one knob of Hardening.strict(...) (jit=True, "
        "devices=(...), capabilities=(...)) instead of dropping them all.",
    ),
    Rule(
        "hardening-relaxed",
        "info",
        "A unit relaxes knobs of the strict sandbox, and each one widens what a "
        "compromised process can do.",
        "Keep the knobs the program does not need; the message lists the relaxed ones.",
    ),
    Rule(
        "unit-extra",
        "info",
        "A service's unit=DropIn(...), or raw unit directives, set what a Service keyword "
        "covers, which nothing then types or validates.",
        "Use the keyword the finding names (resources=, shutdown=, environment=, ...); keep "
        "unit=DropIn(...) for what no keyword covers.",
    ),
    Rule(
        "unit-raw",
        "info",
        "A service is hand-written unit text instead of a typed Service.",
        "Declare Service(NAME, command, ...): the compiler renders the unit, its ordering "
        "after needs= and its enablement.",
    ),
    Rule(
        "unit-as-file",
        "info",
        "A unit file is shipped as a File, so enabled=, needs= and the unit lint skip it.",
        "Ship it as Unit(name, text).",
    ),
    Rule(
        "setting-raw",
        "info",
        "A raw mkosi Setting has a typed spelling.",
        "Declare the typed field the message names; Setting is for keys tundravm has no field for.",
    ),
    Rule(
        "hook-covered",
        "info",
        "A build hook or boot step runs a command a declaration already covers (useradd, "
        "systemctl enable, mkdir and chown).",
        "Declare what the message names instead of the command.",
    ),
    Rule(
        "mkosi-configure-script",
        "error",
        "A Setting adds mkosi ConfigureScripts, which rewrite the configuration after "
        "`mkosi summary` resolved it, so the summary would not describe the build.",
        "Declare what the script would set as typed declarations or Settings.",
    ),
    Rule(
        "mkosi-environment-passthrough",
        "error",
        "A Setting passes the host's environment into the build, so the build depends on "
        "the machine it runs on.",
        "Give each variable its value: Setting('Build', 'Environment', ('NAME=value',)).",
    ),
    # ── the lock, the toolchain, the host and the committed tree ──
    Rule(
        "lock-missing",
        "error",
        "A production build has no lockfile to build against: a production build is a "
        "locked build, so its bytes reproduce and its manifest records every pin (a "
        "development build writes the lockfile first; `check` notes it).",
        "Run `tundravm lock IMAGE`, review and commit the lockfile, then build.",
    ),
    Rule(
        "lock-changed",
        "error",
        "A section of the image or of one profile (or a source's ref) changed since the lock.",
        _LOCK_AGAIN,
    ),
    Rule(
        "local-changed",
        "error",
        "A Local source's directory changed since the lock: its content digest is not the one "
        "the lock pins, so a build refuses it.",
        "Review the change, then run `tundravm lock IMAGE` to record the new digest and "
        "commit the lockfile.",
    ),
    Rule(
        "lock-added",
        "error",
        "The image has a section the lock does not record (a new profile or source).",
        _LOCK_AGAIN,
    ),
    Rule(
        "lock-removed",
        "error",
        "The lock records a section the image no longer has.",
        _LOCK_AGAIN,
    ),
    Rule(
        "lock-stale",
        "error",
        "Every section matches the lock, but the image's digest does not.",
        _LOCK_AGAIN,
    ),
    Rule(
        "toolchain-drift",
        "warning",
        "The running toolchain (mkosi, the tools tree, tundravm, the measurement tools) "
        "differs from the one the lock records, so a build may differ (an error with "
        "strict_toolchain).",
        "Run the locked tools, or `tundravm lock --update toolchain` to record this "
        "toolchain instead.",
    ),
    Rule(
        "tool-missing",
        "error",
        "A host tool the build backend needs is missing (a warning when it is optional).",
        "Install the tool the message names; `tundravm doctor` lists what this host can do.",
    ),
    Rule(
        "tree-drift",
        "error",
        "A file of the committed mkosi tree differs from what the image compiles to.",
        "Rewrite the tree with `tundravm build --tree-only DIR`, review the diff and commit it.",
    ),
)

RULES: dict[str, Rule] = {rule.code: rule for rule in _REGISTRY}
"""Every code ``tundravm check`` reports, by code."""

_PASSES: tuple[RuleCheck, ...] = tuple(dict.fromkeys(c for rule in _REGISTRY for c in rule.checks))
"""The compiler passes :func:`check` runs, each once."""


def _sort_key(d: Diagnostic) -> tuple[str, int, str, str, str]:
    return (d.profile, _SEVERITY[d.severity], d.code, d.subject or "", d.message)


def check(image: Lowered, *, profiles: Sequence[str] | None = None) -> list[Diagnostic]:
    """Run every rule over *profiles* (default: the image's active profiles)."""
    names = tuple(profiles) if profiles is not None else image.active
    found: dict[Diagnostic, None] = {}
    for name in dict.fromkeys(names):
        state = image.state.effective_profile(name)
        for rule in _PASSES:
            found.update(dict.fromkeys(rule(image, name, state)))
    return sorted(found, key=_sort_key)


def summarize(diagnostics: Sequence[Diagnostic]) -> dict[str, int]:
    return {
        "errors": sum(d.severity == "error" for d in diagnostics),
        "warnings": sum(d.severity == "warning" for d in diagnostics),
        "infos": sum(d.severity == "info" for d in diagnostics),
    }


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


_SOURCE_LINE = re.compile(r"(?P<file>.+):(?P<line>[0-9]+)")
"""A ``Diagnostic.location`` naming a source line: ``file:line``."""


def _where(d: Diagnostic) -> str:
    """*d*'s profile as the reports show it: ``image`` for the whole image."""
    return d.profile or "image"


def render(diagnostics: Sequence[Diagnostic]) -> str:
    """Human-readable report, one line per finding plus a summary line.

    A finding several profiles share is one line naming them (``[dev, production]``), and
    a hint consecutive findings share follows the last of them, once. The summary counts
    findings, one per profile, and says how many lines they took when that differs.
    """
    if not diagnostics:
        return "no findings"
    merged = _merged(diagnostics)
    lines: list[str] = []
    for index, (d, where) in enumerate(merged):
        subject = f" {d.subject}" if d.subject else ""
        lines.append(f"{d.severity} {d.code} [{where}]{subject}: {d.message}")
        if d.location:
            lines.append(f"    at: {d.location}")
        following = merged[index + 1][0].hint if index + 1 < len(merged) else None
        if d.hint and d.hint != following:
            run = 1
            while run <= index and merged[index - run][0].hint == d.hint:
                run += 1
            shared = f" (all {run} above)" if run > 1 else ""
            lines.append(f"    hint{shared}: {d.hint}")
    lines.append(render_summary(diagnostics, lines=len(merged)))
    return "\n".join(lines)


def _merged(diagnostics: Sequence[Diagnostic]) -> list[tuple[Diagnostic, str]]:
    """*diagnostics* in order, those that differ only in their profile as one, each with
    the profiles it is in (``dev, production``)."""
    groups: dict[Diagnostic, list[str]] = {}
    for d in diagnostics:
        groups.setdefault(replace(d, profile=""), []).append(_where(d))
    return [(d, ", ".join(dict.fromkeys(where))) for d, where in groups.items()]


def explain(codes: Sequence[str]) -> str:
    """What each of *codes* checks and how to fix it, as ``tundravm check --explain`` prints it.

    A code :data:`RULES` does not register is a :class:`~tundravm.errors.ValidationError`
    whose hint names the closest code and the lint reference.
    """
    unknown = [code for code in codes if code not in RULES]
    if unknown:
        close = difflib.get_close_matches(unknown[0], list(RULES), n=1)
        guess = f"Did you mean {close[0]!r}? " if close else ""
        raise ValidationError(
            f"Unknown check code(s): {', '.join(unknown)}.",
            hint=f"{guess}docs/reference/lint.md lists every code tundravm checks; a code "
            "a Component's check() returns is explained by that component.",
        )
    blocks = []
    for code in dict.fromkeys(codes):
        rule = RULES[code]
        summary, fix = (
            textwrap.fill(
                text, 88, initial_indent="    ", subsequent_indent="    ", break_on_hyphens=False
            )
            for text in (rule.summary, f"fix: {rule.fix}")
        )
        blocks.append(f"{rule.code} ({rule.severity})\n{summary}\n{fix}")
    return "\n\n".join(blocks)


def explain_command(diagnostics: Sequence[Diagnostic]) -> str:
    """``explain: tundravm check --explain CODE [--explain CODE ...]`` naming each registered
    code of *diagnostics* once; ``""`` when none is registered."""
    codes = [code for code in dict.fromkeys(d.code for d in diagnostics) if code in RULES]
    flags = " ".join(f"--explain {code}" for code in codes)
    return f"explain: tundravm check {flags}" if codes else ""


def render_summary(diagnostics: Sequence[Diagnostic], *, lines: int | None = None) -> str:
    """``no findings`` or ``N errors, N warnings, N infos``, one finding per profile.

    *lines* is how many lines the report printed them on: ``(1 line)`` follows when it is
    not one line per finding.
    """
    if not diagnostics:
        return "no findings"
    counts = summarize(diagnostics)
    text = ", ".join(
        (
            _plural(counts["errors"], "error"),
            _plural(counts["warnings"], "warning"),
            _plural(counts["infos"], "info"),
        )
    )
    if lines is not None and lines != len(diagnostics):
        text += f" ({_plural(lines, 'line')})"
    return text


def render_github(
    diagnostics: Sequence[Diagnostic], recipe_path: str | Path, *, strict: bool = False
) -> str:
    """GitHub workflow commands, one ``::error``/``::warning``/``::notice`` per finding.

    Each line is ``::error file=RECIPE,title=<code>::[variant] subject: message (hint)``,
    so findings show inline on the pull request; a finding whose ``location`` is
    ``file:line`` is annotated at that file and line instead. With *strict*, warnings
    (which fail the run) are reported as errors. A plain summary line follows.
    """
    if not diagnostics:
        return "no findings"
    path = annotation_path(recipe_path)
    lines: list[str] = []
    for d in diagnostics:
        command = _GITHUB_COMMANDS[d.severity]
        if strict and d.severity == "warning":
            command = "error"
        subject = f" {d.subject}" if d.subject else ""
        hint = f" ({d.hint})" if d.hint else ""
        message = f"[{_where(d)}]{subject}: {d.message}{hint}"
        where = _SOURCE_LINE.fullmatch(d.location)
        if where is None:
            lines.append(workflow_command(command, message, file=path, title=d.code))
        else:
            source = annotation_path(where["file"])
            line = where["line"]
            lines.append(workflow_command(command, message, file=source, line=line, title=d.code))
    lines.append(render_summary(diagnostics))
    return "\n".join(lines)


def failing(diagnostics: Sequence[Diagnostic], *, strict: bool = False) -> bool:
    """True when a finding fails ``check``: an error, or with *strict* a warning."""
    levels = {"error", "warning"} if strict else {"error"}
    return any(d.severity in levels for d in diagnostics)


_GITHUB_COMMANDS: dict[str, str] = {"error": "error", "warning": "warning", "info": "notice"}


# ── Raw mkosi settings and build commands (recipe declarations) ─────────
#
# ``declaration_check`` reads the recipe's own declarations rather than the lowered
# state: a typed field and the ``Setting`` it replaces lower to the same options.
# Declarations composed by tundravm's shipped fragments are skipped (the recipe
# cannot change them).

TYPED_SETTINGS: dict[tuple[str, str], str] = {
    ("Output", "Seed"): "Mkosi(seed=...)",
    ("Output", "OutputDirectory"): "Mkosi(output_dir=...)",
    ("Output", "CompressOutput"): "Mkosi(compress=...)",
    ("Output", "ManifestFormat"): "Mkosi(manifest=...)",
    ("Build", "PackageCacheDirectory"): "Mkosi(package_cache=...)",
    ("Build", "WithNetwork"): "Mkosi(build_network=...)",
    ("Content", "CleanPackageMetadata"): "Debian(clean_package_metadata=...)",
}
"""mkosi keys a typed field sets, and that field (lint ``setting-raw``)."""
_VARIANT_SETTING = " The field is image-wide: keep the setting when only this profile needs it."
_CHROOT = "mkosi-chroot "
_COVERED: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"groupadd\b.*?(?P<name>[a-z_][a-z0-9_-]*)$"), 'Group("{name}")'),
    (re.compile(r"useradd\b.*?(?P<name>[a-z_][a-z0-9_-]*)$"), 'User("{name}")'),
    (
        re.compile(
            r"usermod\s+(?:-a\s+-G|-aG|--append\s+--groups)\s+(?P<groups>\S+)\s+(?P<name>\S+)$"
        ),
        'User("{name}", groups={groups})',
    ),
    (re.compile(r"systemctl\s+enable\s+(?P<name>\S+)$"), 'Unit("{name}", enabled=True)'),
    (re.compile(r"systemctl\s+disable\s+(?P<name>\S+)$"), 'Unit("{name}", enabled=False)'),
    (re.compile(r"systemctl\s+mask\s+(?P<name>\S+)$"), 'Unit("{name}", masked=True)'),
)
"""Build-time commands a declaration already runs, and that declaration (``hook-covered``)."""


def declaration_check(recipe: Recipe, *, variants: Sequence[str] | None = None) -> list[Diagnostic]:
    """``setting-raw`` and ``hook-covered`` findings of *recipe*'s own declarations.

    Declarations of ``Recipe.items`` and the platform report with variant ``""``,
    a variant's ``add`` with its name (only when *variants* selects it).
    """

    selected = set(variants) if variants is not None else {v.name for v in recipe.variants}
    groups = [("", (*recipe.platform.contribution(), *recipe.items))]
    groups += [(v.name, (v.add,)) for v in recipe.variants if v.name in selected]
    found: list[Diagnostic] = []
    for variant, items in groups:
        own = list(_own_declarations(items))
        for item in own:
            found.extend(_declaration_findings(item, variant))
    return found


def _own_declarations(items: Iterable[object]) -> Iterator[object]:
    """*items* flattened through fragments, but not into tundravm's shipped composites and
    components (a component's fragment type lives in the component's module)."""
    from .declarative.model import Fragment

    for item in items:
        if isinstance(item, Fragment):
            shipped = type(item) is not Fragment and type(item).__module__.startswith("tundravm.")
            if not shipped:
                yield from _own_declarations(item.items)
        else:
            yield item


def _unreproducible_setting(setting: Any, variant: str) -> Iterator[Diagnostic]:
    """``mkosi-configure-script`` and ``mkosi-environment-passthrough``: a ``Setting`` the
    compiler refuses, since ``mkosi summary`` would not describe the build, or the host's
    environment would enter it."""
    subject = f"{setting.section}.{setting.key}"
    if setting.key == "ConfigureScripts":
        yield Diagnostic(
            code="mkosi-configure-script",
            severity="error",
            message=f"Setting {subject} rewrites the configuration after `mkosi summary` "
            "resolved it",
            subject=subject,
            profile=variant,
            hint="Declare what the script would set as typed declarations or Settings.",
        )
    passed = [v for v in setting.values if "=" not in v]
    if setting.key == "PassEnvironment" or (setting.key == "Environment" and passed):
        yield Diagnostic(
            code="mkosi-environment-passthrough",
            severity="error",
            message=f"Setting {subject} passes the host's {', '.join(passed or setting.values)} "
            "into the build",
            subject=subject,
            profile=variant,
            hint="Give each variable its value: Setting('Build', 'Environment', ('NAME=value',)).",
        )


def _declaration_findings(item: object, variant: str) -> Iterator[Diagnostic]:
    from .declarative.model import BootStep, Hook, Setting

    if isinstance(item, Setting):
        yield from (_located(d, item) for d in _unreproducible_setting(item, variant))
        typed = TYPED_SETTINGS.get((item.section, item.key))
        if typed is not None:
            yield _located(
                Diagnostic(
                    code="setting-raw",
                    severity="info",
                    message=f"Setting {item.section}.{item.key} has a typed spelling: {typed}",
                    subject=f"{item.section}.{item.key}",
                    profile=variant,
                    hint=f"Declare {typed} instead; Setting is for keys tundravm has no field for."
                    + (
                        _VARIANT_SETTING
                        if variant and typed.startswith(("Mkosi", "Debian"))
                        else ""
                    ),
                ),
                item,
            )
    elif isinstance(item, Hook | BootStep):
        chroot = isinstance(item, Hook)
        for declaration in _covered(item.script, chroot=chroot):
            yield _located(
                Diagnostic(
                    code="hook-covered",
                    severity="info",
                    message=f"{type(item).__name__} {item.name!r} runs what {declaration} declares",
                    subject=item.name,
                    profile=variant,
                    hint=f"Declare {declaration} instead of the command.",
                ),
                item,
            )


def _covered(script: str, *, chroot: bool) -> list[str]:
    """The declarations that cover commands of *script* (a hook's lines run through
    ``mkosi-chroot`` when *chroot*; a boot step's run in the image as they are)."""
    found: list[str] = []
    commands = [part.strip() for line in script.splitlines() for part in re.split(r"&&|;", line)]
    for command in commands:
        if chroot:
            if not command.startswith(_CHROOT):
                continue
            command = command[len(_CHROOT) :].strip()
        for pattern, template in _COVERED:
            if match := pattern.match(command):
                fields = match.groupdict()
                if "groups" in fields:
                    fields["groups"] = repr(tuple(fields["groups"].split(",")))
                found.append(template.format(**fields).replace("'", '"'))
    words = [command.split()[0] for command in commands if command.split()]
    if not chroot and "mkdir" in words and "chown" in words:
        found.append("RuntimeDirectory(path, owner=...)")
    return list(dict.fromkeys(found))


def _located(diagnostic: Diagnostic, item: object) -> Diagnostic:
    from ._location import display_location

    where = getattr(item, "declared_at", "")
    return replace(diagnostic, location=display_location(where)) if where else diagnostic


__all__ = [
    "RULES",
    "TYPED_SETTINGS",
    "Diagnostic",
    "Rule",
    "RuleCheck",
    "check",
    "declaration_check",
    "explain",
    "explain_command",
    "unit_declaration_check",
    "failing",
    "render",
    "render_github",
    "render_summary",
    "summarize",
]
