"""Persistent volumes: :class:`Volume`, how it is unlocked, and the directories on it.

A volume is a logical disk with a stable identity (plan section 6.2): the image says
what it is called, where it mounts and how its LUKS2 key arrives; capacity and the
attachment itself are deployment configuration. Today's ``key-gen`` and ``disk-setup``
(tools/) are the in-image half: the volume lowers onto them in
:mod:`tundravm._image._runtime`.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Literal

from tundravm._location import Located
from tundravm.errors import ValidationError

from .._keywords import Keywords
from ._component import BootReady
from ._secrets import RUNTIME_DIRECTORY, BrokerKey

__all__ = [
    "DEFAULT_MOUNT",
    "SERIAL_PREFIX",
    "TPM_PLATFORMS",
    "OperatorKey",
    "TpmSeal",
    "Unlock",
    "Volume",
    "VolumeDirectory",
]

SERIAL_PREFIX = "tundravm-"
"""Every volume's attachment name starts with it: a QEMU ``serial=``, a GCP device name."""

TPM_PLATFORMS: dict[str, str] = {
    "azure": "Azure's paravisor vTPM, inside the TD's trust boundary on Azure TDX VMs",
    "gcp": "GCP's vTPM, which Google's hypervisor provides: trusted only as far as the "
    "operator trusts Google",
    "swtpm": "a local swtpm with persisted state, which the host controls: development only",
}
"""The vTPMs a :class:`TpmSeal` can name, with the trust boundary each one is."""

_TARGET_TPM: dict[str, str] = {"azure": "azure", "gcp": "gcp", "qemu": "swtpm"}
"""The vTPM each target boots with."""

_VOLUME_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,10}")
_DIRECTORY = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_GROUP = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DEFAULT_MOUNT = "/persistent/<name>"
"""Where a volume mounts unless ``mount=`` says otherwise, ``<name>`` its name."""

_RESERVED_MOUNTS = ("/", "/usr", "/etc", "/boot", "/efi", "/run", "/proc", "/sys", "/dev")


@dataclass(frozen=True, slots=True)
class OperatorKey(Keywords):
    """Unlock with a passphrase an operator supplies on every boot (flashbots' model).

    ``tundravm secrets unlock INSTANCE VOLUME`` sends it over the attested channel once
    the instance has proved what it runs; nothing about it is in the image. The volume
    stays closed, and every service bound to it waits, until it arrives. Today the image
    waits on a FIFO (``/run/tundravm/unlock/<volume>``, ``key-gen``'s pipe strategy) that
    the attested endpoint writes (``POST /unlock``) on a session a fresh quote bound. The
    volume's LUKS2 header pins who operates the instance: the SSH key of the first signed
    write, whose signature over the session's nonce every later write needs, after every
    reboot (``tundravm secrets rotate`` hands it to another key; a lost key means a new
    volume). The control plane still answers before the volume opens.
    """

    def manifest(self) -> dict[str, object]:
        """The unlock as the runtime manifest records it."""
        return {"kind": "operator-key"}


