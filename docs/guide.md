# The tundravm guide

tundravm is mkosi made opinionated for confidential VMs. You write one Python `Image`.
tundravm compiles it to one mkosi 27.1 tree, builds that tree with the mkosi it pins, and
gives you an immutable build directory with a manifest you can sign. Then it runs, deploys and
attests the build. The choices are made for you: Debian trixie, systemd, a measured UKI,
Intel TDX. Each default below has one escape hatch, and the hatch is a typed value, not a
string.

This guide follows the loop: create a project, build the example up stage by stage, lock,
test, build, run, deploy.
The [command reference](reference/cli.md), the [API reference](reference/api.md) and the
[lint reference](reference/lint.md) are generated from the code. The [trust page](trust.md)
says what the image defends against and how to recover.

## What is proven today

The work follows the gates of [the v2 plan](design/plan-v2-2026-10-30.md). Gates 1, 4, 5
and 6 are implemented, and so are gate 3's `run --mode` separation and attested endpoint.
Gate 2 is not.

| Area | State |
|---|---|
| The mkosi tree, `payload.efi`, `qemu.raw`, `azure.vhd`, `gcp.tar.gz`, both root layouts | built in CI from a fixture; `qemu.raw` boots through its ESP and from dm-verity; two builds match, `azure.vhd` included |
| Building an `Image` in CI | the `stages` and `boot` jobs build stage 1 of the example and boot its dev build |
| `Image`, `Service`, `Component`, `Profile` and the runtime slots | compile and resolve under mkosi in the test suite |
| The thirteen commands, build directories, signed manifests | `verify` checked a signed manifest from a second checkout |
| `run --mode simulated --probe` | CI runs the fixture's dev build: every probe check passes and its page is fetched |
| The fail-closed production profile | CI boots the production fixture without TDX: `tdx-evidence.service` refuses and its services never start |
| `Kernel.build` | a weekly CI workflow compiles Debian's 6.12 with every target's drivers built in and boots it |
| A real TDX quote, raw-disk measurements matched against it (gate 2) | not done |
| The attested endpoint: quote-bound TLS, channel-bound delivery and unlock | CI attests the fixture's dev build (simulated) and delivers to it; `BrokerKey`'s pull client is not built |
| `deploy` to GCP or Azure, `attest` against a real quote | tested against fakes only |

## A project

### Create one

Install a release by its tag, which pins the version; `new` writes a project pinning that
commit:

```console
$ uv tool install git+https://github.com/Hyodar/tundra@v2.0.0
$ tundravm new demo --ci
$ cd demo
$ uv sync
$ uv run tundravm lock
$ uv run tundravm build --tree-only mkosi
$ uv run pytest
```

`new` prints this loop, then `dev`. The generated tests and the CI's `check --tree mkosi`
compare the image with the committed `mkosi/` tree, which depends on the lock: write the
tree after locking and commit both.

`new NAME` writes:

