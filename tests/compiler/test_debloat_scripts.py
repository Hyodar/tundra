"""The generated debloat and kernel module shell, run against a fake ``$BUILDROOT``.

``mkosi-chroot`` is a stub on ``PATH``: ``dpkg-query -L systemd`` lists the fake package's
files and ``depmod`` records its call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tundravm.compiler.emit_mkosi import _DEBLOAT_MASK, _globbed_path, _kernel_module_check

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

UNITS = "usr/lib/systemd/system"
SYSTEMD_FILES = {
    "systemd-ask-password-console.path": "[Path]\nDirectoryNotEmpty=/run/systemd/ask-password\n",
    "systemd-ask-password-console.service": "[Service]\nExecStart=/bin/true\n",
    "proc-sys-fs-binfmt_misc.automount": "[Automount]\nWhere=/proc/sys/fs/binfmt_misc\n",
    "proc-sys-fs-binfmt_misc.mount": "[Mount]\nWhat=binfmt_misc\n",
    "systemd-journald-dev-log.socket": "[Socket]\nService=systemd-journald.service\n",
    "systemd-journald.service": "[Service]\nExecStart=/bin/true\n",
    "kept-trigger.timer": "[Timer]\nOnBootSec=1\n",
    "kept-trigger.service": "[Service]\nExecStart=/bin/true\n",
    "network-pre.target": "[Unit]\n",
    "network.target": "[Unit]\n",
    "multi-user.target": "[Unit]\n",
}


def _stub_chroot(tmp_path: Path, root: Path) -> dict[str, str]:
    """An environment whose ``mkosi-chroot`` serves *root*'s fake systemd package."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    listing = "\n".join(f"/{UNITS}/{name}" for name in SYSTEMD_FILES)
    stub = bindir / "mkosi-chroot"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "$1" = dpkg-query ]; then printf "%s\\n" "{listing}"; exit 0; fi\n'
        f'echo "$*" >> "{tmp_path}/chroot.log"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "BUILDROOT": str(root)}


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    text = "set -euo pipefail\n" + script
    return subprocess.run(["bash", "-c", text], env=env, capture_output=True, text=True)


def test_debloat_masks_triggers_with_their_units_and_keeps_referenced_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    (root / UNITS).mkdir(parents=True)
    for name, text in SYSTEMD_FILES.items():
        (root / UNITS / name).write_text(text, encoding="utf-8")
    (root / UNITS / "nftables.service").write_text(
        "[Unit]\nWants=network-pre.target\nBefore=network-pre.target shutdown.target\n",
        encoding="utf-8",
    )
    wants = root / "etc/systemd/system/sysinit.target.wants"
    wants.mkdir(parents=True)
    (wants / "nftables.service").symlink_to(f"/{UNITS}/nftables.service")
    keep = '"systemd-journald.service" "systemd-journald-dev-log.socket" "kept-trigger.timer"'

    done = _run(_DEBLOAT_MASK.replace("@KEEP@", keep), _stub_chroot(tmp_path, root))

    assert done.returncode == 0, done.stderr
    system = root / "etc/systemd/system"
    masked = {p.name for p in system.iterdir() if p.is_symlink() and os.readlink(p) == "/dev/null"}
    # The path and automount whose units are masked go with them; so does a kept trigger.
    assert {
        "systemd-ask-password-console.path",
        "systemd-ask-password-console.service",
        "proc-sys-fs-binfmt_misc.automount",
        "proc-sys-fs-binfmt_misc.mount",
        "kept-trigger.timer",
        "network.target",
        "multi-user.target",
    } <= masked
    # Kept units, a kept trigger whose unit is kept, and nftables' ordering target stay.
    assert (
        not {
            "systemd-journald.service",
            "systemd-journald-dev-log.socket",
            "network-pre.target",
        }
        & masked
    )


def _kdir(tmp_path: Path, *, crc32c: bool) -> Path:
    """A fake ``/usr/lib/modules/<kver>`` with nf_tables, its dependencies and libcrc32c's
    softdep (satisfied by crc32c_generic when *crc32c*)."""
    root = tmp_path / "root"
    kdir = root / "usr/lib/modules/6.12.0"
    present = ["kernel/net/netfilter/nf_tables.ko.xz", "kernel/lib/libcrc32c.ko.xz"]
    present += ["kernel/net/netfilter/nfnetlink.ko.xz"]
    if crc32c:
        present.append("kernel/crypto/crc32c_generic.ko.xz")
    for path in present:
        (kdir / path).parent.mkdir(parents=True, exist_ok=True)
        (kdir / path).write_bytes(b"")
    (kdir / "modules.dep").write_text(
        "kernel/net/netfilter/nf_tables.ko.xz: kernel/net/netfilter/nfnetlink.ko.xz "
        "kernel/lib/libcrc32c.ko.xz\n"
        "kernel/net/netfilter/nft_compat.ko.xz: kernel/net/netfilter/x_tables.ko.xz\n",
        encoding="utf-8",
    )
    (kdir / "modules.softdep").write_text("softdep libcrc32c pre: crc32c\n", encoding="utf-8")
    (kdir / "modules.alias").write_text(
        "alias crc32c crc32c_generic\nalias crc32c crc32c_intel\n", encoding="utf-8"
    )
    return root


@pytest.mark.parametrize("crc32c", [True, False])
def test_the_module_check_needs_softdep_providers_then_reindexes(
    tmp_path: Path, crc32c: bool
) -> None:
    root = _kdir(tmp_path, crc32c=crc32c)
    (root / "usr/sbin").mkdir(parents=True)
    (root / "usr/sbin/depmod").write_text("", encoding="utf-8")
    (root / "usr/sbin/depmod").chmod(0o755)

    done = _run("\n".join(_kernel_module_check(("nf_tables",))), _stub_chroot(tmp_path, root))

    if crc32c:
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "chroot.log").read_text(encoding="utf-8") == "depmod -a 6.12.0\n"
    else:
        assert done.returncode == 1
        assert "libcrc32c loads crc32c first (softdep)" in done.stderr


def test_the_module_check_needs_every_module_dep(tmp_path: Path) -> None:
    root = _kdir(tmp_path, crc32c=True)
    (root / "usr/lib/modules/6.12.0/kernel/net/netfilter/nft_compat.ko.xz").write_bytes(b"")

    done = _run("\n".join(_kernel_module_check(("nft_compat",))), _stub_chroot(tmp_path, root))

    assert done.returncode == 1
    assert "nft_compat needs kernel/net/netfilter/x_tables.ko.xz" in done.stderr


def test_a_removal_glob_expands_under_buildroot(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "etc/ssh").mkdir(parents=True)
    for name in ("ssh_host_ed25519_key", "ssh_host_ed25519_key.pub", "sshd_config"):
        (root / "etc/ssh" / name).write_text("", encoding="utf-8")

    word = _globbed_path("/etc/ssh/ssh_host_*_key*")
    done = _run(
        f"rm -rf {word}\nrm -rf {_globbed_path('/etc/none_*')}",
        {**os.environ, "BUILDROOT": str(root)},
    )

    assert done.returncode == 0, done.stderr
    assert word == '"$BUILDROOT/etc/ssh/ssh_host_"*"_key"*'
    assert sorted(p.name for p in (root / "etc/ssh").iterdir()) == ["sshd_config"]