@dataclass(frozen=True, slots=True)
class TpmSeal(Keywords):
    """Unlock with a random key sealed to a vTPM the operator trusts.

    Nothing about the trust boundary is implied, so every field is stated:

    - *platform*: the vTPM it seals to (:data:`TPM_PLATFORMS`): ``"azure"`` (the
      paravisor's), ``"gcp"`` (the hypervisor's) or ``"swtpm"`` (a local swtpm with
      persisted state, development only). They are not interchangeable: an image whose
      ``targets=`` boot anywhere else is refused, since one payload boots on every target
      and a key sealed on one platform does not open on another.
    - *pcrs*: the PCR policy the seal is bound to. Today's ``key-gen`` keeps the key in the
      vTPM's NV storage without a PCR policy; the runtime manifest records *pcrs* and gate
      3's tools/ work enforces it.
    - *recovery*: what happens when the vTPM state is lost. ``"none"`` is the only method
      today: the data is gone, and the volume is never reformatted for it (plan 6.2: an
      unlock failure is never permission to format).
    """

    _: KW_ONLY
    platform: Literal["azure", "gcp", "swtpm"]
    pcrs: Sequence[int]
    recovery: Literal["none"]

    def __post_init__(self) -> None:
        if self.platform not in TPM_PLATFORMS:
            raise ValidationError(
                f"TpmSeal: platform {self.platform!r} is not a vTPM tundravm knows.",
                hint=f"Name one of: {', '.join(TPM_PLATFORMS)}.",
            )
        if isinstance(self.pcrs, (str, bytes)) or not isinstance(self.pcrs, Sequence):
            raise ValidationError(
                "TpmSeal: pcrs= takes a list of PCR indexes, e.g. [7].",
                hint="Pass pcrs=[7], or pcrs=[] to bind to the vTPM alone.",
            )
        object.__setattr__(self, "pcrs", tuple(self.pcrs))
        for pcr in self.pcrs:
            if isinstance(pcr, bool) or not isinstance(pcr, int) or not 0 <= pcr <= 23:
                raise ValidationError(
                    f"TpmSeal: PCR {pcr!r} is not an index in 0..23.",
                    hint="A TPM 2.0 has PCRs 0 to 23.",
                )
        if len(set(self.pcrs)) != len(self.pcrs):
            raise ValidationError(
                "TpmSeal: pcrs= names a PCR twice.",
                hint="List each PCR once.",
            )
        if self.recovery != "none":
            raise ValidationError(
                f"TpmSeal: recovery {self.recovery!r} is not offered.",
                hint="recovery='none' is the only method today: losing the vTPM state loses "
                "the volume. Use unlock=OperatorKey() for a key an operator can supply again.",
            )

    def refusal(self, targets: Sequence[str]) -> str | None:
        """Why *targets* cannot boot a volume sealed this way, or ``None`` when they can."""
        foreign = [target for target in targets if _TARGET_TPM.get(target) != self.platform]
        if not foreign:
            return None
        boots = ", ".join(f"{t} boots with {_TARGET_TPM.get(t, 'no vTPM')}" for t in foreign)
        return f"it seals to {self.platform} ({TPM_PLATFORMS[self.platform]}), but {boots}"

    def manifest(self) -> dict[str, object]:
        """The unlock as the runtime manifest records it."""
        return {
            "kind": "tpm-seal",
            "platform": self.platform,
            "pcrs": list(self.pcrs),
            "recovery": self.recovery,
        }


type Unlock = OperatorKey | TpmSeal | BrokerKey
"""How a :class:`Volume`'s LUKS2 key arrives."""


@dataclass(frozen=True, slots=True)
class Volume(Located):
    """A persistent, encrypted volume: ``Volume("data", unlock=...)``.

    The image says what the volume is called, how it unlocks and, when ``/persistent/<name>``
    will not do, where it mounts (*mount*); its
    size and the disk itself are deployment configuration (``deploy --volume
    data=create:100GiB``). The attachment is found by name: QEMU's ``serial=`` or GCP's
    device name ``tundravm-<name>`` (:attr:`serial`), through ``/dev/disk/by-id/``, never
    by probe order. It is LUKS2 with an *filesystem* inside (ext4, the one ``disk-setup``
    formats), formatted only while it carries no initialisation token: an unlock failure
    is never permission to format. Backup, resize and rollback protection are not
    provided.

    *unlock* is the trust decision and has no default: :class:`OperatorKey` (a passphrase
    over the attested channel each boot), :class:`TpmSeal` (a key sealed to a stated vTPM)
    or :class:`BrokerKey` (released by a key broker after attestation).

    ``volume.directory("app")`` is a directory on it, which a service binds as its
    state (``Service(state=data.directory("app"))``) and a component takes as a field;
    ``volume.ready`` waits until the volume is open and its directories exist.
    """

    name: str
    _: KW_ONLY
    unlock: Unlock
    mount: str = DEFAULT_MOUNT
    """Where it mounts: ``/persistent/<name>`` unless given."""
    filesystem: Literal["ext4"] = "ext4"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _VOLUME_NAME.fullmatch(self.name):
            raise ValidationError(
                f"Volume: {self.name!r} is not a volume name.",
                hint="Use 1 to 11 of a-z, 0-9 and '-': it becomes the attachment name "
                f"{SERIAL_PREFIX}<name>, which QEMU limits to 20 bytes.",
            )
        if self.mount == DEFAULT_MOUNT:
            object.__setattr__(self, "mount", DEFAULT_MOUNT.replace("<name>", self.name))
        if not isinstance(self.mount, str) or not self.mount.startswith("/"):
            raise ValidationError(
                f"Volume {self.name!r}: mount {self.mount!r} must be absolute.",
                hint=f"Leave mount= out for /persistent/{self.name}.",
            )
        mount = posixpath.normpath(self.mount)
        if any(mount == r or (r != "/" and mount.startswith(r + "/")) for r in _RESERVED_MOUNTS):
            raise ValidationError(
                f"Volume {self.name!r}: {mount} is part of the measured system.",
                hint=f"Leave mount= out for /persistent/{self.name}.",
            )
        object.__setattr__(self, "mount", mount)
        if not isinstance(self.unlock, (OperatorKey, TpmSeal, BrokerKey)):
            raise ValidationError(
                f"Volume {self.name!r}: unlock {self.unlock!r} is not an unlock policy.",
                hint="Pass unlock=OperatorKey(), TpmSeal(platform=..., pcrs=..., "
                "recovery='none') or BrokerKey(url, audience=...).",
            )
        if self.filesystem != "ext4":
            raise ValidationError(
                f"Volume {self.name!r}: filesystem {self.filesystem!r} is not offered.",
                hint="disk-setup formats ext4 only: filesystem='ext4'.",
            )

    @property
    def serial(self) -> str:
        """The attachment name: QEMU's ``serial=``, GCP's device name."""
        return f"{SERIAL_PREFIX}{self.name}"

    @property
    def device(self) -> str:
        """The ``/dev/disk/by-id/`` glob that finds the attachment on every target."""
        return f"/dev/disk/by-id/*-{self.serial}"

    @property
    def pipe(self) -> str:
        """The FIFO an operator (or a broker client) writes the passphrase into."""
        return f"{RUNTIME_DIRECTORY}/unlock/{self.name}"

    @property
    def step(self) -> str:
        """The runtime-init step that prepares the volume's directories once it is open."""
        return f"volume-{self.name}"

    @property
    def ready(self) -> BootReady:
        """ "It is open, mounted, and its directories exist"."""
        return BootReady(f"runtime-init-{self.step}.service")

    def directory(
        self, name: str, *, mode: int = 0o750, group: str | None = None
    ) -> VolumeDirectory:
        """The directory *name* on this volume, for a service's (or component's) state.

        One service owns it, with *mode*; several services share it only through *group*
        (the directory is ``root:<group>`` and each of them joins the group).
        """
        return VolumeDirectory(self, name, mode=mode, group=group)

    def manifest(self) -> dict[str, object]:
        """The volume as the runtime manifest records it."""
        return {
            "name": self.name,
            "mount": self.mount,
            "serial": self.serial,
            "filesystem": self.filesystem,
            "unlock": self.unlock.manifest(),
        }


