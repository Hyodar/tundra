"""The ``Module`` base class of the runtime tools lowering configures, and their source."""

from __future__ import annotations

import functools
from collections.abc import Mapping
from pathlib import Path

from tundravm._source import GitSource, LocalSource
from tundravm.errors import ValidationError

TOOLS_SUBDIR = "tools"
"""The directory of this repository that holds the Go module the runtime tools build from."""
TOOLS_LOCAL = "tundravm:tools"
"""The path the lock records for the tools of the checkout tundravm runs from."""

TOOL_PROGRAMS: Mapping[str, Mapping[str, str]] = {
    "attested-endpoint": {},
    "disk-setup": {
        "cryptsetup": "cryptsetup",
        "mkfs.ext4": "e2fsprogs",
        "mount": "mount",
        "umount": "mount",
    },
    "image-metrics": {"systemctl": "systemd"},
    "key-gen": {},
    "mode-switch": {"nft": "nftables"},
    "tdxs": {},
}
"""Each ``tools/cmd`` command an image runs: the programs it executes, and the Debian
package that ships each. A module that installs a command lists those packages, so the
image has what the command runs (``tests/compiler/test_unit_programs.py`` reads the Go
code for the programs)."""

TPM_PROGRAMS: Mapping[str, str] = {
    "tpm2_nvdefine": "tpm2-tools",
    "tpm2_nvread": "tpm2-tools",
    "tpm2_nvundefine": "tpm2-tools",
    "tpm2_nvwrite": "tpm2-tools",
}
"""What ``key-gen`` executes for a TPM-sealed key alone (``tools/pkg/tpm``)."""

HOST_TOOLS = frozenset({"attested-client", "measured-boot"})
"""The ``tools/cmd`` commands that run on the operator's host, never in an image."""


def tool_packages(tool: str) -> tuple[str, ...]:
    """The packages that ship what *tool* executes (:data:`TOOL_PROGRAMS`), sorted."""
    return tuple(sorted(set(TOOL_PROGRAMS[tool].values())))


def tools_checkout() -> Path | None:
    """``<checkout>/tools`` when tundravm runs from a checkout of its repository, else None.

    A checkout is the directory holding ``src/tundravm`` (this package) and ``tools/go.mod``.
    """
    package = Path(__file__).resolve().parents[1]
    root = package.parents[1]
    tools = root / TOOLS_SUBDIR
    if package.parent.name == "src" and (tools / "go.mod").is_file():
        return tools
    return None


def tools_commit() -> tuple[str, str]:
    """``(repository url, commit)`` of the installed tundravm, as ``tundravm new`` pins it.

    Raises :class:`~tundravm.errors.ValidationError` naming the fix when the install
    records no commit (an editable install of a copy that is not a git checkout).
    """
    from tundravm.templates import tundravm_source

    installed = tundravm_source()
    if installed.pinned:
        return installed.url, installed.rev
    raise ValidationError(
        "Cannot tell which tundravm commit is installed, so the image cannot build its "
        "evidence, key, disk and secret tools from tools/ at that commit.",
        hint=f'Install tundravm from git (uv add "tundravm @ git+{installed.url}@COMMIT"), '
        f'or pass the tools source: Tdxs(source=Git("{installed.url}", COMMIT, '
        f'subdir="{TOOLS_SUBDIR}")).',
    )


@functools.cache
def tools_source() -> GitSource | LocalSource:
    """The source the runtime tools build from by default: this repository's ``tools/``.

    From a checkout (:func:`tools_checkout`), that directory as a local source, pinned by
    its content digest; otherwise the repository at the installed commit
    (:func:`tools_commit`), ``subdir="tools"``.
    """
    checkout = tools_checkout()
    if checkout is not None:
        return LocalSource(TOOLS_LOCAL, directory=checkout)
    url, commit = tools_commit()
    return GitSource(url, commit, subdir=TOOLS_SUBDIR)


class Module:
    """A runtime tool's configuration, recorded on each profile it configures.

    Lowering (``tundravm.declarative.state``) writes what the module renders
    (its config files, source build and ``/usr/bin/runtime-init`` step) and
    records it per profile; ``check(image, profile)`` contributes the
    module's diagnostics to ``check()`` for each of those profiles.

    The base class declares no instance state, so ``@dataclass(slots=True)``
    subclasses work unchanged.
    """

    __slots__ = ()


__all__ = [
    "HOST_TOOLS",
    "TOOLS_LOCAL",
    "TOOLS_SUBDIR",
    "TOOL_PROGRAMS",
    "TPM_PROGRAMS",
    "Module",
    "tools_checkout",
    "tools_commit",
    "tool_packages",
    "tools_source",
]
