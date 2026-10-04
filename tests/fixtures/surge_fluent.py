"""The surge-tdx-prover recipe as it was written in the fluent ``Image`` API.

A frozen copy kept only to prove the declarative recipe in
``examples/surge-tdx-prover/image.py`` compiles to the same trees for every
profile. The application modules it used are inlined as the calls they made.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from tundravm._image import Image
from tundravm._modules import (
    DevTools,
    DiskEncryption,
    DiskSpec,
    KeyGeneration,
    KeySpec,
    SecretDelivery,
    Tdxs,
)
from tundravm._options import MkosiOptions
from tundravm._source import CargoBuild, DotnetBuild, GitSource, GoBuild, Install, SourceBuild
from tundravm.models import Kernel
from tundravm.platforms import AzurePlatform, GcpPlatform

ROOT = Path(__file__).resolve().parents[2]
PINNED_MIRROR = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"


def _contents() -> ModuleType:
    path = ROOT / "examples" / "surge-tdx-prover" / "contents.py"
    spec = importlib.util.spec_from_file_location("surge_fluent_contents", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _contents()

RAIKO_CARGO_ENV = {
    "RUSTFLAGS": (
        "-C target-cpu=generic -C link-arg=-Wl,--build-id=none "
        "-C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu"
    ),
    "CARGO_HOME": "/build/.cargo",
    "CARGO_PROFILE_RELEASE_LTO": "thin",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "1",
    "CARGO_PROFILE_RELEASE_PANIC": "abort",
    "CARGO_PROFILE_RELEASE_INCREMENTAL": "false",
    "CARGO_PROFILE_RELEASE_OPT_LEVEL": "3",
    "CARGO_TERM_COLOR": "never",
}
NETHERMIND_DOTNET_ENV = {
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_CLI_HOME": "/tmp/dotnet",
    "NUGET_PACKAGES": "/tmp/nuget",
}


def _after_init(body: str) -> str:
    """*body* with ``runtime-init.service`` first in ``After=``/``Requires=``."""
    unit, blank, rest = body.partition("\n\n")
    lines = unit.split("\n")
    for key in ("After=", "Requires="):
        index = next((i for i, line in enumerate(lines) if line.startswith(key)), None)
        if index is None:
            lines.append(f"{key}runtime-init.service")
        else:
            lines[index] = f"{key}runtime-init.service {lines[index].removeprefix(key)}"
    return "\n".join(lines) + blank + rest


RUNTIME_PACKAGES = (
    "prometheus", "prometheus-node-exporter", "prometheus-process-exporter", "rclone",
    "libsnappy1v5", "openntpd", "bubblewrap", "dropbear", "iptables", "iproute2", "socat",
    "conntrack", "netfilter-persistent", "curl", "jq", "ncat", "logrotate", "sudo", "uidmap",
    "passt", "fuse-overlayfs", "cryptsetup", "openssh-sftp-server", "udev", "pkg-config",
    "libtss2-dev",
)  # fmt: skip
BUILD_PACKAGES = (
    "dotnet-sdk-10.0", "dotnet-runtime-10.0", "golang", "libleveldb-dev", "libsnappy-dev",
    "zlib1g-dev", "libzstd-dev", "libpq-dev", "libssl-dev", "libtss2-dev", "build-essential",
    "pkg-config", "git", "gcc",
)  # fmt: skip


def _base() -> Image:
    img = Image(
        base="debian/trixie",
        reproducible=True,
        kernel=Kernel.tdx_kernel(
            "6.13.12",
            cmdline=(
                "console=tty0 console=ttyS0,115200n8 mitigations=auto,nosmt "
                "spec_store_bypass_disable=on nospectre_v2"
            ),
            config_file=str(ROOT / "kernel" / "kernel-yocto.config"),
            source_repo="https://github.com/gregkh/linux",
        ),
        mkosi=MkosiOptions(
            init_script=Image.DEFAULT_TDX_INIT,
            seed="630b5f72-a36a-4e83-b23d-6ef47c82fd9c",
            output_directory="build",
            package_cache_directory="mkosi.cache",
            environment_passthrough=("KERNEL_IMAGE", "KERNEL_VERSION"),
        ),
    )
    img.efi_stub(snapshot_url=PINNED_MIRROR, package_version="255.4-1")
    img.backports()
    img.install(
        "kmod", "systemd", "systemd-boot-efi", "busybox", "util-linux", "procps",
        "ca-certificates", "openssl", "iproute2", "udhcpc", "e2fsprogs",
    )  # fmt: skip
    img.build_packages(
        "build-essential", "git", "curl", "cmake", "pkg-config", "clang", "cargo/sid", "flex",
        "bison", "elfutils", "bc", "perl", "gawk", "zstd", "libssl-dev", "libelf-dev",
    )  # fmt: skip
    img.skeleton("/etc/resolv.conf", content=C.RESOLV_CONF)
    img.skeleton("/etc/systemd/system/network-setup.service", content=C.NETWORK_SETUP_SERVICE)
    img.debloat(enabled=True)
    Tdxs().apply(img)
    return img.pin_mirror(PINNED_MIRROR)


def _service(img: Image, name: str, unit: str, account: str) -> None:
    img.file(f"/usr/lib/systemd/system/{name}.service", content=unit)
    img.shell(account, phase="postinst")
    img.enable(name)


def _prover_stack(img: Image) -> None:
    img.shell("mkosi-chroot groupadd -r eth", phase="postinst")
    img.build_packages("build-essential", "pkg-config", "git", "clang", "libssl-dev", "libelf-dev")
    img.build_from(
        SourceBuild(
            name="raiko",
            source=GitSource("https://github.com/NethermindEth/raiko.git", "feat/tdx"),
            build=CargoBuild(
                output="raiko",
                package="raiko-host",
                features=("tdx",),
                env=RAIKO_CARGO_ENV,
                packages=(),
            ),
            install=(Install.artifact("/usr/bin/raiko"),),
            cache_key="raiko-feat/tdx",
            mark_unpinned=False,
        )
    )
    _service(
        img,
        "raiko",
        _after_init(C.RAIKO_UNIT),
        "mkosi-chroot useradd --system --home-dir /home/raiko --shell /usr/sbin/nologin "
        "--gid tdx raiko",
    )
    img.build_packages("golang", "git", "build-essential")
    img.build_from(
        SourceBuild(
            name="taiko-client",
            source=GitSource(
                "https://github.com/NethermindEth/surge-taiko-mono",
                "feat/tdx-proving",
                subdir="packages/taiko-client",
            ),
            build=GoBuild(
                output="taiko-client",
                package="cmd/main.go",
                output_dir="bin",
                mkdir=False,
                env={
                    "GO111MODULE": "on",
                    "CGO_CFLAGS": "-O -D__BLST_PORTABLE__",
                    "CGO_CFLAGS_ALLOW": "-O -D__BLST_PORTABLE__",
                },
                packages=(),
            ),
            install=(Install.artifact("/usr/bin/taiko-client"),),
            cache_key="taiko-client-feat/tdx-proving",
            mark_unpinned=False,
        )
    )
    _service(
        img,
        "taiko-client",
        _after_init(C.TAIKO_CLIENT_UNIT),
        "mkosi-chroot useradd --system --home-dir /home/taiko-client --shell /usr/sbin/nologin "
        "--groups eth taiko-client",
    )
    img.build_packages("dotnet-sdk-10.0", "dotnet-runtime-10.0", "build-essential", "git")
    img.build_from(
        SourceBuild(
            name="nethermind",
            source=GitSource("https://github.com/NethermindEth/nethermind.git", "1.32.3"),
            build=DotnetBuild(
                project="src/Nethermind/Nethermind.Runner",
                output="nethermind",
                runtime="linux-x64",
                restore_args=("--disable-parallel", "--force"),
                properties={
                    "PublishSingleFile": "true",
                    "BuildTimestamp": "0",
                    "Commit": "0" * 40,
                    "PublishReadyToRun": "false",
                    "DebugType": "none",
                    "IncludeAllContentForSelfExtract": "true",
                    "IncludePackageReferencesDuringMarkupCompilation": "true",
                    "EmbedUntrackedSources": "true",
                    "PublishRepositoryUrl": "true",
                },
                env=NETHERMIND_DOTNET_ENV,
                packages=(),
            ),
            install=(
                Install.artifact("/usr/bin/nethermind"),
                Install.file(
                    "publish/NLog.config", "/etc/nethermind-surge/NLog.config", mode="0644"
                ),
                Install.tree("publish/plugins", "/etc/nethermind-surge/plugins"),
            ),
            cache_key="nethermind-1.32.3-linux-x64",
            mark_unpinned=False,
        )
    )
    _service(
        img,
        "nethermind-surge",
        _after_init(C.NETHERMIND_UNIT),
        "mkosi-chroot useradd --system --home-dir /home/nethermind-surge "
        "--shell /usr/sbin/nologin --groups eth nethermind-surge",
    )
    img.shell("mkosi-chroot usermod -a -G tdx nethermind-surge", phase="postinst")


def build() -> Image:
    """The fluent surge-tdx-prover image: default plus azure, gcp and devtools."""
    img = _base()
    img.install(*RUNTIME_PACKAGES).build_packages(*BUILD_PACKAGES)
    key = KeySpec("key_persistent", strategy="tpm", output="/tmp/key_persistent")
    disk = DiskSpec(
        "disk_persistent", device=None, key=key, mapper_name="cryptroot", mount_at="/persistent"
    )
    img.apply(
        KeyGeneration(keys=(key,)),
        DiskEncryption(disks=(disk,)),
        SecretDelivery(method="http_post", store_at=disk),
    )
    _prover_stack(img)
    for path, name in (
        ("/etc/default/dropbear", "DROPBEAR_CONFIG"),
        ("/etc/sysctl.d/99-surge.conf", "SYSCTL_CONF"),
        ("/etc/udev/rules.d/65-tdx-guest.rules", "TDX_GUEST_PERMISSIONS"),
        ("/etc/udev/rules.d/99-tdx-symlink.rules", "TDX_GUEST_SYMLINK"),
        ("/etc/openntpd/ntpd.conf", "OPENNTPD_CONF"),
        ("/etc/default/prometheus", "PROMETHEUS_DEFAULTS"),
        ("/etc/nethermind-surge/env", "NETHERMIND_ENV"),
        ("/etc/raiko/env", "RAIKO_ENV"),
        ("/etc/taiko-client/env", "TAIKO_CLIENT_ENV"),
    ):
        img.file(path, content=getattr(C, name))
    img.enable("network-setup", "openntpd", "logrotate", "dropbear")
    img.disable("ssh.service", "ssh.socket").mask("ssh.service", "ssh.socket")
    for name, module in (
        ("azure", AzurePlatform()),
        ("gcp", GcpPlatform()),
        ("devtools", DevTools()),
    ):
        img.profile(name)
        with img.profiles(name):
            img.apply(module)
    return img


def blank_image(**options: object) -> Image:
    """An empty fluent image: the parity tests' oracle for what lowering must reproduce."""
    return Image(**options)  # type: ignore[arg-type]
