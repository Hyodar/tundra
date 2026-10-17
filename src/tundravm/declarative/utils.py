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

import posixpath
import re
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tundravm._modules import devtools as _devtools
from tundravm._modules import tdxs as _tdxs
from tundravm._source import FrozenMap
from tundravm.errors import MeasurementError, ValidationError
from tundravm.measure.policy import (
    is_placeholder,
    read_policy,
    validator_measurements,
)

from ._component import Composite, Export, HttpEndpoint, component
from .config import ConfigFile
from .model import (
    Build,
    Cargo,
    File,
    Fragment,
    Git,
    Go,
    Group,
    Hook,
    Http,
    Install,
    Package,
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
    the ``tdxs`` user. *after_init* orders both units after ``runtime-init.service``.
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
    after_init: bool = False
    policy_file: Path | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def from_policy(
        cls,
        policy: str | Path | Mapping[str, object],
        *,
        validator: TdxsType = "tdx",
        mrtd: str | None = None,
        allow_placeholder: bool = False,
        **kwargs: Any,
    ) -> Tdxs:
        """A verifier whose ``expected_measurements`` are *policy*'s registers.

        *policy* is a file ``tundravm measure --export-policy`` wrote (or its
        dict). Registers become the validator's lower-case keys (``rtmr0``..);
        RTMR tools do not report MRTD, so pass *mrtd* to have it checked too.
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
        registers = data["registers"]
        assert isinstance(registers, dict)
        expected = validator_measurements(registers, mrtd=mrtd)
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
        """:meth:`from_policy` for a ``measure()`` result (rtmr scheme) in memory."""
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
                Unit(
                    spec.service_name,
                    spec.render_service_unit(),
                    enabled=True,
                    after_init=self.after_init,
                ),
                Unit(
                    spec.socket_name,
                    spec.render_socket_unit(),
                    enabled=True,
                    after_init=self.after_init,
                ),
                Group(spec.group),
                User(spec.user, home=f"/home/{spec.user}", primary_group=spec.group),
            ),
        )


@component
class DevTools(Composite):
    """Debugging tools, a serial console and password root login. Never ship it: mark its
    variant ``Variant(..., release=False)``.

    The root password is hashed with a fixed salt, so a second build of the variant
    matches the first: *salt* (1 to 16 of ``[A-Za-z0-9./]``), or by default the first 16
    hex digits of ``sha256("tundravm-devtools:<variant name>")``, computed in the
    postinst script from mkosi's ``$IMAGE_ID``. ``reproducible=False`` keeps the
    historical random salt (``nethermind-v1`` trees). Fragment name: ``devtools``.
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
                Unit("serial-console.service", _devtools.SERIAL_CONSOLE_SERVICE),
                Hook(
                    "devtools-serial-console",
                    "postinst",
                    "mkosi-chroot systemctl enable serial-console.service",
                ),
                Hook("devtools-root-login", "postinst", login),
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

    The ``Build`` named *name* runs ``go build -trimpath`` of *package* (``Go``)
    into ``build/<file name of install>`` and installs it at *install*
    (``/usr/bin/<name>`` by default). ``executable`` is that path, for a
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

    The ``Build`` named *name* runs ``cargo build --frozen`` (``Cargo``; *bin* or
    *package* selects the target, *profile* its directory) and installs
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
        )
        build = Build(
            self.name,
            self.source,
            using=cargo,
            install=(Install(cargo.artifact, destination),),
            cache_namespace=self.cache_namespace,
        )
        return Fragment(self.name, items=(build,))


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
    "GoBinary",
    "HttpEndpoint",
    "Tdxs",
    "TdxsType",
]
