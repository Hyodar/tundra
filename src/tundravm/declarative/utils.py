"""Shipped fragments: :class:`EfiStub`, :class:`Backports`, :class:`Tdxs` and :class:`DevTools`.

Each is a :class:`Composite`: a :class:`Fragment` whose configuration is its own
dataclass fields, so ``EfiStub(snapshot=SNAPSHOT, version="257.13-1~deb13u1")`` reads like a
declaration and goes wherever a ``Fragment`` does.

The committed ``examples/attested-node/mkosi`` tree pins their output.
"""

from __future__ import annotations

import functools
import ipaddress
import posixpath
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tundravm._modules import devtools as _devtools
from tundravm._modules import tdxs as _tdxs
from tundravm._modules.base import tools_source as _tools_source
from tundravm._modules.init import OWNERS_STEP
from tundravm._source import FrozenMap, LocalSource
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import (
    approved_registers,
    is_placeholder,
    read_policy,
    validator_measurements,
)

from .._keywords import Keywords
from ._component import Composite, Export, HttpEndpoint, component
from ._network import NETWORK_DIR
from .config import ConfigFile
from .model import (
    BootStep,
    Build,
    Cargo,
    Declaration,
    DropIn,
    File,
    Firewall,
    Fragment,
    Git,
    Go,
    Group,
    Hook,
    Http,
    Install,
    Local,
    Package,
    Port,
    Ready,
    Task,
    Unit,
    User,
    recorded_local,
)
from .systemd import Condition, Hardening, Root

if TYPE_CHECKING:
    from tundravm._image._secrets import Secret

    from .lifecycle import Measurements

TdxsType = Literal["tdx", "azure", "gcp", "simulator"]


@functools.cache
def tools_source() -> Git | Local:
    """What :class:`Tdxs` and the volume and delivery tools build from by default:
    ``tools/`` of tundravm's repository.

    Run from a checkout of the repository, ``Local`` of its ``tools/`` (the lock pins its
    content digest, so an edit there is drift); installed from git, ``Git(url, commit,
    subdir="tools")`` at the commit installed. Raises ``ValidationError`` naming the fix
    when the install records no commit.
    """
    source = _tools_source()
    if isinstance(source, LocalSource):
        return recorded_local(source.path, source.directory)
    return Git(source.url, source.ref, subdir=source.subdir)


DEFAULT_DEBIAN_MIRROR = "http://deb.debian.org/debian"
"""The mirror :class:`Backports` uses when neither it nor the image sets one."""
_SNAPSHOT_ROOT = "https://snapshot.debian.org"
"""The mirror root mkosi reads a ``Snapshot=`` from when ``Mirror=`` is unset."""
_BACKPORTS_PRIORITY = 200
"""Above sid, below the release (500): backports fill gaps before sid, never shadow the release."""
_SID_PRIORITY = 100
_SOURCES_STANZA = (
    "Types: deb deb-src\n"
    "URIs: {mirror}\n"
    "Suites: {suite}\n"
    "Components: main\n"
    "Enabled: yes\n"
    "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
)

_DEFAULT_ROOT_PASSWORD = "tdx"
_UNSAFE_PASSWORD = frozenset('"$`\\\n')


@component
class Backports(Composite):
    """Debian backports (and, with *include_sid*, sid) sources for apt during the build.

    Neither lands in the image. The compiler writes them to the tree's
    ``mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources``
    (see :meth:`render_sources`), with ``preferences.d/debian-backports.pref`` pinning
    backports to 200 and sid to 100 (see :meth:`render_preferences`): packages
    come from the release unless it lacks them, then from backports, then from
    sid. ``include_sid=False`` drops the sid source and its pin, so a package
    missing from the release and backports fails the build instead of coming from
    unstable. *archive_url* is the apt URI used
    verbatim; without it the sources follow the platform's ``mirror`` (a mirror root)
    and ``snapshot`` the way mkosi does, else ``deb.debian.org``. Without
    *release*, the platform's release. Component name: ``backports`` (it declares nothing
    itself: lowering writes its files).
    """

    archive_url: str | None = None
    release: str | None = None
    include_sid: bool = True

    def compose(self) -> Fragment:
        if not isinstance(self.include_sid, bool):
            raise ValidationError(
                f"Backports: include_sid must be True or False, got {self.include_sid!r}.",
                hint="Pass include_sid=False to leave sid out of the build's apt sources.",
            )
        return Fragment("backports", items=())

    def render_sources(
        self, *, mirror: str | None, release: str, snapshot: str | None = None
    ) -> str:
        """The deb822 sources file the compiler writes into the build sandbox.

        *mirror* (a mirror root), *release* and *snapshot* are the image's; the
        component's own fields win.
        """
        if self.archive_url:
            uri = self.archive_url
        elif snapshot:
            uri = _join(mirror or _SNAPSHOT_ROOT, f"archive/debian/{snapshot}")
        elif mirror:
            uri = _join(mirror, "debian")
        else:
            uri = DEFAULT_DEBIAN_MIRROR
        suite = self.release or release
        if not suite:
            raise ValidationError(
                "Backports needs a Debian release: the platform names none.",
                hint="Use platform=Debian('trixie'), or pass Backports(release=...).",
            )
        backports = _SOURCES_STANZA.format(mirror=uri, suite=f"{suite}-backports")
        if not self.include_sid:
            return backports
        return backports + "\n" + _SOURCES_STANZA.format(mirror=uri, suite="sid")

    def render_preferences(self, *, release: str) -> str:
        """The apt pins the compiler writes next to :meth:`render_sources`' file.

        sid has no ``NotAutomatic`` flag, so unpinned it would outrank the release
        wherever its versions are newer.
        """
        suite = self.release or release
        pins = (
            f"Package: *\nPin: release n={suite}-backports\nPin-Priority: {_BACKPORTS_PRIORITY}\n"
        )
        if not self.include_sid:
            return pins
        return pins + f"\nPackage: *\nPin: release n=sid\nPin-Priority: {_SID_PRIORITY}\n"


def _join(root: str, path: str) -> str:
    """*path* under mirror *root*, as mkosi joins ``Mirror=`` and its suffixes."""
    return f"{root.rstrip('/')}/{path}"


def _snapshot_archive(snapshot: str) -> str:
    """*snapshot* as a URL: a snapshot ID becomes its ``snapshot.debian.org`` archive."""
    return snapshot if "/" in snapshot else _join(_SNAPSHOT_ROOT, f"archive/debian/{snapshot}")


def _security_archive(snapshot: str) -> str | None:
    """The ``debian-security`` archive of a snapshot ID; ``None`` for an archive URL."""
    return None if "/" in snapshot else _join(_SNAPSHOT_ROOT, f"archive/debian-security/{snapshot}")