@dataclass(frozen=True, slots=True)
class VolumeDirectory(Keywords):
    """A directory on a :class:`Volume`: ``data.directory("app")``, bound as a state.

    The image creates it once the volume is open, owned by the one service that binds it
    (or ``root:<group>`` when it is shared), and the service waits for that before it
    starts. Its absolute :attr:`path` is the service's working directory and
    ``$STATE_DIRECTORY``.
    """

    volume: Volume = field(repr=False)
    name: str
    _: KW_ONLY
    mode: int = 0o750
    group: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.volume, Volume):
            raise ValidationError(
                "VolumeDirectory: volume must be a Volume.",
                hint="Use volume.directory('name').",
            )
        if not isinstance(self.name, str) or not _DIRECTORY.fullmatch(self.name):
            raise ValidationError(
                f"Volume {self.volume.name!r}: {self.name!r} is not a directory name.",
                hint="Use a relative path such as 'app' or 'state/app'.",
            )
        if ".." in self.name.split("/"):
            raise ValidationError(
                f"Volume {self.volume.name!r}: {self.name!r} leaves the volume.",
                hint="Name a directory inside the volume, without '..'.",
            )
        if isinstance(self.mode, bool) or not isinstance(self.mode, int):
            raise ValidationError(
                f"VolumeDirectory: mode {self.mode!r} must be an octal int.",
                hint="Pass mode=0o750.",
            )
        if not 0 <= self.mode <= 0o7777:
            raise ValidationError(
                f"VolumeDirectory: mode {oct(self.mode)} is not a mode.",
                hint="Pass a permission mode such as 0o750 or 0o2770.",
            )
        if self.group is not None and (
            not isinstance(self.group, str) or not _GROUP.fullmatch(self.group)
        ):
            raise ValidationError(
                f"VolumeDirectory: group {self.group!r} is not a group name.",
                hint="Pass a group name such as group='eth'.",
            )

    @property
    def path(self) -> str:
        """The absolute path: the volume's mount plus :attr:`name`."""
        return posixpath.join(self.volume.mount, self.name)

    @property
    def ready(self) -> BootReady:
        """The volume's readiness: it exists once the volume is open."""
        return self.volume.ready

    def __str__(self) -> str:
        return self.path
