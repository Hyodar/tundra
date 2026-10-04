"""Shipped fragments: :class:`EfiStub`, :class:`Backports`, :class:`Tdxs` and :class:`DevTools`.

Each is a :class:`Composite`: a :class:`Fragment` whose configuration is its own
dataclass fields, so ``EfiStub(snapshot=MIRROR, version="255.4-1")`` reads like a
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
"""The ``SandboxTrees=`` entry that exposes the generated backports sources to apt."""

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
                f"{type(self).__name__}.compose() returned {composed!r}, not a Fragment."
            )
        for name in _DERIVED:
            object.__setattr__(self, name, getattr(composed, name))

    def compose(self) -> Fragment:
        """The fragment this instance stands for."""
        raise NotImplementedError(f"{type(self).__name__} does not implement compose().")


@dataclass(frozen=True, slots=True, kw_only=True)
class Backports(Composite):
    """Debian backports and sid sources, generated at sync time and mounted for apt.

    Without *mirror* the hook reads the build's configured mirror (falling back to
    ``deb.debian.org``); *release* overrides ``$RELEASE``. Fragment name: ``backports``.
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
        stanza = (
            "Types: deb deb-src\n"
            "URIs: $MIRROR\n"
            "Suites: {suite}\n"
            "Components: main\n"
            "Enabled: yes\n"
            "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
        )
        lines.append(
            'cat > "$BUILDDIR/debian-backports.sources" <<EOF\n'
            + stanza.format(suite="${RELEASE}-backports")
            + "\n"
            + stanza.format(suite="sid")
            + "EOF"
        )
        return Fragment(
            "backports",
            items=(
                Hook("backports", "sync", "\n".join(lines)),
                Setting("Build", "SandboxTrees", (BACKPORTS_TREE,)),
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EfiStub(Composite):
    """``systemd-boot-efi`` *version* from the Debian *snapshot*, for a pinned EFI stub.

    A postinst hook. Fragment name: ``efi-stub``.
    """

    snapshot: str
    version: str

    def compose(self) -> Fragment:
        if not self.snapshot or not self.version:
            raise ValidationError("EfiStub requires a non-empty snapshot and version.")
        script = (
            f'EFI_SNAPSHOT_URL="{self.snapshot}"\n'
            f'EFI_PACKAGE_VERSION="{self.version}"\n'
            'DEB_URL="${EFI_SNAPSHOT_URL}/pool/main/s/systemd/'
            'systemd-boot-efi_${EFI_PACKAGE_VERSION}_amd64.deb"\n'
            "WORK_DIR=$(mktemp -d)\n"
            'curl -sSfL -o "$WORK_DIR/systemd-boot-efi.deb" "$DEB_URL"\n'
            'cp "$WORK_DIR/systemd-boot-efi.deb" "$BUILDROOT/tmp/"\n'
            "mkosi-chroot dpkg -i /tmp/systemd-boot-efi.deb\n"
            'cp "$BUILDROOT/usr/lib/systemd/boot/efi/systemd-bootx64.efi" '
            '"$BUILDROOT/usr/lib/systemd/boot/efi/linuxx64.efi.stub" 2>/dev/null || true\n'
            'rm -rf "$WORK_DIR" "$BUILDROOT/tmp/systemd-boot-efi.deb"'
        )
        return Fragment("efi-stub", items=(Hook("efi-stub", "postinst", script),))


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
                "'`' or '\\'."
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