@component
class EfiStub(Composite):
    """``systemd-boot-efi`` *version* from the Debian *snapshot*, for a pinned EFI stub.

    *snapshot* is a snapshot ID (``20261018T082309Z``, read from
    ``snapshot.debian.org``) or the URL of a snapshot archive. The snapshot must
    carry *version*: the pool keeps only what some suite listed at that time, so
    pick the version a suite of that snapshot ships. The
    package (:attr:`deb_url`) is a source named ``efi-stub``: ``tundravm lock`` pins
    its sha256, ``tundravm build`` downloads it on the host and the postinst hook
    installs the mounted copy with ``dpkg -i``, so the build sandbox downloads
    nothing. Compiled without a pin, the hook downloads it instead and fails the
    build with that advice when the download fails (see :meth:`render_script`).
    A postinst hook. Component name: ``efi-stub``.
    """

    snapshot: str
    version: str

    def compose(self) -> Fragment:
        if not self.snapshot or not self.version:
            raise ValidationError(
                "EfiStub requires a non-empty snapshot and version.",
                hint="Pass snapshot= a snapshot ID such as '20261018T082309Z' and version= "
                "the systemd-boot-efi version it ships, e.g. '257.13-1~deb13u1'.",
            )
        return Fragment("efi-stub", items=(Hook("efi-stub", "postinst", self.render_script()),))

    @property
    def deb_url(self) -> str:
        """The package in the snapshot's pool: ``systemd-boot-efi_<version>_amd64.deb``."""
        return (
            f"{_snapshot_archive(self.snapshot)}/pool/main/s/systemd/"
            f"systemd-boot-efi_{self.version}_amd64.deb"
        )

    def render_script(self) -> str:
        """The postinst hook's script, unpinned: it downloads the package (a pinned compile
        installs the mounted copy instead, ``DebFile.render``).

        mkosi-chroot mounts its own ``/tmp``, so the package goes to the image root;
        a failed download names the version and snapshot. It leaves
        ``linuxx64.efi.stub`` alone: copying ``systemd-bootx64.efi`` over it makes the
        UKI a systemd-boot binary.
        """
        return (
            f'EFI_SNAPSHOT_URL="{_snapshot_archive(self.snapshot)}"\n'
            f'EFI_PACKAGE_VERSION="{self.version}"\n'
            'DEB_URL="${EFI_SNAPSHOT_URL}/pool/main/s/systemd/'
            'systemd-boot-efi_${EFI_PACKAGE_VERSION}_amd64.deb"\n'
            'if ! curl -sSfL -o "$BUILDROOT/systemd-boot-efi.deb" "$DEB_URL"; then\n'
            '    echo "EfiStub: no systemd-boot-efi ${EFI_PACKAGE_VERSION} in'
            ' ${EFI_SNAPSHOT_URL}; pick the version a suite of that snapshot ships" >&2\n'
            "    exit 1\n"
            "fi\n"
            "mkosi-chroot dpkg -i /systemd-boot-efi.deb\n"
            'rm -f "$BUILDROOT/systemd-boot-efi.deb"'
        )


@component
class Tdxs(Composite):
    """The ``tdxs`` attestation service: source build, config, socket-activated units, account.

    Declares the ``tdx`` group (other services join it for ``/dev/tdx_guest``, which the
    typed units' udev rule makes ``root:tdx 0660``) and the ``tdxs`` user. *needs* lists
    the runtime-init steps the service waits for. The service is a typed
    ``Service(socket=Listen(...))``, hardened for its job (``_modules/tdxs.py``
    ``Tdxs.service`` gives the reasoning: ``CAP_DAC_OVERRIDE`` for configfs-tsm, unverified
    on TDX hardware).
    *issuer* is the real issuer (``None``: validate only). A development profile also ships
    the simulator issuer and takes it at boot where the execution gate finds no TDX quote
    path; a production profile never ships it, so no recipe names ``simulator`` here.
    Component name: ``tdxs``. ``policy_file`` is the file :meth:`from_policy` read
    (``None`` otherwise); it is not configuration, so equality and ``repr`` skip it.
    """

    source: Git | Local = field(default_factory=tools_source)
    issuer: Literal["tdx", "azure", "gcp"] | None = "tdx"
    validator: TdxsType | None = None
    expected_measurements: Mapping[str, str] = field(default_factory=FrozenMap)
    check_revocations: bool = False
    get_collateral: bool = False
    verify_imds: bool = False
    verify_identity_token: bool = False
    needs: Sequence[Ready] = ()
    policy_file: Path | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def from_policy(
        cls,
        policy: str | Path | Mapping[str, object],
        *,
        validator: TdxsType = "tdx",
        mrtd: str | None = None,
        firmware: str | None = None,
        label: str | None = None,
        allow_placeholder: bool = False,
        **kwargs: Any,
    ) -> Tdxs:
        """A verifier whose ``expected_measurements`` are *policy*'s registers.

        *policy* is a verifier policy (a record's ``policy`` in a build's
        ``measurements.json``), or its dict. Registers become the validator's lower-case
        keys (``rtmr0``..), one value each: the tdxs validator compares a single expected
        value per register, so a register the policy accepts several values for (a gcp
        policy's per-firmware MRTD and RTMR0) is narrowed with *firmware* and *label*.
        Unverifiable registers are not checked unless the policy's ``platform_approval``
        (``MRTD``, ``RTMR0``) approves values for them, which are
        checked like measured ones; *mrtd* overrides the MRTD to check (a qemu host you
        trust).
        A placeholder policy is refused unless *allow_placeholder*. Other
        keyword arguments are ``Tdxs`` fields (``issuer``, ``check_revocations``...).
        """
        if "expected_measurements" in kwargs:
            raise ValidationError(
                "Tdxs.from_policy() takes the measurements from the policy.",
                hint="Drop expected_measurements=, or build Tdxs(...) directly.",
            )
        data = read_policy(policy)
        if is_placeholder(data) and not allow_placeholder:
            raise MeasurementError(
                "Refusing to build a verifier from a placeholder policy.",
                hint="Export the policy from a real measurement, or pass "
                "allow_placeholder=True for a test image.",
                context={"policy": str(policy) if not isinstance(policy, Mapping) else "dict"},
            )
        registers = approved_registers(data)
        expected = validator_measurements(registers, mrtd=mrtd, firmware=firmware, label=label)
        if not expected:
            raise MeasurementError(
                "The policy accepts no TDX register value, so the validator would check nothing.",
                hint="An azure policy measures the vTPM, not the TD; verify its vtpm_pcrs with a "
                "vTPM verifier, or export a qemu/gcp/portable policy.",
            )
        verifier = cls(validator=validator, expected_measurements=dict(expected), **kwargs)
        if not isinstance(policy, Mapping):
            object.__setattr__(verifier, "policy_file", Path(policy))
        return verifier

    @classmethod
    def from_measurements(
        cls,
        measurements: Measurements,
        *,
        validator: TdxsType = "tdx",
        mrtd: str | None = None,
        **kwargs: Any,
    ) -> Tdxs:
        """:meth:`from_policy` for a ``measure()`` result in memory."""
        return cls.from_policy(measurements.to_policy(), validator=validator, mrtd=mrtd, **kwargs)

    def compose(self) -> Fragment:
        if self.issuer == _tdxs.SIMULATOR:
            raise ValidationError(
                "Tdxs: issuer= names the real issuer; the simulator there would ship in "
                "production too.",
                hint="Drop issuer=: a development profile takes the simulator issuer at boot "
                "where it finds no TDX, and a production profile never ships it.",
            )
        if not isinstance(self.expected_measurements, Mapping):
            raise ValidationError(
                f"Tdxs: expected_measurements= must be a mapping, "
                f"got {self.expected_measurements!r}.",
                hint="Pass a dict: expected_measurements={'rtmr0': '<hex>'}.",
            )
        for key, value in self.expected_measurements.items():
            if not isinstance(value, str):
                raise ValidationError(
                    f"Tdxs: expected_measurements[{key!r}] must be one hex string, got {value!r}.",
                    hint="The tdxs validator compares one value per register; pick one "
                    "(Tdxs.from_policy(policy, firmware=..., label=...) does it for a policy).",
                )
        spec = _tdxs.Tdxs(
            issuer_type=self.issuer,
            validator_type=self.validator,
            expected_measurements=dict(self.expected_measurements) or None,
            check_revocations=self.check_revocations,
            get_collateral=self.get_collateral,
            verify_imds=self.verify_imds,
            verify_identity_token=self.verify_identity_token,
        )
        go = Go(package="./cmd/tdxs", output="tdxs")
        return Fragment(
            "tdxs",
            items=(
                Build(
                    "tdxs", self.source, using=go, install=(Install(go.artifact, "/usr/bin/tdxs"),)
                ),
                File(spec.config_path, spec.render_config()),
                *self._typed_units(spec),
                Group(spec.group),
                User(spec.user, home=f"/home/{spec.user}", primary_group=spec.group),
            ),
        )

    def _typed_units(self, spec: _tdxs.Tdxs) -> tuple[Declaration, ...]:
        """The typed service and the udev rule handing ``/dev/tdx_guest`` to ``tdx``."""
        return (
            spec.service(self.needs),
            File(_tdxs.TDX_GUEST_UDEV_RULE, spec.render_udev_rule()),
        )


