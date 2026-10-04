"""Shipped composition functions: each returns a :class:`Fragment` of ordinary declarations.

They lower to the bytes the fluent ``efi_stub()``/``backports()`` methods and the
``Tdxs``/``DevTools`` modules emit; the unit and config text comes from the
fluent renderers so the two stay identical. Under ``Mkosi(dialect="nethermind-v1")``
the account lines and build hooks match those modules exactly; the ``current``
dialect lowers the ``Group``/``User`` declarations through the compiler's account
prelude instead.
"""

from __future__ import annotations

from typing import Literal

from tundravm.errors import ValidationError
from tundravm.modules.devtools import (
    DEVTOOLS_PACKAGES,
    DEVTOOLS_POSTINST_SCRIPT,
    SERIAL_CONSOLE_SERVICE,
)
from tundravm.modules.tdxs import TDXS_BUILD_PACKAGES, Tdxs

from .model import (
    Build,
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
"""The ``tundra-tools`` repository ``tdxs()`` builds from by default."""

BACKPORTS_TREE = (
    "mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"
)
"""The ``SandboxTrees=`` entry that exposes the generated backports sources to apt."""

_DEFAULT_ROOT_PASSWORD = "tdx"
_UNSAFE_PASSWORD = frozenset('"$`\\\n')


def backports(*, mirror: str | None = None, release: str | None = None) -> Fragment:
    """Debian backports and sid sources, generated at sync time and mounted for apt.

    Without *mirror* the hook reads the build's configured mirror (falling back to
    ``deb.debian.org``); *release* overrides ``$RELEASE``.
    """
    lines: list[str] = []
    if mirror is not None:
        lines.append(f'MIRROR="{mirror}"')
    else:
        lines.extend(
            (
                'MIRROR=$(jq -r .Mirror "$BUILDDIR/config.json" 2>/dev/null || echo "")',
                'if [ -z "$MIRROR" ] || [ "$MIRROR" = "null" ]; then',
                '    MIRROR="http://deb.debian.org/debian"',
                "fi",
            )
        )
    if release is not None:
        lines.append(f'RELEASE="{release}"')
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


def efi_stub(*, snapshot: str, version: str) -> Fragment:
    """Install ``systemd-boot-efi`` *version* from the Debian *snapshot* for a pinned EFI stub."""
    if not snapshot or not version:
        raise ValidationError("efi_stub() requires a non-empty snapshot and version.")
    script = (
        f'EFI_SNAPSHOT_URL="{snapshot}"\n'
        f'EFI_PACKAGE_VERSION="{version}"\n'
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


def tdxs(
    *,
    source: Git = TUNDRA_TOOLS,
    issuer: TdxsType | None = "tdx",
    validator: TdxsType | None = None,
    expected_measurements: Pairs = (),
    check_revocations: bool = False,
    get_collateral: bool = False,
    verify_imds: bool = False,
    verify_identity_token: bool = False,
    after_init: bool = False,
) -> Fragment:
    """The ``tdxs`` attestation service: source build, config, socket-activated units, account.

    Declares the ``tdx`` group (other services join it for ``/dev/tdx_guest``) and
    the ``tdxs`` user. *after_init* orders both units after ``runtime-init.service``.
    """
    spec = Tdxs(
        issuer_type=issuer,
        validator_type=validator,
        expected_measurements=dict(expected_measurements) or None,
        check_revocations=check_revocations,
        get_collateral=get_collateral,
        verify_imds=verify_imds,
        verify_identity_token=verify_identity_token,
    )
    return Fragment(
        "tdxs",
        items=(
            *(Package(name, role="build") for name in TDXS_BUILD_PACKAGES),
            Build(
                "tdxs",
                source,
                script=(
                    'mkdir -p ./build && go build -trimpath -ldflags "-s -w -buildid=" '
                    "-o ./build/tdxs ./cmd/tdxs"
                ),
                install=(Install("build/tdxs", "/usr/bin/tdxs"),),
            ),
            File(spec.config_path, spec._render_config()),
            Unit(
                spec.service_name,
                spec._render_service_unit(after=()),
                enabled=True,
                after_init=after_init,
            ),
            Unit(
                spec.socket_name,
                spec._render_socket_unit(after=()),
                enabled=True,
                after_init=after_init,
            ),
            Group(spec.group),
            User(spec.user, home=f"/home/{spec.user}", primary_group=spec.group),
        ),
    )


def devtools(*, root_password: str = _DEFAULT_ROOT_PASSWORD) -> Fragment:
    """Debugging tools, a serial console and password root login. Never ship it."""
    if not root_password or _UNSAFE_PASSWORD & set(root_password):
        raise ValidationError(
            "devtools() root_password must be non-empty one-line text without '\"', '$', "
            "'`' or '\\'."
        )
    login = DEVTOOLS_POSTINST_SCRIPT.replace(
        f'openssl passwd -6 "{_DEFAULT_ROOT_PASSWORD}"', f'openssl passwd -6 "{root_password}"'
    )
    return Fragment(
        "devtools",
        items=(
            *(Package(name) for name in DEVTOOLS_PACKAGES),
            Unit("serial-console.service", SERIAL_CONSOLE_SERVICE),
            Hook(
                "devtools-serial-console",
                "postinst",
                "mkosi-chroot systemctl enable serial-console.service",
            ),
            Hook("devtools-root-login", "postinst", login),
        ),
    )


__all__ = ["BACKPORTS_TREE", "TUNDRA_TOOLS", "backports", "devtools", "efi_stub", "tdxs"]
