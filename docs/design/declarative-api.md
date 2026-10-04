# Declarative API design (external review, 2026-10-04)

Blank-slate redesign proposal produced by an external model review of the fluent `Image` API. It is the reference for the declarative frontend being built in `tundravm.declarative`; signatures here are the target, the compiler and the surge golden tree are the constraints.


This is a design, not an implementation. I inspected the repository without modifying files or running tests.

**1. Design thesis — five sentences**

A recipe should be an immutable Python value describing the desired image, with no active profile, backend session, or previous bake hidden inside it. Reusable modules should be ordinary functions returning named fragments of declarations, so composition uses Python arguments and values rather than subclass lifecycle hooks. Variants should be explicit overlays with one parent, deliberate replacement and removal, and a target that supplies the corresponding platform integration. References between declarations should express dependencies directly, while the compiler resolves boot ordering and systemd dependencies after composition. Compilation, locking, baking, measurement, and deployment should consume explicit inputs and return explicit results, making reproducibility and testing properties of the data flow.

There are **seven core concepts**: recipe, declaration, fragment, variant, lock, compiled tree, and artifact. Packages, disks, and units are declaration types, not separate programming models.

**2. Complete proposed public surface**

The following is the proposed interface, not existing imports. Records are frozen dataclasses; constructor fields are also readable attributes. Collections use tuples throughout, and there is no `Any`, dynamic forwarding, registration decorator, or public mutable state.

Root and composition:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type Target = Literal["qemu", "azure", "gcp"]
type Phase = Literal[
    "sync", "skeleton", "prepare", "build", "extra",
    "postinst", "finalize", "postoutput", "clean", "repart", "boot",
]
type Pairs = tuple[tuple[str, str], ...]
type Check = Callable[[Resolved], tuple[Diagnostic, ...]]

@dataclass(frozen=True)
class Fragment:
    name: str
    items: tuple[Declaration | Fragment, ...] = ()
    requires: tuple[str, ...] = ()
    checks: tuple[Check, ...] = ()

@dataclass(frozen=True)
class Variant:
    name: str
    parent: str | None = "base"
    add: Fragment = Fragment("empty")
    replace: tuple[Declaration, ...] = ()
    remove: tuple[Declaration, ...] = ()
    target: Target | None = None

@dataclass(frozen=True)
class Mkosi:
    layout: Literal["directories", "native"] = "directories"
    dialect: Literal["current", "nethermind-v1"] = "current"

@dataclass(frozen=True)
class Recipe:
    name: str
    common: Fragment
    variants: tuple[Variant, ...] = (Variant("default", target="qemu"),)
    base: str = "debian/trixie"
    arch: Literal["x86_64", "aarch64"] = "x86_64"
    mirror: str | None = None
    tools_mirror: str | None = None
    epoch: int | None = 0
    mkosi: Mkosi = Mkosi()
```

`base` is the reserved parent representing `common`; it is not itself an emitted variant. A parentless variant receives no common declarations. Distribution, architecture, and compiler configuration remain recipe-wide.

Declarations:

```python
@dataclass(frozen=True)
class Package:
    name: str
    role: Literal["runtime", "build"] = "runtime"

@dataclass(frozen=True)
class File:
    path: str
    content: str | bytes | Path
    mode: int = 0o644
    stage: Literal["skeleton", "extra"] = "extra"

@dataclass(frozen=True)
class Directory:
    path: str
    source: Path
    exclude: tuple[str, ...] = ()
    mode: int | None = None
    stage: Literal["skeleton", "extra"] = "extra"

@dataclass(frozen=True)
class Group:
    name: str
    system: bool = True
    gid: int | None = None

@dataclass(frozen=True)
class User:
    name: str
    system: bool = True
    home: str | None = None
    shell: str = "/usr/sbin/nologin"
    uid: int | None = None
    primary_group: str | int | None = None
    groups: tuple[str, ...] = ()

@dataclass(frozen=True)
class Unit:
    name: str
    content: str | Path | None = None
    enabled: bool | None = None
    masked: bool | None = None
    after_init: bool = False

@dataclass(frozen=True)
class Hook:
    name: str
    phase: Phase
    script: str
    env: Pairs = ()
    cwd: str | None = None
    after: tuple[str, ...] = ()

@dataclass(frozen=True)
class Init:
    name: str
    script: str
    priority: int = 100
    after: tuple[str, ...] = ()

@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    suite: str
    components: tuple[str, ...] = ("main",)
    keyring: str | None = None
    priority: int = 100

@dataclass(frozen=True)
class Partition:
    name: str
    size: str
    mount: str
    filesystem: str = "ext4"

@dataclass(frozen=True)
class Debloat:
    enabled: bool = True
    remove: tuple[str, ...] | None = None
    extra_remove: tuple[str, ...] = ()
    keep_paths: tuple[str, ...] = ()
    minimize_systemd: bool = True
    keep_units: tuple[str, ...] | None = None
    keep_binaries: tuple[str, ...] | None = None

@dataclass(frozen=True)
class Setting:
    section: str
    key: str
    values: tuple[str, ...]
```

`Unit` deliberately accepts systemd text: services, sockets, timers, repeated directives, and exact formatting need no parallel SDK vocabulary. `content=None` controls a packaged unit. `enabled=False, masked=True` means disable **and** mask.

`File(..., Path(...))` captures host bytes during resolution; a string means literal content. Templates use ordinary Python formatting. `Setting` is the explicit mkosi escape hatch, including environment passthrough, build-source mounts, cache directories, and conversion settings.

Keys, disks, and secrets:

```python
@dataclass(frozen=True)
class Key:
    name: str
    output: str | None = None
    strategy: Literal["random", "pipe"] = "random"
    persist_in_tpm: bool = True
    size: int = 64
    pipe: str | None = None

@dataclass(frozen=True)
class Disk:
    name: str
    mount: str
    device: str | None = None
    key: Key | Path | None = None
    mapper: str | None = None
    format: Literal["always", "on_initialize", "on_fail", "never"] = "on_fail"
    directories: tuple[str, ...] = ("ssh", "data", "logs")

@dataclass(frozen=True)
class Schema:
    kind: Literal["string", "json"] = "string"
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: tuple[str, ...] = ()

@dataclass(frozen=True)
class SecretFile:
    path: str
    mode: int = 0o400
    owner: str | None = None

@dataclass(frozen=True)
class SecretEnv:
    name: str
    service: str | None = None  # None means global environment.

@dataclass(frozen=True)
class Secret:
    name: str
    targets: tuple[SecretFile | SecretEnv, ...]
    required: bool = True
    schema: Schema | None = None