@component
class DevTools(Composite):
    """Debugging tools, a serial console and password root login. Never ship it: put it
    in a non-production profile (``Profile(production=False)``, or ``dev=``).

    The root password is hashed with a fixed salt, so a second build of the profile
    matches the first: *salt* (1 to 16 of ``[A-Za-z0-9./]``), or by default the first 16
    hex digits of ``sha256("tundravm-devtools:<profile name>")``, computed in the
    postinst script from mkosi's ``$IMAGE_ID``. The serial getty on ``ttyS0`` is enabled
    at build time. ``reproducible=False`` keeps the historical random salt and the
    ``serial-console.service`` that enables the getty at boot. The password logs in on the
    console only; ``network_login=True`` is a network login: it also lets root in over SSH
    (openssh and dropbear) with that password.
    Component name: ``devtools``.
    """

    root_password: str = _DEFAULT_ROOT_PASSWORD
    salt: str | None = None
    reproducible: bool = True
    network_login: bool = False

    def compose(self) -> Fragment:
        if not self.root_password or _UNSAFE_PASSWORD & set(self.root_password):
            raise ValidationError(
                "DevTools root_password must be non-empty one-line text without '\"', '$', "
                "'`' or '\\'.",
                hint="Pick a password of plain letters, digits and punctuation on one line.",
            )
        if self.salt is not None and (
            not self.reproducible or not re.fullmatch(r"[A-Za-z0-9./]{1,16}", self.salt)
        ):
            raise ValidationError(
                f"DevTools salt {self.salt!r} must be 1 to 16 of A-Z, a-z, 0-9, '.' and '/', "
                "with reproducible=True.",
                hint="Pass a short salt such as salt='devtools01', or omit it for the one "
                "derived from the profile name.",
            )
        hashed = f'openssl passwd -6 "{self.root_password}"'
        if self.reproducible:
            salt = _devtools.DERIVED_SALT_LINE if self.salt is None else f'SALT="{self.salt}"'
            hashed = f'openssl passwd -6 -salt "$SALT" "{self.root_password}"'
            line = f"{salt}\nROOT_PASS=$({hashed})"
        else:
            line = f"ROOT_PASS=$({hashed})"
        login = _devtools.DEVTOOLS_POSTINST_SCRIPT.replace(_devtools.HISTORICAL_HASH_LINE, line)
        if self.network_login:
            login += _devtools.NETWORK_LOGIN_SCRIPT
        return Fragment(
            "devtools",
            items=(
                *(Package(name) for name in _devtools.DEVTOOLS_PACKAGES),
                *(self._serial_console() if not self.reproducible else ()),
                *((Unit(_devtools.SERIAL_GETTY, enabled=True),) if self.reproducible else ()),
                Hook("devtools-root-login", "postinst", login),
            ),
        )

    @staticmethod
    def _serial_console() -> tuple[Unit, Hook]:
        """The historical ``serial-console.service``, which enables the getty at boot."""
        return (
            Unit("serial-console.service", _devtools.SERIAL_CONSOLE_SERVICE),
            Hook(
                "devtools-serial-console",
                "postinst",
                "mkosi-chroot systemctl enable serial-console.service",
            ),
        )


_GO_LDFLAGS = "-s -w -buildid="


def _install_path(kind: str, name: str, install: str | None) -> str:
    path = f"/usr/bin/{name}" if install is None else install
    if not isinstance(path, str) or not path.startswith("/") or path.endswith("/"):
        raise ValidationError(
            f"{kind} {name!r} install {path!r} must be an absolute file path.",
            hint=f"Pass install='/usr/bin/{name}' or another absolute path.",
        )
    return path


@dataclass(frozen=True, slots=True)
class GoBinary(Composite):
    """A Go program built from *source* and installed at :attr:`executable`.

    The ``Build`` named *name* runs ``go build -trimpath -buildvcs=false`` of *package*
    (the module root by default, ``"./cmd/server"`` for a sub-command; ``Go``;
    ``CGO_ENABLED=0`` unless *cgo*) into ``build/<file name of install>`` and
    installs it at *install* (``/usr/bin/<name>`` by default). ``executable`` is that path,
    the first element of a ``Service`` argv: ``Service(name, [binary.executable, "--flag"])``.
    Component name: *name*.
    """

    name: str
    source: Git | Http | Local
    _: KW_ONLY
    package: str = "."
    install: str | None = None
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    tags: Sequence[str] = ()
    ldflags: str = _GO_LDFLAGS
    cgo: bool = False
    cache_namespace: str | None = None

    @property
    def executable(self) -> str:
        """The absolute path the binary is installed at."""
        return _install_path("GoBinary", self.name, self.install)

    def compose(self) -> Fragment:
        destination = self.executable
        go = Go(
            output=posixpath.basename(destination),
            package=self.package,
            ldflags=self.ldflags,
            tags=self.tags,
            env=self.env,
            cgo=self.cgo,
        )
        build = Build(
            self.name,
            self.source,
            using=go,
            install=(Install(go.artifact, destination),),
            cache_namespace=self.cache_namespace,
        )
        return Fragment(self.name, items=(build,))


@dataclass(frozen=True, slots=True)
class CargoBinary(Composite):
    """A Rust program built from *source* and installed at :attr:`executable`.

    The ``Build`` named *name* runs ``cargo auditable build --frozen`` (``Cargo``; plain
    ``cargo build`` with ``auditable=False``; *bin* or *package* selects the target,
    *profile* its directory) with reproducible ``rustflags`` and installs
    ``target/<profile>/<bin, package or install file name>`` at *install*
    (``/usr/bin/<name>`` by default). Component name: *name*.
    """

    name: str
    source: Git | Http | Local
    _: KW_ONLY
    bin: str | None = None
    package: str | None = None
    install: str | None = None
    features: Sequence[str] = ()
    profile: str = "release"
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    auditable: bool | None = None
    cache_namespace: str | None = None

    @property
    def executable(self) -> str:
        """The absolute path the binary is installed at."""
        return _install_path("CargoBinary", self.name, self.install)

    def compose(self) -> Fragment:
        destination = self.executable
        cargo = Cargo(
            output=posixpath.basename(destination),
            bin=self.bin,
            package=self.package,
            features=self.features,
            profile=self.profile,
            env=self.env,
            auditable=self.auditable,
        )
        build = Build(
            self.name,
            self.source,
            using=cargo,
            install=(Install(cargo.artifact, destination),),
            cache_namespace=self.cache_namespace,
        )
        return Fragment(self.name, items=(build,))


