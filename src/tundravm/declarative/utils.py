"""Shipped fragments: :class:`EfiStub`, :class:`Backports`, :class:`Tdxs` and :class:`DevTools`.

Each is a :class:`Composite`: a :class:`Fragment` whose configuration is its own
dataclass fields, so ``EfiStub(snapshot=SNAPSHOT, version="257.8-1~deb13u1")`` reads like a
declaration and goes wherever a ``Fragment`` does.

The committed ``examples/surge-tdx-prover/mkosi`` trees pin their output. Under
``Mkosi(dialect="nethermind-v1")`` the account lines and build hooks are the
historical ones; the ``current`` dialect lowers the ``Group``/``User``
declarations through the compiler's account prelude instead.
"""

from __future__ import annotations

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
from tundravm._source import FrozenMap
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import (
    approved_registers,
    is_placeholder,
    read_policy,
    validator_measurements,
)

from ._component import Composite, Export, HttpEndpoint, component
from ._kernel_hardening import KernelHardening
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
    Package,
    Port,
    Ready,
    Setting,
    Unit,
    User,
)

if TYPE_CHECKING:
    from .lifecycle import Measurements

TdxsType = Literal["tdx", "azure", "gcp", "simulator"]

TUNDRA_TOOLS = Git("https://github.com/Hyodar/tundra-tools.git", "master")
"""The ``tundra-tools`` repository :class:`Tdxs` builds from by default."""

BACKPORTS_TREE = (
    "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
)
"""The ``nethermind-v1`` ``SandboxTrees=`` entry that exposes the generated sources to apt."""
DEFAULT_DEBIAN_MIRROR = "http://deb.debian.org/debian"
"""The mirror :class:`Backports` uses when neither it nor the recipe sets one."""
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

    Neither lands in the image. The ``current`` dialect writes them at compile time
    to the variant's ``mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources``
    (see :meth:`render_sources`), with ``preferences.d/debian-backports.pref`` pinning
    backports to 200 and sid to 100 (see :meth:`render_preferences`): packages
    come from the release unless it lacks them, then from backports, then from
    sid. ``include_sid=False`` drops the sid source and its pin, so a package
    missing from the release and backports fails the build instead of coming from
    unstable. *archive_url* is the apt URI used
    verbatim; without it the sources follow the platform's ``mirror`` (a mirror root)
    and ``snapshot`` the way mkosi does, else ``deb.debian.org``. Without
    *release*, the platform's release. Under ``nethermind-v1`` a sync hook
    generates them into ``mkosi.builddir``, reading the build's mirror and
    ``$RELEASE``, with no pins. Fragment name: ``backports``.
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
        lines: list[str] = []
        if self.archive_url is not None:
            lines.append(f'MIRROR="{self.archive_url}"')
        else:
            lines.extend(
                (
                    'MIRROR=$(jq -r .Mirror "$BUILDDIR/config.json" 2>/dev/null || echo "")',
                    'if [ -z "$MIRROR" ] || [ "$MIRROR" = "null" ]; then',
                    '    MIRROR="http://deb.debian.org/debian"',
                    "fi",
                )
            )
        if self.release is not None:
            lines.append(f'RELEASE="{self.release}"')
        lines.append(
            'cat > "$BUILDDIR/debian-backports.sources" <<EOF\n'
            + _SOURCES_STANZA.format(mirror="$MIRROR", suite="${RELEASE}-backports")
            + (
                "\n" + _SOURCES_STANZA.format(mirror="$MIRROR", suite="sid")
                if self.include_sid
                else ""
            )
            + "EOF"
        )
        return Fragment(
            "backports",
            items=(
                Hook("backports", "sync", "\n".join(lines)),
                Setting("Build", "SandboxTrees", (BACKPORTS_TREE,)),
            ),
        )

    def render_sources(
        self, *, mirror: str | None, release: str, snapshot: str | None = None
    ) -> str:
        """The deb822 sources file the ``current`` dialect writes into the build sandbox.

        *mirror* (a mirror root), *release* and *snapshot* are the recipe's; the
        fragment's own fields win.
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
        """The apt pins the ``current`` dialect writes next to :meth:`render_sources`' file.

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


