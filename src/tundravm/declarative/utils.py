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

from dataclasses import dataclass, field, fields
from typing import Literal

from tundravm._modules import devtools as _devtools
from tundravm._modules import tdxs as _tdxs
from tundravm.errors import ValidationError

from .model import (
    Build,
    Check,
    Declaration,
    File,
    Fragment,
    Git,
    Group,
    Hook,
    Install,
    Package,
    Pairs,
    Setting,
    Unit,
    User,
)

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
_DERIVED = ("name", "items", "requires", "checks")


@dataclass(frozen=True, slots=True)
class Composite(Fragment):
    """A :class:`Fragment` built from its own fields.

    Subclass it as ``@dataclass(frozen=True, slots=True, kw_only=True)``, declare
    the configuration as fields and return the contents from :meth:`compose`. The
    instance takes that fragment's ``name``, ``items``, ``requires`` and ``checks``;
    its ``repr`` and equality are its configuration fields. Lists passed for
    fields are frozen into tuples.
    """

    name: str = field(init=False, repr=False, compare=False)
    items: tuple[Declaration | Fragment, ...] = field(init=False, repr=False, compare=False)
    requires: tuple[str, ...] = field(init=False, repr=False, compare=False)
    checks: tuple[Check, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for item in fields(self):
            if item.init and isinstance(value := getattr(self, item.name), list):
                object.__setattr__(self, item.name, tuple(value))
        composed = self.compose()
        if not isinstance(composed, Fragment):
            raise ValidationError(
                f"{type(self).__name__}.compose() returned {composed!r}, not a Fragment.",
                hint="Return Fragment(name, items=(...)) from compose().",
            )
        for name in _DERIVED:
            object.__setattr__(self, name, getattr(composed, name))

    def compose(self) -> Fragment:
        """The fragment this instance stands for."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compose().")


@dataclass(frozen=True, slots=True, kw_only=True)
class Backports(Composite):
    """Debian backports and sid sources for apt during the build (not in the image).

    The ``current`` dialect writes them at compile time to the variant's
    ``mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources`` (see
    :meth:`render_sources`), with ``preferences.d/debian-backports.pref`` pinning
    backports to 200 and sid to 100 (see :meth:`render_preferences`): packages
    come from the release unless it lacks them. *mirror* is the apt URI used
    verbatim; without it the sources follow ``Recipe.mirror`` (a mirror root) and
    ``Recipe.snapshot`` the way mkosi does, else ``deb.debian.org``. Without
    *release*, the release of ``Recipe.base``. Under ``nethermind-v1`` a sync hook
    generates them into ``mkosi.builddir``, reading the build's mirror and
    ``$RELEASE``, with no pins. Fragment name: ``backports``.
    """

    mirror: str | None = None
    release: str | None = None

    def compose(self) -> Fragment:
        lines: list[str] = []
        if self.mirror is not None:
            lines.append(f'MIRROR="{self.mirror}"')
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
            + "\n"
            + _SOURCES_STANZA.format(mirror="$MIRROR", suite="sid")
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
        if self.mirror:
            uri = self.mirror
        elif snapshot:
            uri = _join(mirror or _SNAPSHOT_ROOT, f"archive/debian/{snapshot}")
        elif mirror:
            uri = _join(mirror, "debian")
        else:
            uri = DEFAULT_DEBIAN_MIRROR
        suite = self.release or release
        if not suite:
            raise ValidationError(
                "Backports needs a Debian release: Recipe.base names none.",
                hint="Set Recipe.base to e.g. 'debian/trixie', or pass Backports(release=...).",
            )
        return (
            _SOURCES_STANZA.format(mirror=uri, suite=f"{suite}-backports")
            + "\n"
            + _SOURCES_STANZA.format(mirror=uri, suite="sid")
        )

    def render_preferences(self, *, release: str) -> str:
        """The apt pins the ``current`` dialect writes next to :meth:`render_sources`' file.

        sid has no ``NotAutomatic`` flag, so unpinned it would outrank the release
        wherever its versions are newer.
        """
        suite = self.release or release
        return (
            f"Package: *\nPin: release n={suite}-backports\nPin-Priority: {_BACKPORTS_PRIORITY}\n"
            f"\nPackage: *\nPin: release n=sid\nPin-Priority: {_SID_PRIORITY}\n"
        )


def _join(root: str, path: str) -> str:
    """*path* under mirror *root*, as mkosi joins ``Mirror=`` and its suffixes."""
    return f"{root.rstrip('/')}/{path}"


def _snapshot_archive(snapshot: str) -> str:
    """*snapshot* as a URL: a snapshot ID becomes its ``snapshot.debian.org`` archive."""
    return snapshot if "/" in snapshot else _join(_SNAPSHOT_ROOT, f"archive/debian/{snapshot}")


@dataclass(frozen=True, slots=True, kw_only=True)
class EfiStub(Composite):
    """``systemd-boot-efi`` *version* from the Debian *snapshot*, for a pinned EFI stub.

    *snapshot* is a snapshot ID (``20251113T083151Z``, read from
    ``snapshot.debian.org``) or the URL of a snapshot archive. The snapshot must
    carry *version*: the pool keeps only what some suite listed at that time, so
    pick the version a suite of that snapshot ships. The ``current`` dialect
    fails the build with that advice when the download fails (see
    :meth:`render_script`). A postinst hook. Fragment name: ``efi-stub``.
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


@dataclass(frozen=True, slots=True, kw_only=True)
class Tdxs(Composite):
    """The ``tdxs`` attestation service: source build, config, socket-activated units, account.

    Declares the ``tdx`` group (other services join it for ``/dev/tdx_guest``) and
    the ``tdxs`` user. *after_init* orders both units after ``runtime-init.service``.
    Fragment name: ``tdxs``.
    """

    source: Git = TUNDRA_TOOLS
    issuer: TdxsType | None = "tdx"
    validator: TdxsType | None = None
    expected_measurements: Pairs = ()
    check_revocations: bool = False
    get_collateral: bool = False
    verify_imds: bool = False
    verify_identity_token: bool = False
    after_init: bool = False

    def compose(self) -> Fragment:
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


@dataclass(frozen=True, slots=True, kw_only=True)
class DevTools(Composite):
    """Debugging tools, a serial console and password root login. Never ship it.

    Fragment name: ``devtools``.
    """

    root_password: str = _DEFAULT_ROOT_PASSWORD

    def compose(self) -> Fragment:
        if not self.root_password or _UNSAFE_PASSWORD & set(self.root_password):
            raise ValidationError(
                "DevTools root_password must be non-empty one-line text without '\"', '$', "
                "'`' or '\\'.",
                hint="Pick a password of plain letters, digits and punctuation on one line.",
            )
        login = _devtools.DEVTOOLS_POSTINST_SCRIPT.replace(
            f'openssl passwd -6 "{_DEFAULT_ROOT_PASSWORD}"',
            f'openssl passwd -6 "{self.root_password}"',
        )
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


__all__ = [
    "BACKPORTS_TREE",
    "TUNDRA_TOOLS",
    "Backports",
    "Composite",
    "DevTools",
    "EfiStub",
    "Tdxs",
    "TdxsType",
]