_FB = "flashbots-images shared/mkosi.extra"
_KSPP = "KSPP: kspp.github.io/Recommended_Settings"
_SYSCTL_DOC = "linux: Documentation/admin-guide/sysctl"

TEE_NTS_SERVERS = ("time.cloudflare.com", "nts.netnod.se")
"""The NTS (RFC 8915) servers :class:`TeeBase` syncs from: flashbots-images'
``shared/mkosi.extra/etc/chrony/chrony.conf``."""

TEE_HYPERVISOR_CLOCKS = ("169.254.169.254", "169.254.169.123")
"""Cloud hypervisor NTP endpoints (GCP metadata, AWS time sync) that refine the NTS sources."""

TEE_NAMESERVERS = ("1.1.1.1", "1.0.0.1")
"""The resolvers :class:`TeeBase` writes to ``/etc/resolv.conf``: flashbots-images'
``shared/mkosi.extra/etc/systemd/resolved.conf`` (Cloudflare)."""

TEE_SYSCTL: tuple[tuple[str, str, str], ...] = (
    ("kernel.kptr_restrict", "2", f"hide kernel pointers from every user ({_KSPP})"),
    ("kernel.dmesg_restrict", "1", f"only CAP_SYSLOG reads the kernel log ({_KSPP})"),
    ("kernel.unprivileged_bpf_disabled", "1", f"no unprivileged eBPF, for good ({_KSPP})"),
    ("net.core.bpf_jit_harden", "2", f"constant blinding in the BPF JIT ({_KSPP})"),
    ("kernel.kexec_load_disabled", "1", f"the measured kernel cannot be replaced ({_KSPP})"),
    ("kernel.yama.ptrace_scope", "2", f"only CAP_SYS_PTRACE attaches to processes ({_KSPP})"),
    ("kernel.perf_event_paranoid", "3", f"no unprivileged perf events ({_FB}/etc/sysctl.d)"),
    ("kernel.sysrq", "0", f"the host-owned console sends no magic SysRq ({_SYSCTL_DOC}/kernel)"),
    ("fs.suid_dumpable", "0", f"setuid programs never dump core ({_KSPP})"),
    ("fs.protected_symlinks", "1", f"no symlink races in sticky dirs ({_FB}/etc/sysctl.d)"),
    ("fs.protected_hardlinks", "1", f"no hardlinks to others' files ({_FB}/etc/sysctl.d)"),
    ("fs.protected_fifos", "2", f"no FIFO spoofing in sticky dirs ({_KSPP})"),
    ("fs.protected_regular", "2", f"no regular-file spoofing in sticky dirs ({_KSPP})"),
    ("net.ipv4.conf.all.accept_redirects", "0", f"ignore ICMP redirects ({_SYSCTL_DOC}/net)"),
    ("net.ipv4.conf.default.accept_redirects", "0", "ignore ICMP redirects on new interfaces"),
    ("net.ipv6.conf.all.accept_redirects", "0", f"ignore ICMPv6 redirects ({_SYSCTL_DOC}/net)"),
    ("net.ipv4.conf.all.send_redirects", "0", f"the guest is not a router ({_SYSCTL_DOC}/net)"),
    ("net.ipv4.conf.all.accept_source_route", "0", "drop source-routed packets"),
    ("net.ipv4.conf.all.rp_filter", "2", f"drop unroutable sources ({_SYSCTL_DOC}/net)"),
    ("net.ipv4.tcp_syncookies", "1", f"survive SYN floods ({_SYSCTL_DOC}/net)"),
)
"""``(key, value, reason)`` lines of :class:`TeeBase`'s ``/etc/sysctl.d`` file. Each line is
``-``-prefixed, so a key the kernel lacks (no Yama, no IPv6) is skipped, not a boot failure
(``sysctl.d(5)``)."""

TEE_MASKED_UNITS: tuple[tuple[str, str], ...] = (
    ("apt-daily.timer", "no package list refresh at runtime: the image is what was measured"),
    ("apt-daily-upgrade.timer", "no unattended upgrades: they would change measured software"),
    ("dpkg-db-backup.timer", "no dpkg database backups on a volatile root"),
    ("getty@tty1.service", "no login prompt on the host-owned console"),
)
"""``(unit, reason)`` of the units :class:`TeeBase` masks; flashbots-images masks every
unlisted systemd unit (``shared/mkosi.postinst.d/90-debloat-systemd.sh``), and tundravm's
debloat does that for the ``systemd`` package, so these are the other packages' units."""

_TEE_HEADER = "# tundravm TeeBase\n"
_JOURNALD_DROPIN = "/etc/systemd/journald.conf.d/50-tundravm-tee.conf"
_SYSCTL_FILE = "/etc/sysctl.d/50-tundravm-tee.conf"
_CHRONY_TAIL = """\
# Reject a source whose root distance exceeds one second
maxdistance 1.0
driftfile /var/lib/chrony/chrony.drift
ntsdumpdir /var/lib/chrony
# Step the clock in the first three updates if it is off by more than a second
makestep 1 3
rtcsync
maxupdateskew 100.0
"""


NETWORK_FILE = f"{NETWORK_DIR}/50-tundravm.network"
"""The ``.network`` file :class:`Network` writes."""
_WAIT_ONLINE = "/usr/lib/systemd/systemd-networkd-wait-online"
_INTERFACE = re.compile(r"[A-Za-z0-9*?\[\]!_.:-]+")


@dataclass(frozen=True, slots=True)
class StaticAddress(Keywords):
    """A fixed address for :class:`Network`: ``StaticAddress("10.0.0.5/24", gateway="10.0.0.1")``.

    *address* is CIDR (address and prefix length); *gateway*, when set, is the default route.
    """

    address: str
    gateway: str | None = None

    def __post_init__(self) -> None:
        try:
            interface = ipaddress.ip_interface(self.address)
        except ValueError:
            interface = None
        if interface is None or "/" not in self.address:
            raise ValidationError(
                f"StaticAddress: {self.address!r} is not an address with a prefix length.",
                hint="Pass CIDR: StaticAddress('10.0.0.5/24').",
            )
        if self.gateway is not None:
            try:
                gateway = ipaddress.ip_address(self.gateway)
            except ValueError:
                gateway = None
            if gateway is None or gateway.version != interface.version:
                raise ValidationError(
                    f"StaticAddress: gateway {self.gateway!r} is not an IPv{interface.version} "
                    "address.",
                    hint="Pass the router's address in the same family as the address.",
                )

    @property
    def version(self) -> int:
        """4 or 6."""
        return ipaddress.ip_interface(self.address).version