@component
class EfiStub(Composite):
    """``systemd-boot-efi`` *version* from the Debian *snapshot*, for a pinned EFI stub.

    *snapshot* is a snapshot ID (``20251113T083151Z``, read from
    ``snapshot.debian.org``) or the URL of a snapshot archive. The snapshot must
    carry *version*: the pool keeps only what some suite listed at that time, so
    pick the version a suite of that snapshot ships. Outside ``nethermind-v1`` the
    package (:attr:`deb_url`) is a source named ``efi-stub``: ``tundravm lock`` pins
    its sha256, ``tundravm fetch`` downloads it on the host and the postinst hook
    installs the mounted copy with ``dpkg -i``, so the build sandbox downloads
    nothing. Compiled without a pin, the hook downloads it instead and fails the
    build with that advice when the download fails (see :meth:`render_script`).
    A postinst hook. Fragment name: ``efi-stub``.
    """

    snapshot: str
    version: str

    def compose(self) -> Fragment:
        if not self.snapshot or not self.version:
            raise ValidationError(
                "EfiStub requires a non-empty snapshot and version.",
                hint="Pass snapshot= a snapshot ID such as '20251113T083151Z' and version= "
                "the systemd-boot-efi version it ships, e.g. '257.8-1~deb13u1'.",
            )
        script = (
            f'EFI_SNAPSHOT_URL="{_snapshot_archive(self.snapshot)}"\n'
            f'EFI_PACKAGE_VERSION="{self.version}"\n'
            'DEB_URL="${EFI_SNAPSHOT_URL}/pool/main/s/systemd/'
            'systemd-boot-efi_${EFI_PACKAGE_VERSION}_amd64.deb"\n'
            "WORK_DIR=$(mktemp -d)\n"
            'curl -sSfL -o "$WORK_DIR/systemd-boot-efi.deb" "$DEB_URL"\n'
            'cp "$WORK_DIR/systemd-boot-efi.deb" "$BUILDROOT/tmp/"\n'
            "mkosi-chroot dpkg -i /tmp/systemd-boot-efi.deb\n"
            + _STUB_COPY
            + 'rm -rf "$WORK_DIR" "$BUILDROOT/tmp/systemd-boot-efi.deb"'
        )
        return Fragment("efi-stub", items=(Hook("efi-stub", "postinst", script),))

    @property
    def deb_url(self) -> str:
        """The package in the snapshot's pool: ``systemd-boot-efi_<version>_amd64.deb``."""
        return (
            f"{_snapshot_archive(self.snapshot)}/pool/main/s/systemd/"
            f"systemd-boot-efi_{self.version}_amd64.deb"
        )

    def render_script(self) -> str:
        """The postinst script the ``current`` dialect runs in place of the composed hook's.

        mkosi-chroot mounts its own ``/tmp``, so the package goes to the image root;
        a failed download names the version and snapshot. Unlike ``nethermind-v1``,
        it leaves ``linuxx64.efi.stub`` alone: copying ``systemd-bootx64.efi`` over
        it makes the UKI a systemd-boot binary.
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


_STUB_COPY = (
    'cp "$BUILDROOT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" '
    '"$BUILDROOT/usr/lib/systemd/boot/efi/linuxx64.efi.stub" 2>/dev/null || true\n'
)


@component
class Tdxs(Composite):
    """The ``tdxs`` attestation service: source build, config, socket-activated units, account.

    Declares the ``tdx`` group (other services join it for ``/dev/tdx_guest``) and
    the ``tdxs`` user. *needs* lists the runtime-init steps the service waits for. The
    service is a typed ``Service(socket=Listen(...))``, hardened for its job
    (``_modules/tdxs.py`` ``Tdxs.service`` gives the reasoning); ``legacy_units=True``
    ships the historical ``nethermind-v1`` unit texts instead.
    Fragment name: ``tdxs``. ``policy_file`` is the file :meth:`from_policy` read
    (``None`` otherwise); it is not configuration, so equality and ``repr`` skip it.
    """

    source: Git = TUNDRA_TOOLS
    issuer: TdxsType | None = "tdx"
    validator: TdxsType | None = None
    expected_measurements: Mapping[str, str] = field(default_factory=FrozenMap)
    check_revocations: bool = False
    get_collateral: bool = False
    verify_imds: bool = False
    verify_identity_token: bool = False
    needs: Sequence[Ready] = ()
    legacy_units: bool = False
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

        *policy* is a file ``tundravm measure --export-policy`` wrote (or its
        dict). Registers become the validator's lower-case keys (``rtmr0``..), one value
        each: the tdxs validator compares a single expected value per register, so a
        register the policy accepts several values for (a gcp policy's per-firmware MRTD
        and RTMR0) is narrowed with *firmware* and *label*.
        Unverifiable registers are not checked unless the policy's ``platform_approval``
        (``measure --approve-mrtd/--approve-rtmr0``) approves values for them, which are
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
        allow_placeholder: bool = False,
        **kwargs: Any,
    ) -> Tdxs:
        """:meth:`from_policy` for a ``measure()`` result in memory."""
        return cls.from_policy(
            measurements.to_policy(allow_placeholder=allow_placeholder),
            validator=validator,
            mrtd=mrtd,
            allow_placeholder=allow_placeholder,
            **kwargs,
        )

    def compose(self) -> Fragment:
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
        return Fragment(
            "tdxs",
            items=(
                *(Package(name, role="build") for name in _tdxs.TDXS_BUILD_PACKAGES),
                Build(
                    "tdxs",
                    self.source,
                    script=(
                        'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
                        "-o ./build/tdxs ./cmd/tdxs"
                    ),
                    install=(Install("build/tdxs", "/usr/bin/tdxs"),),
                ),
                File(spec.config_path, spec.render_config()),
                *(self._legacy_units(spec) if self.legacy_units else (spec.service(self.needs),)),
                Group(spec.group),
                User(spec.user, home=f"/home/{spec.user}", primary_group=spec.group),
            ),
        )

    def _legacy_units(self, spec: _tdxs.Tdxs) -> tuple[Unit, Unit]:
        """The historical ``tdxs.service``/``tdxs.socket`` texts (``_units/v1.py``)."""
        return (
            Unit(spec.service_name, spec.render_service_unit(), enabled=True, needs=self.needs),
            Unit(spec.socket_name, spec.render_socket_unit(), enabled=True, needs=self.needs),
        )


@component
class DevTools(Composite):
    """Debugging tools, a serial console and password root login. Never ship it: mark its
    variant ``Variant(..., release=False)``.

    The root password is hashed with a fixed salt, so a second build of the variant
    matches the first: *salt* (1 to 16 of ``[A-Za-z0-9./]``), or by default the first 16
    hex digits of ``sha256("tundravm-devtools:<variant name>")``, computed in the
    postinst script from mkosi's ``$IMAGE_ID``. The serial getty on ``ttyS0`` is enabled
    at build time. ``reproducible=False`` keeps the historical random salt and the
    ``serial-console.service`` that enables the getty at boot (``nethermind-v1`` trees).
    Fragment name: ``devtools``.
    """

    root_password: str = _DEFAULT_ROOT_PASSWORD
    salt: str | None = None
    reproducible: bool = True

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
                "derived from the variant name.",
            )
        hashed = f'openssl passwd -6 "{self.root_password}"'
        if self.reproducible:
            salt = _devtools.DERIVED_SALT_LINE if self.salt is None else f'SALT="{self.salt}"'
            hashed = f'openssl passwd -6 -salt "$SALT" "{self.root_password}"'
            line = f"{salt}\nROOT_PASS=$({hashed})"
        else:
            line = f"ROOT_PASS=$({hashed})"
        login = _devtools.DEVTOOLS_POSTINST_SCRIPT.replace(_devtools.HISTORICAL_HASH_LINE, line)
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
    (``Go``; ``CGO_ENABLED=0`` unless *cgo*) into ``build/<file name of install>`` and
    installs it at *install* (``/usr/bin/<name>`` by default). ``executable`` is that path, for a
    ``Service`` command: ``Service(name, f"{binary.executable} --flag")``.
    Fragment name: *name*.
    """

    name: str
    source: Git | Http
    _: KW_ONLY
    package: str
    install: str | None = None
    env: Mapping[str, str] = field(default_factory=FrozenMap)
    tags: tuple[str, ...] = ()
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
    (``/usr/bin/<name>`` by default). Fragment name: *name*.
    """

    name: str
    source: Git | Http
    _: KW_ONLY
    bin: str | None = None
    package: str | None = None
    install: str | None = None
    features: tuple[str, ...] = ()
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
class StaticAddress:
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
    networkd units and the ``.network`` file. Fragment name: ``network``.
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
    *firewall*: :class:`Firewall`, which drops inbound traffic except the ports the variant's
    services declare; pass ``firewall=False`` and add your own ``Firewall(...)`` to change it.
    Fragment name: ``tee-base``.
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
    "BACKPORTS_TREE",
    "TUNDRA_TOOLS",
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
    "KernelHardening",
    "NETWORK_FILE",
    "Network",
    "Port",
    "RUN_STAGES",
    "Run",
    "RunStage",
    "RuntimeDirectory",
    "RuntimeFile",
    "StaticAddress",
    "TEE_HYPERVISOR_CLOCKS",
    "TEE_MASKED_UNITS",
    "TEE_NAMESERVERS",
    "TEE_NTS_SERVERS",
    "TEE_SYSCTL",
    "Tdxs",
    "TdxsType",
    "TeeBase",
]
