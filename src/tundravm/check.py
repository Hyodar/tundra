"""Compiler diagnostics on a lowered recipe, reported by ``lint()`` and ``tundravm check``.

``RULES`` registers every code tundravm itself reports, wherever it comes from
(resolution, a module, the compiler, unit text, the lock, the host, ``--tree``), as a
:class:`Rule`: its severity, what it checks and how to fix it. A component's codes are
the rules its class declares (``Component.rules``). ``tundravm check --explain CODE``
prints one, and ``docs/reference/lint.md`` lists them.
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
import shlex
import textwrap
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._modules.init import is_init_path
from ._secret_scan import Finding, is_unit_path, scan_file, scan_unit
from ._source import DebFile, KernelFile, KernelSource, LocalSource, NamedSource
from .compiler.emit_mkosi import _systemd_unit_content
from .diagnostics import Diagnostic, Rule, RuleCheck, Severity, tundravm_sets
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
from .project import lock_command

if TYPE_CHECKING:
    from ._units.lint import ShippedUnit
    from .declarative._lowered import Lowered
    from .declarative.model import Recipe, Service, Task

_SEVERITY: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


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

_ESSENTIAL_BINARIES = frozenset(
    {
        "basename",
        "bash",
        "cat",
        "chgrp",
        "chmod",
        "chown",
        "cmp",
        "cp",
        "cut",
        "dash",
        "date",
        "dd",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "env",
        "expr",
        "false",
        "find",
        "findmnt",
        "flock",
        "grep",
        "gzip",
        "head",
        "hostname",
        "id",
        "install",
        "kill",
        "ln",
        "logger",
        "ls",
        "mkdir",
        "mktemp",
        "mount",
        "mountpoint",
        "mv",
        "nice",
        "nohup",
        "nsenter",
        "perl",
        "printf",
        "readlink",
        "realpath",
        "rm",
        "rmdir",
        "run-parts",
        "runuser",
        "sed",
        "setpriv",
        "sh",
        "sha256sum",
        "sleep",
        "sort",
        "stat",
        "sync",
        "tail",
        "tar",
        "tee",
        "test",
        "timeout",
        "touch",
        "tr",
        "true",
        "umount",
        "uname",
        "unshare",
        "wc",
        "xargs",
        "yes",
    }
)
"""What Debian's Essential packages (coreutils, util-linux, bash, dash, perl-base, ...) put
in ``/usr/bin`` and ``/usr/sbin``: every image has them, whatever it lists."""

_PACKAGE_BINARIES: Mapping[str, frozenset[str]] = {
    "bind9-dnsutils": frozenset({"dig", "nslookup"}),
    "dnsutils": frozenset({"dig", "nslookup"}),
    "e2fsprogs": frozenset({"e2fsck", "mkfs.ext4", "resize2fs"}),
    "iproute2": frozenset({"bridge", "ip", "ss", "tc"}),
    "iputils-ping": frozenset({"ping"}),
    "kmod": frozenset({"insmod", "lsmod", "modprobe", "rmmod"}),
    "net-tools": frozenset({"ifconfig", "netstat", "route"}),
    "netcat-openbsd": frozenset({"nc"}),
    "nodejs": frozenset({"node", "nodejs"}),
    "openssh-client": frozenset({"scp", "ssh", "ssh-keygen"}),
    "openssh-server": frozenset({"scp", "ssh", "ssh-keygen", "sshd"}),
    "procps": frozenset({"free", "pgrep", "pkill", "ps", "sysctl", "top"}),
    "python3": frozenset({"python3"}),
    "python3-minimal": frozenset({"python3"}),
    "ruby": frozenset({"ruby"}),
    "systemd": frozenset(
        {
            "busctl",
            "journalctl",
            "systemctl",
            "systemd-cat",
            "systemd-creds",
            "systemd-escape",
            "systemd-notify",
            "systemd-run",
            "systemd-socket-activate",
            "systemd-tmpfiles",
        }
    ),
    "udev": frozenset({"udevadm"}),
}
"""The programs a package ships under a name other than its own, beside the interpreters
a converted unit runs (``python3``, ``node``, ``ruby``; ``perl``, ``bash`` and ``sh`` are
essential)."""

_PROGRAM_PACKAGE = {
    "node": "nodejs",
    "python3": "python3",
    "ruby": "ruby",
    "sshd": "openssh-server",
}
"""The package a ``service-program-missing`` finding names for a program it knows."""

_BIN_DIRS = ("/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")
_EXEC_PREFIXES = "@-:+!|"
"""systemd's ``ExecStart=`` prefixes, which come before the program."""


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
            f"Declare User({user!r}) in users= of the image or profile {profile_name!r}, or "
            "drop user= to run as the system user named after the service, declared for you."
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
                    f"Add Group({group!r}) to groups= of the image or profile "
                    f"{profile_name!r}, or drop it from the User's groups=; useradd fails "
                    "on an unknown group."
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


def _program(command: tuple[str, ...]) -> str | None:
    """The program *command* runs (systemd's lookup for a bare name), ``None`` when it
    is a variable only the running unit expands."""
    program = command[0].lstrip(_EXEC_PREFIXES)
    if not program or "$" in program:
        return None
    return _norm(program) if "/" in program else f"/usr/bin/{program}"


def _packaged(program: str, packages: Iterable[str]) -> bool:
    """Whether a listed package (or an essential one) plausibly ships *program*.

    Without the packages' file lists: a program in a ``bin`` directory named after a
    package, one the table knows a package for, or an essential one; anywhere else, a
    directory named after a listed package. ``systemd`` is in every image.
    """
    name = posixpath.basename(program)
    listed = {*packages, "systemd"}
    if program.startswith(_BIN_DIRS):
        if name in _ESSENTIAL_BINARIES or name in listed:
            return True
        return any(name in _PACKAGE_BINARIES.get(package, ()) for package in listed)
    parts = posixpath.dirname(program).split("/")
    return any(
        part in listed or any(p.startswith(f"{part}-") for p in listed) for part in parts if part
    )


def _shipped(state: ProfileState) -> tuple[set[str], tuple[str, ...]]:
    """The paths a profile ships itself, and the directories it ships whole."""
    paths = _declared_paths(state)
    if state.init_scripts:
        paths.update(_INIT_FILE_PATHS)
    trees: list[str] = []
    for build in state.source_builds.values():
        for step in build.install:
            if step.kind == "tree":
                trees.append(_norm(step.dest).rstrip("/") + "/")
            else:
                paths.add(_norm(step.dest))
    return paths, tuple(trees)