@dataclass(frozen=True)
class Secrets:
    name: str = "secrets"
    entries: tuple[Secret, ...] = ()
    store: Disk | None = None
    host: str = "0.0.0.0"
    port: int = 8080
    ssh_directory: str = "/root/.ssh"
    ssh_key_path: str | None = "/etc/root_key"

@dataclass(frozen=True)
class RuntimeTools:
    source: Git
    key_config: str = "/etc/tdx/key-gen.yaml"
    disk_config: str = "/etc/tdx/disk-setup.yaml"
    secret_config: str = "/etc/tdx/secrets.yaml"
    secret_manifest: str = "/etc/tdx/secrets.json"
```

References do not silently declare their referents. A disk referencing a missing key is an error. `device=None` selects the largest eligible unpartitioned disk; `key=None` requests a plain disk. A key without an output file uses the runtime tool’s environment handoff, subject to its aggregate-key limitation.

The compiler lowers these declarations into tools, configuration, and init fragments named `keys`, `disks`, and `secrets`, at priorities 10, 20, and 30. `Secrets` currently means HTTP POST delivery; there is no one-value `method` parameter.

Source builds:

```python
@dataclass(frozen=True)
class Git:
    url: str
    ref: str
    subdir: str | None = None
    submodules: bool = False

@dataclass(frozen=True)
class Http:
    url: str
    sha256: str | None = None

@dataclass(frozen=True)
class Install:
    source: str
    destination: str
    mode: int | None = 0o755
    directory: bool = False

@dataclass(frozen=True)
class Build:
    name: str
    source: Git | Http
    script: str
    install: tuple[Install, ...]
    packages: tuple[str, ...] = ()
    env: Pairs = ()

@dataclass(frozen=True)
class Kernel:
    version: str
    source: Git | Http
    config: Path | None = None
    cmdline: str = ""
    tdx: bool = True

type Declaration = (
    Package | File | Directory | Group | User | Unit | Hook | Init
    | Repository | Partition | Debloat | Setting | Key | Disk
    | Secrets | RuntimeTools | Build | Kernel
)
```

One `Build` replaces separate language-builder APIs. Its script runs inside mkosi-chroot with the source/subdirectory as its working directory; installation paths are relative to that directory. Fetching, integrity verification, caching, and installation remain compiler responsibilities.

Shipped composition functions:

```python
def backports(
    *, mirror: str | None = None, release: str | None = None,
) -> Fragment: ...

def efi_stub(*, snapshot: str, version: str) -> Fragment: ...

def tdxs(
    *,
    source: Git = Git("https://github.com/Hyodar/tundra-tools.git", "master"),
    issuer: Literal["tdx", "azure", "gcp", "simulator"] | None = "tdx",
    validator: Literal["tdx", "azure", "gcp", "simulator"] | None = None,
    expected_measurements: Pairs = (),
    check_revocations: bool = False,
    get_collateral: bool = False,
    verify_imds: bool = False,
    verify_identity_token: bool = False,
    after_init: bool = False,
) -> Fragment: ...

def devtools(*, root_password: str = "tdx") -> Fragment: ...
```

These return ordinary declarations. Azure/GCP integration belongs to `Variant.target`; selecting Azure automatically adds provisioning and VHD conversion, while GCP adds metadata DNS, disk naming, and tar conversion.

Lifecycle and result types:

```python
@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    level: Literal["error", "warning", "info"] = "error"
    variant: str = ""
    subject: str = ""

@dataclass(frozen=True)
class Resolved:
    variant: str
    target: Target
    items: tuple[Declaration, ...]
    fragments: tuple[str, ...]

@dataclass(frozen=True)
class Pin:
    identity: str
    source: Git | Http
    digest: str

@dataclass(frozen=True)
class Lock:
    recipe_digest: str
    sections: Pairs
    pins: tuple[Pin, ...]
    compiler_version: str

@dataclass(frozen=True)
class Entry:
    path: str
    content: bytes | None
    mode: int
    symlink: str | None = None

@dataclass(frozen=True)
class Tree:
    entries: tuple[Entry, ...]
    digest: str
    def write(self, path: Path) -> None: ...

@dataclass(frozen=True)
class Backend:
    kind: Literal["lima", "nix", "local"]
    cpus: int = 2
    memory: str = "4GiB"
    disk: str = "40GiB"

@dataclass(frozen=True)
class Artifact:
    path: Path
    variant: str
    target: Target
    sha256: str
    recipe_digest: str
    lock_digest: str
    tree_digest: str
    simulated: bool = False

@dataclass(frozen=True)
class Measurements:
    scheme: Literal["rtmr", "azure", "gcp"]
    values: Pairs
    tool: str
    artifact_digest: str

@dataclass(frozen=True)
class Qemu:
    memory: str = "2G"
    cpus: int = 2
    ssh_port: int = 2222
    tdx: bool = False
    daemonize: bool = True

@dataclass(frozen=True)
class Azure:
    storage_account: str
    resource_group: str = "tdx-vms"
    location: str = "eastus"
    vm_size: str = "Standard_DC2s_v3"

@dataclass(frozen=True)
class Gcp:
    project: str
    bucket: str
    zone: str = "us-central1-a"
    machine_type: str = "n2d-standard-2"

@dataclass(frozen=True)
class Deployment:
    id: str
    target: Target
    endpoint: str | None
    metadata: Pairs

class Error(RuntimeError):
    code: str
    def __init__(self, code: str, message: str) -> None: ...