@component
class Network(Composite):
    """Interfaces systemd-networkd brings up at boot; ``network-online.target`` waits for them.

    Writes :data:`NETWORK_FILE` matching *interfaces* (shell globs; the defaults cover
    predictable ``en*`` and kernel ``eth*`` names) and enables ``systemd-networkd`` (Debian
    ships it in ``systemd``). *dhcp* takes a DHCPv4 lease, and with *ipv6* IPv6 router
    advertisements and DHCPv6 when they ask for it; the DHCP server sets no hostname, resolver
    or time source (:class:`TeeBase` pins those). *static* is a fixed address instead of DHCP
    (pass ``dhcp=False``). With *wait_online*, ``systemd-networkd-wait-online`` holds
    ``network-online.target`` until any matched interface is configured, at most
    *wait_timeout* seconds, so a boot without a NIC continues. The implicit debloat keeps the
    networkd units and the ``.network`` file. Component name: ``network``.
    """

    dhcp: bool = True
    interfaces: tuple[str, ...] = ("en*", "eth*")
    static: StaticAddress | None = None
    ipv6: bool = True
    wait_online: bool = True
    wait_timeout: int = 30

    def compose(self) -> Iterator[Declaration]:
        if not self.interfaces or not all(_INTERFACE.fullmatch(i) for i in self.interfaces):
            raise ValidationError(
                f"Network: interfaces {self.interfaces!r} must be interface names or globs.",
                hint="Pass interfaces=('en*', 'eth*').",
            )
        if self.static is not None and not isinstance(self.static, StaticAddress):
            raise ValidationError(
                "Network: static= must be a StaticAddress or None.",
                hint="Pass static=StaticAddress('10.0.0.5/24', gateway='10.0.0.1').",
            )
        if self.dhcp == (self.static is not None):
            raise ValidationError(
                "Network takes DHCP or a static address: "
                + ("both are set." if self.dhcp else "neither is set."),
                hint="Network() for DHCP, Network(dhcp=False, static=StaticAddress(...)).",
            )
        if self.static is not None and self.static.version == 6 and not self.ipv6:
            raise ValidationError(
                "Network: a static IPv6 address needs ipv6=True.",
                hint="Drop ipv6=False, or pass an IPv4 address.",
            )
        if self.wait_timeout <= 0:
            raise ValidationError(
                "Network: wait_timeout must be a positive number of seconds.",
                hint="Pass wait_timeout=30, or wait_online=False not to wait.",
            )
        yield File.text(NETWORK_FILE, self.network_file())
        yield Unit("systemd-networkd.service", enabled=True)
        yield Unit("systemd-networkd.socket", enabled=True)
        wait = Unit(
            "systemd-networkd-wait-online.service",
            enabled=True,
            dropin=DropIn(
                "50-tundravm-network",
                exec_start=(_WAIT_ONLINE, "--any", f"--timeout={self.wait_timeout}"),
            ),
        )
        yield wait if self.wait_online else Unit(wait.name, enabled=False)

    def network_file(self) -> str:
        """The text of :data:`NETWORK_FILE`."""
        network = [f"DHCP={('yes' if self.ipv6 else 'ipv4') if self.dhcp else 'no'}"]
        if self.static is not None:
            network.append(f"Address={self.static.address}")
            if self.static.gateway is not None:
                network.append(f"Gateway={self.static.gateway}")
        network.append(f"IPv6AcceptRA={'yes' if self.ipv6 else 'no'}")
        if not self.ipv6:
            network.append("LinkLocalAddressing=no")
        text = (
            "# tundravm Network: the interfaces systemd-networkd brings up\n"
            f"[Match]\nName={' '.join(self.interfaces)}\n\n[Network]\n" + "\n".join(network) + "\n"
        )
        if self.dhcp:
            text += (
                "\n[DHCPv4]\n# The host's DHCP server names no hostname, resolver or time source\n"
                "UseHostname=no\nUseDNS=no\nUseNTP=no\n"
            )
        return text


@component
class TeeBase(Composite):
    """The baseline every confidential VM image wants; each piece opts out with its flag.

    *time*: ``chrony`` syncs from the *nts_servers* over NTS (RFC 8915), so the host cannot
    set the guest's clock (TLS, certificate and policy checks depend on it); the
    *hypervisor_clocks* only refine them (flashbots-images ``etc/chrony/chrony.conf``).
    *journal*: journald keeps logs in RAM, capped (``Storage=volatile``, a
    ``journald.conf.d`` drop-in): nothing reaches a host-readable disk.
    *sysctl*: the :data:`TEE_SYSCTL` hardening in ``/etc/sysctl.d/50-tundravm-tee.conf``.
    *dns*: a static ``/etc/resolv.conf`` naming the *nameservers*; on Compute Engine
    ``gcp-metadata-dns.service`` replaces it at boot with the metadata server.
    *mask*: masks :data:`TEE_MASKED_UNITS` (apt timers, the tty1 getty).
    *network*: :class:`Network` (DHCP on every ``en*``/``eth*`` interface); pass
    ``network=False`` and add your own ``Network(...)`` to configure it.
    *firewall*: :class:`Firewall`, which drops inbound traffic except the ports the image's
    services declare; pass ``firewall=False`` and add your own ``Firewall(...)`` to change it.
    Component name: ``tee-base``.
    """

    time: bool = True
    nts_servers: tuple[str, ...] = TEE_NTS_SERVERS
    hypervisor_clocks: tuple[str, ...] = TEE_HYPERVISOR_CLOCKS
    journal: bool = True
    journal_max_use: str = "128M"
    sysctl: bool = True
    dns: bool = True
    nameservers: tuple[str, ...] = TEE_NAMESERVERS
    mask: bool = True
    network: bool = True
    firewall: bool = True

    def compose(self) -> Iterator[Declaration | Fragment]:
        if self.time:
            if not self.nts_servers:
                raise ValidationError(
                    "TeeBase(time=True) needs at least one NTS server.",
                    hint="Pass nts_servers=('time.cloudflare.com',), or time=False.",
                )
            yield Package("chrony")
            yield Package("ca-certificates")  # NTS-KE checks the servers' TLS certificates
            yield File.text("/etc/chrony/chrony.conf", self._chrony_conf())
            yield Unit("chrony.service", enabled=True)
        if self.journal:
            journal = {
                "Storage": "volatile",
                "RuntimeMaxUse": self.journal_max_use,
                "ForwardToConsole": "no",
            }
            yield ConfigFile.ini(_JOURNALD_DROPIN, {"Journal": journal}, sort_keys=False)
        if self.sysctl:
            lines = [f"# {reason}\n-{key} = {value}" for key, value, reason in TEE_SYSCTL]
            yield File.text(_SYSCTL_FILE, _TEE_HEADER + "\n".join(lines) + "\n")
        if self.dns:
            servers = "".join(f"nameserver {server}\n" for server in self.nameservers)
            yield File.text("/etc/resolv.conf", _TEE_HEADER + servers)
        if self.mask:
            for unit, _reason in TEE_MASKED_UNITS:
                yield Unit(unit, masked=True)
        if self.network:
            yield Network()
        if self.firewall:
            yield Firewall()

    def _chrony_conf(self) -> str:
        nts = "".join(f"server {server} iburst nts\n" for server in self.nts_servers)
        refine = "".join(
            f"server {clock} iburst minpoll 4 maxpoll 4\n" for clock in self.hypervisor_clocks
        )
        return (
            f"{_TEE_HEADER}# Authenticated NTS sources\n{nts}"
            + (
                "# Hypervisor clocks refine them; authselectmode mix (the default) keeps an\n"
                f"# unauthenticated source from overruling the NTS ones\n{refine}"
                if refine
                else ""
            )
            + _CHRONY_TAIL
        )


SSH_CONFIG = "/etc/ssh/sshd_config.d/50-tundravm.conf"
"""The sshd drop-in :class:`SshAccess` ships; ``sshd_config`` includes it before its own lines."""
SSH_AUTHORIZED_KEYS_DIR = "/etc/ssh/authorized_keys.d"
"""Where :class:`SshAccess` bakes ``authorized_keys=``, one file per user (``root``)."""
SSH_HOST_KEY = "/etc/ssh/ssh_host_ed25519_key"
_SSH_PUBLIC_KEY = re.compile(
    r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com) [A-Za-z0-9+/]+={0,3}( [^\n]*)?"
)