| File | Holds |
|---|---|
| `NAME.py` | the image: [stage 1](#1-the-boot) of the example |
| `tests/test_NAME.py` | lint, compile and committed-tree tests |
| `pyproject.toml` | tundravm from git at a commit, pytest, and `[tool.tundravm]` |
| `README.md` | the commands of the loop |
| `.gitignore` | ignores `/.tundravm/` and `/build/*`, keeps `build/tundravm.lock` |
| `.github/workflows/tundravm.yml` | with `--ci`: see [CI](#ci) |

Run the commands with `uv run` inside the project. The examples below leave it out. Commit
`uv.lock`. `uv add --editable PATH/TO/tundravm` works against a local checkout instead.

### `[tool.tundravm]`

```toml
[tool.tundravm]
image = "demo.py"
build = "build"
```

Each setting resolves in this order: the command-line argument, the environment, the table,
the default.

| Key | Environment | Default |
|---|---|---|
| `image` | `TUNDRAVM_IMAGE` | none: name it here or on the command line |
| `build` | `TUNDRAVM_BUILD` | `build` |
| `backend` | `TUNDRAVM_BACKEND` | `local` on Linux x86-64, `lima` elsewhere |

Relative paths in the table are relative to `pyproject.toml`. Commands look for the
nearest `pyproject.toml` that has the table, walking up from the working directory.
`deploy` reads two more tables, see [Deploy](#deploy).

### The build backend

An image file names no backend. The host decides.

| Backend | Host | Needs |
|---|---|---|
| `local` | Linux x86-64 | Python, apt and dpkg with `debian-archive-keyring`, and unprivileged user namespaces or `sudo` |
| `lima` | macOS, other Linux | Lima; the guest is x86-64 Debian running the same pinned mkosi |

`local` runs mkosi 27.1 in-process, never from `PATH`; every other build tool comes from
mkosi's tools tree, pinned to the same Debian snapshot. Without unprivileged user
namespaces (Ubuntu 24.04's default), mkosi runs under `sudo`. `lima` on Apple Silicon
emulates the x86-64 guest: the same bytes, several times slower. A build never switches
backend on failure.

`doctor` prints one row per capability: `ready`, `blocked` with its fix, `optional`,
`unavailable` or `unsupported`. `build:go` blocks without a host `go` (see
[Sources and toolchains](#sources-and-toolchains)), `run:simulated` without QEMU:

```console
$ tundravm doctor
$ tundravm doctor --for build
```

## The attested node, stage by stage

The example, `examples/attested-node/`, is an Ethereum node on Hoodi that signs its block
responses inside the TD. Six stages under `stages/` build it up. Each stage is a
complete image file that adds to the one before. The last is `image.py`, whose `mkosi/`
tree is committed. [`dev`](#run) builds any stage's dev profile and boots it. It prints the
boot probe, the checklist a dev build runs at boot ([The boot probe](#the-boot-probe)):

```console
$ tundravm dev examples/attested-node/stages/01-boot.py
```

### 1. The boot

`tundravm new` writes this file. Its docstring lists what the one `Image` call implies:

<!-- listing: examples/attested-node/stages/01-boot.py -->
```python
"""attested-node: the boot, stage 1 of the guide.

Debian trixie from one snapshot, with systemd, the TEE baseline (NTS time, a volatile
journal, sysctl hardening), DHCP, a firewall that drops inbound traffic nothing exposes,
filtered egress, and a dev profile with a serial console and root password "tdx".
"""

from tundravm import Debian, Image
from tundravm.components import DevAccess, Egress, Network

image = Image(
    "attested-node",
    base=Debian("trixie", snapshot="20261018T082309Z"),
    packages=["ca-certificates"],
    network=Network(egress=Egress()),
    dev=DevAccess(password="tdx"),
)
```

Its probe reports `mode` simulated and a refused `egress:` datagram.

### 2. Nethermind, a component of your own

Stage 2 adds Nethermind. Application components are not part of tundravm: the project
writes them, here as `components/nethermind.py`, which the image file imports:

<!-- diff: examples/attested-node/stages/01-boot.py examples/attested-node/stages/02-nethermind.py -->
```diff
--- stages/01-boot.py
+++ stages/02-nethermind.py
@@
 
-from tundravm import Debian, Image
+from components.nethermind import Nethermind
+
+from tundravm import Debian, Image, RootFS
 from tundravm.components import DevAccess, Egress, Network
+from tundravm.secrets import Generated
+from tundravm.storage import OperatorKey, Volume
+
+data = Volume("data", unlock=OperatorKey())
+jwt = Generated("jwt")
+nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
 
@@
     packages=["ca-certificates"],
+    components=[nethermind],
     network=Network(egress=Egress()),
+    rootfs=RootFS.verity(),
     dev=DevAccess(password="tdx"),
```

The class is the whole Component API:

<!-- listing: examples/attested-node/components/nethermind.py Nethermind -->
```python
class Nethermind(Component):
    """Nethermind on *config*, its JSON-RPC and Engine API on the loopback."""

    jwt: Secret | Generated
    state: VolumeDirectory | None = None
    config: str = "hoodi"
    rpc_port: int = 8545
    engine_port: int = 8551

    @property
    def rpc(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.rpc_port)

    @property
    def engine_api(self) -> Endpoint:
        return self.service("nethermind").endpoint(self.engine_port)

    def outbound(self) -> Egress:
        return Egress(ports=[TCP(30303), UDP(30303)])

    def compose(self) -> Iterator[object]:
        yield Build(
            "nethermind",
            SOURCE,
            using=Dotnet(
                project="src/Nethermind/Nethermind.Runner",
                output="nethermind",
                properties=PROPERTIES,
            ),
            install=INSTALL,
            cache_namespace=f"nethermind-{SOURCE.ref}",
        )
        for package in RUNTIME:
            yield Package(package)
        datadir = "/var/lib/nethermind" if self.state is None else self.state.path
        yield Service(
            "nethermind",
            [
                f"{HOME}/nethermind",
                f"--config={self.config}",
                f"--datadir={datadir}",
                "--JsonRpc.Enabled=true",
                "--JsonRpc.Host=127.0.0.1",
                f"--JsonRpc.Port={self.rpc_port}",
                "--JsonRpc.EngineHost=127.0.0.1",
                f"--JsonRpc.EnginePort={self.engine_port}",
                f"--JsonRpc.JwtSecretFile={self.jwt.path}",
            ],
            readiness=TcpProbe(self.rpc_port, timeout="10min"),
            state="nethermind" if self.state is None else self.state,
            credentials=[self.jwt],
            hardening="dotnet",
        )

    rules = (
        Rule(
            "nethermind-volatile-state",
            "warning",
            "Nethermind keeps its database on the volatile root, so it resyncs at every boot.",
            "Pass state=volume.directory('nethermind') on a Volume.",
        ),
    )

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        if self.state is not None:
            return ()
        message = "Nethermind keeps its database on the volatile root: it resyncs every boot"
        return (Diagnostic("nethermind-volatile-state", "warning", message),)
```

- Fields are configuration: the class is a frozen, keyword-only dataclass.
- `compose()` yields what it stands for: a `Build` and the `Service` running it.
- `using=Dotnet(...)`: its `global.json`'s SDK builds it offline.
- `rpc` and `engine_api` are exports: endpoints carrying the service's readiness.
- `outbound()` is what a filtered image lets it send: its peers on 30303.
- `check()` is a lint rule per profile; `rules` declares each code it reports.

Nethermind brings three things the boot did not have. `data` is a LUKS2
[volume](#volumes); `unlock=OperatorKey()` means an operator sends its passphrase after
[attesting](#attest) the instance, and Nethermind waits until then. A volume brings the
[control plane](#the-control-plane) with it: the attested endpoint on 8745, and SSH on 22
for the key `attest` hands over. `Generated("jwt")` is a [secret](#secrets) the image
writes at boot. The build is large, so `RootFS.verity()` makes the root a read-only
dm-verity disk ([The root filesystem](#the-root-filesystem)).

### 3. Lighthouse, ready-made

Stage 3 adds the consensus client that drives Nethermind to the chain's head.
`components/lighthouse.py` has the same shape, with a pinned download in place of a build:
open it. Pairing it takes the Engine API export and the same secret:

<!-- diff: examples/attested-node/stages/02-nethermind.py examples/attested-node/stages/03-lighthouse.py -->
```diff
--- stages/02-nethermind.py
+++ stages/03-lighthouse.py
@@
 
+from components.lighthouse import Lighthouse
 from components.nethermind import Nethermind
@@
 nethermind = Nethermind(jwt=jwt, config="hoodi", state=data.directory("nethermind"))
+lighthouse = Lighthouse(
+    execution=nethermind.engine_api,
+    jwt=jwt,
+    checkpoint_sync_url="https://checkpoint-sync.hoodi.ethpandaops.io",
+    state=data.directory("lighthouse"),
+)
 
@@
     packages=["ca-certificates"],
-    components=[nethermind],
+    components=[nethermind, lighthouse],
     network=Network(egress=Egress()),
```

### 4. The signer

Stage 4 adds `signer/`, a Go proxy that signs Nethermind's block responses with the
instance key, a fresh secp256k1 key every boot generates ([Services](#services)):

<!-- diff: examples/attested-node/stages/03-lighthouse.py examples/attested-node/stages/04-signer.py -->
```diff
--- stages/03-lighthouse.py
+++ stages/04-signer.py
@@
 
-from tundravm import Debian, Image, RootFS
+from tundravm import Debian, Image, RootFS, Service
 from tundravm.components import DevAccess, Egress, Network
-from tundravm.secrets import Generated
+from tundravm.secrets import Generated, InstanceKey
+from tundravm.sources import GoBinary, Local
 from tundravm.storage import OperatorKey, Volume
+from tundravm.systemd import Resources, TcpProbe
 
@@
 )
+program = GoBinary("signer", Local("../signer"))
+signer = Service(
+    "signer",
+    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
+    credentials=[InstanceKey()],
+    requires=[nethermind.rpc],
+    readiness=TcpProbe(8547),
+    resources=Resources(memory_max="256M"),
+    hardening="static",
+)
 
@@
     components=[nethermind, lighthouse],
+    services=[signer],
     network=Network(egress=Egress()),
```

`GoBinary(..., Local("../signer"))` builds that directory, pinned by its files' digest
([Sources](#sources-and-toolchains)). `credentials=[InstanceKey()]` hands the service the
key. It listens on the loopback, so nothing reaches it yet.

### 5. The attested ingress

Stage 5 puts attested TLS on 8546 in front of it:

<!-- diff: examples/attested-node/stages/04-signer.py examples/attested-node/stages/05-attested.py -->
```diff
--- stages/04-signer.py
+++ stages/05-attested.py
@@
 from tundravm.components import DevAccess, Egress, Network
+from tundravm.ports import AttestedTLS
 from tundravm.secrets import Generated, InstanceKey
@@
     "signer",
-    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc],
+    [program, "-listen", "127.0.0.1:8547", "-upstream", nethermind.rpc, "-quotes"],
     credentials=[InstanceKey()],
+    quotes=True,
     requires=[nethermind.rpc],
@@
     services=[signer],
+    ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
     network=Network(egress=Egress()),
```

The ingress opens 8546; beside it, only the control plane's 8745 and 22 are open.
`quotes=True` lets the signer ask the evidence service for a quote over its key. It starts
only once one is issued. `GET /quote?nonce=HEX` answers one, so a client can check the key
it verifies with.

### 6. Operating it

Stage 6, which `image.py` repeats with `Local("signer")`, adds what operating it takes:

<!-- diff: examples/attested-node/stages/05-attested.py examples/attested-node/stages/06-operate.py -->
```diff
--- stages/05-attested.py
+++ stages/06-operate.py
@@
 
-from tundravm import Debian, Image, RootFS, Service
-from tundravm.components import DevAccess, Egress, Network
+from tundravm import Debian, File, Image, Profile, RootFS, Service
+from tundravm.components import (
+    DevAccess,
+    Egress,
+    JournalExport,
+    Network,
+    Observability,
+    PrometheusRemoteWrite,
+)
 from tundravm.ports import AttestedTLS
-from tundravm.secrets import Generated, InstanceKey
+from tundravm.secrets import Generated, InstanceKey, Secret
 from tundravm.sources import GoBinary, Local
 from tundravm.storage import OperatorKey, Volume
-from tundravm.systemd import Resources, TcpProbe
+from tundravm.systemd import Every, Resources, TcpProbe
 
@@
 )
+motd = File.text("/etc/motd", "attested-node: Nethermind and Lighthouse on Hoodi\n")
+sync = Service(
+    "sync-status",
+    ["/usr/bin/curl", "-fsS", f"http://{lighthouse.http.address}/eth/v1/node/syncing"],
+    requires=[lighthouse.http],
+    timer=Every("1h"),
+)
+logs = JournalExport(credential=Secret("logs-client"))
+metrics = PrometheusRemoteWrite(credential=Secret("metrics-token"))
 
@@
     base=Debian("trixie", snapshot="20261018T082309Z"),
-    packages=["ca-certificates"],
+    packages=["ca-certificates", "curl"],
+    files=[motd],
     components=[nethermind, lighthouse],
-    services=[signer],
+    services=[signer, sync],
     ingress=[AttestedTLS(8546, upstream=signer.endpoint(8547))],
@@
     rootfs=RootFS.verity(),
+    observability=Observability(logs=logs, metrics=metrics),
+    targets=("qemu", "azure", "gcp"),
+    profiles={
+        "staging": Profile(changes=[motd.ref.edit(content="attested-node: staging\n")]),
+        "bench": Profile(add=["fio"], production=False),
+    },
     dev=DevAccess(password="tdx"),
```

`Observability` ships the journal and the node metrics. Neither sink names a `url=`,
because the receiver is a deployment fact. Until each `url=` names yours, the export goes
to a receiver in the guest (`journal-sink`, `metrics-sink`), so a dev boot shows it
working. A production profile reports `observability-sink-unset` until then. Once `url=`
is set, the delivered credential authenticates the export. `sync-status` runs hourly and
puts Lighthouse's sync status in the journal.
`staging` edits one file. `bench` adds `fio`, and `production=False` keeps it out of
production builds. `targets=` adds the Azure and GCP disks of the same payload.

## The image

An image file binds an `Image` to the name `image`, as each stage does.
`Image` is a frozen dataclass. Every slot is optional and takes small typed values.
Every public name has one home, without aliases: the [API reference](reference/api.md)
lists each namespace.

### What is implied

| Implied | Escape hatch |
|---|---|
| Debian trixie from snapshot `20261018T082309Z` | `base=Debian("trixie", snapshot="...")`; another release is untested |
| a measured UKI with a curated module set: `tdx_guest`, virtio, NVMe, Hyper-V, gVNIC | `kernel=Kernel.default(modules=[...])` to keep more, `kernel=Kernel.build(...)` to compile one |
| NTS time from two authenticated servers, a volatile journal, sysctl hardening, a static resolver, no apt timers, no console getty (`TeeBase`) | pass `TeeBase(...)` in `components=`, or remove a piece in a profile |
| DHCP on `en*` and `eth*` (`Network`) | `network=Network(dhcp=False, static=StaticAddress(...))` |
| an nftables firewall that drops inbound traffic except exposed ports; a volume or a secret opens 8745 and 22 for the control plane (`Firewall`) | `expose=` opens a port; `network=Network(egress=Egress(...))` filters outbound |
| one generated system user per service, the `static` sandbox, `Restart=on-failure` | `Service(user=, hardening=, unit=DropIn(...))` |
| the evidence service `tdxs` from `tools/`, socket-activated on `/var/tdxs.sock` (`Tdxs`) | `components=[Tdxs(source=Git(url, commit, subdir="tools"))]` for a mirror |
| `tdx-evidence.service`, which every service requires: without TDX a production profile starts none, a development one runs simulated ([trust](trust.md#threat-model)) | none: a dev profile is the way to run without TDX |
| for each cloud in `targets=`, its provisioning glue, inert elsewhere through `ConditionVirtualization=`, so one payload boots on every target | `targets=` |
| a raw bootable disk for QEMU | `targets=("qemu", "azure", "gcp")` adds the cloud disks |

Passing a baseline component in `components=` replaces the implied one.

### The slots

| Slot | Takes | From |
|---|---|---|
| `base` | `Debian(release, snapshot=, efi_stub=, repositories=[AptRepository(...)])` | `tundravm` |
| `packages` | package names, or `Package(name, backports=True)`: runtime packages only | `tundravm` |
| `files` | `File.text(path, text, mode=, owner=)`, `File.copy(src, dst)`, `Tree(src, dst, exclude=)`, a `Build` | `tundravm` |
| `services` | `Service` | `tundravm` |
| `components` | `Component` instances | `tundravm.components` |
| `ingress` | `AttestedTLS` or `TLS`: see [Ingress](#ingress) | `tundravm.ports` |
| `network` | `Network(dhcp=, static=, egress=, interfaces=)` | `tundravm.components` |
| `users`, `groups` | `User`, `Group` for resources several services share | `tundravm` |
| `kernel` | `Kernel.default(modules=, cmdline=)` or `Kernel.build(version, source, config=, patches=, cmdline=)` | `tundravm` |
| `rootfs` | `RootFS.initramfs()` (default) or `RootFS.verity()` | `tundravm` |
| `observability` | `Observability(logs=JournalExport(...), metrics=PrometheusRemoteWrite(...))` | `tundravm.components` |
| `targets` | a subset of `("qemu", "azure", "gcp")`; `qemu` is always built | |
| `dev` | `DevAccess(ssh_keys=, password=)`: the non-production `dev` profile | `tundravm.components` |
| `profiles` | `{"name": Profile(...)}` | `tundravm` |
| `mkosi` | `Mkosi(settings={...}, scripts=[...])`: see [The escape to mkosi](#the-escape-to-mkosi) | `tundravm` |

`AptRepository(url, suite, components, key=)` adds an archive for the build's apt, signed
by `key=` (a host keyring `Path`, or one under `/usr/share/keyrings/`) and pinned: a
`https://snapshot.debian.org/archive/NAME` URL by the image's snapshot (or `snapshot=`),
any other by `sha256=` of its `dists/SUITE/InRelease`, which `lock` records. The example
needs no archive beyond Debian's.

`File.copy` and `Tree` read host files when the image lowers; a relative path is beside
the image file, as `Local` is. A file is root-owned unless `owner=` says otherwise; the
runtime-init step `owners` applies it at boot.

`cmdline=` on either `Kernel` appends parameters (`name` or `name=value`) to the UKI's
measured command line, after the image's own: `Kernel.default(cmdline=["nospectre_v2"])`.
A parameter tundravm sets (`root=`, `init=`, `console=` and their kin) is refused, naming
what sets it. The example keeps Debian's kernel, whose curated modules cover its three
targets: `Kernel.build` is for a driver built in or a symbol turned off.

## Services

A `Service` is an argv, never a command line. What a bare `Service(name, argv)` gets:
`Type=exec`, `Restart=on-failure` with `RestartSec=5s`, a system user named after the
service, `hardening="static"`, and `WantedBy=minimal.target`. Each argv element is quoted
the way systemd parses it. The one visible exception for a shell is
`["/bin/sh", "-eu", "-c", script]`. A built binary is a valid argv element too
([Sources and toolchains](#sources-and-toolchains)).

Every image generates an instance key at boot: a fresh secp256k1 key on tmpfs, root-only.
A service takes it with `credentials=[InstanceKey()]`, and nothing else may name it.
`show` says which service takes it; `run status` and `attest` print its public key.

### Reaching a service

`expose=[TCP(8080)]` (or `UDP(53)`) opens that port in the firewall. The only other open
ports are an [ingress](#ingress)'s and the [control plane](#the-control-plane)'s 22 and
8745. `expose=` does not protect the requests: an ingress fronts `app.endpoint(8080)`, the
loopback address with the service's readiness.

### Readiness and order

`requires=[other.ready]` is a real dependency: `Requires=` and `After=` on whatever proves
the other service ready. `After=` alone is never generated.

| Readiness | What it does |
|---|---|
| none | ready once the program is exec'd; a `requires=` on it is refused, since nothing proves it |
| `Notify()` | `Type=notify`: ready when the program sends `READY=1` |
| `TcpProbe(port)` | a bounded probe unit polls the port; installs nothing |
| `HttpProbe(url)` | a bounded probe unit waits for a 2xx; installs `curl` |

A probe is its own oneshot unit (`app-ready.service`), so a dependent waits for an answer,
not for an exec.

### The sandbox

`hardening="NAME"` picks a sandbox per runtime class. Each preset is the
strict sandbox minus what that runtime cannot live without:

| Preset | Differs from `static` | Because |
|---|---|---|
| `static` | nothing | a static binary needs nothing more |
| `jit`, `node`, `python` | no `MemoryDenyWriteExecute=` | a JIT maps pages writable, then executable; CPython needs it for libffi |
| `dotnet` | the same, and `@resources` syscalls allowed | the .NET runtime sizes itself with `sched_setaffinity` and `setrlimit` |

The `static` sandbox is `ProtectSystem=strict`, private `/tmp` and devices, no new
privileges, an empty capability bounding set, the kernel and namespace protections, the
`@system-service` syscall filter and `UMask=0077`.
`Hardening.preset("static", devices=["/dev/tdx_guest"])` replaces `PrivateDevices=yes`
with an allow list. `hardening="relaxed"` and `Hardening.none()` are for programs you
cannot fix.

### Timers, sockets and the rest

| Need | Write |
|---|---|
| run periodically | `timer=Every("15m")` or `timer=Calendar("daily")` |
| socket activation | `socket=Listen(8080)` or `Listen("/run/app.sock")` |
| persistent state | `state=volume.directory("app")`; a bare name is a volatile `StateDirectory=` |
| a delivered secret | `[app, "--key-file", secret]` |
| a shared group | `groups=["tdx"]` |
| anything systemd knows and no field covers | `unit=DropIn(...)` |

A timer, as [stage 6](#6-operating-it)'s `sync-status`, runs a scheduled oneshot and
refuses what only a long-lived service has: `socket=`, `readiness=`, `expose=`, a volume
`state=`, and any credential but `InstanceKey()`. Writing a unit under
`/etc/systemd/system` at boot is the `unit-written-at-runtime` error: declare it instead.

`Service.packaged("prometheus-node-exporter.service")` runs the unit a Debian package ships
and installs the package (`package=` names another). Its sandbox is `hardening=`, `static`
unless said, which replaces the package's own; beside it only `requires=`, a probe
`readiness=`, `expose=` and `unit=DropIn(...)` apply. A capability the daemon needs is a
`Hardening.preset("static", capabilities=["CAP_SYS_TIME"])` deviation; one it lacks shows
at boot, as the probe's `service:` failure naming it. The example runs no packaged unit:
the one it would, the node exporter, comes with [stage 6](#6-operating-it)'s
`Observability(metrics=)`, and declaring it beside that is an `identity-collision`.

## Components

A `Component` groups declarations and exports typed values: [stage 2](#2-nethermind-a-component-of-your-own) writes one.

Each code `check()` reports is declared in `rules`: `--explain` prints it, and an
undeclared code is a `rule-undeclared` error.

### The shipped library

`tundravm.components` ships the baseline and the ways in; application components
are the project's, as the example's are:

| Component | What it is | Exports |
|---|---|---|
| `TeeBase` | the confidential-VM baseline (implied) | none |
| `Network` | systemd-networkd, DHCP or a static address (implied) | `online` |
| `Firewall` | nftables, inbound drop by default (implied) | none |
| `Tdxs` | the evidence service (implied) | `EVIDENCE`, `socket`, `group` |
| `SshAccess` | root SSH by public key, keys baked or delivered | `port` |
| `DevAccess` | debug access, only in the `dev` profile | none |
| `Observability` | journal and metrics export | `service("journal-export")`, `service("metrics-export")` |

A service that asks for quotes (`quotes=True`) joins the `tdx` group and requires
`EVIDENCE`. The evidence service depends on no volume or secret: the evidence unlocks
them.

`Tdxs` and the volume and delivery tools build from `tools/` of tundravm's repository at
the installed commit (a checkout builds its own `tools/`, pinned by digest).
`Tdxs(source=Git(mirror, commit, subdir="tools"))` points every one of them at a mirror.
`Tdxs.from_policy(policy)` also validates quotes against the registers of an image you
trust; the example's node checks no peer, so it uses none.

## Profiles

The image itself is the `production` profile. `dev=DevAccess(...)` adds a non-production
`dev` profile: debugging tools, a serial console and root SSH by public key only.
`password=` is a root password for that console, never a network login. Anything else is
a `Profile`, as [stage 6](#6-operating-it) declares two.

| Field | Meaning |
|---|---|
| `extends` | the profile it starts from; `None` is `production` |
| `add` | more content: packages, files, services, components |
| `changes` | `value.ref.edit(...)` or `value.ref.remove()` on something the image declares |
| `production` | `False` makes its builds development builds |

A change names a declaration that exists, or the image does not resolve. Removing a
service removes its unit, its user, its firewall rule and the dependencies nothing else
uses. A component's service is `component.service("name").ref`. An edit takes the keywords
of the value's own constructor and is checked like a new value. Refused: another built
binary, a credential bound through argv or `environment=` (bind it in `credentials=`),
another `user=` for a directory on a volume, dropping a required readiness.

How profiles lower: what every profile builds goes once in the shared `payload/`, and what
each builds beyond that, or keeps of what another removes, in `payload/mkosi.profiles/<name>/`.
A component a profile adds opens its `outbound()` in that profile's firewall only.
`build --profile staging` is `mkosi --profile=staging`.

## Volumes, secrets and ingress

The image collects each volume and secret from the services that bind it.

### Volumes

`Volume(name, unlock=)` is a LUKS2 volume with ext4 inside, mounted at `/persistent/<name>`
unless `mount=` says otherwise. Its size and the disk are deployment configuration
(`deploy --volume data=create:100GiB`). The image finds the disk by name through
`/dev/disk/by-id/*-tundravm-<name>`, never by probe order. It formats a volume only while it
carries no initialisation token: an unlock failure never permits formatting.

`unlock=` is the trust decision and has no default:

| Unlock | Means | State |
|---|---|---|
| `OperatorKey()` | an operator sends a passphrase each boot with `secrets unlock` | over the attested endpoint, which hands it to `key-gen` |
| `TpmSeal(platform=, pcrs=, recovery="none")` | a random key sealed to one stated vTPM: `azure`, `gcp` or `swtpm` | `key-gen` seals without a PCR policy today; `pcrs` is recorded, enforced in gate 3 |
| `BrokerKey(url, audience=)` | a key broker releases the key after attestation | no pull client yet: the image waits as for an operator |

`TpmSeal` is refused for `targets=` that boot with another vTPM: one payload boots on every
target, and a key sealed on one platform does not open on another. That is why the
example, built for three targets, unlocks with `OperatorKey()`.

`data.directory("nethermind")` is a directory on the volume. A service binds it with
`state=`, and the image creates it, owned by that service, once the volume is open. The
service waits for it. `directory(name, group=)` shares it through a group.

### Secrets

`Secret(name)` is a value the image needs and never contains. An operator delivers it
after attestation ([Delivering secrets](#delivering-secrets)). It lands in `/run/tundravm/credentials/<name>`, root-owned tmpfs, mode 0600. A service
binds it by naming it as an argv element or an `environment=` value, where it renders as
the path the service reads it at, or with `credentials=[secret]` for a program that finds
it otherwise; either renders `LoadCredential=<name>:/run/tundravm/credentials/<name>`.
systemd copies it into the unit's read-only `$CREDENTIALS_DIRECTORY` at start.

| Option | Meaning |
|---|---|
| `required=True` | every bound service waits for it (default) |
| `required=False` | the service starts without it and reads an empty credential |
| `source=BrokerKey(...)` | a broker delivers it instead of an operator (no pull client yet) |

`rotation=` promises nothing today: the endpoint delivers each secret once per boot.

`Generated(name)` needs no delivery: each boot a root oneshot writes 32 random bytes as
hex to `/run/tundravm/secrets/<name>`, which every service binding it reads.

### Ingress

`AttestedTLS(8546, upstream=signer.endpoint(8547))` is the confidential way in. It opens
8546. The upstream must listen on the loopback and expose no port of its own, or the image
is refused, so the ingress is the only path to it. Today the relay is `socat`'s
OpenSSL listener, as its own user with only `CAP_NET_BIND_SERVICE`, with a self-signed
certificate generated in the guest at every start, which no quote binds yet: a client
pins it on first contact.

`TLS(port, upstream=, certificate=secret)` is ordinary HTTPS with a certificate you deliver
as a secret. Nothing about it is attested.

### The root filesystem

| Layout | Use |
|---|---|
| `RootFS.initramfs()` | the OS in the UKI's initrd: small services, one file to measure (default); `max_size="4G"` caps its tmpfs, else half the memory |
| `RootFS.verity()` | a read-only erofs root, its dm-verity hash partition beside the ESP and root hash on the command line, under a volatile overlay |

Choose verity when the payload is large. The ESP holds a UKI of
`RootFS.initramfs(uki_max_size=)` (256M by default), verity's 128 MiB: a bigger UKI fails
the build with both sizes. The layout and the cap are recorded in the manifest,
never switched by a size threshold.

### Network and observability

`network=Network(egress=Egress(ports=[TCP(443)], hosts=["10.0.0.0/8"]))` filters outbound
traffic, accepting what the baseline (NTS time, resolvers, cloud metadata) and each
component's `outbound()` send. A host may be `HOST:PORT`, and since nftables matches no
names, `"logs.example:443"` opens 443 to any address. A production profile without it is
an `egress-unfiltered` warning. `Observability(proxy="IP:PORT")` reaches every sink
through that one address.

`Observability(logs=JournalExport(url=, credential=), metrics=PrometheusRemoteWrite(url=,
credential=))` ships the journal and node metrics, as [stage 6](#6-operating-it) does. An
`http://` sink is a lint warning: it carries the logs in the clear.

### The control plane

An image with a volume or a secret runs the attested endpoint on port 8745, which needs
nothing on a volume ([trust](trust.md#what-first-contact-proves)), and SSH on 22, for the
key `attest` hands over. `/etc/tundravm/runtime.json` records every volume, secret and
ingress.

## Sources and toolchains

`GoBinary`, `CargoBinary` and `Build` (from `tundravm.sources`) are valid argv elements: they
pull their build and install into the image, and `binary.executable` is the installed
path. Go builds the module root (`package="./cmd/server"` for a sub-command) with
`-trimpath -buildvcs=false` and `CGO_ENABLED=0`; Rust with `cargo auditable build --frozen`
and reproducible `rustflags`, so the SBOM can read the crate graph from the binary.

`Build(name, source, command=["hugo", "--minify"], packages=["hugo"], install=[...])` runs
one program in the build environment, never in the image, and `files=[build]` ships what
its `install=` names. A command over the image's own contents has no spelling beside the
recorded `Mkosi(scripts=)` escape, since nothing could say what it changed.

A build takes one source, which `tundravm lock` pins: `Local("app")`, a directory beside
the image file, by the digest of its files; `Git(url, ref)` by commit; `Http(url, sha256=)`
by sha256. `Git(url, ref, subdir="tools")` builds one directory of the repository. Your own
program is a `Local` source, as the [signer](#4-the-signer) is.

The path stays inside the project (the nearest `pyproject.toml`). The digest covers the
path, sha256, exec bit and symlink target of what `git ls-files --cached --others
--exclude-per-directory=.gitignore` lists, and `build` copies those files. An edit is
`local-changed` ([the lock](#the-lock) says who re-pins it).

The build sandbox never fetches a source. `build` checks each one out on the host, as you,
into `build/<image>/.work/`, verifies a reused checkout against its pin, and mounts the
checkouts into the build. Dependencies are prefetched there, so `build --offline` runs
without network. A modified checkout is `E_SOURCE`; `build --clean-cache` starts
the workspace afresh. The manifest records each source's toolchain; `show BUILD` prints it.

A Go source builds with the toolchain its `go.mod` names (the `toolchain` line, else the
`go` line: `go 1.22` is `go1.22.0`), fetched with the modules by the host's `go` 1.21+;
Debian's `golang` only bootstraps it. A release the host cannot fetch is `E_SOURCE`; a
`go.mod` older than 1.21 builds with Debian's `go`.

A Cargo source builds with the channel its `rust-toolchain.toml` (or `rust-toolchain`)
pins, installed into the workspace by the host's `rustup`; its `cargo` fetches the crates,
and the offline build runs it over Debian's linker. The channel must be a release
(`1.91.0`) or dated (`nightly-2026-09-01`): a moving one is `E_SOURCE`, and no file means
Debian's `cargo`. `build:rustup` blocks only with a pinned source.

A Dotnet source builds with the SDK its `global.json` names: `lock` resolves it under its
`rollForward` policy, within that .NET release, and pins its tarball's sha512; `build`
downloads it; its `dotnet` restores the packages and builds offline over Debian's ICU,
OpenSSL and libstdc++. The host needs no `dotnet`; no `global.json` is `E_SOURCE`.

## The lock

```console
$ tundravm lock
$ tundravm lock --update tdxs
$ tundravm lock --check
```

`lock` writes `build/tundravm.lock`: a digest per section of the resolved image, a commit,
sha256 or content digest per source, and the toolchain (mkosi, the tools tree, tundravm,
the measurement tools), and prints the snapshot, each pin and the toolchain. Commit it. Packages and tools tree share the
snapshot this release pins; `Debian(snapshot=)` overrides it.

- Pins already locked stay while their source is unchanged. `--update NAME` resolves the
  named inputs again; no name resolves all of them.
- `--check` never writes or fetches; it prints the drift (a missing lock too) and exits 1.
- A profile's sections are `profiles.<name>.<key>`; `check` reports their drift as that
  profile's finding (`lock-changed`).
- An older tundravm's lockfile is refused, never migrated: lock again.
- A production build refuses a missing (`lock-missing`) or stale lock and builds every
  source at its pin. A development build writes a missing lock and re-pins `Local` sources.
- `build --offline` builds only what the lock pins, and checks an `InRelease` against
  the lock's digest instead of fetching it.

`check` warns `source-unpinned` once per source the lock does not pin yet, as
`[image] sources.NAME`, then one hint for the sources that share it.

## The mkosi tree

Everything tundravm builds is an mkosi 27.1 tree you can read, commit and build without
tundravm.

```console
$ tundravm build --tree-only mkosi
$ tundravm check --tree mkosi
```

`build --tree-only DIR` writes the tree and prints the mkosi command per profile
(`mkosi -C mkosi build`, `mkosi -C mkosi --profile=dev build`). `check --tree DIR` exits 1
when the committed tree differs from what the image compiles to. The starter's tree:

```text
mkosi.conf                     the QEMU disk: Format=disk, Dependencies=payload, the ESP
mkosi.repart/00-esp.conf       a 272M FAT32 ESP sized to hold the UKI
mkosi.images/payload/mkosi.conf  Include=../../payload
payload/mkosi.conf             the OS: Format=uki, packages, kernel modules
payload/mkosi.conf.d/          one file per concern: 10-platform, 20-tee-base, 30-firewall
payload/mkosi.extra/           the files and the generated units
payload/mkosi.skeleton/        minimal.target, which services are enabled into
payload/mkosi.build.d/  mkosi.postinst.d/  mkosi.finalize.d/
payload/mkosi.profiles/dev/    the dev overlay: the boot probe and the root password
```

How to read it as an mkosi user:

- `targets=` adds `mkosi.images/azure/` and `mkosi.images/gcp/`. Each depends on the
  payload, carries the same ESP and converts the disk in `mkosi.postoutput.d/`. tundravm
  selects them with `--dependency=`.
- `RootFS.verity()` makes the payload a `Format=disk` with root and verity partitions, and
  every disk copies them beside the ESP.
- The root `mkosi.conf` pins `MinimumVersion=27.1`, `Snapshot=`, `ToolsTree=default` with
  its snapshot, `SourceDateEpoch=0`, `Seed=` and `History=no`, so a second build gives
  the same bytes; `build --reproduce` checks it.

### The escape to mkosi

`mkosi=Mkosi(settings={"Locale": "C.UTF-8"}, scripts=[("postinst", "...")])` adds
verbatim settings, each in the section mkosi 27.1 reads it from, and scripts at an mkosi
stage (`prepare`, `build`, `postinst`, `finalize`, `postoutput`). The manifest records
them. Settings the compiler writes itself or that would contradict a guarantee are refused
at construction, each with the slot that sets it (`KernelCommandLine` names
`Kernel(cmdline=)`, `Packages` names `Image(packages=)`) or saying tundravm sets it
(`Seed`, `SourceDateEpoch`).

## Testing a project

`tundravm.testing` reads an image as values without building it, as the generated
`tests/test_NAME.py` does:

```python
from tundravm import Image
from tundravm.components import Egress, Network
from tundravm.testing import assert_clean, plan

image = Image("node", network=Network(egress=Egress()))


def test_lints_clean() -> None:
    # The evidence service's source stays unpinned until `tundravm lock`.
    assert_clean(plan(image), allow=("source-unpinned",))


def test_egress_is_filtered() -> None:
    nftables = plan(image).file("/etc/nftables.conf").content.decode()
    assert "hook output priority filter; policy drop" in nftables

```

| Helper | Does |
|---|---|
| `plan(image, profile)`, `plan(component)` | one profile (a component on an image of its own) compiled in memory: `service()`, `unit()`, `file()`, `packages`, `users`, `settings()`, `lint()` |
| `assert_clean(subject, allow=)` | fails on any error or warning not allowed |
| `assert_diagnostic(subject, code)` | the first finding with that code, or a failure listing all |
| `assert_tree(tundravm.compile(image), golden)` | every path, byte, exec bit and symlink; `TUNDRAVM_UPDATE_GOLDEN=1` rewrites |
| `read_lock(path)` | the lockfile, as `compile(image, lock=)` and `plan(image, lock=)` take it |
| `FakeMkosi`, fixture `fake_mkosi` | mkosi replaced at the adapter: nothing forks, `build` writes placeholders, `calls` records each argv |
| `FakeSources` | git and http fetches replaced by stub checkouts, `fetched` records each |
| `boot(artifact)`, fixture `boot` | boots a built disk or UKI under QEMU with OVMF and reads the serial console |
| `assert_probe(result)`, `read_probe` | a dev boot's probe report; fails unless it finished clean (`markers=[PROBE_DONE]` waits for it) |
| `run_cli(*argv)` | runs the CLI in-process: `(exit code, stdout, stderr)` |

## Build

```console
$ tundravm build
$ tundravm build --target gcp --reproduce --sign ~/.ssh/release-key
```

`build` builds one profile: `production` by default, `--profile P` or `--dev`, against the
[lock](#the-lock). It writes:

```text
build/demo/
  production/<id>/
    payload.efi          the UKI, the measured payload
    qemu.raw             the raw GPT disk OVMF boots through its ESP (always built)
    azure.vhd            with the azure target
    gcp.tar.gz           with the gcp target
    payload.*.raw        the verity root and hash partitions, with RootFS.verity()
    packages.json        mkosi's package manifest
    measurements.json    per target and boot path: predictions, unknowns, a policy
    sbom.spdx.json       the SBOM, bound to the lock and the package manifest
    inputs/              tundravm.lock, summary.json, sources.json, toolchain.json, mkosi-argv.json
    tree/                the mkosi project it was built from
    manifest.json        the identity; <id> is its digest
    manifest.sig         with --sign
    observations.json    timestamps, runner, reproduction: not identity
  development/<id>/      builds of a non-production profile
  latest-production -> production/<id>
  latest-development -> development/<id>
  .work/                 the workspace: fetched sources, mkosi's cache and tools tree
```

The directory is published with one rename, read-only, and never written again. Building
the same identity twice leaves it alone. `tundravm.build(image, lock=, backend=, into=BUILD)`
does it from Python.

`build` ends with a report, read back from that directory, so `show BUILD` prints it too.
The fixture's production build in CI, digests cut short:

```text
built build/fixture/production/c51b7d941cf88309
  payload.efi    68.0 MiB  sha256 4484014ecb65d546  ESP 272.0 MiB, 199.8 MiB left
  qemu.raw      273.0 MiB  sha256 54675b3b4771aee6
source            kind  ref           pin           toolchain  checkout  deps
  git-codereview  git   6c2c97b0f412  6c2c97b0f412  go1.26.0   fetched   fetched
  rust-probe      git   7fdfe4315f7b  7fdfe4315f7b  1.91.0     fetched   fetched
time  checkouts 1.4s  prefetch 11.0s  mkosi 2m04s  measurements 0.9s  total 2m18s
measured  payload.efi authenticode sha384 cd9f923dfb43ae67…
  qemu   uki   RTMR1      e1cb07ce75668255…
  qemu   uki   RTMR2      4667a5f86d3fbeb7…
  qemu   disk  RTMR1      1a5af53790d7b1d4…
  qemu   disk  RTMR2      4667a5f86d3fbeb7…
  unchecked: RTMR3: outside the policy: no event schema is defined for RTMR3
```

The `authenticode` line is the UKI's identity in RTMR1's event log, and the register rows
are predictions. [What `build` prints](trust.md#what-build-prints) says which printed value
is which.

### The manifest

`manifest.json` (schema `tundravm.manifest/2`) is canonical JSON. Its `id` is the first 16
hex digits of the sha256 of the document without `id`. It binds:

| Key | Binds |
|---|---|
| `artifacts` | every file's sha256 and size |
| `uki` | the UKI's sha256, command line and SHA-384 Authenticode digest |
| `root_hash`, `rootfs` | the verity root hash (or `null`) and the layout |
| `tree` | a digest over the tree's paths, modes, contents and symlink targets |
| `lock` | the lock it was built against and the committed one it was checked against |
| `sources`, `packages`, `sbom`, `summary` | source pins and toolchains, packages, the SBOM, mkosi's summary |
| `measurements` | the state of each measurement record |
| `toolchain` | the backend, mkosi, the tools tree, tundravm, the measurement tools |
| `kind`, `profile`, `targets`, `ports` | what the build is |
| `unchecked` | what the build could not check, by name |

`observations.json` holds what is not identity: when, where, how long (per phase), what the
fetch found cached, and the reproduction.

### Reproduce, sign, verify

`--reproduce` builds again in an independent workspace (`.reproduce/`: its own sources,
mkosi state and tree) and compares every artifact. The outcome goes in
`observations.json`; a difference exits 1, and the build is still published.

`--sign KEY` signs the manifest with an SSH key into `manifest.sig` (namespace
`tundravm-manifest`). `verify` checks a build anywhere, offline:

```console
$ tundravm verify latest --require-signature --allowed-signers allowed_signers
```

It checks that the manifest is canonical, its id is its digest and its directory's name,
every artifact, input and the tree hash to what it records, nothing else is there, and the
UKI and root hash are the recorded ones. Each check is `ok`, `failed`, `missing` or
`unchecked`. `--rebuild` builds the recorded inputs again and compares; it runs the
project's code, so it is never implied.

### Show

```console
$ tundravm show
$ tundravm show latest --measurements
$ tundravm show --diff OLD NEW
```

Without a build, `show` prints the image as written, then what each profile adds; with
one (a directory, an id prefix, `latest`), its report, provenance and probe checks. `--diff`
compares two image files or two builds.

## Run

```console
$ tundravm dev
$ tundravm run --mode simulated --dev --probe
$ tundravm run status demo-1
$ tundravm run stop demo-1
```

`dev` is `build --dev` then `run --mode simulated --probe`: the probe's table, then the
console until Ctrl-C stops the instance. It reuses the latest development build while the
image, the lock and its `Local` sources are unchanged, and says why it builds;
`--no-follow` returns after the probe. `run` boots `qemu.raw` through OVMF from its ESP on
a snapshot and forwards from `127.0.0.1` every TCP port the image accepts (8745 where the
key delivery listens). `--mode simulated` boots without TDX (KVM when
usable, else TCG), verdicts `simulated`; `--mode tdx` boots TDVF with a `tdx-guest`
object, refused unless `doctor --for tdx` is ready. A production build boots `simulated`
fail-closed: no service starts, and `run status` says so; a dev build runs its services.

The instance is `.tundravm/instances/<image>-<n>/`: `instance.json`,
`events.jsonl` (one record per action, never a value), `console.log`, a sparse 16 GiB disk
per volume. `run ssh` opens a shell on a dev build with SSH; `run mode` explains the mode.

### The boot probe

A development profile, never a production one, carries `tundravm-probe.service`, its
declarations' checklist, printing `tundravm-probe: <check> ok|fail|skip <detail>` per
check; a `fail` says what to do:

| Check | Holds when |
|---|---|
| `mode` | `/run/tundravm/mode` is `simulated` or `tdx` |
| `service:`, `socket:`, `timer:` | each `Service`, socket or timer is active (or exited 0) |
| `port:` | each `expose=` port has a listener |
| `volume:` | each `Volume` is mounted, ext4, on dm-crypt |
| `egress:` | with `Egress`, a datagram no rule accepts is refused |
| `observability:` | exporters run or retry, took their configuration, block nothing, egress loaded |
| `hardening:` | a preset's `systemd-analyze security` score is under its ceiling |
| `kernel:` | no disabled symbol in `/proc/kallsyms` (`Kernel.build`), else no uncurated module |
| `host-keys` | with SSH, the host key is root-owned, read-only to others |
| `endpoint:` | the attested endpoint answers evidence for the nonce it was sent |

`--probe` waits for it (up to `--timeout`), prints the table, writes `probe.json`,
exits 1 on a failure; `run probe INSTANCE` reads a running instance's.

## Deploy

```console
$ tundravm deploy gcp --build build/attested-node/production/<id> --volume data=create:100GiB
$ tundravm deploy status attested-node-1
```

`deploy` publishes the build's disk, starts a TDX VM from it and writes a receipt; `BUILD`
is never guessed. `--plan` prints every step and cloud call in order, and runs none.
Settings come from `pyproject.toml` or the environment:

| Cloud | Table | Keys | Environment |
|---|---|---|---|
| GCP | `[tool.tundravm.gcp]` | `project`, `bucket`, `zone`, `machine_type` | `TUNDRAVM_GCP_*` |
| Azure | `[tool.tundravm.azure]` | `storage_account`, `resource_group`, `location`, `vm_size`, `gallery` | `TUNDRAVM_AZURE_*` |

Every volume the image declares needs `--volume NAME=create:SIZE` or
`--volume NAME=existing:ID`. GCP attaches each as device `tundravm-NAME`. Azure with a
volume is refused: its data disks are named by LUN, which the image cannot match. The
receipt records the VM, its address and the firewall rule the management port needs,
which tundravm does not create.

### Attest

```console
$ tundravm attest attested-node-1 --policy policies/attested-node-gcp.json --output attest-1.json
```

`attest` asks the endpoint for a quote binding a fresh nonce, the TLS session's
key, the SSH host keys and the instance public key, and checks MRTD and RTMR0 to RTMR2
against the policy. On success it writes `known_hosts`, prints the instance key and hands
over your SSH key (`--identity`). The policy is a build's `measurements.json`, or a
`tundravm.policy/2` record copied into `policies/` with the platform registers you approve
(`platform_approval`: MRTD, RTMR0).

| Verdict | Means |
|---|---|
| `trusted` | a `tdxs` validator (`--validator`) accepted the quote, the nonce is bound, and MRTD and RTMR0 to RTMR2 all matched |
| `partial` | nothing disagreed, but one of the four had no accepted value |
| `measurements-match` | the registers matched; no validator checked the signature |
| `measurements-mismatch` | a register or the nonce did not match; no validator checked |
| `simulated` | the simulator: binding checked, registers not; its key is published |
| `untrusted` | the validator rejected the quote, a validated quote did not match, or a production build's TD has the `DEBUG` attribute |

A dev build attests `simulated` by default: without TDX it takes the simulator
issuer, which production never ships; `run status` and `run mode` name it. An Azure
policy is refused: its quote measures the paravisor, not the image.

### Delivering secrets

```console
$ tundravm secrets unlock attested-node-1 data
$ tundravm secrets deliver attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

Both verify again on a session of their own and write on it only, once per name per
boot. `deliver` writes each value to `/run/tundravm/credentials/NAME`; `unlock` uses the
last `attest`'s policy and hands the passphrase (stdin, else a prompt) to the volume. Values
never go on argv, in a log, a tree or a receipt.

| Instance | Releases on |
|---|---|
| production build on TDX | `trusted` only, with no `DEBUG` attribute and no simulated evidence |
| development build on TDX | `trusted` or `measurements-match` |
| development build, simulated | `simulated` only; tundravm cannot tell a test value from a real one |
| production build, simulated | nothing: refused before contact, since its services never start |

### Upgrade and rollback

```console
$ tundravm deploy upgrade attested-node-1 --build build/attested-node/production/<id2> --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
$ tundravm deploy rollback attested-node-1 --policy policies/attested-node-gcp.json logs-client=@logs-client.pem
```

`upgrade` replaces a GCP instance with a candidate built from the new build: attest it and
deliver its credentials first, then stop the old instance, move the volumes, unlock and
wait for readiness. `rollback` is the same back to the instance it replaced. The
[trust page](trust.md#recovery) has the steps, what each refuses and the recovery rules.

## Converting

### Systemd units

```console
$ tundravm convert ./units --from systemd > image.py
```

reads a unit file or a directory of them and prints an `Image` file: one `Service` per
service unit, its drop-ins and the timer or socket that starts it folded in, every other
directive in `unit=DropIn(...)`. A `# note:` says where the unit differs from what the
image implies (`Type=exec`, the generated user, the `static` sandbox); a `# unsupported:`
names a unit that is not a service. `check` passes once the image ships each program a
unit runs: `service-program-missing` names the rest (`packages=["python3"]`).

### An mkosi tree

```console
$ tundravm convert ./old-tree --from mkosi
```

prints an `Image` file for a tree mkosi resolves, then a line per setting:
`# kept:` (the image's tree resolves it to the same value), `# mapped:` (the slot carrying
it), `# defaulted:` (the opinion that replaced it) or `# refused:` (why nothing carries
it). Nothing is dropped without a line; `--json` prints
both as one object. A tree with an executable `mkosi.version` or `mkosi.configure` is
refused unless `--allow-scripts`, which parsing runs.

## CI

`new --ci` writes `.github/workflows/tundravm.yml` with two jobs:

| Job | Runs | When |
|---|---|---|
| `check` | `show` into the job summary, `check --tree mkosi`, `lock --check` | every push to `main` and every pull request |
| `build` | `build --reproduce`, `verify latest`, uploads `build/*/production/` | after `check`, when the repository variable `TUNDRAVM_BACKEND` names a backend |

Both jobs install what `uv.lock` locks. Findings show as annotations on
the pull request. A project without a committed `build/tundravm.lock` fails `lock --check`:
lock, commit it and push again.

## When something fails

Every error prints `error [CODE]: message`, `fix:`, context and `at:`, and exits 2.
A failed check exits 1, an interrupt 130. An unknown command gets a "did you mean"; an
unknown flag, the usage line.
With `--json`, stderr gets one JSON line instead (`tundravm.error/1`). `--traceback` raises
with the Python traceback.

| Code | Usually | Do |
|---|---|---|
| `E_VALIDATION` | a malformed declaration, an unknown profile | `tundravm check` lists everything |
| `E_LINT` | `build` refused error-level findings | `tundravm check` |
| `E_LOCKFILE` | `build` met a stale lock, or a production build none (`lock-missing`) | `tundravm lock --check`, then `tundravm lock` |
| `E_SOURCE` | a source did not resolve, or a checkout changed | `tundravm build --clean-cache` |
| `E_POLICY` | `build --offline` met a source the workspace never fetched, or a tree that builds with the network | build once online, then `--offline` |
| `E_BACKEND_EXECUTION` | mkosi failed | the error names mkosi's log; `build -v` echoes it; `doctor --for build` |
| `E_STATE` | no build, instance or host tool where one was expected | the hint names the command |
| `E_ARTIFACT_CHANGED` | an artifact's bytes changed | `tundravm verify BUILD` names the file |
| `E_REPRODUCIBILITY` | two builds that should match did not | compare `observations.json` |
| `E_MEASUREMENT` | an artifact that is not a UKI or a disk booting one | rebuild it with `tundravm build` |
| `E_ATTESTATION` | the endpoint, issuer or validator failed, or evidence was not bound to the session | `run logs`, `journalctl -u tundravm-endpoint -u tdxs` |
| `E_DEPLOYMENT` | a missing tool, no TDX for `--mode tdx`, a refused upgrade | the hint names it |

A `check` finding is `SEVERITY CODE [PROFILES] SUBJECT: message` and its hint; `[image]`
marks one about the whole image, and profiles sharing a finding share its line. The
report ends with the command that explains its codes, a component's included (here the
example's `Nethermind`):

```console
$ tundravm check --explain nethermind-volatile-state
```

```text
nethermind-volatile-state: warning
    declared by Nethermind in components/nethermind.py
    Nethermind keeps its database on the volatile root, so it resyncs at every boot.
    fix: Pass state=volume.directory('nethermind') on a Volume.
```

The [lint reference](reference/lint.md) lists tundravm's and the shipped components'
codes, what each checks and how to fix it. `--json` and the GitHub annotations (in GitHub
Actions) keep one finding per profile and subject.