def load(path: Path, *, attribute: str = "recipe") -> Recipe: ...
def resolve(recipe: Recipe, *, variant: str) -> Resolved: ...
def lint(
    recipe: Recipe, *, variants: tuple[str, ...] | None = None,
    lock: Lock | None = None,
) -> tuple[Diagnostic, ...]: ...
def compile(
    recipe: Recipe, *, variants: tuple[str, ...] | None = None,
    lock: Lock | None = None,
) -> Tree: ...
def diff(tree: Tree, against: Tree | Path) -> str: ...
def lock(
    recipe: Recipe, *, previous: Lock | None = None,
    update: tuple[str, ...] = (), offline: bool = False,
) -> Lock: ...
def lock_status(recipe: Recipe, locked: Lock) -> tuple[Diagnostic, ...]: ...
def read_lock(path: Path) -> Lock: ...
def write_lock(locked: Lock, path: Path) -> None: ...
def bake(
    recipe: Recipe, *, locked: Lock, backend: Backend, out: Path,
    variants: tuple[str, ...] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[Artifact, ...]: ...
def read_artifacts(manifest: Path) -> tuple[Artifact, ...]: ...
def measure(
    artifact: Artifact, *, scheme: Literal["rtmr", "azure", "gcp"] = "rtmr",
) -> Measurements: ...
def deploy(artifact: Artifact, *, using: Qemu | Azure | Gcp) -> Deployment: ...
def doctor(backend: Backend) -> tuple[Diagnostic, ...]: ...
```

`compile()` returns an in-memory tree; writing is explicit. `bake()` always validates the lock and saves an artifact manifest. Measurement consumes an artifact and fails when a real measurement implementation is unavailable; synthetic hashes leave the production measurement API.

Testing helpers:

```python
# tundravm.testing
def assert_clean(diagnostics: tuple[Diagnostic, ...], *, strict: bool = True) -> None: ...
def assert_diagnostic(
    diagnostics: tuple[Diagnostic, ...], code: str, *, subject: str | None = None,
) -> None: ...
def assert_tree(tree: Tree, golden: Path) -> None: ...
def fake_bake(tree: Tree, *, variant: str, target: Target, out: Path) -> Artifact: ...
```

`fake_bake()` marks artifacts simulated. Production measurement and deployment reject them.

CLI grammar:

```text
tundravm init DIRECTORY --name NAME
tundravm inspect RECIPE [--variant NAME ...] [--json]
tundravm lint RECIPE [--variant NAME ...] [--strict]
tundravm compile RECIPE --out DIRECTORY [--lockfile FILE] [--check]
tundravm diff RECIPE --against DIRECTORY [--lockfile FILE]
tundravm lock RECIPE --path FILE [--update SOURCE ...] [--offline | --check]
tundravm bake RECIPE --lockfile FILE --backend lima|nix|local --out DIRECTORY
tundravm measure MANIFEST --variant NAME --scheme rtmr|azure|gcp
tundravm deploy MANIFEST --variant NAME --config FILE
tundravm doctor --backend lima|nix|local
```

All recipe operations accept repeatable `--variant`; omission selects all declared variants. Deployment configuration parses into `Qemu`, `Azure`, or `Gcp`. `new` merges into `init`; `digest` becomes an inspection field; `ci` becomes the three explicit lint/tree/lock checks.

**3. Complete surge recipe in the proposed API**

Only file-content constants are omitted. The proposed `contents.py` carries the existing configuration strings, exact systemd unit bodies, and base init script; it contains no recipe-building logic. The application modules are expanded here so their source builds and account declarations remain visible.

```python
from pathlib import Path

from tundravm import (
    Backend, Build, Debloat, Disk, File, Fragment, Git, Group,
    Install, Kernel, Key, Mkosi, Package, Recipe, RuntimeTools,
    Secrets, Setting, Unit, User, Variant,
)
from tundravm.modules import backports, devtools, efi_stub, tdxs
from contents import (
    DROPBEAR_CONFIG, NETHERMIND_ENV, NETHERMIND_UNIT,
    NETWORK_SETUP_SERVICE, OPENNTPD_CONF, PROMETHEUS_DEFAULTS,
    RAIKO_ENV, RAIKO_UNIT, SYSCTL_CONF,
    TAIKO_CLIENT_ENV, TAIKO_CLIENT_UNIT,
    TDX_GUEST_PERMISSIONS, TDX_GUEST_SYMLINK, TDX_INIT,
)

ROOT = Path(__file__).resolve().parents[2]
MIRROR = "https://snapshot.debian.org/archive/debian/20251113T083151Z/"
TOOLS = Git("https://github.com/Hyodar/tundra-tools.git", "master")

backend = Backend("lima", cpus=6, memory="12GiB", disk="100GiB")

key = Key("key_persistent", output="/tmp/key_persistent")
disk = Disk(
    "disk_persistent",
    mount="/persistent",
    device=None,
    key=key,
    mapper="cryptroot",
    format="on_fail",
    directories=("ssh", "data", "logs"),
)

recipe = Recipe(
    name="surge-tdx-prover",
    base="debian/trixie",
    mirror=MIRROR,
    tools_mirror=MIRROR,
    epoch=0,
    mkosi=Mkosi(dialect="nethermind-v1"),
    common=Fragment(
        "surge",
        items=(
            Kernel(
                "6.13.12",
                Git("https://github.com/gregkh/linux", "v6.13.12"),
                config=ROOT / "kernel/kernel-yocto.config",
                cmdline=(
                    "console=tty0 console=ttyS0,115200n8 "
                    "mitigations=auto,nosmt "
                    "spec_store_bypass_disable=on nospectre_v2"
                ),
            ),
            Setting("Output", "Seed",
                    ("630b5f72-a36a-4e83-b23d-6ef47c82fd9c",)),
            Setting("Output", "OutputDirectory", ("build",)),
            Setting("Build", "PackageCacheDirectory", ("mkosi.cache",)),
            Setting("Build", "Environment", ("KERNEL_IMAGE", "KERNEL_VERSION")),
            File("/init", TDX_INIT, mode=0o755, stage="skeleton"),
            efi_stub(snapshot=MIRROR, version="255.4-1"),
            backports(),

            *(Package(p) for p in (
                "kmod", "systemd", "systemd-boot-efi", "busybox",
                "util-linux", "procps", "ca-certificates", "openssl",
                "iproute2", "udhcpc", "e2fsprogs",
                "prometheus", "prometheus-node-exporter",
                "prometheus-process-exporter", "rclone", "libsnappy1v5",
                "openntpd", "bubblewrap", "dropbear", "iptables",
                "socat", "conntrack", "netfilter-persistent", "curl",
                "jq", "ncat", "logrotate", "sudo", "uidmap", "passt",
                "fuse-overlayfs", "cryptsetup", "openssh-sftp-server",
                "udev", "pkg-config", "libtss2-dev",
            )),
            *(Package(p, role="build") for p in (
                "build-essential", "git", "curl", "cmake", "pkg-config",
                "clang", "cargo/sid", "flex", "bison", "elfutils", "bc",
                "perl", "gawk", "zstd", "libssl-dev", "libelf-dev",
                "dotnet-sdk-10.0", "dotnet-runtime-10.0", "golang",
                "libleveldb-dev", "libsnappy-dev", "zlib1g-dev",
                "libzstd-dev", "libpq-dev", "libtss2-dev", "gcc",
            )),
            File(
                "/etc/resolv.conf",
                "nameserver 8.8.8.8\nnameserver 8.8.4.4",
                stage="skeleton",
            ),
            File(
                "/etc/systemd/system/network-setup.service",
                NETWORK_SETUP_SERVICE,
                stage="skeleton",
            ),
            Debloat(),
            tdxs(source=TOOLS, after_init=False),
            RuntimeTools(TOOLS),
            key,
            disk,
            Secrets(store=disk),

            Group("eth"),
            Build(
                "raiko",
                Git("https://github.com/NethermindEth/raiko.git", "feat/tdx"),
                script=(
                    "cargo fetch && cargo build --release --frozen "
                    "--features tdx --package raiko-host"
                ),
                install=(Install("target/release/raiko-host", "/usr/bin/raiko"),),
                env=(
                    ("RUSTFLAGS",
                     "-C target-cpu=generic -C link-arg=-Wl,--build-id=none "
                     "-C symbol-mangling-version=v0 -L /usr/lib/x86_64-linux-gnu"),
                    ("CARGO_HOME", "/build/.cargo"),
                    ("CARGO_PROFILE_RELEASE_LTO", "thin"),
                    ("CARGO_PROFILE_RELEASE_CODEGEN_UNITS", "1"),
                    ("CARGO_PROFILE_RELEASE_PANIC", "abort"),
                    ("CARGO_PROFILE_RELEASE_INCREMENTAL", "false"),
                    ("CARGO_PROFILE_RELEASE_OPT_LEVEL", "3"),
                    ("CARGO_TERM_COLOR", "never"),
                ),
            ),
            User("raiko", home="/home/raiko", primary_group="tdx"),
            Unit("raiko.service", RAIKO_UNIT, enabled=True, after_init=True),

            Build(
                "taiko-client",
                Git(
                    "https://github.com/NethermindEth/surge-taiko-mono",
                    "feat/tdx-proving",
                    subdir="packages/taiko-client",
                ),
                script=(
                    'go build -trimpath -ldflags "-s -w -buildid=" '
                    "-o bin/taiko-client cmd/main.go"
                ),
                install=(Install("bin/taiko-client", "/usr/bin/taiko-client"),),
                env=(
                    ("GO111MODULE", "on"),
                    ("CGO_CFLAGS", "-O -D__BLST_PORTABLE__"),
                    ("CGO_CFLAGS_ALLOW", "-O -D__BLST_PORTABLE__"),
                ),
            ),
            User("taiko-client", home="/home/taiko-client", groups=("eth",)),
            Unit(
                "taiko-client.service", TAIKO_CLIENT_UNIT,
                enabled=True, after_init=True,
            ),

            Build(
                "nethermind",
                Git("https://github.com/NethermindEth/nethermind.git", "1.32.3"),
                script=(
                    "dotnet restore src/Nethermind/Nethermind.Runner "
                    "--runtime linux-x64 --disable-parallel --force && "
                    "dotnet publish src/Nethermind/Nethermind.Runner "
                    "--configuration Release --runtime linux-x64 "
                    "--self-contained true --output \"$PWD/publish\" "
                    "-p:Deterministic=true -p:ContinuousIntegrationBuild=true "
                    "-p:PublishSingleFile=true -p:BuildTimestamp=0 "
                    "-p:Commit=0000000000000000000000000000000000000000 "
                    "-p:PublishReadyToRun=false -p:DebugType=none "
                    "-p:IncludeAllContentForSelfExtract=true "
                    "-p:IncludePackageReferencesDuringMarkupCompilation=true "
                    "-p:EmbedUntrackedSources=true -p:PublishRepositoryUrl=true"
                ),
                install=(
                    Install("publish/nethermind", "/usr/bin/nethermind"),
                    Install(
                        "publish/NLog.config",
                        "/etc/nethermind-surge/NLog.config",
                        mode=0o644,
                    ),
                    Install(
                        "publish/plugins", "/etc/nethermind-surge/plugins",
                        mode=None, directory=True,
                    ),
                ),
                env=(
                    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
                    ("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1"),
                    ("DOTNET_NOLOGO", "1"),
                    ("DOTNET_CLI_HOME", "/tmp/dotnet"),
                    ("NUGET_PACKAGES", "/tmp/nuget"),
                ),
            ),
            User(
                "nethermind-surge",
                home="/home/nethermind-surge",
                groups=("eth", "tdx"),
            ),
            Unit(
                "nethermind-surge.service", NETHERMIND_UNIT,
                enabled=True, after_init=True,
            ),

            File("/etc/default/dropbear", DROPBEAR_CONFIG),
            File("/etc/sysctl.d/99-surge.conf", SYSCTL_CONF),
            File("/etc/udev/rules.d/65-tdx-guest.rules", TDX_GUEST_PERMISSIONS),
            File("/etc/udev/rules.d/99-tdx-symlink.rules", TDX_GUEST_SYMLINK),
            File("/etc/openntpd/ntpd.conf", OPENNTPD_CONF),
            File("/etc/default/prometheus", PROMETHEUS_DEFAULTS),
            File("/etc/nethermind-surge/env", NETHERMIND_ENV),
            File("/etc/raiko/env", RAIKO_ENV),
            File("/etc/taiko-client/env", TAIKO_CLIENT_ENV),
            Unit("network-setup.service", enabled=True),
            Unit("openntpd.service", enabled=True),
            Unit("logrotate.service", enabled=True),
            Unit("dropbear.service", enabled=True),
            Unit("ssh.service", enabled=False, masked=True),
            Unit("ssh.socket", enabled=False, masked=True),
        ),
    ),
    variants=(
        Variant("default", target="qemu"),
        Variant("azure", parent="default", target="azure"),
        Variant("gcp", parent="default", target="gcp"),
        Variant("devtools", parent="default", add=devtools()),
    ),
)
```

This preserves the existing Raiko/Taiko environment files without inventing new service wiring for them. It also preserves TDXS’s current pre-runtime-init ordering explicitly, instead of deriving it from module application time.

**4. A custom module**

A module is a typed function. Here the storage fragment is composed directly, its presence is declared as a dependency, two boot steps have explicit ordering, and its check sees the final resolved variant.

```python
from tundravm import (
    Diagnostic, Disk, Fragment, Init, Package, Resolved, Unit,
)

def exporter_check(image: Resolved) -> tuple[Diagnostic, ...]:
    if any(isinstance(item, Disk) and item.mount == "/persistent"
           for item in image.items):
        return ()
    return (Diagnostic(
        "exporter-storage-missing",
        "The exporter requires a disk mounted at /persistent.",
        variant=image.variant,
        subject="prometheus-node-exporter",
    ),)

def prometheus_exporter(storage: Fragment) -> Fragment:
    return Fragment(
        "prometheus-exporter",
        requires=(storage.name,),
        checks=(exporter_check,),
        items=(
            storage,
            Package("prometheus-node-exporter"),
            Init(
                "exporter-directory",
                "install -d -m 0755 /persistent/exporter\n",
                priority=25,
                after=("disks",),
            ),
            Init(
                "exporter-ready",
                "printf 'tundra_boot_ready 1\\n' "
                "> /persistent/exporter/boot.prom\n",
                priority=40,
                after=("exporter-directory", "secrets"),
            ),
            Unit(
                "prometheus-node-exporter.service",
                content=(
                    "[Unit]\nDescription=Node exporter\n\n"
                    "[Service]\n"
                    "ExecStart=/usr/bin/prometheus-node-exporter "
                    "--collector.textfile.directory=/persistent/exporter\n"
                    "User=prometheus\nRestart=on-failure\n\n"
                    "[Install]\nWantedBy=minimal.target\n"
                ),
                enabled=True,
                after_init=True,
            ),
        ),
    )

storage = Fragment("secure-storage", items=(key, disk, Secrets(store=disk)))
monitoring = prometheus_exporter(storage)
```

Repeated identical named fragments deduplicate; conflicting definitions fail. Dependencies never import or instantiate modules automatically.

**5. Compiler, inheritance, and lock semantics**

Resolution expands fragments, resolves variant ancestry, applies explicit changes, then validates references. Declaration identity is type plus natural key: package name/role, file stage/path, unit name, build name, or setting section/key. `replace` requires an existing identity; `remove` matches identity, not object address. Accidental collisions fail rather than silently implementing “last call wins.”

Parents resolve before children. Targets inherit unless replaced. Ordinary Python comprehensions generate matrices; there is no matrix language or multiple inheritance.

Init fragments sort by priority, with dependency ordering inside equal-priority groups. A dependency contradicting priorities is an error. Hook order remains stable within each phase, subject to explicit dependencies. `after_init=True` adds both systemd `After=` and `Requires=` after all declarations are known.

Locking retains matching previous pins unless explicitly updated. Source identity includes URL, requested ref, subdirectory, and submodule policy; variant-local build names cannot overwrite each other’s pins. Frozen bakes fetch pinned commits or verify HTTP digests, and cache keys include source pins, build commands, environment, installation maps, architecture, and toolchain identity.

The lock covers kernel configuration bytes, captured local files, mkosi settings, compiler dialect/version, mirrors, epoch, and effective variants. Arbitrary shell downloads remain outside automatic dependency discovery and must become declared inputs for a reproducibility claim.

The golden contract needs two distinct checks:

- **Historical emission:** `nethermind-v1`, explicit variant selection, no lock, exact existing tree bytes.
- **Pinned emission:** a committed lock, exact pinned tree bytes, and fetches that use those pins.

A pinned fetch cannot simultaneously have the bytes of the historical branch-clone command. Compatibility belongs in versioned compiler lowering: script ordering, cache labels, account-command spelling, whitespace, modes, symlinks, and cloud conversion templates. For example, it can lower the Nethermind group membership into the historical `useradd` followed by `usermod`.

The inspected [upstream comparison test](../../tests/integration/test_nethermind_tdx_golden.py) currently permits semantic differences; it does not establish whole-tree equality. The proposed `assert_tree` must compare every relative path, byte sequence, mode, and symlink target. The committed tree I inspected contains only `default`, so cloud variants need their own exact fixtures.

**6. Migration and three risks**

| Today | Proposed mapping |
|---|---|
| `install`, `build_install` | `Package` with runtime/build role |
| `file`, `skeleton`, `directory` | `File`/`Directory` with stage |
| `user`, `group`, packaged unit controls | `User`, `Group`, `Unit` |
| `run`, `hook`, `add_init_script` | Named `Hook` and `Init` |
| `SourceBuild` plus language builder | `Build` with explicit script/install map |
| Mutable key/disk/secret modules | Immutable referenced declarations |
| `Module.setup/install/check` | Function returning `Fragment` |
| Profile contexts and forwarding | `Variant` overlays |
| Backend and last-bake state on `Image` | Explicit operation arguments/artifacts |
| Platform modules | Variant target expansion |

Migrate through one shared compiler IR: first freeze output fixtures, then add the declarative frontend, then port modules and recipes, and finally remove the mutable frontend. Lifecycle aliases and active-profile contexts disappear; explicit replacement, dependency validation, and complete compiler-input hashing are new.

The three biggest risks are:

1. **Byte compatibility:** semantically equivalent account commands, source scripts, or unit formatting still violate the golden contract. Keep compatibility lowering isolated and fixture-tested.
2. **Shell opacity:** a smaller builder API makes commands visible but cannot infer their network inputs or hermeticity. Declared sources and captured inputs define the lock’s actual boundary.
3. **Overlay complexity:** removals can invalidate references or boot dependencies. Resolve and lint every final variant, including inherited module checks, before compilation or baking.

**7. Alternatives rejected**

**A cleaned-up fluent builder.** Explicit profile objects and better annotations would improve today’s API, but modules would still receive mutation authority over a shared recipe. Source registration, generated service dependencies, and lifecycle state would remain sensitive to operation order. Removing those effects would effectively turn the builder into a verbose frontend for the immutable model anyway.

**TOML/YAML with Python escape hatches.** Static manifests suit package lists but handle typed key-to-disk references, reusable source builds, conditional composition, and custom lint checks poorly. Adding references, overlays, interpolation, includes, and callable extensions creates a second language alongside Python. Frozen Python values supply those capabilities with normal functions, imports, type checking, and test tooling.
## Implementation notes

Stage 1 (2026-10-04) lives in `src/tundravm/declarative/` and is exported from `tundravm`:

- `model.py`: every section-2 declaration as a frozen, slotted dataclass with the field names and defaults above; `__post_init__` validates at construction (absolute paths, names, phases, modes, `Disk.key` is a `Key`/`Path`, `Secrets.store` is a `Disk`) and freezes lists into tuples.
- `resolve.py`: `resolve`, `resolve_all`, `lint`, `identity`, `order_hooks`, `order_inits`.
- `lower.py`: `lower(recipe, *, variants=None) -> Image`. The fluent `Image`/`RecipeState` is now an internal lowering target, so compile, check, diff, lock, bake and explain run unchanged.
- `load.py`: `load(path, *, attribute="recipe")`. `tundravm.recipe.load_recipe()` also finds and lowers a `Recipe` (the CLI path). A module-level `backend` holding a backend instance becomes the image's backend.

Semantics chosen where this document leaves room:

- Identity: equal repeated declarations dedupe. Unequal ones with the same identity fail with `identity-collision`, both within one level and between a variant's `add` and what it inherits (use `replace`). A variant applies `replace`/`remove` to its inherited items before adding its own.
- `Init` names `keys`/`disks`/`secrets` are reserved for the built-in steps (priorities 10/20/30), which `after=` may reference. `Hook.after` names hooks only. A dependency on a later phase or a higher priority is an error.
- `Unit(after_init=True)` prepends `runtime-init.service` to the unit text's `[Unit]` `After=`/`Requires=` (or adds them), matching what modules render through `resolve_after`. If the variant has no init step, `lint` warns (`unit-after-init-without-init`) and the text is left unchanged.
- A missing `Debloat` means the compiler default (debloat enabled), not "no debloating". A missing `RuntimeTools` means the `tundra-tools` master default with the default config paths.
- `epoch=0` is reproducible output (Seed, `SOURCE_DATE_EPOCH=0`, IMAGE_VERSION strip). `None` is non-reproducible. Any other value sets `SOURCE_DATE_EPOCH`.
- `Recipe.base` defaults to `debian/trixie` as designed (the fluent default is bookworm). `Mkosi.dialect` is accepted, but both dialects lower identically because the compiler has a single emission today.
- `Install(directory=True)` requires `mode=None`, mirroring `Install.tree()`. `Unit` without content must enable, disable or mask. `Unit` with content needs a type suffix.
- Top-level `tundravm.Kernel` is still the fluent kernel spec, because `examples/nethermind_tdx.py` imports it. The declarative one is `tundravm.declarative.Kernel`. Top-level `Diagnostic` and `Install` are now the declarative types; the fluent ones live in `tundravm.check` and `tundravm.source`.

Lowering limits (each raises `ValidationError`):

- A variant whose parent is another non-default variant.
- A `parent="base"` sibling of a default variant that has its own overlay.
- `remove`, or `replace` of anything but File/User/Group/Partition/Repository/Debloat, in a variant that extends the default.
- Keys, disks or secrets changed in an extending variant when the default already declares some.
- More than one `Secrets` per variant.
- `Setting`/`Kernel` that differ per variant.
- `Setting`s without a `MkosiOptions` mapping (supported: Output.Seed/OutputDirectory/CompressOutput/ManifestFormat, Build.PackageCacheDirectory/WithNetwork/Environment/SandboxTrees, Content.CleanPackageMetadata).
- A `Kernel` from `Http` or a git ref other than `v<version>`.

`SecretEnv.service` lowers to the fluent `scope="service"`, which carries no service name.

Fluent-only features with no declarative equivalent yet:

- `service()` unit generation (env, limits, security profiles); its replacement is `Unit` text.
- `template()`, which has its own lockfile section.
- `mount_build_source()` (`BuildSources=`).
- `strip_image_version()` separate from `epoch`.
- The `efi_stub()`/`backports()` methods and the `Tdxs`/`DevTools` modules. The `efi_stub`/`backports`/`tdxs`/`devtools` fragment functions do not exist yet.
- Language builders (`GoBuild`/`CargoBuild`/`DotnetBuild`) and `SourceBuild.cache_key`/`mark_unpinned`. `Build` lowers to `ScriptBuild` with the default cache key, so the surge builds are not byte-identical yet.
- `DebloatConfig.paths_skip_for_profiles`/`extra_keep_units`.
- `MkosiOptions.init_script`, `generate_version_script` and `generate_cloud_postoutput`.
- `Policy`, `build_dir`, several output targets per profile, and a replaceable runtime-init generator.
- The section-2 lifecycle functions (`compile`/`lock`/`bake`/`measure`/`deploy`/`Backend`/`Lock`/`Tree`/`Artifact`). The CLI reaches them through lowering.

The CLI `check`/`ci` commands run the fluent rules on the lowered image. Warnings from `declarative.lint` and `Fragment.checks` are not surfaced there yet, though errors fail at load.

Stage 2 (2026-10-04): the surge recipe is declarative.

- `Build` gained `cache_key: str | None = None` (letters, digits, `._+@/-`; `/` is cached as `_`). `None` keeps the derived `<name>-<sha256(url)[:12]>-<ref>`. `Build` lowers to `ScriptBuild` (`export <env> && cd <workdir> && <script>`), which already spells the historical Cargo and .NET hooks; Go builds that want a command-prefix environment put it in `script` (taiko-client), and absolute paths such as `--output /build/nethermind/publish` are written out. No language recipes were added to the model.
- `tundravm.declarative.modules` (exported from `tundravm.declarative`): `backports(*, mirror=None, release=None)` (a sync `Hook` plus `Setting("Build", "SandboxTrees", ...)`, so a recipe with its own `SandboxTrees` setting collides), `efi_stub(*, snapshot, version)` (a postinst `Hook`), `tdxs(*, source, issuer, validator, expected_measurements, check_revocations, get_collateral, verify_imds, verify_identity_token, after_init=False)` (build packages, `Build("tdxs")`, config `File`, the two `Unit`s rendered by the fluent `Tdxs` renderers, `Group("tdx")`, `User("tdxs")`) and `devtools(*, root_password="tdx")` (packages, the `serial-console.service` `Unit` without enablement, and the two historical postinst hooks). Fragment names: `backports`, `efi-stub`, `tdxs`, `devtools`.
- `Mkosi(dialect="nethermind-v1")` rules, all in `lower.py`: `Group`/`User` lower to postinst lines at their declaration position (`mkosi-chroot groupadd [--system] [--gid N] <name>`; `mkosi-chroot useradd [--system] [--home-dir H] --shell S [--uid N] [--gid G] [--groups a,b] <name>`, no `--create-home`) instead of the account prelude, and an extending variant may not replace a `Group`/`User`; `Build` never emits the `# unpinned: <ref>` marker. The `current` dialect keeps the prelude spelling (`--create-home`) and the marker.
- The surge quirks that no rule produces stay `Hook`s in the recipe: `groupadd -r eth` and `usermod -a -G tdx nethermind-surge` (so `nethermind()` declares `groups=("eth",)` only).
- `examples/nethermind_tdx.py` is `nethermind_base(*, snapshot=PINNED_MIRROR) -> Fragment` plus `NETHERMIND_V1 = Mkosi(dialect="nethermind-v1")` and its own `recipe`; it stays in `examples/` because it reads the repository's kernel config. `examples/modules` holds `raiko(*, source)`, `taiko_client(*, source)` and `nethermind(*, source)`, each with its build, user, unit and env file; `contents.py` re-exports their literals. The fluent `Raiko`/`TaikoClient`/`Nethermind` classes and their tests are gone; `tests/fixtures/surge_fluent.py` keeps the fluent recipe (modules inlined) as the equivalence reference.
- The CLI now compiles every declared variant, so `examples/surge-tdx-prover/mkosi/` also commits `azure/`, `gcp/` and `devtools/`, byte-identical to the fluent recipe's trees (`tests/compiler/test_surge_golden.py`).

Stage 3 (2026-10-04): the lifecycle functions and the CLI grammar.

- `declarative/lifecycle.py` holds the section-2 lifecycle. Each function lowers the recipe and runs the compiler's existing step on the image (`Image.compile`, `check`, `_recipe_payload` with `build_lockfile`/`resolve_pins`/`compare_lock`/`source_drift`, `Image.bake`, `derive_measurements`, the deploy adapters), so it produces the same bytes the CLI does. Everything is exported from `tundravm.declarative`. The top level exports all of it except `diff`, `measure` and `deploy`, because those names are the `tundravm.diff`/`measure`/`deploy` subpackages and shadowing them breaks `monkeypatch.setattr("tundravm.measure.rtmr...")`. Top-level `Measurements` is now the declarative record.
- Additions to the design's signatures:
  - `Tree.variants`
  - `Lock.lockfile` (the wrapped `Lockfile`, `compare=False`) and `Lock.text()`
  - `lock(..., resolver=None, variants=None)` and `lock_status(..., variants=None)`
  - `Backend.kind="inprocess"` and `Backend.build_backend()`
  - `measure(..., allow_placeholder=False)`
  - `deploy(..., allow_placeholder=False, adapter=None)`
  - `doctor(..., runner=None)`
  - `lock()` keeps every matching pin in `previous` and resolves only the unpinned sources and those named in `update`. Unknown `update` names fail.
  - `compiler_version` is the running tundravm version, because the lockfile does not store it.
- `compile(lock=None)` consults no lockfile: source builds use their refs. With a lock, its pins apply, through a scratch build dir that holds the lockfile.
- `lint()` returns resolution and fragment-check diagnostics first, in resolution order. If none is an error, it adds the lowered image's `check()` findings, sorted by variant, level and code. It drops `backend-missing`, because the backend is a `bake()` argument. With `lock=`, drift is added as `lock-changed`, `lock-added` or `lock-removed` diagnostics (`subject` = section).
- `bake()` writes `locked` to `out/tundravm.lock` and bakes frozen into `out`. It then adds a top-level `"declarative": {recipe_digest, tree_digest, simulated}` key to `bake-result.json`, which `BakeResult.load` ignores. `read_artifacts()` reads it back and accepts the file or its directory. `simulated` is true for the in-process backend, and `measure`/`deploy` refuse simulated artifacts unless `allow_placeholder=True`. `Artifact.lock_digest` is the sha256 of the lockfile bytes, as before.
- CLI verbs: `init`, `inspect`, `lint`, `compile`, `diff`, `lock`, `bake`, `measure`, `deploy`, `doctor`, `ci`.
  - `explain`, `check`, `digest` and `new` are removed, with no aliases. `digest` is `inspect --json` `["digest"]`.
  - `-p/--profile` and `--all-profiles` became repeatable `--variant`. Without it a command runs on every declared variant (a legacy `Image` file keeps its active selection).
  - `lint` and `ci` report declarative and fragment-check diagnostics alongside the compiler rules. `ci`'s first step is named `lint`.
  - `bake` is frozen when `--lockfile` is given or `build/tundravm.lock` exists. Otherwise it bakes unpinned with a note. `--frozen`, `--lock` and `--force` are gone. `--backend lima|nix|local|inprocess` overrides the file's `backend`.
  - `measure` and `deploy` take the manifest (`bake-result.json` or its directory) and `--variant`. `measure --scheme` replaces `--backend`. `deploy --param` keys are the `Qemu`/`Azure`/`Gcp` fields, replacing `--memory`/`--cpus`. The design's `deploy --config FILE` is not implemented.
  - `doctor [RECIPE] [--backend KIND]` prints `lint: <summary>`.
- `tundravm.testing`:
  - New: `assert_tree(tree, golden)` compares every file path, its bytes, exec bit and symlink target, and ignores empty directories. `TUNDRAVM_UPDATE_GOLDEN=1` rewrites `golden`.
  - New: `fake_bake(tree, *, variant, target, out)` writes a simulated artifact and its manifest.
  - Extended: `assert_clean` and `assert_diagnostic` also take `lint()` diagnostics as a positional first argument. `strict` defaults to true for diagnostics. `assert_diagnostic` gained `variant=`. `compile_tree` accepts a `Recipe`.

Stage 4 (2026-10-04): the declarative API is the only public API.

- `tundravm` exports the declarative names (minus `diff`/`measure`/`deploy`, which are subpackage names), `Policy`, the errors, `__version__` and `load_recipe` (now returns the `Recipe`). The compiler's image, options, sources and module classes moved to `tundravm._image`, `tundravm._options`, `tundravm._source` and `tundravm._modules`; `tundravm.profile` is gone (`Image.profile()` declares and returns the name). `tundravm.modules` is the fragment functions (`tdxs`, `devtools`, `efi_stub`, `backports`). Recipe files must bind a `Recipe` (`recipe`/`RECIPE`, or a `build`/`recipe` factory); `tundravm.recipe.load_image()` is the internal lowered loader.
- New declarations: `Service(name, exec_start, *, description, user, group, working_dir, env, env_file, exec_start_pre, after, requires, wants, wanted_by, type, restart="no", limits, kill_mode, timeout_stop, security="default", after_init=True)` lowers through the compiler's unit generator, so generated units keep their bytes; `after_init=False` opts out of the automatic `runtime-init.service` dependency (identity `Service(<unit name>)`). `Template(path, template, variables=(), mode=0o644, stage="extra")` renders with `str.format_map`; `extra` keeps the lockfile `templates` section, `skeleton` writes the rendered text as a skeleton file (identity `Template(stage, path)`).
- `Debloat.keep_units_extra` and `Debloat.keep_paths_by_variant` map to `extra_keep_units` and `paths_skip_for_profiles`. `Setting("Build", "BuildSources", ("src[:dest]", ...))` mounts host directories per variant. `Mkosi` gained `init_script`, `version_script`, `cloud_postoutput` and `strip_os_release` (`None` follows `epoch`). `Recipe.policy` sets the bake/lock `Policy`. `Variant.targets` lists several outputs (`target` is the shorthand; passing both fails); `Resolved.targets` holds them and `Resolved.target` is the first. Platform glue is applied for each cloud target.
- Variant lowering: a variant whose parent is `base` or the default variant still lowers as an extending profile when the profile merge can express it (pure additions and File/User/Group/Partition/Repository/Debloat replacements), which keeps the committed trees byte-identical. Every other variant (chained onto another variant, parentless, removing anything, replacing other kinds, changing keys/disks/secrets, adding `BuildSources` or per-variant debloat paths) is lowered standalone from its own resolved declarations. Still rejected: standalone-lowered variants under `Mkosi(layout="native")` (mkosi applies the root config to every profile), variants that change `Setting`s other than `BuildSources` or the `Kernel` (recipe-wide), more than one `Secrets` per variant, and the default variant having a non-`base` parent.
- Not carried over, by design: the fluent `build_dir` (lifecycle functions take explicit output paths), replacing the runtime-init generator, `Module` subclassing (fragments replace it), `enabled=False` generated services (declare `Unit(name, enabled=False)` for packaged units).
- `tests/fixtures/surge_fluent.py` stays as the fluent parity oracle (internal imports); it and the parity tests in `tests/test_declarative_lower.py`/`tests/test_declarative_modules.py` are the only test code that builds a compiler image directly (`blank_image()`).

Stage 5 (2026-10-04): lifecycle fixes and variant wording.

- Compiling is side-effect free: `Image._compile` generates runtime-init (`/usr/bin/runtime-init`, `runtime-init.service`, the `After=`/`Requires=` patches) into a scratch copy of the state, so a later `lint`/`lock`/`lock --check` on the same lowered image (as in `tundravm ci`) sees the declared recipe. Lock payloads were already pre-init, so no digest moved.
- Variant subsets: `compare_lock(lock, payload, partial=True)` skips the lock's sections of variants the payload does not select and checks the whole-recipe digest only when the selection is every variant the lock holds. Frozen bakes always compare this way, so a lock of every variant satisfies `bake --variant X` / `bake(variants=("X",))`. `lock --check --variant X` and `lock_status(recipe, lock, variants=("X",))` use it when the selection leaves out a declared variant; without `--variant` (the full set) comparison is unchanged. Sources pinned only for unselected variants are not reported as removed.
- Lockfile version 3: per-variant sections are `variants.<name>.<key>` (version 2 said `profiles.`; `parse_lockfile` maps them and reports version 3). The embedded `recipe` payload keeps its `profiles` key because the recipe digest covers it.
- User-facing wording says variant: `inspect` prints `variant=`, a `Parent:` line (the recipe's `Variant.parent`) and `Fragments:` (the resolved fragment names); `inspect --json` entries carry `variant`, `parent`, `fragments` in place of `profile`, `extends`, `modules`, `extends_modules`. The bake summary column, `baked N variants`, `--json-logs` events (`variant` key), `lint --format json` (`variant` key), the lint Markdown `Variant` column and the `variant-empty` code (was `profile-empty`). `bake-result.json`/`report.json` keep their `profiles`/`profile` keys (`read_artifacts` reads them).
- `lock_status(recipe, locked, *, variants=None, resolver=None)`; `Measurements.to_json(path=None)` and `Measurements.verify(expected) -> tuple[str, ...]` (registers that differ, including ones only one side has).
- `Build(name, source, script=None, install=(), packages=(), env=(), cache_key=None, recipe=None)`: exactly one of `script` and `recipe`; `recipe` is `Go`, `Cargo` or `Dotnet` (the compiler's `GoBuild`/`CargoBuild`/`DotnetBuild`, exported from `tundravm` and `tundravm.declarative`), which carry their own `packages`/`env`.
- `epoch` other than 0 writes `SourceDateEpoch=<epoch>` next to `Environment=SOURCE_DATE_EPOCH=<epoch>`.
- Lint rules `output-target-platform-mismatch`, `platform-target-missing` and `secret-undelivered` are removed: lowering always applies the platform glue for a variant's cloud targets, and every `Secret` has a target and every `Secrets` lowers to its delivery step.

Stage 6 (2026-10-04): the compiler lowers the whole model.

- `Kernel.source` is honoured. `Git(url, ref)` clones `ref` (`--branch`) or, for a 40-hex commit, `git init` + `fetch --depth 1` + `checkout FETCH_HEAD`; `submodules=True` adds `--recurse-submodules --shallow-submodules` (or `submodule update`), and `subdir=` clones to `$KERNEL_CACHE/repo` and links `src` to the subdirectory. `Http(url, sha256=)` downloads with `curl`, checks `sha256sum -c` and untars with `--strip-components=1`; the variant gains the `curl` build package. `Http` without `sha256` raises: the kernel is not a lockfile source, so the recipe pins it. Tag `v<version>` with no subdir/submodules emits the historical clone lines and cache key unchanged; any other source adds its identity to the cache-key hash. The compiler `Kernel` carries `source_ref` (`None` for the tag), `source_subdir`, `source_submodules`, `source_archive` and `source_sha256`; `inspect --json` describes the kernel source as `source: {repo, ref, subdir, submodules}` or `{url, sha256}` (was `source_repo`). `Kernel.config` stays a `Path`: inline text would be indistinguishable from a file name.
- Settings and the kernel are per variant. A variant whose settings (other than `BuildSources`) or kernel differ from the default variant's lowers standalone with its own `MkosiOptions` and kernel (`Image.profile_mkosi`/`profile_kernels`, read through `mkosi_for`/`kernel_for`; `EmitConfig.profiles`/`for_profile`), so its `mkosi.conf`, kernel build hook and `kernel/kernel.config` are its own. The default variant's bytes do not change.
- `Setting`s without a compiler field are written verbatim, one `Key=value` line per value (`Key=` for no values): in `[Distribution]`, `[Output]`, `[Build]` or `[Content]` (before `ExtraTrees=`) when the section is one the compiler writes, otherwise as a trailing `[Section]`. Keys the compiler writes itself (`Packages`, `Mirror`, `Format`, the script keys, ...) raise with the declaration that sets them; a mapped key in the wrong section names the right `Setting`.
- Several `Secrets` per variant: one keeps the `RuntimeTools` paths; with several, each writes `<stem>-<name><suffix>` (`/etc/tdx/secrets-api.yaml`, `.json`) and gets its own `secret-delivery setup` runtime-init step, sharing one `secret-delivery` build. Raises when two of them (or a key/disk config) write the same path, or two deliver a secret to the same file.
- `Mkosi(layout="native")`: extending variants emit as `mkosi.profiles/<name>/` overlays as before. The error for the others lists every offending variant with the reason it lowers standalone (removes/replaces what, parentless, chained, own settings or kernel, keys/disks/secrets, `BuildSources`, per-variant debloat paths, leaving a cloud target). It stays an error: mkosi applies the root `mkosi.conf` to every profile, so a profile cannot remove from it.
- A `base`-parented variant whose targets differ from a cloud-targeted default variant's now lowers standalone instead of raising (an overlay cannot drop the default's platform glue).
- The default variant may have any parent: it is lowered from its resolved declarations, and its parent lowers standalone (it lacks the default's additions).
- `Policy.require_integrity` is removed (inert since `fetch/` went away); `Policy(require_frozen_lock, mutable_ref_policy, network_mode)`.
- `bake --lockfile X` no longer replaces a different `<out>/tundravm.lock`: the lock is copied there only when that file is absent or identical; otherwise the bake reads `X` (`Image.lock_file`) and `bake-result.json` records it under `declarative.lockfile` (`bake_image(..., lock_source=)`).
- `Build(recipe=Go(...), packages=...)` hints `Go(...)`/`Cargo(...)`/`Dotnet(...)`, not the internal class names.
- Remaining lowering errors are genuine: unreadable/non-UTF-8 files, empty `Directory` sources, missing template placeholders, malformed `Setting` values, compiler-written mkosi keys, unpinned kernel archives, overlapping `Secrets`, and standalone variants under the native layout.