def _rule_service_program_missing(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    paths, trees = _shipped(state)
    mentions = _command_text(state) + "\n" + "\n".join(t for _, t in state.build_sources)
    for svc in state.services:
        program = _program(svc.command) if svc.command else None
        if program is None or program in paths or program.startswith(trees):
            continue
        if program in mentions or _packaged(program, state.packages):
            continue
        package = _PROGRAM_PACKAGE.get(posixpath.basename(program))
        add = (
            f"its package: packages=[{package!r}]" if package else "its Debian package in packages="
        )
        yield Diagnostic(
            severity="error",
            code="service-program-missing",
            message=f"service {svc.name!r} runs {program}, which nothing in the image ships",
            hint=(
                f"List {add}, or build it: a GoBinary, CargoBinary or Build as the argv's "
                "first element puts its program in the image. A file of your own is "
                f"File.copy(Path(...), {program!r}, mode=0o755)."
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


TMPFILES_SETUP = "systemd-tmpfiles-setup.service"
TMPFILES_DIRS = ("/etc/tmpfiles.d/", "/usr/lib/tmpfiles.d/")


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
                    "Rename the Service: the debloat masks the systemd units tundravm does "
                    f"not keep, so report {unit!r} if the image needs it."
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
                    "Drop the dependency (a Requires= on a masked unit stops the service), "
                    f"or report {dep!r} if the image needs it: tundravm decides which "
                    "systemd units the debloat keeps."
                ),
                profile=profile_name,
                subject=svc.name,
            )
    if not masked(TMPFILES_SETUP):
        return
    for path in sorted(_declared_paths(state)):
        if path.startswith(TMPFILES_DIRS):
            yield Diagnostic(
                severity="warning",
                code="debloat-removes-needed-unit",
                message=f"{path} is applied at boot by {TMPFILES_SETUP}, which debloat masks",
                hint=(
                    "Ship the path itself: File(..., owner=) or Tree(..., owner=), whose owner "
                    "the runtime-init step owners applies, or a service's state= or runtime= "
                    "for a directory it writes."
                ),
                profile=profile_name,
                subject=path,
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
                        f"Ship the file outside {pattern}, which the image empties at the "
                        "end of the build; a service's logs= and state= directories are "
                        "made at boot."
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
            subject=f"sources.{spec.name}",
        )


def _unpinned_advice(spec: NamedSource) -> tuple[str, str]:
    """The ``source-unpinned`` message and hint for the unpinned lockable source *spec*."""
    source = spec.source
    fetch = " (`tundravm build` then fetches it on the host)"
    if isinstance(spec, KernelSource):
        return (
            f"kernel {spec.version} ({source.kind} {source.url} @ {source.requested}) "
            "is not pinned in the lockfile",
            f"Run `{lock_command()}` to pin it as {spec.name!r}{fetch}, or give "
            "Kernel a Git source with a 40-hex commit ref.",
        )
    if isinstance(spec, KernelFile):
        return (
            f"the kernel's {spec.name} ({source.kind} {source.url}) is not pinned in the lockfile",
            f"Run `{lock_command()}` to pin its sha256 as {spec.name!r}{fetch}.",
        )
    if isinstance(spec, DebFile):
        return (
            f"the EFI stub package ({source.kind} {source.url}) is not pinned in the lockfile",
            f"Run `{lock_command()}` to pin its sha256 as {spec.name!r} "
            "(`tundravm build` then downloads it on the host). "
            "Unpinned, the postinst hook downloads it in the build sandbox.",
        )
    if isinstance(source, LocalSource):
        return (
            f"source build {spec.name!r} ({source.describe()}) is not pinned in the lockfile",
            f"Run `{lock_command()}` to pin its content digest (`tundravm build` then "
            "copies the directory into its workspace).",
        )
    return (
        f"source build {spec.name!r} ({source.kind} {source.url} @ {source.requested}) "
        "is not pinned in the lockfile",
        f"Run `{lock_command()}` to pin it{fetch}, or declare an immutable source "
        "(Git(url, ref) with a 40-hex commit ref, or Http(url, sha256=...)).",
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
        hint=tundravm_sets("the kernel package"),
        profile=profile_name,
    )


_UNHARDENED_FIX = (
    "Drop hardening=False from Kernel.build(...); to keep one option it turns off, set that "
    'option in a fragment: fragments=[KernelFragment({"CONFIG_IO_URING": "y"})].'
)


def _rule_kernel_unhardened(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    """``Kernel.build(hardening=False)``: an error in a production profile, a warning in a
    development one."""
    kernel = image.kernel
    if kernel is None or kernel.hardening is not False:
        return
    production = profile_name not in image.unreleased
    yield Diagnostic(
        severity="error" if production else "warning",
        code="kernel-unhardened",
        message=(
            "the built kernel is not hardened (Kernel.build(hardening=False)): it keeps every "
            "subsystem its base config turns on"
            + (", and this production profile ships it" if production else "")
        ),
        hint=_UNHARDENED_FIX,
        profile=profile_name,
        subject="kernel",
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
            hint=tundravm_sets("a built kernel's modules"),
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
                hint=tundravm_sets("whether the debloat keeps the kernel's modules"),
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
        hint=tundravm_sets("whether the debloat keeps the kernel's modules"),
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
            code="image-arch-unsupported",
            message=(
                "the platform's arch is aarch64 but EfiStub installs the amd64 systemd-boot-efi "
                "package, whose x86-64 EFI stub cannot boot an arm64 UKI"
            ),
            hint=tundravm_sets("the architecture"),
            profile=profile_name,
            subject="EfiStub",
        )
    if "qemu" in state.output_targets:
        yield Diagnostic(
            severity="error",
            code="image-arch-unsupported",
            message=(
                "the platform's arch is aarch64 but the profile targets qemu, whose deploy adapter "
                "boots images with qemu-system-x86_64"
            ),
            hint=tundravm_sets("the architecture"),
            profile=profile_name,
            subject="qemu",
        )


SECRET_HINT = (
    "Deliver it at boot instead of baking it into the image: declare Secret(NAME) and pass "
    "it in the service's argv or environment=, where it renders as the path the service "
    "reads it at and binds it; `tundravm secrets deliver` sends it after attestation."
)
"""The secret lints' hint, and the fix of ``secret-in-file`` and ``secret-in-env``."""


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
                hint=SECRET_HINT,
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
                hint=SECRET_HINT,
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
                hint=SECRET_HINT,
                profile=profile_name,
                subject=path,
            )


def _rule_instance_key_exposed(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    from ._modules import instance_key
    from ._modules import probe as probe_module

    key = instance_key.KEY
    owned = {instance_key.SCRIPT_PATH, probe_module.SCRIPT_PATH}
    bound = f"LoadCredential={instance_key.CREDENTIAL}:{key}"
    found: list[tuple[str, str]] = []
    for path, content in _shipped_files(state):
        if path not in owned and (key in path or key in _decoded(content)):
            found.append((path, f"the file {path}"))
    for svc in state.services:
        name = unit_name(svc.name)
        if not svc.command or name == instance_key.UNIT:
            continue
        lines = _systemd_unit_content(svc).splitlines()
        if any(key in line for line in lines if line != bound):
            found.append((name, f"service {name!r}"))
    for subject, what in found:
        yield Diagnostic(
            severity="error",
            code="instance-key-exposed",
            message=f"{what} names {key}, the instance key, which only a bound service reads",
            hint="Bind it instead: Service(credentials=[InstanceKey()]) (or InstanceKey() in "
            "argv or environment=), and read $CREDENTIALS_DIRECTORY/instance-key.",
            profile=profile_name,
            subject=subject,
        )


# ── systemd unit text (every unit a variant ships) ──────────────────────
#
# ``tundravm._units.lint`` holds the rules; these wrappers feed them the variant's
# units (shipped files and generated services) and the facts they check against.


def _profile_units(state: ProfileState) -> list[ShippedUnit]:
    """Every unit *state* ships: its unit files and drop-ins, then the services it generates."""
    from ._units.lint import ShippedUnit

    units: list[ShippedUnit] = []
    for path, content in _shipped_files(state):
        if is_unit_path(path) and not path.endswith("/"):
            subject = path if posixpath.dirname(path).endswith(".d") else posixpath.basename(path)
            units.append(ShippedUnit(subject, path, _decoded(content)))
    shipped = {unit.name for unit in units if not unit.is_dropin}
    for svc in state.services:
        name = unit_name(svc.name)
        if svc.command and name not in shipped:
            path = f"/usr/lib/systemd/system/{name}"
            units.append(ShippedUnit(svc.name, path, _systemd_unit_content(svc), generated=True))
    return units


def _decoded(content: str | bytes) -> str:
    return content.decode("utf-8", "replace") if isinstance(content, bytes) else content


def _rule_unit_text(image: Lowered, profile_name: str, state: ProfileState) -> Iterator[Diagnostic]:
    from ._units.lint import Facts, unit_findings

    units = _profile_units(state)
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


_UNIT_DIRS = (
    "/etc/systemd/system",
    "/run/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
)
"""Where systemd loads units from: what runs in the image is what is there at build time."""

_WRITERS = frozenset({"cp", "dd", "install", "ln", "mv", "rsync", "tee", "touch", "truncate"})
"""Programs that write the paths they are given."""

_SHELLS = frozenset({"bash", "dash", "sh"})
_EXEC_LINE = re.compile(r"^\s*Exec\w+\s*=\s*(?P<command>.*)$", re.MULTILINE)
_SHELL_SPLIT = re.compile(r"&&|\|\||[;&|\n]")
_TMPFILES_WRITES = frozenset("fFwLC")
"""tmpfiles.d line types that create or write the path they name."""


def _unit_dir_path(word: str) -> str | None:
    """The path under a unit directory *word* names (``of=PATH`` and ``--opt=PATH`` too)."""
    for candidate in (word, word.partition("=")[2]):
        path = posixpath.normpath(candidate) if candidate.startswith("/") else ""
        if any(path == d or path.startswith(d + "/") for d in _UNIT_DIRS):
            return path
    return None


def _argv_writes(argv: Sequence[str]) -> str | None:
    """The unit path *argv* writes, when its program is a writer or a ``sh -c`` script."""
    words = list(argv)
    while words and ("=" in words[0] and not words[0].startswith("/") or words[0] == "env"):
        words.pop(0)
    if not words:
        return None
    program = posixpath.basename(words[0].lstrip("-@:+!"))
    if program in _SHELLS and "-c" in words[1:-1]:
        return _script_writes(words[words.index("-c") + 1])
    writes = program in _WRITERS or (program == "sed" and any(w.startswith("-i") for w in words))
    if program == "systemctl":
        writes = any(w in ("edit", "link") for w in words[1:])
    if not writes:
        return None
    return next((path for w in words[1:] if (path := _unit_dir_path(w)) is not None), None)


def _script_writes(script: str) -> str | None:
    """The unit path a shell *script* writes: a redirection or a writer's argument."""
    for command in _SHELL_SPLIT.split(script):
        try:
            words = shlex.split(command, comments=True)
        except ValueError:
            words = command.split()
        for index, word in enumerate(words):
            if word.lstrip("0123456789&").startswith(">") and ">&" not in word:
                target = word.lstrip("0123456789&>") or (words[index + 1 :] or [""])[0]
                path = _unit_dir_path(target)
                if path is not None:
                    return path
        found = _argv_writes(words)
        if found is not None:
            return found
    return None


def _rule_unit_written_at_runtime(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    found: list[tuple[str, str, str]] = []
    for unit in _profile_units(state):
        for match in _EXEC_LINE.finditer(unit.text.replace("\\\n", " ")):
            try:
                argv = shlex.split(match["command"])
            except ValueError:
                argv = match["command"].split()
            path = _argv_writes(argv)
            if path is not None:
                found.append((unit.subject, f"unit {unit.subject!r}", path))
                break
    for entry in state.init_scripts:
        path = _script_writes(entry.script)
        if path is not None:
            found.append((entry.name, f"boot step {entry.name!r}", path))
    for path, content in _shipped_files(state):
        if not path.startswith(("/etc/tmpfiles.d/", "/usr/lib/tmpfiles.d/")):
            continue
        for line in _decoded(content).splitlines():
            fields = line.split()
            if len(fields) < 2 or fields[0].startswith("#"):
                continue
            target = _unit_dir_path(fields[1])
            if fields[0][0] in _TMPFILES_WRITES and target is not None:
                found.append((path, f"the tmpfiles.d line {fields[0]} {fields[1]}", target))
    for subject, what, path in found:
        yield Diagnostic(
            severity="error",
            code="unit-written-at-runtime",
            message=(
                f"{what} writes {path} at boot, so the attested image does not pin what "
                "that unit runs"
            ),
            hint=(
                "Declare the unit as a Service(...) in services=, so the measurement "
                "covers it; a value it needs at boot comes from a Secret(NAME) in "
                "credentials=, never from writing the unit."
            ),
            profile=profile_name,
            subject=subject,
        )


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


_METADATA_FIX = (
    "A service rarely needs it: tundravm's cloud glue already reads the hostname, the "
    "forwarded IPs and the provisioning state, and the identity token lets whoever holds it "
    "act as the instance in the cloud. Deliver what the service needs as a Secret, and keep "
    "Egress(metadata=True) for a program that must speak for the instance itself."
)


def _typed_unit_findings(item: Service | Task, variant: str) -> Iterator[Diagnostic]:
    """``service-runs-as-root``, ``service-metadata``, ``hardening-disabled`` and
    ``hardening-relaxed`` of a typed ``Service``/``Task``."""
    from ._units.render import STRICT_HARDENING
    from .declarative.model import CLOUD_METADATA
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
            hint="Drop user= to run as a system user named after the service (declared for "
            'you); grant one capability with hardening=Hardening.preset("static", '
            "capabilities=[...]) if it needs one.",
        )
    if any(rule.host in CLOUD_METADATA for rule in item.egress):
        yield Diagnostic(
            code="service-metadata",
            severity="warning",
            message=f"unit {name!r} reaches the cloud metadata server "
            "(egress=Egress(metadata=True)), which hands out the instance's identity token",
            subject=name,
            profile=variant,
            hint=_METADATA_FIX,
        )
    sandbox = item.sandbox if isinstance(item, TypedService) else item.hardening
    entries = STRICT_HARDENING if sandbox is None else sandbox.entries()
    if not entries:
        yield Diagnostic(
            code="hardening-disabled",
            severity="warning",
            message=f"unit {name!r} runs without a sandbox (hardening=Hardening.none())",
            subject=name,
            profile=variant,
            hint='Name the preset for its runtime (hardening="static", "jit", "node", '
            '"python" or "dotnet"), relax one knob of it (hardening=Hardening.preset('
            '"static", devices=[...], capabilities=[...])) or use hardening="relaxed", '
            "instead of dropping them all.",
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
            hint="Each relaxed knob widens what a compromised process can do; keep only the "
            "ones the program needs (docs/reference/api.md: Hardening).",
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
            f"Declare Service({posixpath.splitext(name)[0]!r}, argv, ...) instead: the "
            "compiler renders the unit, its ordering after requires= and its enablement."
        ),
    )


def _unit_as_file(path: str, variant: str) -> Diagnostic:
    return Diagnostic(
        code="unit-as-file",
        severity="info",
        message=f"the unit file {path!r} is shipped as a File",
        subject=path,
        profile=variant,
        hint=(
            f"Declare it as a Service({posixpath.splitext(posixpath.basename(path))[0]!r}, "
            "argv, ...) in services=, so tundravm renders, orders and sandboxes the unit; "
            "Service.packaged(NAME) runs a package's own."
        ),
    )


def _rule_module_checks(
    image: Lowered, profile_name: str, state: ProfileState
) -> Iterator[Diagnostic]:
    for module in image.applied_modules(profile_name, inherited=True):
        yield from module.check(image, profile_name)


_UNIT_TEXT = (_rule_unit_text,)
_MODULES = (_rule_module_checks,)
_LOCK_AGAIN = (
    "Review the change, then run `tundravm lock` to record it and commit the lockfile; "
    "`tundravm lock --check` shows the same drift without writing."
)

CONFIGURE_SCRIPT_HINT = (
    "Drop it: set what the script would set through the Image keywords or Mkosi(settings=)."
)
"""The fix of ``mkosi-configure-script``."""
ENVIRONMENT_HINT = "Drop it from Mkosi(settings=): the build sees no host variable."
"""The fix of ``mkosi-environment-passthrough``."""

_REGISTRY: tuple[Rule, ...] = (
    # ── resolution: the declarations of each profile ──
    Rule(
        "edit-invalid",
        "error",
        "A profile's edit sets a field to a value the declaration refuses.",
        "Fix the field values in the profile's changes=; the message carries the "
        "declaration's own error.",
        internal=True,
    ),
    Rule(
        "edit-missing",
        "error",
        "A profile's changes= edit a value it does not inherit from the image or the profile "
        "it extends, so the edit changes nothing.",
        "Edit only what the profile inherits (value.ref.edit(...) on a value the image or the "
        "extended profile declares); to declare the value in this profile, put it in add=.",
    ),
    Rule(
        "remove-missing",
        "error",
        "A profile's changes= remove a value it does not inherit from the image or the "
        "profile it extends, so the removal does nothing.",
        "Remove only what the profile inherits (value.ref.remove() on a value the image or "
        "the extended profile declares), or drop the removal from changes=.",
    ),
    Rule(
        "identity-collision",
        "error",
        "One thing is declared twice with different values (a File at one path, a Service and "
        "Service.packaged() of one name, a User), so the image could keep only one of them.",
        "Declare it once; a profile that needs another value edits the inherited one in "
        "changes= with value.ref.edit(...) instead of declaring it again in add=.",
    ),
    Rule(
        "component-conflict",
        "error",
        "Two configurations of one component are included under one name (components=, a "
        "profile's add=), so their declarations and exports would mix.",
        "Include one configuration of the component, or name the second one apart: a name= "
        'or id= field on it, or class Second(Component, name="...").',
    ),
    Rule(
        "component-requires-missing",
        "error",
        "A component holds an export (an Endpoint, a ServiceRef, a readiness) of a component "
        "the profile does not include.",
        "Add the component that exports it to components= (or to the profile's add=), beside "
        "the one that holds it.",
    ),
    Rule(
        "target-inconsistent",
        "error",
        "A profile targets outputs that drop the cloud platform integration it inherits, "
        "so its UKI carries glue for a cloud it never boots on.",
        tundravm_sets("each profile's targets"),
        internal=True,
    ),
    Rule(
        "unit-collision",
        "error",
        'Two services write one unit file (Service("app", argv) and Service("app", argv, '
        "timer=...)), so one silently replaces the other.",
        "Give each its own name; what a service's unit says changes through its keywords "
        "and unit=DropIn(...), never through a second Service.",
    ),
    Rule(
        "port-unreachable",
        "warning",
        "A service listens on a port the firewall drops, so nothing outside reaches it.",
        tundravm_sets("the ports the firewall opens: each service's expose="),
        internal=True,
    ),
    Rule(
        "network-missing",
        "warning",
        "The profile serves ports or syncs time but brings up no network interface.",
        tundravm_sets("the network: every image implies Network()"),
        internal=True,
    ),
    Rule(
        "step-undefined",
        "error",
        "A boot step orders itself after or before a step the profile does not declare.",
        tundravm_sets("the boot steps and their order"),
        internal=True,
    ),
    Rule(
        "step-cycle",
        "error",
        "The boot steps' after= and before= form a cycle, so runtime-init has no order to "
        "run them in.",
        tundravm_sets("the boot steps and their order"),
        internal=True,
    ),
    Rule(
        "needs-undefined",
        "error",
        "A unit waits for a boot step or boot task the profile does not declare, so it "
        "would wait forever.",
        tundravm_sets("what a unit waits for: a service's requires="),
        internal=True,
    ),
    Rule(
        "needs-without-init",
        "warning",
        "A unit waits for runtime-init, but the profile declares no runtime-init step, so "
        "it waits for nothing.",
        tundravm_sets("what a unit waits for: a service's requires="),
        internal=True,
    ),
    Rule(
        "hook-after-undefined",
        "error",
        "A build hook runs after a hook the profile does not declare.",
        tundravm_sets("the build hooks and their order"),
        internal=True,
    ),
    Rule(
        "hook-order",
        "error",
        "A build hook is ordered after a hook of a later phase, which only runs after it.",
        tundravm_sets("the build hooks and their order"),
        internal=True,
    ),
    Rule(
        "disk-key-undefined",
        "error",
        "A volume is encrypted with a key the profile does not declare, so it cannot be "
        "unlocked at boot.",
        tundravm_sets("a volume's key"),
        _MODULES,
        internal=True,
    ),
    Rule(
        "secret-store-undefined",
        ("error", "warning"),
        "The secrets are stored on a disk the profile does not declare.",
        tundravm_sets("where the secrets are stored"),
        _MODULES,
        when=("when the compiled profile, not its declarations, lacks the disk",),
        internal=True,
    ),
    Rule(
        "export-stale",
        ("error", "warning"),
        "A component holds an export of a provider this profile configures differently: "
        "another configuration in add=, or changes= editing what the provider composed, "
        "which its exports do not follow.",
        "Pass the consumer the export of the configuration this profile includes, or change "
        "the provider through its own fields instead of editing what it composed.",
        when=(
            "when the profile only edits what the provider composed, which exports do not follow",
        ),
    ),
    Rule(
        "executable-stale",
        "error",
        "A service's argv names a path a Build installed before the profile's changes= "
        "edited or removed that Build, so the image no longer has the file.",
        "Point the argv at what the edited Build installs (its install= destination), or "
        "edit the service's argv in the same profile.",
    ),
    Rule(
        "external-delivery-unwired",
        "warning",
        "Nothing in the image can tell when the secret delivery has written the secrets, "
        "so services start without them.",
        tundravm_sets("the secret delivery"),
        internal=True,
    ),
    Rule(
        "volume-tpm-on-cloud",
        "info",
        "A Volume(unlock=TpmSeal(...)) seals its key to a cloud's vTPM (Azure's paravisor, "
        "Google's hypervisor), so the platform, not an attested release, decides who opens "
        "it; today's key-gen binds no PCR.",
        "Unlock it with OperatorKey() (an operator sends the key after attesting the "
        "instance) or BrokerKey(url, audience=...) (a key broker does); keep TpmSeal(...) "
        "when that platform's vTPM is a boundary you accept.",
    ),
    # ── components ──
    Rule(
        "rule-undeclared",
        "error",
        "A component reports a code its class does not declare, so --explain cannot say "
        "what it checks or how to fix it.",
        "Declare the code on the component class: rules = (Rule(CODE, SEVERITY, SUMMARY, "
        "FIX),), with Rule from tundravm.components.",
    ),
    # ── modules ──
    Rule(
        "disk-key-path-mismatch",
        "warning",
        "A disk reads its key from a path other than the one its key is written to, so the "
        "unlock finds no key.",
        tundravm_sets("where a volume's key is written"),
        _MODULES,
        internal=True,
    ),
    Rule(
        "disk-device-guessed",
        ("error", "warning"),
        "A disk that is formatted names no device, and automatic selection can pick the "
        "boot disk and format it.",
        tundravm_sets("each volume's device"),
        _MODULES,
        when=("under Policy(storage_safety='warn')",),
        internal=True,
    ),
    Rule(
        "disk-device-unstable",
        "warning",
        "A disk is formatted through a kernel name (/dev/sdb), which follows probe order "
        "and can point at another disk on the next boot.",
        tundravm_sets("each volume's device"),
        _MODULES,
        internal=True,
    ),
    Rule(
        "secret-target-unsupported",
        "error",
        "A secret goes to a target its delivery does not write at boot, so it never arrives.",
        tundravm_sets("where each secret is delivered"),
        _MODULES,
        internal=True,
    ),
    Rule(
        "secret-env-service-unknown",
        "error",
        "A secret is delivered into the environment of a service the profile neither "
        "generates, ships nor enables.",
        tundravm_sets("which service a secret is delivered to"),
        _MODULES,
        internal=True,
    ),
    Rule(
        "key-pipe-outside-run",
        "info",
        "A key reads its pipe outside /run, where a FIFO survives reboots and can be "
        "replaced by a regular file.",
        tundravm_sets("where a volume's key pipe is"),
        _MODULES,
        internal=True,
    ),
    # ── the compiler: each profile's lowered state ──
    Rule(
        "service-user-missing",
        "error",
        "A Service(user=...) (or a shipped unit file's User=/Group=) names an account that "
        "nothing creates, so systemd refuses to start it.",
        "Declare User(NAME) in users= (Group(NAME) in groups=) of the image or the same "
        "profile, or drop user= to run as the system user named after the service, which "
        "is declared for you.",
        (_rule_service_user_missing, _rule_unit_text),
    ),
    Rule(
        "user-group-undefined",
        "error",
        "A User(groups=[...]) names a group that nothing creates, so useradd fails during "
        "the build.",
        "Add Group(NAME) to groups= of the image or the same profile, or drop it from the "
        "User's groups=.",
        (_rule_user_group_undefined,),
    ),
    Rule(
        "file-path-duplicate",
        "error",
        "One path is shipped twice with different content (a File and a Tree holding the "
        "same path, two components), and only the last one ends up in the image.",
        "Keep one: drop the File, or leave the path out of the Tree with Tree(exclude=[...]); "
        "if two components write it, drop one or merge their content into one File.",
        (_rule_file_path_duplicate,),
    ),
    Rule(
        "file-path-relative",
        "error",
        "A file path contains '..' and may escape the image root.",
        "Write the normalized absolute path instead.",
        (_rule_file_path_relative,),
        internal=True,
    ),
    Rule(
        "service-program-missing",
        "error",
        "A Service's program (its argv's first element) is nothing the image ships: no "
        "File, Tree or Build installs it, and no listed package is known to.",
        "Add the Debian package that ships it to packages=, or run a GoBinary, CargoBinary "
        "or Build as the argv's first element, or ship it with File.copy(Path(...), PATH, "
        "mode=0o755).",
        (_rule_service_program_missing,),
    ),
    Rule(
        "profile-empty",
        "info",
        "A Profile declares nothing of its own (no add=, no changes=), so it builds the same "
        "image as the one it extends.",
        "Declare what it adds in add= or changes=, or take it out of profiles=.",
        (_rule_profile_empty,),
    ),
    Rule(
        "debloat-removes-needed-unit",
        "warning",
        "A Service is named like a systemd unit the image's debloat masks (systemd-*), or a "
        "shipped unit file depends on one, so it stops or never starts; or the image ships a "
        "tmpfiles.d file, which nothing applies: the debloat masks boot-time tmpfiles.",
        "Rename the Service, or drop the dependency; tundravm decides which systemd units "
        "the debloat keeps, so report one the image needs. Ship a tmpfiles.d path itself: "
        "File(owner=), Tree(owner=), or a service's state= or runtime=.",
        (_rule_debloat_removes_needed_unit,),
    ),
    Rule(
        "debloat-removes-declared-file",
        "warning",
        "A File is shipped under a directory the image empties at the end of the build "
        "(/var/log, /var/cache), so the file never reaches the image.",
        "Ship the file outside the directory the message names; a service's logs= and "
        "state= directories are made at boot.",
        (_rule_debloat_removes_declared_file,),
    ),
    Rule(
        "source-unpinned",
        ("warning", "error", "info"),
        "A source build, built kernel or downloaded package has no pin in the lockfile, so "
        "a moving ref builds different bytes.",
        "Run `tundravm lock` to pin every source at once (`tundravm build` then "
        "fetches each on the host), or declare an immutable source: Git(url, ref) with a "
        "40-hex commit ref, or Http(url, sha256=...). Locking names a dead upstream ref, "
        "with every other source it cannot resolve, before anything is written.",
        (_rule_source_unpinned,),
        when=(
            "when the policy refuses moving refs",
            "when the policy allows them",
        ),
    ),
    Rule(
        "kernel-missing",
        "error",
        "A bootable image installs no kernel, so mkosi cannot build its UKI.",
        tundravm_sets("the kernel package"),
        (_rule_kernel_missing,),
        internal=True,
    ),
    Rule(
        "kernel-unhardened",
        ("error", "warning"),
        "A built kernel without Tundra's hardening fragment keeps every subsystem of its "
        "base config, io_uring, userfaultfd and kexec among them.",
        _UNHARDENED_FIX,
        (_rule_kernel_unhardened,),
        when=("in a development profile (Profile(production=False), dev=)",),
    ),
    Rule(
        "kernel-modules-ignored",
        "warning",
        "Kernel modules are selected, but the built kernel has no loadable modules, so the "
        "selection does nothing.",
        tundravm_sets("a built kernel's modules"),
        (_rule_built_kernel_modules,),
        internal=True,
    ),
    Rule(
        "debloat-removes-modules",
        "error",
        "The debloat removes /usr/lib/modules while the kernel loads its drivers (virtio, "
        "tdx_guest, cloud NICs) from there, so they never load.",
        tundravm_sets("whether the debloat keeps the kernel's modules"),
        (_rule_debloat_removes_modules, _rule_built_kernel_modules),
        internal=True,
    ),
    Rule(
        "image-arch-unsupported",
        "error",
        "An aarch64 image uses what only boots x86-64: the amd64 EFI stub or the qemu "
        "target. TDX guests are x86-64.",
        tundravm_sets("the architecture"),
        (_rule_recipe_arch_unsupported,),
        internal=True,
    ),
    Rule(
        "secret-in-file",
        "error",
        "A File ships what looks like a secret (a private key, a token), baked into every "
        "copy of the image and every registry that holds it.",
        SECRET_HINT,
        (_rule_secret_in_file,),
    ),
    Rule(
        "secret-in-env",
        "error",
        "A Service's environment= or argv (or a shipped unit file) holds what looks like a "
        "secret, baked into every copy of the image.",
        SECRET_HINT,
        (_rule_secret_in_env,),
    ),
    Rule(
        "instance-key-exposed",
        "error",
        "A File, a unit=DropIn(...) or a shipped unit file names /run/tundravm/instance-key.pem, "
        "the private key the image generates at boot, which only the services that bind it "
        "may read, and only through their credentials.",
        "Bind it: Service(credentials=[InstanceKey()]) (or InstanceKey() in argv or "
        "environment=) renders LoadCredential= and the ordering; the program reads "
        "$CREDENTIALS_DIRECTORY/instance-key.",
        (_rule_instance_key_exposed,),
    ),
    Rule(
        "unit-written-at-runtime",
        "error",
        "A service's argv (cp, tee, install, ln, a shell redirection) or a shipped tmpfiles.d "
        "line writes a unit under /etc/systemd/system, /run/systemd/system or "
        "/usr/lib/systemd/system at boot, so the attested image does not pin what runs.",
        "Declare the unit as a Service(...) in services=; a value it needs at boot comes "
        "from a Secret(NAME) in credentials=, never from writing the unit.",
        (_rule_unit_written_at_runtime,),
    ),
    # ── systemd unit text: every unit a profile ships, raw or generated ──
    Rule(
        "unit-section-invalid",
        "error",
        "A shipped unit file (a File under a systemd unit directory) has a section its unit "
        "type does not allow, which systemd ignores.",
        "Move its directives to [Unit], [Install] or the unit type's own section; a "
        "Service(...) in services= renders a unit with the right sections.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-directive-unknown",
        "warning",
        "A shipped unit file sets a directive systemd 257 does not know, which systemd ignores.",
        "Check the spelling against systemd.directives(7); X- keys are left alone. A "
        "Service(...) keyword or unit=DropIn(...) is checked for you.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-value-invalid",
        "error",
        "A shipped unit file gives a directive a value it does not accept, so systemd keeps "
        "the default.",
        "Use a value the directive accepts; the message names what is wrong with it. A "
        "Service(...) keyword is checked for you.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-exec-relative",
        "warning",
        "A unit command (ExecStart= and the other Exec directives, unit=DropIn(exec_start_pre=) "
        "included) names its program by a relative name, and systemd's search path is not "
        "$PATH.",
        "Spell the absolute path (/usr/bin/NAME or /usr/sbin/NAME).",
        _UNIT_TEXT,
    ),
    Rule(
        "state-dir-mkdir",
        "info",
        "A unit=DropIn(exec_start_pre=...) (or a shipped unit file's ExecStartPre=) creates a "
        "state directory that systemd can create and own for the service user.",
        "Pass state=NAME, logs=NAME or runtime=NAME on the Service; a shipped unit file "
        "uses StateDirectory=, LogsDirectory=, RuntimeDirectory= or CacheDirectory=.",
        _UNIT_TEXT,
    ),
    Rule(
        "relation-conflict",
        "error",
        "A unit's relations contradict each other: it pulls in and conflicts with one unit "
        "(unit=DropIn(requires=[...], conflicts=[...])), is ordered both before and after "
        "one, or names itself.",
        "Drop one of the relations.",
        _UNIT_TEXT,
    ),
    Rule(
        "requires-without-after",
        "info",
        "A shipped unit file Requires= another without After=, so both start in parallel.",
        "Add After= for the same unit, so this one starts once it is up; a Service's "
        "requires=[other.ready] renders both.",
        _UNIT_TEXT,
    ),
    Rule(
        "socket-service-unpaired",
        ("error", "warning"),
        "A shipped .socket file activates a service the profile neither ships nor enables.",
        "Ship the service next to the socket, or name the one it starts with Service=; a "
        "Service(socket=Listen(...)) renders both units.",
        _UNIT_TEXT,
        when=("when the profile installs packages, which may ship it",),
    ),
    Rule(
        "timer-unit-missing",
        ("error", "warning"),
        "A shipped .timer file activates a unit the profile neither ships nor enables.",
        "Ship the unit next to the timer, or name the one it starts with Unit=; a "
        'Service(timer=Every("1h")) renders both units.',
        _UNIT_TEXT,
        when=("when the profile installs packages, which may ship it",),
    ),
    Rule(
        "timer-service-enabled",
        "warning",
        "A service a shipped .timer file starts is also enabled at boot, so it runs off schedule.",
        'Declare the schedule on the service instead, Service(..., timer=Every("1h")) or '
        "Calendar(...), which enables the timer alone; a shipped unit file leaves [Install] "
        "out of the service.",
        _UNIT_TEXT,
    ),
    Rule(
        "socket-service-started",
        "info",
        "A service a shipped .socket file starts on demand is also enabled at boot.",
        "Declare the socket on the service instead, Service(..., socket=Listen(...)), which "
        "enables the socket alone; a shipped unit file leaves [Install] out of the service.",
        _UNIT_TEXT,
    ),
    Rule(
        "unit-enable-noop",
        "warning",
        "A unit the image enables (Service.packaged(NAME)) is shipped by a File without an "
        "[Install] section, so systemctl enable does nothing.",
        "Add [Install] WantedBy=multi-user.target to the shipped unit text, or declare it as "
        "a Service(NAME, argv) instead of shipping its file.",
        _UNIT_TEXT,
    ),
    Rule(
        "cloud-glue-unconditional",
        "warning",
        "A unit that only works on one cloud (a cloud agent's) has no ConditionVirtualization= "
        "for it, while the image's targets= also boot elsewhere, where it runs and fails.",
        "Gate it on its cloud, so it stays inert elsewhere: Service(..., unit=DropIn("
        'extra={"Unit": {"ConditionVirtualization": "ID"}})).',
        _UNIT_TEXT,
    ),
    # ── the image file's own declarations ──
    Rule(
        "service-runs-as-root",
        "info",
        'A Service runs as root (user="root"), so a compromise owns the whole guest.',
        "Drop user= to run as a system user named after the service (declared for you); "
        'grant one capability with hardening=Hardening.preset("static", capabilities=[...]) '
        "if it needs one.",
    ),
    Rule(
        "service-metadata",
        "warning",
        "A Service reaches the cloud metadata server (egress=Egress(metadata=True)), which "
        "hands out the instance's identity token.",
        _METADATA_FIX,
    ),
    Rule(
        "hardening-disabled",
        "warning",
        "A Service runs without a sandbox (hardening=Hardening.none()).",
        'Name the preset for its runtime (hardening="static", "jit", "node", "python" or '
        '"dotnet"), relax one knob of it (hardening=Hardening.preset("static", '
        'devices=[...], capabilities=[...])) or use hardening="relaxed", instead of '
        "dropping them all.",
    ),
    Rule(
        "hardening-relaxed",
        "info",
        'A Service relaxes knobs of the strict sandbox (hardening="relaxed", or a preset\'s '
        "deviations), and each one widens what a compromised process can do.",
        "Keep only the knobs the program needs; the message lists the relaxed ones.",
    ),
    Rule(
        "unit-extra",
        "info",
        "A Service's unit=DropIn(...) (or a shipped unit file) sets what a Service keyword "
        "covers, which nothing then types or validates.",
        "Use the keyword the finding names (resources=, shutdown=, environment=, ...); keep "
        "unit=DropIn(...) for what no keyword covers.",
    ),
    Rule(
        "unit-raw",
        "info",
        "A service is hand-written unit text instead of a typed Service.",
        "Declare Service(NAME, argv, ...): the compiler renders the unit, its ordering "
        "after requires= and its enablement.",
        internal=True,
    ),
    Rule(
        "unit-as-file",
        "info",
        "A unit file is shipped as a File under a systemd unit directory, so tundravm "
        "neither renders, orders nor sandboxes it.",
        "Declare it as a Service(NAME, argv, ...) in services=; its timer= and socket= "
        "render those units, and Service.packaged(NAME) runs a package's own.",
    ),
    Rule(
        "setting-raw",
        "info",
        "An mkosi setting sets a key tundravm already sets itself.",
        "Drop it from Mkosi(settings=); it is for keys tundravm does not set.",
        internal=True,
    ),
    Rule(
        "hook-covered",
        "info",
        "A Mkosi(scripts=[...]) script runs a command a declaration already covers (useradd, "
        "groupadd, usermod, systemctl enable).",
        "Declare what the message names (User(...), Group(...), Service(...)) instead of "
        "running the command.",
    ),
    Rule(
        "mkosi-configure-script",
        "error",
        "An mkosi setting adds ConfigureScripts, which rewrite the configuration after "
        "`mkosi summary` resolved it, so the summary would not describe the build.",
        CONFIGURE_SCRIPT_HINT,
        internal=True,
    ),
    Rule(
        "mkosi-environment-passthrough",
        "error",
        "An mkosi setting passes the host's environment into the build, so the build "
        "depends on the machine it runs on.",
        ENVIRONMENT_HINT,
        internal=True,
    ),
    # ── the lock, the toolchain, the host and the committed tree ──
    Rule(
        "lock-missing",
        "error",
        "A production build has no lockfile to build against: a production build is a "
        "locked build, so its bytes reproduce and its manifest records every pin (a "
        "development build writes the lockfile first; `check` notes it).",
        "Run `tundravm lock`, review and commit the lockfile, then build.",
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
        "Review the change, then run `tundravm lock` to record the new digest and "
        "commit the lockfile; a development build (`tundravm build --dev`) re-pins it itself.",
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
        "differs from the one the lock records, so a build may differ.",
        "Run the locked tools, or `tundravm lock --update toolchain` to record this "
        "toolchain instead.",
    ),
    Rule(
        "tool-missing",
        ("error", "warning"),
        "A host tool the build backend needs is missing.",
        "Install the tool the message names; `tundravm doctor` lists what this host can do.",
        when=("when the tool is optional",),
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


@dataclass(frozen=True, slots=True)
class Declared:
    """A rule a component class declares, and which class declares it where."""

    rule: Rule
    origin: str
    """``App in demo.py``; ``TeeBase in tundravm.components`` for a shipped component."""


def component_rules(classes: Iterable[type]) -> dict[str, Declared]:
    """The rules each of *classes* declares (``Component.rules``), by code: the first class
    to declare a code is the one that explains it."""
    found: dict[str, Declared] = {}
    for cls in classes:
        origin = f"{cls.__name__} in {_declared_in(cls)}"
        for rule in getattr(cls, "rules", ()):
            found.setdefault(rule.code, Declared(rule, origin))
    return found


def _declared_in(cls: type) -> str:
    """Where *cls* is written: ``tundravm.components`` for a shipped component, else its
    file, relative to the working directory."""
    import inspect

    from ._location import display_location

    if cls.__module__.startswith("tundravm.components."):
        return "tundravm.components"
    try:
        file = inspect.getsourcefile(cls)
    except TypeError:
        file = None
    return display_location(f"{file}:0").rpartition(":")[0] if file else cls.__module__


def shipped_rules() -> dict[str, Declared]:
    """The rules the shipped components of :mod:`tundravm.components` declare, by code."""
    from . import components

    shipped = (getattr(components, name) for name in components.__all__)
    return component_rules(
        cls for cls in shipped if isinstance(cls, type) and issubclass(cls, components.Component)
    )


def explain(codes: Sequence[str], declared: Mapping[str, Declared] | None = None) -> str:
    """What each of *codes* checks and how to fix it, as ``tundravm check --explain`` prints it.

    A code is looked up in :data:`RULES`, then in *declared* (default: the shipped
    components' rules), and a component's rule says which class declares it; an internal
    rule says an image file cannot reach it. An unknown code is a
    :class:`~tundravm.errors.ValidationError` whose hint names the closest code and the
    lint reference.
    """
    components = shipped_rules() if declared is None else declared
    known = [*RULES, *components]
    unknown = [code for code in codes if code not in known]
    if unknown:
        close = difflib.get_close_matches(unknown[0], known, n=1)
        guess = f"Did you mean {close[0]!r}? " if close else ""
        raise ValidationError(
            f"Unknown check code(s): {', '.join(unknown)}.",
            hint=f"{guess}docs/reference/lint.md lists every code tundravm and its shipped "
            "components check; a code an image's own component declares is explained with "
            "the image: tundravm check IMAGE --explain CODE.",
        )
    blocks = []
    for code in dict.fromkeys(codes):
        origin = None if code in RULES else components[code].origin
        rule = RULES[code] if origin is None else components[code].rule
        summary, fix = (
            textwrap.fill(
                text, 88, initial_indent="    ", subsequent_indent="    ", break_on_hyphens=False
            )
            for text in (rule.summary, f"fix: {rule.fix}")
        )
        by = "" if origin is None else f"\n    declared by {origin}"
        if rule.internal:
            by = "\n    internal: an image file cannot reach it; one that does is a bug to report"
        heading = textwrap.fill(
            f"{rule.code}: {rule.severity_text}",
            88,
            subsequent_indent="    ",
            break_on_hyphens=False,
            break_long_words=False,
        )
        blocks.append(f"{heading}{by}\n{summary}\n{fix}")
    return "\n\n".join(blocks)


def explain_command(
    diagnostics: Sequence[Diagnostic],
    declared: Mapping[str, Declared] | None = None,
    *,
    image: str | Path | None = None,
) -> str:
    """``explain: tundravm check --explain CODE [--explain CODE ...]`` naming each code of
    *diagnostics* once that :data:`RULES` or *declared* (default: the shipped components'
    rules) explains; ``""`` when none is.

    *image* is the image file the report was given on the command line: it is named when a
    code is one only its own components declare, which ``--explain`` finds through it.
    """
    components = shipped_rules() if declared is None else declared
    known = {*RULES, *components}
    codes = [code for code in dict.fromkeys(d.code for d in diagnostics) if code in known]
    flags = " ".join(f"--explain {code}" for code in codes)
    shipped = shipped_rules()
    own = any(code not in RULES and code not in shipped for code in codes)
    where = f"{image} " if image is not None and own else ""
    return f"explain: tundravm check {where}{flags}" if codes else ""


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

_SETS_IT = "Drop it: tundravm sets it, so a build reproduces."
TYPED_SETTINGS: dict[tuple[str, str], str] = {
    ("Output", "Seed"): "Drop it: tundravm sets a fixed seed, so the partition UUIDs reproduce.",
    ("Output", "OutputDirectory"): "Drop it: `tundravm build` publishes under build/.",
    ("Output", "CompressOutput"): _SETS_IT,
    ("Output", "ManifestFormat"): _SETS_IT,
    ("Build", "PackageCacheDirectory"): _SETS_IT,
    ("Build", "WithNetwork"): "Drop it: the build runs offline; a component whose builds fetch "
    "dependencies says class X(Component, build_network=True).",
    ("Content", "CleanPackageMetadata"): _SETS_IT,
}
"""mkosi keys the compiler sets from a typed field, each with the sentence the public
``Mkosi(settings=)`` refuses it with (``RESERVED_SETTINGS``): the hint of ``setting-raw``."""
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
    (
        re.compile(r"systemctl\s+enable\s+(?P<name>[^\s.]+)(?:\.service)?$"),
        'Service("{name}", argv)',
    ),
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
            hint=CONFIGURE_SCRIPT_HINT,
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
            hint=ENVIRONMENT_HINT,
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
                    message=f"Setting {item.section}.{item.key} sets what tundravm writes itself",
                    subject=f"{item.section}.{item.key}",
                    profile=variant,
                    hint=typed,
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
    "Declared",
    "Diagnostic",
    "Rule",
    "RuleCheck",
    "check",
    "component_rules",
    "declaration_check",
    "explain",
    "explain_command",
    "unit_declaration_check",
    "failing",
    "render",
    "render_github",
    "render_summary",
    "shipped_rules",
    "summarize",
]