SSH_LOGIN_CONFIG = "/etc/ssh/sshd_config.d/60-tundravm-login-{name}.conf"
"""Each :class:`Login`'s ``Match User`` block, one file each: a ``Match`` in an included
file ends with that file, so Debian's own lines after the ``Include`` stay global."""
SUDOERS_DIRECTORY = "/etc/sudoers.d"
"""Where a :class:`Login` with ``sudo=`` gets its rules, one file per login (``0440``)."""
NOLOGIN = "/usr/sbin/nologin"
_FORCED_SHELL = "/bin/sh"
_LOGIN_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_SUDO_REFUSED = frozenset("*?[]\\\"'")
_COMMAND_REFUSED = frozenset('"\\\n')


@dataclass(frozen=True, slots=True)
class Sudo(Keywords):
    """One command a :class:`Login` runs as root, exactly as written:
    ``Sudo("/usr/bin/systemctl restart app")``.

    The rule is one ``sudoers`` line matching these words and no others: no wildcard, no
    quoting, and a command without arguments takes none (a bare path in ``sudoers`` takes
    any). *nopasswd* stays ``True``: a login has no password to type.
    """

    command: str
    nopasswd: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or "\n" in self.command or not self.command.split():
            raise ValidationError(
                f"Sudo: {self.command!r} is not one command line.",
                hint='Sudo("/usr/bin/systemctl restart app").',
            )
        words = self.command.split()
        if not words[0].startswith("/"):
            raise ValidationError(
                f"Sudo: {words[0]!r} is not an absolute path, and sudo runs nothing by name.",
                hint=f'Sudo("/usr/bin/{words[0]}{self.command[len(words[0]) :]}").',
            )
        refused = "".join(sorted(_SUDO_REFUSED & set(self.command)))
        if refused:
            raise ValidationError(
                f"Sudo: {self.command!r} holds {refused!r}, which sudoers reads as a wildcard "
                "or a quote, so the rule would allow more than it says.",
                hint="Write each argument literally: one Sudo per exact command line.",
            )
        if self.nopasswd is not True:
            raise ValidationError(
                "Sudo: nopasswd=False asks for a password the login does not have, so the "
                "rule could never run.",
                hint="Drop nopasswd=False.",
            )
        object.__setattr__(self, "command", " ".join(words))

    def rule(self, user: str) -> str:
        """The ``sudoers`` line letting *user* run this command as root."""
        words = [re.sub(r"([,:=])", r"\\\1", word) for word in self.command.split()]
        if len(words) == 1:
            words.append('""')
        return f"{user} ALL=(root) NOPASSWD: {' '.join(words)}"


@dataclass(frozen=True, slots=True)
class Login(Keywords):
    """An account other than root that logs in over SSH into exactly what the image allows:
    ``Login("tenant", keys=[...], command="/usr/bin/app-status")``.

    The account has no password and no other way in. *keys* are public keys baked into the
    image (``/etc/ssh/authorized_keys.d/<name>``), or a ``Secret`` the attested endpoint
    writes there once ``tundravm secrets deliver`` verified a quote binding the host keys.
    *command* is a forced command: whatever the client asks for, sshd runs this (the
    request is in ``$SSH_ORIGINAL_COMMAND``), without a terminal, through ``/bin/sh``
    unless *shell* is given. *shell* is the account's login shell, for a restricted shell
    the image ships (a ``searchersh``-like menu built with ``GoBinary``); tundravm ships
    none. One of them is required. Every key is ``restrict``-ed and sshd's ``Match User``
    block disables forwarding and reads only those keys, so a delivered key gets the same
    limits as a baked one. *sudo* is the fixed list of commands it runs as root
    (``/etc/sudoers.d/<name>``, checked with ``visudo -c`` while the image is built).
    *home* makes ``/home/<name>``, which the ``owners`` runtime-init step hands to the
    login at boot (an initramfs root holds every file as root); without it sshd says
    it cannot change to one.
    """

    name: str
    keys: Sequence[str] | Secret = ()
    command: str | None = None
    shell: str | None = None
    sudo: Sequence[Sudo] = ()
    home: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _LOGIN_NAME.fullmatch(self.name):
            raise ValidationError(
                f"Login: {self.name!r} is not an account name.",
                hint='Use lower-case letters, digits, "-" and "_": Login("tenant", ...).',
            )
        if self.name == "root":
            raise ValidationError(
                "Login: root is not a login; SshAccess(keys=) or from_delivery=True is how "
                "root logs in.",
                hint='Name the account the login is for: Login("tenant", ...).',
            )
        self._check_keys()
        for keyword in ("command", "shell"):
            value = getattr(self, keyword)
            if value is None:
                continue
            words = value.split() if isinstance(value, str) else []
            if not words or not words[0].startswith("/") or _COMMAND_REFUSED & set(value):
                raise ValidationError(
                    f"Login {self.name}: {keyword}={value!r} is not one line starting with "
                    "an absolute path, without '\"' or '\\'.",
                    hint=f'Login("{self.name}", {keyword}="/usr/bin/app-status").',
                )
            if keyword == "shell" and len(words) > 1:
                raise ValidationError(
                    f"Login {self.name}: shell={value!r} is a command line, not a shell.",
                    hint=f'Pass the shell alone, or the line as command="{value}".',
                )
            object.__setattr__(self, keyword, " ".join(words))
        if self.command is None and self.shell is None:
            raise ValidationError(
                f"Login {self.name} runs nothing: it has no command= and no shell=.",
                hint=f'Login("{self.name}", command="/usr/bin/app-status"), or shell= for a '
                "restricted shell the image ships.",
            )
        if isinstance(self.sudo, Sudo) or isinstance(self.sudo, str):
            raise ValidationError(
                f"Login {self.name}: sudo= takes a list of Sudo rules.",
                hint='sudo=[Sudo("/usr/bin/systemctl restart app")].',
            )
        object.__setattr__(self, "sudo", tuple(self.sudo))
        for rule in self.sudo:
            if not isinstance(rule, Sudo):
                raise ValidationError(
                    f"Login {self.name}: sudo= holds {rule!r}, not a Sudo.",
                    hint=f'sudo=[Sudo("{rule}")].' if isinstance(rule, str) else "Pass Sudo(...).",
                )
        if not isinstance(self.home, bool):
            raise ValidationError(
                f"Login {self.name}: home= must be True or False.", hint="Pass home=True."
            )

    def _check_keys(self) -> None:
        from tundravm._image._secrets import Secret

        if isinstance(self.keys, Secret):
            return
        if isinstance(self.keys, str):
            raise ValidationError(
                f"Login {self.name}: keys= takes a list of public keys or a Secret.",
                hint="Pass keys=['ssh-ed25519 AAAA... me@host'].",
            )
        object.__setattr__(self, "keys", tuple(self.keys))
        for key in self.keys:
            if not isinstance(key, str) or not _SSH_PUBLIC_KEY.fullmatch(key.strip()):
                raise ValidationError(
                    f"Login {self.name}: {key!r} is not one OpenSSH public key line.",
                    hint="Paste the .pub file's line: 'ssh-ed25519 AAAA... comment'.",
                )
        if not self.keys:
            raise ValidationError(
                f"Login {self.name} has no key to accept, so nobody could log in.",
                hint=f"Pass keys=['ssh-ed25519 AAAA...'], or keys=Secret('{self.name}-keys') "
                "for keys delivered after attestation.",
            )

    @property
    def delivered(self) -> bool:
        """Whether the keys arrive after attestation (a ``Secret``) rather than baked in."""
        return not isinstance(self.keys, tuple)

    @property
    def key_file(self) -> str:
        """Where its keys are read: ``/etc/ssh/authorized_keys.d/<name>``."""
        return f"{SSH_AUTHORIZED_KEYS_DIR}/{self.name}"

    @property
    def options(self) -> str:
        """The ``authorized_keys`` options every baked key carries."""
        if self.command is not None:
            return f'restrict,command="{self.command}"'
        return "restrict,pty"

    @property
    def account_shell(self) -> str:
        """The login shell: *shell*, else ``/bin/sh`` to run *command* (sshd runs a forced
        command through it, so ``nologin`` would refuse it)."""
        return self.shell or _FORCED_SHELL

    @property
    def sudoers_file(self) -> str | None:
        """``/etc/sudoers.d/<name>``, or ``None`` without *sudo*."""
        return f"{SUDOERS_DIRECTORY}/{self.name}" if self.sudo else None

    def config(self) -> str:
        """The ``sshd_config.d`` file: its own keys only, no forwarding, the command if any."""
        lines = [
            f"# tundravm Login {self.name}: its own keys, no forwarding"
            + (", this command only" if self.command is not None else ""),
            f"Match User {self.name}",
            f"    AuthorizedKeysFile {SSH_AUTHORIZED_KEYS_DIR}/%u",
            "    DisableForwarding yes",
        ]
        if self.command is not None:
            lines += ["    PermitTTY no", f"    ForceCommand {self.command}"]
        return "\n".join(lines) + "\n"

    def sudoers(self) -> str:
        """The ``sudoers.d`` file: exactly the *sudo* rules."""
        rules = "".join(f"{rule.rule(self.name)}\n" for rule in self.sudo)
        return f"# tundravm Login {self.name}: these commands as root, nothing else\n{rules}"

    def declarations(self) -> Iterator[Declaration]:
        """The account, its sshd block, its keys and its sudo rules."""
        home = f"/home/{self.name}" if self.home else None
        yield User(self.name, system=False, home=home, shell=self.account_shell)
        if home is not None:
            chown = shlex.join(["chown", "-R", f"{self.name}:{self.name}", home])
            yield BootStep(f"{OWNERS_STEP}-login-{self.name}", f"{chown}\n")
        yield File.text(SSH_LOGIN_CONFIG.format(name=self.name), self.config())
        if isinstance(self.keys, tuple):
            keys = "".join(f"{self.options} {key.strip()}\n" for key in self.keys)
            yield File.text(self.key_file, keys, mode=0o644)
        else:
            # the endpoint may write only an existing directory (ReadWritePaths=-...)
            script = f'mkdir -p "$BUILDROOT{SSH_AUTHORIZED_KEYS_DIR}"'
            yield Hook(f"ssh-login-{self.name}-keys", "postinst", script)
        if self.sudoers_file is not None:
            yield Package("sudo")
            yield File.text(self.sudoers_file, self.sudoers(), mode=0o440)
            check = f"mkosi-chroot visudo -c -f {self.sudoers_file}"
            yield Hook(f"ssh-login-{self.name}-sudoers", "postinst", check)


