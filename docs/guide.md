# The tundravm guide

tundravm is mkosi made opinionated for confidential VMs. You write one Python `Image`.
tundravm compiles it to one mkosi 27.1 tree, builds that tree with the mkosi it pins, and
gives you an immutable build directory with a manifest you can sign. Then it runs, deploys and
attests the build. The choices are made for you: Debian trixie, systemd, a measured UKI,
Intel TDX. Each default below has one escape hatch, and the hatch is a typed value, not a
string.

This guide is task-ordered. The [command reference](reference/cli.md), the
[API reference](reference/api.md) and the [lint reference](reference/lint.md) are
generated from the code. The [trust page](trust.md) says what the image defends against
and how to recover.

## What is proven today

The work follows the gates of the v2 plan. Gates 1, 4 and 5 are implemented, and so is
gate 3's `run --mode` separation. Gate 2 and the rest of gate 3 are not.

| Area | State |
|---|---|
| The mkosi tree, `payload.efi`, `qemu.raw`, `azure.vhd`, `gcp.tar.gz`, both root layouts | built in CI from a fixture; `qemu.raw` boots through its ESP and from dm-verity; two builds of the initramfs layout give identical digests |
| Building an `Image` in CI | needs the `TUNDRA_TOOLS_TOKEN` secret (see [CI](#ci)): every image builds tundra-tools, a private repository the CI token cannot read |
| `Image`, `Service`, `Component`, `Profile` and the runtime slots | compile and resolve under mkosi in the test suite |
| The twelve commands, build directories, signed manifests | `verify` checked a signed manifest from a second checkout |
| `run --mode simulated` | unit-tested against a fake QEMU; CI runs the fixture's dev build with it and fetches its page |
| The fail-closed production profile | CI boots the production fixture without TDX: `tdx-evidence.service` refuses and its services never start |
| A real TDX quote, raw-disk measurements matched against it (gate 2) | not done |
| The attested endpoint, channel-bound delivery, `OperatorKey` unlock over it, `BrokerKey` (gate 3) | not done in tundra-tools |
| `deploy` to GCP or Azure, `attest` against a real quote | unit-tested against fake `gcloud`, `az` and SSH only |

Until gate 3, `attest` and `secrets` reach an instance over an SSH session that is not bound
to the quote. Every command that uses it prints `unverified first contact`.

## A project

### Create one

tundravm is not on PyPI. `uvx` runs it from git, and `new` writes a project that pins the
commit it ran from:

```console
$ uvx --from git+https://github.com/Hyodar/tundra tundravm new demo --ci
$ cd demo
$ uv sync
$ uv run tundravm lock
$ uv run tundravm build --tree-only mkosi
$ uv run pytest
```

`lock` pins every source, tundra-tools included, so it needs read access to that
repository (see [the shipped library](#the-shipped-library)). The generated tests and the
CI's `check --tree mkosi` compare the image with the committed `mkosi/` tree, which depends
on the lock: write the tree after locking and commit both. `new` prints this loop.

`new NAME` writes:

| File | Holds |
|---|---|
| `NAME.py` | the image, from the `service` template (or `--template prover`) |
| `app/` | with `service`: the Go program it builds through `Local("app")` |
| `tests/test_NAME.py` | lint, compile and committed-tree tests |
| `pyproject.toml` | tundravm from git at a commit, pytest, and `[tool.tundravm]` |
| `README.md` | the commands of the loop |
| `.gitignore` | ignores `/.tundravm/` and `/build/*`, keeps `build/tundravm.lock` |
| `policies/README.md` | with `--template prover`: where approved verifier policies go |
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

`local` runs mkosi 27.1 in-process, in a child forked by mkosi's own `fork_and_wait`. It
never looks for mkosi on `PATH`. Every other build tool comes from mkosi's tools tree,
pinned to the same Debian snapshot. When the kernel refuses unprivileged user namespaces
(Ubuntu 24.04 does by default), mkosi runs under `sudo`.

`lima` on Apple Silicon emulates the x86-64 guest with QEMU TCG. The bytes are the same
and the build is several times slower. A build never switches backend on failure.

`doctor` says what this host can do, one row per capability, each failure with its fix.
`build:go` and `build:rustup` block only when the image (`IMAGE`, else the project's) has
a Go source, or a Cargo source that pins a Rust toolchain (one not checked out yet counts);
otherwise they say `optional: not needed by this image`, and without an image a missing
tool is `optional`. Neither fails `doctor` then:

```console
$ tundravm doctor
$ tundravm doctor --for build
```

```text
build:x86-64     ready (mkosi 27.1, sudo, which may ask for a password, as this host has no unprivileged user namespaces)
build:go         ready (go1.24.4, which fetches each Go source's modules and the toolchain its go.mod names for the offline build (every image's Tdxs is one))
build:rustup     optional: not needed by this image
run:simulated    blocked: qemu-system-x86_64 not found: install qemu-system-x86
run:tdx          unavailable: no Intel TDX host: kvm_intel tdx is not enabled
deploy:gcp       blocked: gcloud not found: install the Google Cloud CLI
deploy:azure     blocked: az not found: install the Azure CLI
verify:qemu      ready (built-in TDX replay 1.0+attest-12f1e29.dstack-mr-gcp-b16e08b)
verify:gcp       ready (built-in replay, GCP firmware table dstack-mr-gcp@b16e08b)
verify:azure     unsupported: Azure evidence is vTPM-bound: no TDX register can be predicted
```

## The image

An image file binds an `Image` to the name `image`. This is a complete one:

```python
from tundravm import File, Image, Service
from tundravm.ports import TCP

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)
```

`Image` is a frozen dataclass. Every slot is optional and takes small typed values.
`tundravm show` prints what the image resolves to, profile by profile, and where each
declaration came from.

### What is implied

| Implied | Escape hatch |
|---|---|
| Debian trixie from snapshot `20251113T083151Z` | `base=Debian("trixie", snapshot="...")`; another release is untested |
| a measured UKI with a curated module set: `tdx_guest`, virtio, NVMe, Hyper-V, gVNIC | `kernel=Kernel.default(modules=[...])` to keep more, `kernel=Kernel.build(...)` to compile one |
| NTS time from two authenticated servers, a volatile journal, sysctl hardening, a static resolver, no apt timers, no console getty (`TeeBase`) | pass `TeeBase(...)` in `components=`, or remove a piece in a profile |
| DHCP on `en*` and `eth*` (`Network`) | `network=Network(dhcp=False, static=StaticAddress(...))` |
| an nftables firewall that drops inbound traffic except exposed ports and the management port 8745 (`Firewall`) | `expose=` opens a port; `network=Network(egress=Egress(...))` filters outbound |
| one generated system user per service, the `static` sandbox, `Restart=on-failure` | `Service(user=, hardening=, unit=DropIn(...))` |
| the evidence service `tdxs` from tundra-tools, socket-activated on `/var/tdxs.sock` (`Tdxs`) | `components=[Tdxs(source=Git(url, ref))]` for a mirror |
| `tdx-evidence.service`, which every service requires: without TDX a production profile starts none, a development one runs simulated ([trust](trust.md#threat-model)) | none: a dev profile is the way to run without TDX |
| for each cloud in `targets=`, its provisioning glue, inert elsewhere through `ConditionVirtualization=`, so one payload boots on every target | `targets=` |
| a raw bootable disk for QEMU | `targets=("qemu", "azure", "gcp")` adds the cloud disks |

The four baseline components (`TeeBase`, `Network`, `Firewall`, `Tdxs`) are in every image.
Passing one of them in `components=` replaces the default one.

### The slots

| Slot | Takes | From |
|---|---|---|
| `base` | `Debian(release, snapshot=, efi_stub=)` | `tundravm` |
| `packages` | package names, or `Package(name, backports=True)`: runtime packages only | `tundravm` |
| `files` | `File.text(path, text, mode=, owner=)`, `File.copy(src, dst)`, `Tree(src, dst, exclude=)` | `tundravm` |
| `services` | `Service` | `tundravm` |
| `components` | `Component` instances | `tundravm.components` |
| `volumes` | `Volume`: see [Volumes](#volumes) | `tundravm.storage` |
| `secrets` | `Secret`: see [Secrets](#secrets) | `tundravm.secrets` |
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

`File.copy` and `Tree` read host files when the image lowers. Build their paths from
`Path(__file__).parent`, so the result does not depend on the working directory. A file is
root-owned unless `owner=` says otherwise; systemd-tmpfiles applies the owner at
boot. Build dependencies are not image packages: they belong to the `GoBinary`,
`CargoBinary` or `Build` that needs them.

`cmdline=` on either `Kernel` appends parameters to the UKI's measured command line, after
the ones the image writes itself: `kernel=Kernel.default(cmdline=["nospectre_v2"])`. None is
added by default (flashbots' `nospectre_v2` is a choice to make, not one to copy). Each
entry is one parameter, `name` or `name=value`. The ones tundravm sets are refused, each
with what sets it: `root=`, `roothash=`, `usrhash=`, `systemd.verity*` and
`systemd.volatile=` follow from `rootfs=`, the image boots systemd into `minimal.target`
(`init=`, `rdinit=`, `systemd.unit=`), and `run` passes `console=` at boot. The parameters
are part of the image: they move the recipe digest the lockfile records, and the manifest
records the UKI's command line, which `show --diff` compares.

### Imports

Every public name has one home, so an image file's imports say which parts of tundravm it
uses:

| Namespace | Holds |
|---|---|
| `tundravm` | `Image`, `Profile`, `Service`, the contents (`Package`, `File`, `Tree`, `User`, `Group`), the foundation (`Debian`, `Kernel`, `RootFS`, `Mkosi`), and `load`, `lower`, `compile`, `bake`, `doctor` |
| `tundravm.ports` | `TCP`, `UDP`, `AttestedTLS`, `TLS` |
| `tundravm.systemd` | what `Service` keywords take: `Hardening`, `Resources`, `Shutdown`, `Notify`, `TcpProbe`, `HttpProbe`, `Every`, `Calendar`, `Listen`, `DropIn` |
| `tundravm.components` | `Component`, its exports (`Endpoint`, `ServiceRef`, `ServiceReady`, `ComponentReady`), `Resolved` and `Diagnostic` for `check()`, and the [shipped library](#the-shipped-library) with `StaticAddress` and `Egress` |
| `tundravm.storage` | `Volume`, `VolumeDirectory` and the unlock policies `OperatorKey`, `TpmSeal`, `BrokerKey` |
| `tundravm.secrets` | `Secret`, `Credential` |
| `tundravm.sources` | `GoBinary`, `CargoBinary`, `Build`, `Install`, `Local`, `Git`, `Http` |
| `tundravm.errors` | the exceptions, each with a stable code |
| `tundravm.testing` | see [Testing a project](#testing-a-project) |

There are no aliases: importing `TCP` from the root is an `ImportError`. The
[API reference](reference/api.md) has one section per namespace.

## Services

A `Service` is an argv, never a command line:

```python
from tundravm import Service
from tundravm.ports import TCP
from tundravm.systemd import HttpProbe, Resources, Shutdown

api = Service(
    "api",
    ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
    expose=[TCP(8080)],
    readiness=HttpProbe("http://127.0.0.1:8080/"),
    resources=Resources(memory_max="512M", tasks_max=64),
    shutdown=Shutdown(timeout="30s"),
    environment={"LANG": "C.UTF-8"},
)
```

What a bare `Service(name, argv)` gets: `Type=exec`, `Restart=on-failure` with
`RestartSec=5s`, a system user named after the service, `Hardening.preset("static")`, and
`WantedBy=minimal.target`. Each argv element is quoted the way systemd parses it. The one
visible exception for a shell is `["/bin/sh", "-eu", "-c", script]`.

### Reaching a service

`expose=[TCP(8080)]` opens that port in the firewall, and nothing else opens a port. It
does not protect the requests: use an [ingress](#ingress) for that. `UDP(53)` works the
same way.

### Readiness and order

`requires=[other.ready]` is a real dependency: `Requires=` and `After=` on whatever proves
the other service ready. `After=` alone is never generated.

| Readiness | What it does |
|---|---|
| none | ready once the program is exec'd |
| `Notify()` | `Type=notify`: ready when the program sends `READY=1` |
| `TcpProbe(port)` | a bounded probe unit polls the port; installs nothing |
| `HttpProbe(url)` | a bounded probe unit waits for a 2xx; installs `curl` |

A probe is its own oneshot unit (`app-ready.service`), so a dependent waits for an answer,
not for an exec.

### The sandbox

`hardening=Hardening.preset(NAME)` picks a sandbox per runtime class. Each preset is the
strict sandbox minus what that runtime cannot live without:

| Preset | Differs from `static` | Because |
|---|---|---|
| `static` | nothing | a static binary needs nothing more |
| `jit`, `node`, `python` | no `MemoryDenyWriteExecute=` | a JIT maps pages writable, then executable; CPython needs it for libffi |
| `dotnet` | the same, and `@resources` syscalls allowed | the .NET runtime sizes itself with `sched_setaffinity` and `setrlimit` |

The `static` sandbox is `ProtectSystem=strict`, private `/tmp` and devices, no new
privileges, an empty capability bounding set, the kernel and namespace protections, the
`@system-service` syscall filter and `UMask=0077`. A deviation is explicit:
`Hardening.preset("static", devices=["/dev/tdx_guest"])` replaces `PrivateDevices=yes`
with an allow list. `Hardening.relaxed()` and `Hardening.none()` exist for programs you
cannot fix.

### Timers, sockets and the rest

| Need | Write |
|---|---|
| run periodically | `timer=Every("15m")` or `timer=Calendar("daily")` |
| socket activation | `socket=Listen(8080)` or `Listen("/run/app.sock")` |
| persistent state | `state=volume.directory("app")`; a bare name is a volatile `StateDirectory=` |
| a delivered secret | `credentials=[secret]` |
| a shared group | `groups=["tdx"]` |
| anything systemd knows and no field covers | `unit=DropIn(...)` |

A timer runs the service as a scheduled oneshot and keeps every other keyword;
`socket=`, `readiness=`, `expose=`, `credentials=` and a volume `state=` belong to a
long-lived service, so a timer refuses them. These values come from `tundravm.systemd`. A built binary is a valid first argv element:
`GoBinary`, `CargoBinary` and `Build` (from `tundravm.sources`) pull
their build and install into the image, and `binary.executable` is the installed path. Go
builds with `-trimpath -buildvcs=false` and `CGO_ENABLED=0`; Rust with
`cargo auditable build --frozen` and reproducible `rustflags`, so the SBOM can read the crate
graph from the binary.

### Sources

A build takes one source, which `tundravm lock` pins: `Local("app")`, a directory beside
the image file, by the digest of its files; `Git(url, ref)` by commit; `Http(url, sha256=)`
by sha256. Your own program is a `Local` source:

<!-- fragment -->
```python
from tundravm import Image, Service
from tundravm.ports import TCP
from tundravm.sources import GoBinary, Local

app = GoBinary("app", Local("app"), package=".")

image = Image("demo", services=[Service("app", [app], expose=[TCP(8080)])])
```

The path stays inside the project (the nearest `pyproject.toml`). The digest covers each
file's path, sha256 and exec bit, and symlink targets, of what `git ls-files --cached
--others --exclude-per-directory=.gitignore` lists (every file outside a work tree).
`build` copies those files like a checkout. An edit is `local-changed`; `build --dev`
re-pins it, a production build refuses until `tundravm lock`.

## Components

A `Component` is a class that groups declarations and exports typed values. This one
serves a page with busybox; the `service` template's `App` builds `app/` instead:

```python
from collections.abc import Iterator

from tundravm import File, Image, Package, Service
from tundravm.components import Component, Diagnostic, Endpoint, Resolved, Rule
from tundravm.ports import TCP
from tundravm.systemd import TcpProbe


class App(Component):
    """A page busybox httpd serves, its readiness, and a lint rule."""

    port: int = 8080

    @property
    def http(self) -> Endpoint:
        return Endpoint("127.0.0.1", self.port, ready=self.service("app").ready)

    def compose(self) -> Iterator[Package | File | Service]:
        yield Package("busybox")
        yield File.text("/srv/www/index.html", "<h1>app</h1>\n")
        yield Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", str(self.port), "-h", "/srv/www"],
            expose=[TCP(self.port)],
            readiness=TcpProbe(self.port),
        )

    rules = (Rule("app-privileged-port", "error", "Ports below 1024 need root.", "Use 8080."),)

    def check(self, resolved: Resolved) -> tuple[Diagnostic, ...]:
        if self.port >= 1024:
            return ()
        return (Diagnostic("app-privileged-port", "error", "app cannot bind below 1024"),)


image = Image("site", components=[App()])
```

The rules:

- Fields are configuration. The subclass becomes a frozen, keyword-only dataclass, so two
  components with the same fields are equal.
- `compose()` yields declarations and nested components. They resolve before validation,
  so a component never hides in `services=`.
- Exports are properties returning an `Endpoint`, a `ServiceRef` (`self.service("app")`) or
  a `ServiceReady`. A caller writes `requires=[app.http]` or puts `app.http` in an argv,
  where it renders as its URL. Nobody outside names a unit, a port or a path inside it.
- `check(resolved)` returns diagnostics. It becomes a lint rule that `tundravm check` runs,
  once per profile: `resolved.profile` names it, and a finding without `profile=` is that
  profile's.
- `class X(Component, build_network=True)` says its builds fetch dependencies, so the
  tree builds with `WithNetwork=yes`.

Each code `check()` reports is declared in `rules`, as `App` declares
`app-privileged-port`: `--explain` prints it, and an undeclared code is a
`rule-undeclared` error. A code reported at several levels lists them, default first, with
a `when=` phrase per other.

### The shipped library

`tundravm.components`:

| Component | What it is | Exports |
|---|---|---|
| `TeeBase` | the confidential-VM baseline (implied) | none |
| `Network` | systemd-networkd, DHCP or a static address (implied) | `online` |
| `Firewall` | nftables, inbound drop by default (implied) | none |
| `Tdxs` | the evidence service (implied) | `EVIDENCE`, `socket`, `group` |
| `SshAccess` | root SSH by public key, keys baked or delivered | `port` |
| `DevAccess` | debug access, only in the `dev` profile | none |
| `Observability` | journal and metrics export | `service("journal-export")`, `service("metrics-export")` |
| `Nethermind` | Nethermind built with the .NET 10 SDK, `dotnet` preset | `rpc`, `ws`, `engine_api` |
| `TaikoClient` | the taiko-client driver, built with Go | none |
| `Raiko` | the Raiko TDX prover, built with Cargo | `http` |

A service that asks for quotes joins the `tdx` group and requires `EVIDENCE`. The evidence
service depends on nothing on a volume or a secret: the evidence is what unlocks them.

`tundravm.components.Tdxs` builds from `https://github.com/Hyodar/tundra-tools.git`, which
is private. Every image implies it, so `lock` and `build` need read access to that
repository. `Tdxs(source=Git(mirror, ref))` points every tundra-tools build at a mirror.

The surge prover in `examples/surge-tdx-prover/image.py` is the three workloads wired
through their exports:

```python
from tundravm import Image, RootFS
from tundravm.components import DevAccess, Nethermind, Raiko, TaikoClient
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume

data = Volume("data", mount="/persistent", unlock=OperatorKey())
jwt = Secret("jwt")
chain = Secret("taiko-chain")
config = Secret("raiko-config")

execution = Nethermind(jwt=jwt, state=data.directory("nethermind"))
driver = TaikoClient(engine=execution.engine_api, rpc=execution.rpc, jwt=jwt, chain=chain)
prover = Raiko(config=config, l2_rpc=execution.rpc, state=data.directory("raiko"))

image = Image(
    "surge-tdx-prover",
    components=[execution, driver, prover],
    volumes=[data],
    secrets=[jwt, chain, config],
    ingress=[AttestedTLS(443, upstream=prover.http)],
    rootfs=RootFS.verity(),
    targets=("qemu", "azure", "gcp"),
    dev=DevAccess(password="tdx"),
)
```

## Profiles

The image itself is the `production` profile. `dev=DevAccess(...)` adds a non-production
`dev` profile: debugging tools, a serial console and root SSH by public key only. Anything
else is a `Profile`:

```python
from tundravm import File, Image, Profile, Service
from tundravm.ports import TCP

web = Service(
    "web",
    ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
    expose=[TCP(8080)],
)
worker = Service("worker", ["/usr/bin/busybox", "sleep", "infinity"])
motd = File.text("/etc/motd", "production\n")

image = Image(
    "chain",
    packages=["busybox"],
    files=[motd],
    services=[web, worker],
    profiles={
        "staging": Profile(
            add=["strace"],
            changes=[motd.ref.edit(content="staging\n"), web.ref.edit(environment={"STAGE": "staging"})],
        ),
        "bench": Profile(
            extends="staging", add=["fio"], changes=[worker.ref.remove()], production=False
        ),
    },
)
```

| Field | Meaning |
|---|---|
| `extends` | the profile it starts from; `None` is `production` |
| `add` | more content: packages, files, services, components |
| `changes` | `value.ref.edit(...)` or `value.ref.remove()` on something the image declares |
| `production` | `False` makes its builds development builds |

A change names a declaration that exists, or the image does not resolve. Removing a
service removes its unit, its user, its firewall rule and the dependencies nothing else
uses. A component's service is `component.service("name").ref`. An edit takes the keywords
of the value's own constructor, and the edited value is checked like a new one. A
`Service` edit sets `argv`, `requires`, `state`, `expose`, `resources`, `shutdown`,
`hardening`, `timer`, `socket`, `user`, `groups`, `working_directory`, `environment`,
`logs` or `runtime`; a `File` edit `content` or `mode`; a `Tree` edit `src` or `exclude`.
What decides other units (`readiness=`, `credentials=`, `unit=`, a timer turned on or off,
another built binary, a state moved onto a volume) is refused: remove the value and add
the new one in `add=`.

How profiles lower: tundravm resolves every profile's whole image, writes what all of them
build once in the shared `payload/`, and puts what each builds beyond that in
`payload/mkosi.profiles/<name>/`. A declaration one profile removes moves into the
overlays of the profiles that keep it, `production`'s included. So what a profile builds does
not depend on which profiles a compile selects. `build --profile staging` is
`mkosi --profile=staging`, and `build --dev` is `mkosi --profile=dev`.

## Volumes, secrets and ingress

These are the runtime contracts of a confidential service. The prover template is the
shortest image that uses all three:

```python
from tundravm import Image, RootFS
from tundravm.components import DevAccess, Raiko
from tundravm.ports import AttestedTLS
from tundravm.secrets import Secret
from tundravm.storage import OperatorKey, Volume

data = Volume("data", mount="/persistent", unlock=OperatorKey())
config = Secret("raiko-config", rotation="restart")
raiko = Raiko(config=config, state=data.directory("raiko"), listen="127.0.0.1:8080")

image = Image(
    "prover",
    components=[raiko],
    volumes=[data],
    secrets=[config],
    ingress=[AttestedTLS(443, upstream=raiko.http)],
    rootfs=RootFS.verity(),
    dev=DevAccess(password="tdx"),
)
```

### Volumes

`Volume(name, mount=, unlock=)` is a LUKS2 volume with ext4 inside. The image says its
name, its mount point and how it unlocks. Its size and the disk are deployment
configuration (`deploy --volume data=create:100GiB`). The image finds the disk by name
through `/dev/disk/by-id/*-tundravm-<name>`, never by probe order. It formats a volume only
while it carries no initialisation token, so an unlock failure is never permission to
format.

`unlock=` is the trust decision and has no default:

| Unlock | Means | State |
|---|---|---|
| `OperatorKey()` | an operator sends a passphrase each boot with `secrets unlock` | works over today's unverified SSH channel; the attested path is gate 3 |
| `TpmSeal(platform=, pcrs=, recovery="none")` | a random key sealed to one stated vTPM: `azure`, `gcp` or `swtpm` | `key-gen` seals without a PCR policy today; `pcrs` is recorded, enforced in gate 3 |
| `BrokerKey(url, audience=)` | a key broker releases the key after attestation | the pull client is gate 3; until then the image waits as for an operator |

`TpmSeal` is refused for `targets=` that boot with another vTPM, since one payload boots on
every target and a key sealed on one platform does not open on another.

`data.directory("raiko")` is a directory on the volume. A service binds it with
`state=`, and the image creates it, owned by that service, once the volume is open. The
service waits for it. `directory(name, group=)` shares it through a group.

### Secrets

`Secret(name)` is a value the image needs and never contains. An operator delivers it
after attestation:

```console
$ tundravm secrets deliver prover-1 --policy policies/prover-gcp.json raiko-config=@raiko.json
```

It lands in `/run/tundravm/credentials/<name>`, root-owned tmpfs, mode 0600. A service
binds it with `credentials=[secret]`, which renders
`LoadCredential=<name>:/run/tundravm/credentials/<name>`. systemd copies it into the unit's
read-only `$CREDENTIALS_DIRECTORY` at start, and `secret.path` is the argv word for it.

| Option | Meaning |
|---|---|
| `required=True` | every bound service waits for it (default) |
| `required=False` | the service starts without it and reads an empty credential |
| `rotation="restart"` | a new delivery restarts the bound units (default) |
| `rotation="none"` | bound units keep the old value until they restart |
| `source=BrokerKey(...)` | a broker delivers it instead of an operator (gate 3) |

There is no `reload` rotation: systemd's credential copy is per start.

### Ingress

`AttestedTLS(443, upstream=raiko.http)` is the confidential way in. It opens 443 and
nothing else. The upstream must listen on the loopback and expose no port of its own, or
the image is refused, so the ingress is the only path to it. Today the relay is `socat`'s
OpenSSL listener with a key pair generated in the guest at every start, as its own user
with only `CAP_NET_BIND_SERVICE`. The certificate is self-signed per boot. Binding its key
into the quote's `report_data` is gate 3; until then a client pins it on first contact.

`TLS(port, upstream=, certificate=secret)` is ordinary HTTPS with a certificate you deliver
as a secret. Nothing about it is attested.

### The root filesystem

| Layout | Use |
|---|---|
| `RootFS.initramfs()` | the OS in the UKI's initrd: small services, one file to measure (default) |
| `RootFS.verity()` | a read-only erofs root with a dm-verity hash partition beside the ESP, the root hash on the UKI command line, a volatile overlay on top |

Choose verity when the payload is large. The ESP is a fixed 128 MiB FAT32, and a UKI over
about 125 MiB fails the disk build. The layout is recorded in the manifest and never
switched by a size threshold.

### Network and observability

`network=Network(egress=Egress(ports=[TCP(443)], hosts=["10.0.0.0/8"]))` filters outbound
traffic. Without `Egress`, outbound traffic is not filtered. An image that resolves names
or syncs time must list those destinations.

`observability=Observability(logs=JournalExport(url, credential=), metrics=PrometheusRemoteWrite(url, credential=))`
exports the volatile journal and node metrics. Their credentials are secrets the image
declares. An `http://` sink is a lint warning: it sees what the guest logs in the clear.

### The control plane today

An image with a volume or a secret runs tundra-tools' `secret-delivery` on the management
port 8745. It accepts one SSH public key per boot as an HTTP POST, and root SSH then
accepts only that key. `attest`, `secrets deliver` and `secrets unlock` use that session.
Nothing binds it to the quote: this is unverified first contact, and every command says
so. The image records every volume, secret and ingress it needs in
`/etc/tundravm/runtime.json`.

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
mkosi.repart/00-esp.conf       a fixed 128M FAT32 ESP holding the UKI
mkosi.images/payload/mkosi.conf  Include=../../payload
payload/mkosi.conf             the OS: Format=uki, packages, kernel modules
payload/mkosi.conf.d/          one file per concern: 10-platform, 20-tee-base, 30-tdxs, ...
payload/mkosi.extra/           the files and the generated units
payload/mkosi.skeleton/        minimal.target, which services are enabled into
payload/mkosi.build.d/  mkosi.postinst.d/  mkosi.finalize.d/
payload/mkosi.profiles/dev/    the dev overlay (public keys only)
```

How to read it as an mkosi user:

- The OS is the `payload` subimage. mkosi builds subimages first, so `payload.efi` exists
  when the main image copies it into the ESP with `ExtraTrees=%O/payload.efi:...`.
- The main image is the QEMU raw disk, so `mkosi vm` boots it.
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
verbatim settings and scripts at an mkosi stage (`prepare`, `build`, `postinst`,
`finalize`, `postoutput`). The manifest records them. Settings the compiler writes itself
or that would contradict a guarantee are refused at construction, each with the slot that
sets it: `KernelCommandLine`, `Packages`, `ExtraTrees`, `ConfigureScripts`, `Environment`,
`Bootable`, `Format`, `Profiles`, `Dependencies`.

### Converting an mkosi tree

```console
$ tundravm convert ./old-tree --from mkosi
```

prints an `Image` file for a tree mkosi itself resolves, with every setting it cannot carry
listed as `# unsupported:`. Save it beside the tree: its `Tree(...)` and script paths are
relative to the file. A tree tundravm wrote (`mkosi.images/payload/`) converts its payload;
the QEMU disk, the cloud wrappers and the payload's trees and scripts are what tundravm
generates, so the notes name them and nothing is copied; the wrappers become `targets=`.
A tree with an executable `mkosi.version` or `mkosi.configure` is refused unless
`--allow-scripts`, since parsing would run it.

## The lock

```console
$ tundravm lock
$ tundravm lock --update tdxs
$ tundravm lock --check
```

`lock` writes `build/tundravm.lock`: a digest per section of the resolved image, a commit,
sha256 or content digest per source, and the toolchain (mkosi and its commit, the tools
tree's distribution, release and snapshot, tundravm, the measurement tools). Commit it.

- Pins already locked stay while their source is unchanged. `--update NAME` resolves the
  named inputs again; no name resolves all of them.
- `--check` never writes and never touches the network. It prints the drift and exits 1;
  a missing lockfile is drift too.
  A profile's sections are `profiles.<name>.<key>`, and `check` reports their drift as
  that profile's finding: `error lock-changed [dev, production] files: files changed since
  the lock: ~/srv/www/index.html`.
- A lockfile an older tundravm wrote is refused, never migrated: run `tundravm lock` again.
- A production build refuses a missing (`lock-missing`) or stale lock; a development
  build writes a missing one and re-pins changed `Local` sources.
- `build --offline` builds only what the lock pins.

`check` reports a `source-unpinned` warning for each source the lock does not pin yet: one
line per source naming the profiles that build it, and the hint once after them.

The build sandbox never fetches a pinned source. `build` checks each one out on the host, as
you, into `build/<image>/.work/`, verifies a reused checkout against its pin, and mounts the
checkouts into the build. When the host has the toolchain, Go, Cargo and .NET dependencies
are prefetched there too, so `build --offline` can run on a machine without network once a
connected build filled the workspace. A modified checkout fails with `E_SOURCE`;
`build --clean-cache` starts the workspace afresh.

A Go source builds with the toolchain its `go.mod` names: the `toolchain` line, else the
`go` line (`go 1.25.0` is `go1.25.0`, `go 1.22` is `go1.22.0`). The host's `go`, 1.21 or
newer and of any version, fetches that release from the Go module proxy with the modules,
and the offline build runs it from the same cache: Debian's `golang` is only the bootstrap
that switches to it. A release the host cannot fetch fails the build with `E_SOURCE`,
naming the release and its module. A `go.mod` older than Go 1.21, which publishes no
toolchain, builds with Debian's `go`. The manifest records each Go source's toolchain with
its checksum, and `show BUILD` prints it. Without a host `go` 1.21 or newer, `build` of an
image with a Go source stops before it fetches anything; `doctor --for build` checks it.

A Cargo source builds with the toolchain its `rust-toolchain.toml` (or the legacy
`rust-toolchain`) pins, looked up as `rustup` does from the build's directory up to the
checkout. The host's `rustup` installs that channel, with the components and targets the
file lists and `--profile minimal` unless it names another, into the workspace; that
toolchain's `cargo` fetches the crates, and the offline build runs its own `cargo` and
`rustc`, with Debian's compiler and linker underneath. The channel must be pinned: a
release (`1.91.0`) or a dated channel (`nightly-2026-09-01`); `stable`, `1.91` or a bare
`nightly` fail the build with `E_SOURCE`, since they move with each release. To build
with another Rust, build a revision that pins it. A source without a toolchain file
builds with Debian's `cargo`. The manifest records each Cargo source's toolchain (its
channel and what its `rustc -vV` says, or `debian`), and `show BUILD` prints it. A pinned
source on a host without `rustup` stops before any toolchain runs; `doctor --for build`
checks it.

## Build

```console
$ tundravm build
$ tundravm build --target gcp --reproduce --sign ~/.ssh/release-key
```

`build` builds one profile: `production` by default, `--profile P` or `--dev`. A
production build is always a locked build: it builds every source at the commit or
sha256 `build/tundravm.lock` pins, so a second builder gets the same bytes, and the
manifest records those pins. Without the lockfile it refuses with `lock-missing`: run
`tundravm lock` and commit the lockfile first. A development build writes a missing
lock itself. `build` writes:

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

The directory is assembled beside its final place and published with one rename. Every
file is read-only, and nothing writes into a published directory again. Building the same
identity twice leaves the existing directory alone.

`build` ends with a report, read back from that directory, so `show BUILD` prints it too.
The fixture's production build in CI, digests cut short:

```text
built build/fixture/production/c51b7d941cf88309
  payload.efi    68.0 MiB  sha256 4484014ecb65d546  ESP 128.0 MiB, 58.1 MiB left
  qemu.raw      129.0 MiB  sha256 54675b3b4771aee6
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

Every disk boots the UKI from a fixed 128 MiB ESP, which holds a UKI of up to 126 MiB. A
larger one fails the build with both sizes and the fix: `RootFS.verity()` keeps the OS
out of the UKI. The `authenticode` line is the UKI's identity in RTMR1's event log, and the
register rows are predictions. [What `build` prints](trust.md#what-build-prints) says which
printed value is which.

### The manifest

`manifest.json` (schema `tundravm.manifest/1`) is canonical JSON. Its `id` is the first 16
hex digits of the sha256 of the document without `id`. It binds:

| Key | Binds |
|---|---|
| `artifacts` | every file's sha256 and size |
| `uki` | the UKI's sha256, command line and SHA-384 Authenticode digest (`authenticode_sha384`, what RTMR1's event log records for it) |
| `root_hash`, `rootfs` | the verity root hash (or `null`) and the layout |
| `tree` | a digest over the tree's paths, modes, contents and symlink targets |
| `lock` | the lock it was built against and the committed one it was checked against |
| `sources`, `packages`, `sbom`, `summary` | the source pins and each Go and Cargo source's toolchain, the package manifest, the SBOM, mkosi's resolved summary |
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

Without a build, `show` prints the image as it resolves: its name and file on the first
line, then the production profile and the others as declared (`--profile P` and `--dev`
pick them). With one (a directory, an id
prefix or `latest`), it prints the build's report (as `build` printed it; `--json` puts
it under `report`), then the manifest's provenance and unchecked guarantees; `--units`,
`--tree` and `--measurements` narrow it. `--diff` compares two builds:
packages, files, units, boot payload, measurements and policy coverage.

## Run

```console
$ tundravm run --mode simulated --dev --detach
$ tundravm run status demo-1
$ tundravm run logs demo-1
$ tundravm run stop demo-1
```

`run` boots `qemu.raw` through OVMF from its ESP, on a snapshot, so the build is never
written. It forwards from `127.0.0.1` every TCP port the image accepts and the management
port 8745, on the same port when it is free and unprivileged. `--mode` is required:

| Mode | Boots | Evidence |
|---|---|---|
| `simulated` | no TDX; KVM when `/dev/kvm` is usable, TCG otherwise | verdicts are `simulated` |
| `tdx` | TDVF with a `tdx-guest` object | real quotes; refused unless `doctor --for tdx` is ready |

A production build boots in `simulated` fail-closed: `tdx-evidence.service` finds no TDX
and refuses, none of its services start, and `run status` says `fail-closed`. The console
still shows the boot. Run the dev build (`--dev`) to use the services without TDX.

The instance is `<image>-<n>` under `.tundravm/instances/`: `instance.json` (the build,
mode, QEMU argv and forwarded ports), `events.jsonl` (one record per action, never a
value) and `console.log`. Each declared volume gets a fresh sparse 16 GiB disk,
`volume-<name>.img` in the instance directory, attached under its serial. `run ssh` opens a shell only on a development
build that runs SSH. `run mode` says what the mode means for evidence.

## Deploy

```console
$ tundravm deploy gcp --build build/prover/production/<id> --volume data=create:100GiB
$ tundravm deploy status prover-1
```

`deploy` publishes the build's disk, starts a TDX VM from it and writes a receipt. `BUILD`
is never guessed. Settings come from `pyproject.toml` or the environment:

| Cloud | Table | Keys | Environment |
|---|---|---|---|
| GCP | `[tool.tundravm.gcp]` | `project`, `bucket`, `zone`, `machine_type` | `TUNDRAVM_GCP_*` |
| Azure | `[tool.tundravm.azure]` | `storage_account`, `resource_group`, `location`, `vm_size`, `gallery` | `TUNDRAVM_AZURE_*` |

Every volume the image declares needs `--volume NAME=create:SIZE` or
`--volume NAME=existing:ID`. GCP attaches each as device `tundravm-NAME`. Azure with a
volume is refused: its data disks are named by LUN, which the image cannot match. The
receipt records the VM, its address and the firewall rule the management port needs;
tundravm does not create cloud firewall rules.

### Attest

```console
$ tundravm attest prover-1 --policy policies/prover-gcp.json --output attest-1.json
```

`attest` asks the instance's `tdxs` for a quote bound to a fresh nonce and checks MRTD and
RTMR0 to RTMR2 against the policy. It delivers nothing. The policy is a `tundravm.policy/2`
file, or a build's `measurements.json`. `measurements.json` holds a policy per record; the
operator copies one into `policies/`, adds the platform registers they approve from a quote
of a host they trust (`platform_approval`: MRTD, RTMR0) and commits it. The
`policies/README.md` that `new --template prover` writes has the `jq` line.

| Verdict | Means |
|---|---|
| `trusted` | a `tdxs` validator (`--validator`) accepted the quote, the nonce is bound, and MRTD, RTMR0, RTMR1 and RTMR2 all matched |
| `partial` | nothing disagreed, but one of the four had no accepted value |
| `measurements-match` | the registers matched; no validator checked the signature |
| `measurements-mismatch` | a register or the nonce did not match; no validator checked |
| `simulated` | a simulated instance; accepted only for one |
| `untrusted` | the validator rejected the quote, a validated quote did not match, or a production build's TD has the `DEBUG` attribute |

An Azure policy is refused: the TDX quote on Azure measures its paravisor, and the image is
measured into the vTPM.

### Secrets

```console
$ tundravm secrets unlock prover-1 data
$ tundravm secrets deliver prover-1 --policy policies/prover-gcp.json raiko-config=@raiko.json
```

Both attest again first. `deliver` then writes each value to
`/run/tundravm/credentials/NAME`. `unlock` re-attests with the policy of the last `attest`,
asks for the passphrase (or reads stdin) and writes it into the volume's FIFO. Values
travel on the SSH command's stdin only: never on argv, in a log, a tree, a manifest or a
receipt.

| Instance | Releases on |
|---|---|
| production build on TDX | `trusted` only, with no `DEBUG` attribute and no simulated evidence |
| development build on TDX | `trusted` or `measurements-match` |
| development build, simulated | `simulated` only; tundravm cannot tell a test value from a real one |
| production build, simulated | nothing: refused before contact, since its services never start |

### Upgrade and rollback

```console
$ tundravm deploy upgrade prover-1 --build build/prover/production/<id2> --policy policies/prover-gcp.json raiko-config=@raiko.json
$ tundravm deploy rollback prover-1 --policy policies/prover-gcp.json raiko-config=@raiko.json
```

`upgrade` runs the one replacement workflow, on GCP: launch the candidate without volumes,
attest it and deliver its credentials, stop the old instance, move every volume to the
candidate, unlock it, wait for readiness. Downtime runs from the stop to readiness. It
refuses Azure and a build whose volumes differ. A candidate that fails attestation leaves
the old instance running with its volumes. `rollback` is the same back to the instance it
replaced. The [trust page](trust.md#recovery) has the recovery rules.

## Testing a project

`tundravm.testing` reads an image as values without building it. The generated
`tests/test_NAME.py` uses it; so can any test:

```python
from tundravm import File, Image, Service
from tundravm.ports import TCP
from tundravm.testing import assert_clean, assert_diagnostic, plan

image = Image(
    "demo",
    packages=["busybox"],
    files=[File.text("/srv/www/index.html", "<h1>demo</h1>\n")],
    services=[
        Service(
            "app",
            ["/usr/bin/busybox", "httpd", "-f", "-p", "8080", "-h", "/srv/www"],
            expose=[TCP(8080)],
        ),
    ],
)


def test_lints_clean() -> None:
    # The evidence service's source stays unpinned until `tundravm lock`.
    assert_clean(plan(image), allow=("source-unpinned",))


def test_the_app_runs_as_its_own_user() -> None:
    app = plan(image).service("app")
    assert app.user == "app"
    assert app.get("Service", "Restart") == "on-failure"


def test_the_firewall_opens_the_port() -> None:
    nftables = plan(image).file("/etc/nftables.conf").content.decode()
    assert "tcp dport 8080 accept" in nftables


def test_the_tdxs_source_is_unpinned() -> None:
    assert assert_diagnostic(plan(image), "source-unpinned").severity == "warning"
```

| Helper | Does |
|---|---|
| `plan(image, profile)` | one profile compiled in memory: `service()`, `unit()`, `file()`, `packages`, `users`, `settings()`, `lint()` |
| `assert_clean(subject, allow=)` | fails on any error or warning not allowed |
| `assert_diagnostic(subject, code)` | the first finding with that code, or a failure listing all |
| `assert_tree(tundravm.compile(image), golden)` | every path, byte, exec bit and symlink; `TUNDRAVM_UPDATE_GOLDEN=1` rewrites |
| `read_lock(path)` | `build/tundravm.lock` as the value `tundravm.compile(image, lock=)` and `plan(image, lock=)` take |
| `FakeMkosi`, fixture `fake_mkosi` | mkosi replaced at the adapter: backends run, nothing forks, `build` writes placeholder outputs, `calls` records each argv |
| `FakeSources` | the source fetch replaced at its git and http boundary: each checkout is a stub a later build reuses, nothing reaches the network, `fetched` records each |
| `boot(artifact)`, fixture `boot` | boots a built disk or UKI under QEMU with OVMF and reads the serial console |
| `run_cli(*argv)` | runs the CLI in-process: `(exit code, stdout, stderr)` |

The pytest fixtures load automatically once tundravm is installed. `boot` needs QEMU and
OVMF; `boot_unavailable()` and `build_unavailable()` say why a host cannot, so a test can
skip on the reason.

## Converting systemd units

```console
$ tundravm convert ./units --from systemd > image.py
```

reads a unit file or a directory of them and prints an `Image` file that `tundravm check`
accepts. Each service unit becomes one `Service`, with its `x.service.d/*.conf` drop-ins
and the `x.timer` or `x.socket` that starts it folded in: `ExecStart=` as the argv,
`Type=notify` as `readiness=Notify()`, the cgroup limits as `resources=`, the stop as
`shutdown=`, the schedule as `timer=`, the socket as `socket=`, and every other directive
systemd knows in `unit=DropIn(...)`. A `# note:` line says where the unit differs from what
the image implies (`Type=exec`, `Restart=on-failure`, the generated user, the `static`
sandbox in place of the unit's own, enabled at boot); a `# unsupported:` line names each
unit or directive no `Image` slot holds (a mount, a path unit, a second `ExecStart=`).

## CI

`new --ci` writes `.github/workflows/tundravm.yml` with two jobs:

| Job | Runs | When |
|---|---|---|
| `check` | `show` into the job summary, `check --tree mkosi`, `lock --check` | every push to `main` and every pull request |
| `build` | `build --reproduce`, `verify latest`, uploads `build/*/production/` | after `check`, when the repository variable `TUNDRAVM_BACKEND` names a backend |

Commit `uv.lock`; both jobs install exactly what it locks. Findings show as annotations on
the pull request. `lock --check` exits 1 on drift, and on a project that has no lockfile
yet, naming `tundravm lock`: lock, commit `build/tundravm.lock` and push again. The `build`
job builds production, so it needs that lock and read access to tundra-tools.

tundravm's own CI bakes the `Image` templates, so it needs the same access. It reads
tundra-tools with the repository secret `TUNDRA_TOOLS_TOKEN` when it exists: a fine-grained
personal access token with read-only Contents on `Hyodar/tundra-tools`, added with
`gh secret set TUNDRA_TOOLS_TOKEN --repo Hyodar/tundra`. Making tundra-tools public does
the same without a secret. Without either, the template tests skip and name the repository.

## When something fails

Every error prints `error [CODE]: message`, an optional `Hint:` and context, and exits 2.
A failed check exits 1, an interrupt 130. An unknown command or flag gets a "did you mean". With `--json`, stderr gets one JSON line instead
(`tundravm.error/1`). `--traceback` raises with the Python traceback.

| Code | Usually | Do |
|---|---|---|
| `E_VALIDATION` | a malformed declaration, an unknown profile | `tundravm check` lists everything |
| `E_LINT` | `build` refused error-level findings | `tundravm check` |
| `E_LOCKFILE` | `build` met a stale lock, or a production build none (`lock-missing`) | `tundravm lock --check`, then `tundravm lock` |
| `E_SOURCE` | a source did not resolve, or a checkout changed | `tundravm build --clean-cache` |
| `E_POLICY` | `build --offline` met a source the workspace never fetched, or a tree that builds with the network | build once online, then `--offline` |
| `E_BACKEND_EXECUTION` | mkosi failed | the error names mkosi's log; `build -v` echoes it; `doctor --for build` |
| `E_STATE` | no build or instance where one was expected | `tundravm build` first, or name the build |
| `E_ARTIFACT_CHANGED` | an artifact's bytes changed | `tundravm verify BUILD` names the file |
| `E_REPRODUCIBILITY` | two builds that should match did not | compare `observations.json` |
| `E_MEASUREMENT` | an artifact that is not a UKI or a disk booting one | rebuild it with `tundravm build` |
| `E_ATTESTATION` | the issuer or validator was unreachable or failed | `run logs`, `journalctl -u tdxs` |
| `E_DEPLOYMENT` | a missing tool, no TDX for `--mode tdx`, a refused upgrade | the hint names it |

A `check` finding is `SEVERITY CODE [PROFILES] SUBJECT: message` and its hint, where
`[image]` marks a finding about the whole image. A finding several profiles share is one
line naming them, and a hint several findings share follows the last of them once. The
report ends with the command that explains its codes, a component's included:

```console
$ tundravm check --explain app-privileged-port
```

```text
app-privileged-port: error
    declared by App in demo.py
    The app runs as a regular user, so it cannot bind a port below 1024.
    fix: Pass App(port=) 1024 or above.
```

The [lint reference](reference/lint.md) lists tundravm's and the shipped components'
codes, what each checks and how to fix it. `--json` and the GitHub annotations (in GitHub Actions) keep one finding per
profile and subject.