@component
class SshAccess(Composite):
    """Root logs in over SSH with a public key, and with nothing else.

    Installs ``openssh-server`` and enables Debian's ``ssh.service`` under the boot target,
    listening on *port*: the image's ``Firewall`` opens it and ``tundravm run`` forwards it
    from ``127.0.0.1`` (an image without ``SshAccess`` gets no SSH forward).
    The image carries no host keys (the build removes them): the ``ssh-host-keys`` task
    generates this instance's at boot (``ssh-keygen -A``, skipped when they exist) and sshd
    starts after it. ``sshd_config.d/50-tundravm.conf`` allows public-key authentication
    only and root by key only (``PermitRootLogin prohibit-password``): root is the account
    the attested endpoint takes a key for, so refusing root would refuse the operator, while
    a password is never accepted.
    *authorized_keys* are public keys baked into the image
    (``/etc/ssh/authorized_keys.d/root``); *from_secrets* accepts the key the image's
    ``Secrets`` endpoint takes once the operator verified a quote binding these host keys
    (``/root/.ssh/authorized_keys``, read at each login, so sshd waits only for its host
    keys). *logins* are the other accounts (:class:`Login`), each with its own
    ``sshd_config.d/60-tundravm-login-<name>.conf``. A key or a login is required.
    Component name: ``ssh-access``.
    """

    port: int = 22
    authorized_keys: tuple[str, ...] = ()
    from_secrets: bool = False
    logins: tuple[Login, ...] = ()

    def compose(self) -> Iterator[Declaration]:
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ValidationError(
                f"SshAccess: port must be an int, got {self.port!r}.", hint="Pass port=22."
            )
        Port(self.port)  # range check
        if isinstance(self.authorized_keys, str):
            raise ValidationError(
                "SshAccess: authorized_keys= takes a tuple of public keys, not one string.",
                hint="Pass authorized_keys=('ssh-ed25519 AAAA... me@host',).",
            )
        for key in self.authorized_keys:
            if not isinstance(key, str) or not _SSH_PUBLIC_KEY.fullmatch(key.strip()):
                raise ValidationError(
                    f"SshAccess: {key!r} is not one OpenSSH public key line.",
                    hint="Paste the .pub file's line: 'ssh-ed25519 AAAA... comment'.",
                )
        if isinstance(self.logins, Login) or not all(isinstance(x, Login) for x in self.logins):
            raise ValidationError(
                f"SshAccess: logins= takes a tuple of Login, got {self.logins!r}.",
                hint='Pass logins=(Login("tenant", ...),).',
            )
        names = [login.name for login in self.logins]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValidationError(
                f"SshAccess: two logins are called {', '.join(twice)}.",
                hint="Give each Login its own name, or merge their keys and rules into one.",
            )
        if not self.authorized_keys and not self.from_secrets and not self.logins:
            raise ValidationError(
                "SshAccess has no key to accept, so nobody could log in.",
                hint="Pass SshAccess(keys=[...]), or SshAccess(from_delivery=True).",
            )
        yield Package("openssh-server")
        yield File.text(SSH_CONFIG, self.config())
        if self.authorized_keys:
            keys = "".join(f"{key.strip()}\n" for key in self.authorized_keys)
            yield File.text(f"{SSH_AUTHORIZED_KEYS_DIR}/root", keys, mode=0o644)
        host_keys = Task(
            "ssh-host-keys",
            "/usr/bin/ssh-keygen -A",
            description="Generate this instance's SSH host keys",
            user=Root(),
            needs=(),
            # /etc must be writable for the keys; /usr and /boot stay read-only
            hardening=Hardening.strict(protect_system="yes", network="none"),
            conditions=(Condition.path_exists(SSH_HOST_KEY, negate=True),),
        )
        yield host_keys
        yield Unit("ssh.service", enabled=True, needs=(host_keys.ready,), ports=(self.port,))
        for login in self.logins:
            yield from login.declarations()

    def config(self) -> str:
        """The ``sshd_config.d`` drop-in: keys only, root by key only."""
        return (
            "# tundravm SshAccess: public keys only; root logs in by key, never by password\n"
            f"Port {self.port}\n"
            "AuthenticationMethods publickey\n"
            "PubkeyAuthentication yes\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "PermitEmptyPasswords no\n"
            "PermitRootLogin prohibit-password\n"
            f"AuthorizedKeysFile .ssh/authorized_keys {SSH_AUTHORIZED_KEYS_DIR}/%u\n"
            "X11Forwarding no\n"
            "PermitTunnel no\n"
            "MaxAuthTries 3\n"
            "LoginGraceTime 30\n"
        )


def RuntimeDirectory(  # noqa: N802 - a declaration factory, spelled like the declarations
    path: str,
    *,
    owner: str | None = None,
    group: str | None = None,
    mode: int = 0o755,
    after: Sequence[Ready] = (),
    name: str | None = None,
) -> BootStep:
    """A :class:`BootStep` that creates directory *path* at boot (``install -d``).

    *after* takes readiness handles (``after=[disk.ready]`` for a directory on a
    ``Disk``); the step is named *name*, else ``dir`` plus *path* with ``/`` as
    ``-`` (``dir-persistent-exporter``). Its ``.ready`` is what ``needs=`` takes.
    """
    target = _runtime_path("RuntimeDirectory", path)
    command = ["install", "-d", "-m", _octal("RuntimeDirectory", mode)]
    command += ["-o", owner] if owner else []
    command += ["-g", group] if group else []
    script = shlex.join([*command, target]) + "\n"
    return BootStep(name or _step_name("dir", target), script, after=tuple(after))


def RuntimeFile(  # noqa: N802 - a declaration factory, spelled like the declarations
    path: str,
    text: str,
    *,
    owner: str | None = None,
    group: str | None = None,
    mode: int = 0o644,
    after: Sequence[Ready] = (),
    name: str | None = None,
) -> BootStep:
    """A :class:`BootStep` that writes *text* to *path* at boot, then sets its mode and owner.

    The parent directory must exist by then: order the step after the one that
    creates it (``after=[directory.ready]``). The step is named *name*, else
    ``file`` plus *path* with ``/`` as ``-``. Its ``.ready`` is what ``needs=`` takes.
    """
    target = _runtime_path("RuntimeFile", path)
    if not isinstance(text, str):
        raise ValidationError(
            f"RuntimeFile({path!r}): text must be a str, got {text!r}.",
            hint="Pass the file's content as a str.",
        )
    quoted = shlex.quote(target)
    lines = [
        f"printf '%s' {shlex.quote(text)} > {quoted}",
        f"chmod {_octal('RuntimeFile', mode)} {quoted}",
    ]
    if owner or group:
        spec = f"{owner or ''}:{group}" if group else str(owner)
        lines.append(f"chown {shlex.quote(spec)} {quoted}")
    script = "\n".join(lines) + "\n"
    return BootStep(name or _step_name("file", target), script, after=tuple(after))


RunStage = Literal["prepare", "build", "postinst", "finalize"]
"""When a :func:`Run` command runs while the image is built."""
RUN_STAGES: tuple[str, ...] = ("prepare", "build", "postinst", "finalize")
_RUN_SHELL = ("bash", "-euo", "pipefail", "-c")


def Run(  # noqa: N802 - a declaration factory, spelled like the declarations
    command: str | Sequence[str],
    *,
    stage: RunStage = "postinst",
    user: str | None = None,
    name: str | None = None,
    after: Sequence[str] = (),
) -> Hook:
    """A :class:`Hook` that runs *command* inside the image while it is built.

    *command* is a shell command line (``"update-ca-certificates --fresh"``), a
    script of several lines, or a sequence of words (``["install", "-d", path]``,
    each quoted for you). A line whose words need no quoting runs as it is; any
    other runs through ``bash -euo pipefail -c`` inside the image, so pipes,
    ``&&`` and redirections never escape to the build host.

    *stage* says when, in plain words:

    - ``"postinst"`` (default): after the packages, files and source builds are in
      the image. Configure the image here.
    - ``"finalize"``: last, once everything else, debloat included, has run.
    - ``"prepare"``: after the packages are installed, before source builds.
    - ``"build"``: inside the build environment, with the build packages; what it
      writes outside ``$DESTDIR`` does not reach the image.

    *user* runs the command as that account (``runuser``), which must exist by
    then (a ``User`` declaration's account exists from ``postinst`` on); ``None``
    runs it as root. The hook is named *name*, else ``run-`` and the program's
    name; *after* names hooks of the same stage it runs after.
    """
    _require_run_choice(stage)
    words = _run_words(command)
    first = words[-1].split()[0] if words[0] == _RUN_SHELL[0] and len(words) > 1 else words[0]
    if user is not None:
        if not isinstance(user, str) or not user or not _ACCOUNT.fullmatch(user):
            raise ValidationError(
                f"Run: user {user!r} is not an account name.", hint='Pass a name such as "app".'
            )
        words = ["runuser", "-u", user, "--", *words]
    script = "mkosi-chroot " + shlex.join(words)
    program = posixpath.basename(first) or "command"
    return Hook(name or _step_name("run-", program), stage, script, after=tuple(after))


_ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]*\$?")


def _require_run_choice(stage: object) -> None:
    if stage not in RUN_STAGES:
        raise ValidationError(
            f"Run: stage {stage!r} is not one of {', '.join(RUN_STAGES)}.",
            hint='Most commands belong in stage="postinst" (the default).',
        )


def _run_words(command: str | Sequence[str]) -> list[str]:
    """*command* as the words ``mkosi-chroot`` runs: the words of a plain command line,
    a sequence as given, anything else wrapped in :data:`_RUN_SHELL`."""
    if isinstance(command, str):
        text = command.strip()
        if not text:
            raise ValidationError("Run: the command is empty.", hint="Pass the command to run.")
        try:
            words = shlex.split(text)
        except ValueError:
            words = []
        if words and shlex.join(words) == text:
            return words
        return [*_RUN_SHELL, text]
    words = list(command)
    if not words or not all(isinstance(word, str) and word for word in words):
        raise ValidationError(
            f"Run: command {command!r} must be a non-empty sequence of non-empty strings.",
            hint='Pass a command line ("update-ca-certificates --fresh") or its words.',
        )
    return words


def _runtime_path(owner: str, path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise ValidationError(
            f"{owner}: path {path!r} must be an absolute path below /.",
            hint="Write the path from / (e.g. /persistent/app).",
        )
    return posixpath.normpath(path)


def _octal(owner: str, mode: int) -> str:
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
        raise ValidationError(
            f"{owner}: mode {mode!r} must be an int such as 0o755.",
            hint="Pass an octal int such as 0o644.",
        )
    return f"{mode:04o}"


def _step_name(prefix: str, path: str) -> str:
    """``prefix`` plus *path* as a step name: ``/`` becomes ``-``, other odd characters ``_``."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", f"{prefix}{path.replace('/', '-')}")


__all__ = [
    "Backports",
    "CargoBinary",
    "Composite",
    "ConfigFile",
    "DevTools",
    "EfiStub",
    "Export",
    "Firewall",
    "GoBinary",
    "HttpEndpoint",
    "Login",
    "NETWORK_FILE",
    "Network",
    "Port",
    "RUN_STAGES",
    "Run",
    "RunStage",
    "RuntimeDirectory",
    "RuntimeFile",
    "SSH_AUTHORIZED_KEYS_DIR",
    "SSH_CONFIG",
    "SSH_LOGIN_CONFIG",
    "SUDOERS_DIRECTORY",
    "SshAccess",
    "StaticAddress",
    "Sudo",
    "TEE_HYPERVISOR_CLOCKS",
    "TEE_MASKED_UNITS",
    "TEE_NAMESERVERS",
    "TEE_NTS_SERVERS",
    "TEE_SYSCTL",
    "Tdxs",
    "TdxsType",
    "TeeBase",
    "tools_source",
]
