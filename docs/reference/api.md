# API reference

Generated from the `__all__` of `tundravm`, `tundravm.ports`, `tundravm.systemd`, `tundravm.components`, `tundravm.storage`, `tundravm.secrets`, `tundravm.sources`, `tundravm.errors`, `tundravm.testing` by `uv run python -m tundravm._reference docs/reference` (Python 3.12). Do not edit it by hand: `tests/docs/test_reference.py` fails when it is stale.

- [`tundravm`](#tundravm)
- [`tundravm.ports`](#tundravmports)
- [`tundravm.systemd`](#tundravmsystemd)
- [`tundravm.components`](#tundravmcomponents)
- [`tundravm.storage`](#tundravmstorage)
- [`tundravm.secrets`](#tundravmsecrets)
- [`tundravm.sources`](#tundravmsources)
- [`tundravm.errors`](#tundravmerrors)
- [`tundravm.testing`](#tundravmtesting)

## tundravm

tundravm: confidential VM images declared as one `Image`.

### `__version__`

*value*

The installed release, from its git tag; `tundravm --version`.

### `AptRepository`

*dataclass*

```python
AptRepository(
    url: str,
    suite: str,
    components: Sequence[str] = ('main',),
    *,
    key: Path | str,
    snapshot: str | None = None,
    sha256: str | None = None,
)
```

A second apt archive, pinned and signed: `Debian(repositories=[AptRepository(...)])`.

| Member | Does |
|---|---|
| `AptRepository.address(snapshot: str) -> str` | The URL apt reads, dated by *snapshot* (the base's) when nothing else dates it. |
| `AptRepository.keyring` | The keyring's path in the image and the build's apt sandbox. |
| `AptRepository.name` | The name its apt source and keyring are written under. |
| `AptRepository.release_url` | The signed `InRelease` file *sha256* pins. |

### `build`

*function*

```python
build(
    image: Image,
    *,
    lock: Lock,
    backend: Backend,
    into: Path,
    profiles: Sequence[str] | None = None,
    progress: Callable[[str], None] | None = None,
    fetch: bool = True,
) -> tuple[Artifact, ...]
```

Build *image*'s artifacts into the build directory *into* (the command's `BUILD`), for *profiles* (default: all): what `tundravm build` does, frozen against *lock*.

### `compile`

*function*

```python
compile(
    image: Image,
    *,
    profiles: Sequence[str] | None = None,
    lock: Lock | None = None,
) -> Tree
```

The mkosi tree *image* compiles to, with *lock*'s source pins applied.

### `Debian`

*dataclass*

```python
Debian(
    release: str = 'trixie',
    *,
    snapshot: str | None = None,
    efi_stub: str | None = None,
    repositories: Sequence[AptRepository] = (),
)
```

The distribution an image is built from: `Debian("trixie")`, the tested baseline.

| Member | Does |
|---|---|
| `Debian.archive` | The snapshot the image reads: *snapshot*, else `LOCKED_SNAPSHOT`. |

### `DebianConfig`

*dataclass*

```python
DebianConfig(
    version: str,
    *,
    flavor: str = 'cloud-amd64',
    arch: str = 'amd64',
    snapshot: str | None = None,
    sha256: str | None = None,
)
```

`Kernel.build(base=DebianConfig("6.12.113-1"))`: the config Debian ships for that kernel.

| Member | Does |
|---|---|
| `DebianConfig.member` | The compressed config inside the package. |
| `DebianConfig.security_url(archive: str) -> str` | The package in the pool of the snapshot security *archive* (an archive URL). |
| `DebianConfig.series` | `6.12` of `6.12.48-1`: the `linux-config-<series>` package. |
| `DebianConfig.url(archive: str) -> str` | The package in the main pool of the snapshot *archive* (an archive URL). |

### `doctor`

*function*

```python
doctor(backend: Backend, *, runner: ProbeRunner | None = None) -> tuple[Diagnostic, ...]
```

One diagnostic per host tool *backend* needs that is missing (warning when optional).

### `File`

*dataclass*

```python
File(
    path: str,
    content: str | bytes | Path,
    *,
    mode: int | None = 0o644,
    owner: str | None = None,
)
```

A file in the image: `File.text(path, text)` or `File.copy(src, dst)`.

| Member | Does |
|---|---|
| `File.copy(src: str \| Path, dst: str, *, mode: int \| None = None, owner: str \| None = None) -> File` | The host file *src*, read when the image lowers, shipped at *dst*. A relative *src* is beside the file that declares it, as `Local` is, not the working directory. |
| `File.ref` | What a `Profile` change targets. |
| `File.text(path: str, text: str, *, mode: int = 0o644, owner: str \| None = None) -> File` | The UTF-8 text *text* at *path*. |

### `Group`

*dataclass*

```python
Group(name: str, system: bool = True, gid: int | None = None)
```

### `Image`

*dataclass*

```python
Image(
    name: str,
    *,
    base: Debian = Debian(release='trixie', snapshot=None, efi_stub=None, repositories=()),
    packages: Sequence[str | Package] = (),
    files: Sequence[File | Tree | Build] = (),
    services: Sequence[Service] = (),
    components: Sequence[Component] = (),
    ingress: Sequence[Ingress] = (),
    network: Network | None = None,
    users: Sequence[User] = (),
    groups: Sequence[Group] = (),
    kernel: Kernel = Kernel.default(),
    rootfs: RootFS = RootFS.initramfs(),
    observability: Observability | None = None,
    targets: Sequence[Target] = ('qemu',),
    dev: DevAccess | None = None,
    profiles: Mapping[str, Profile] = FrozenMap(),
    mkosi: Mkosi | None = None,
)
```

A confidential VM image: what it runs, and nothing about how it is built.

### `Kernel`

*dataclass*

```python
Kernel(
    version: str | None = None,
    source: Git | Http | None = None,
    *,
    base: DebianConfig | None = None,
    config: Path | None = None,
    fragments: Sequence[Path | KernelFragment] = (),
    patches: Sequence[Path | KernelPatch] = (),
    hardening: bool = True,
    modules: Sequence[str] | bool = (),
    cmdline: Sequence[str] = (),
)
```

The kernel: `Kernel.default()` (the distribution's, tested) or `Kernel.build(...)`.

| Member | Does |
|---|---|
| `Kernel.build(version: str \| None = None, source: Git \| Http \| None = None, *, base: DebianConfig \| None = None, config: Path \| None = None, fragments: Sequence[Path \| KernelFragment] = (), patches: Sequence[Path \| KernelPatch] = (), hardening: bool = True, modules: bool = False, cmdline: Sequence[str] = ()) -> Kernel` | Compile a kernel: from *base* or *config*, hardened, then *fragments*, in order. |
| `Kernel.built` | Whether the build compiles this kernel. |
| `Kernel.default(*, modules: Sequence[str] = (), cmdline: Sequence[str] = ()) -> Kernel` | Debian's kernel with the curated module set plus *modules*, booted with *cmdline* after the image's own parameters. |
| `Kernel.loadable` | Whether a built kernel builds and installs loadable modules. |

### `KernelFragment`

*dataclass*

```python
KernelFragment(options: Mapping[str, str | None], *, reason: str = '')
```

Kernel options a built kernel requests: `KernelFragment({"CONFIG_XFS_FS": "y"})`.

| Member | Does |
|---|---|
| `KernelFragment.read(path: Path) -> KernelFragment` | The options of the `.config` fragment file *path*: `CONFIG_X=value` and `# CONFIG_X is not set` lines; blank lines and other comments are skipped. |

### `KernelPatch`

*dataclass*

```python
KernelPatch(source: Path | Http)
```

A patch a built kernel applies with `patch -p1`: `KernelPatch(Http(url, sha256=...))`.

| Member | Does |
|---|---|
| `KernelPatch.name` | The patch's file name, which a failure to apply names. |

### `load`

*function*

```python
load(
    path: str | Path,
    *,
    attribute: str | None = None,
    extra_paths: Sequence[str | Path] = (),
) -> Image
```

The `Image` the file at *path* defines.

### `lower`

*function*

```python
lower(image: Image, *, profiles: Sequence[str] | None = None) -> Lowered
```

*image* as the compiler's value, with *profiles* (default: all) lowered.

### `Mkosi`

*dataclass*

```python
Mkosi(
    *,
    settings: Mapping[str, str] | None = None,
    scripts: Sequence[tuple[str, str]] = (),
)
```

mkosi settings and scripts the image adds verbatim, recorded in the manifest.

### `Package`

*dataclass*

```python
Package(name: str, *, backports: bool = False)
```

A runtime package: `Package("curl")`, `Package("nftables", backports=True)`.

| Member | Does |
|---|---|
| `Package.ref` | What a `Profile` change targets. |

### `Profile`

*dataclass*

```python
Profile(
    *,
    extends: str | None = None,
    add: Sequence[Content] = (),
    changes: Sequence[Change] = (),
    production: bool = True,
)
```

A named variation on the image: what it adds, and what it changes.

### `RootFS`

*dataclass*

```python
RootFS(
    layout: Literal['initramfs', 'verity'] = 'initramfs',
    filesystem: Literal['erofs'] | None = None,
    max_size: str | None = None,
    uki_max_size: str | None = None,
)
```

Where the OS lives: `RootFS.initramfs()` (the default) or `RootFS.verity()`.

| Member | Does |
|---|---|
| `RootFS.initramfs(*, max_size: str \| None = None, uki_max_size: str \| None = None) -> RootFS` | The OS in the UKI's initrd, its tmpfs capped at *max_size* (`"4G"`, `"50%"`), the UKI at most *uki_max_size* (`"384M"`; `None`: `"256M"`). |
| `RootFS.verity(*, filesystem: Literal['erofs'] = 'erofs') -> RootFS` | A read-only *filesystem* root verified by dm-verity. |

### `Service`

*dataclass*

```python
Service(
    name: str,
    argv: Sequence[Argument],
    *,
    requires: Sequence[Requirement] = (),
    readiness: Readiness | None = None,
    state: str | VolumeDirectory | None = None,
    credentials: Sequence[Binding] = (),
    expose: Sequence[PortValue] = (),
    resources: Resources | None = None,
    shutdown: Shutdown | None = None,
    hardening: Sandbox = 'static',
    timer: Every | Calendar | None = None,
    socket: Listen | None = None,
    unit: DropIn | None = None,
    user: str | None = None,
    groups: Sequence[str] = (),
    quotes: bool = False,
    working_directory: str | None = None,
    environment: Mapping[str, str | Binding] = FrozenMap(),
    logs: str | None = None,
    runtime: str | None = None,
    package: str | None = None,
    egress: Egress | None = None,
)
```

A program the image runs, as its own user, in a tested sandbox.

| Member | Does |
|---|---|
| `Service.binaries() -> tuple[Any, ...]` | The built binary declarations this service's argv pulls in. |
| `Service.bindings` | Every credential this service binds: `credentials=`, then each one its argv or `environment=` names. |
| `Service.command() -> tuple[str, ...]` | The argv as words: a built first element becomes its installed path, an `Endpoint` its URL, a credential `${CREDENTIALS_DIRECTORY}/<name>`. |
| `Service.endpoint(port: int, *, path: str = '/', scheme: str = 'http') -> Endpoint` | Where this service answers on the loopback, with its readiness: what an ingress fronts (`upstream=app.endpoint(8080)`) or a dependent requires. It opens no port and changes no listen address. |
| `Service.packaged(name: str, *, package: str \| None = None, hardening: Sandbox = 'static', requires: Sequence[Requirement] = (), readiness: TcpProbe \| HttpProbe \| None = None, expose: Sequence[PortValue] = (), unit: DropIn \| None = None) -> Service` | The unit *name* the Debian *package* ships (default: the unit's stem), enabled and run under `hardening=`, which replaces the package's own sandbox. |
| `Service.probe_unit` | The bounded probe unit a dependent waits for, when readiness needs one. |
| `Service.ready` | "This service is ready": what another service's `requires=` takes. |
| `Service.ref` | What a `Profile` change targets. |
| `Service.secrets` | The delivered secrets this service binds. |
| `Service.state_directory` | The working directory a `state=` implies. |
| `Service.stem` | The unit name without its suffix: the generated user's name. |
| `Service.unit_name` | The `.service` unit this lowers to. |
| `Service.variables() -> dict[str, str]` | `environment=` as text: a credential becomes `%d/<name>`, systemd's specifier for the directory the service reads it in. |

### `Tree`

*dataclass*

```python
Tree(
    src: str | Path,
    dst: str,
    *,
    exclude: Sequence[str] = (),
    owner: str | None = None,
)
```

The host directory *src* shipped at *dst*, keeping each file's permission bits. A relative *src* is beside the file that declares it, as `Local` is.

| Member | Does |
|---|---|
| `Tree.ref` | What a `Profile` change targets. |

### `User`

*dataclass*

```python
User(
    name: str,
    system: bool = True,
    home: str | None = None,
    shell: str = '/usr/sbin/nologin',
    uid: int | None = None,
    primary_group: str | int | None = None,
    groups: Sequence[str] = (),
)
```

## tundravm.ports

Ports: what a service exposes and how traffic reaches it.

### `AttestedTLS`

*dataclass*

```python
AttestedTLS(port: int, *, upstream: Endpoint)
```

The confidential ingress: TLS on *port*, relayed to *upstream* on the loopback.

| Member | Does |
|---|---|
| `AttestedTLS.manifest() -> dict[str, object]` | The ingress as the runtime manifest records it. |
| `AttestedTLS.unit` | The relay's unit. |

### `TCP`

*dataclass*

```python
TCP(port: int)
```

An inbound TCP port: `Service(expose=[TCP(8080)])` opens it in the firewall.

### `TLS`

*dataclass*

```python
TLS(port: int, *, upstream: Endpoint, certificate: Secret)
```

Ordinary HTTPS on *port*, relayed to *upstream*, with a *certificate* you deliver.

| Member | Does |
|---|---|
| `TLS.manifest() -> dict[str, object]` | The ingress as the runtime manifest records it. |
| `TLS.unit` | The relay's unit. |

### `UDP`

*dataclass*

```python
UDP(port: int)
```

An inbound UDP port: `Service(expose=[UDP(53)])` opens it in the firewall.

## tundravm.systemd

The values `Service` keywords take, each rendered as systemd unit directives.

### `Calendar`

*dataclass*

```python
Calendar(spec: str, *, persistent: bool = True, jitter: str | None = None)
```

`Service(timer=Calendar("daily"))`: run at calendar times (`OnCalendar=`, a shorthand such as `daily` or `*-*-* 03:00:00`). *persistent* catches up on a run missed while the machine was off (`Persistent=yes`); *jitter* spreads the start.

| Member | Does |
|---|---|
| `Calendar.entries() -> Pairs` | The `[Timer]` directives. |

### `DropIn`

*dataclass*

```python
DropIn(
    name: str = '20-tundravm',
    *,
    description: str | None = None,
    exec_start: str | Sequence[str] | None = None,
    exec_start_pre: Sequence[str] = (),
    exec_start_post: Sequence[str] = (),
    exec_reload: str | None = None,
    user: str | Dynamic | None = None,
    group: str | None = None,
    groups: Sequence[str] = (),
    working_dir: str | None = None,
    env: Mapping[str, str] = FrozenMap(),
    env_file: str | Sequence[str] | None = None,
    credentials: Mapping[str, str | SecretFile] = FrozenMap(),
    state: str | None = None,
    logs: str | None = None,
    runtime: str | None = None,
    cache: str | None = None,
    write_paths: Sequence[str] = (),
    restart: Literal['always', 'on-failure', 'on-abnormal', 'no'] | None = None,
    restart_sec: str | None = None,
    timeout_start: str | None = None,
    timeout_stop: str | None = None,
    limits: Mapping[str, str | int] = FrozenMap(),
    hardening: Hardening | None = None,
    after: Sequence[UnitTarget] = (),
    requires: Sequence[UnitTarget] = (),
    wants: Sequence[UnitTarget] = (),
    before: Sequence[UnitTarget] = (),
    part_of: Sequence[UnitTarget] = (),
    binds_to: Sequence[UnitTarget] = (),
    conflicts: Sequence[UnitTarget] = (),
    extra: Extra = FrozenMap(),
)
```

A typed drop-in over a service's unit: `Service(..., unit=DropIn(...))`.

| Member | Does |
|---|---|
| `DropIn.sections() -> tuple[str, ...]` | The sections this drop-in writes (`Unit`, `Service` and `extra`'s). |

### `Every`

*dataclass*

```python
Every(interval: str, *, first: str | None = None, jitter: str | None = None)
```

`Service(timer=Every("1h"))`: run *first* after boot (default: *interval*), then *interval* after each start (`OnBootSec=` + `OnUnitActiveSec=`); *jitter* spreads the start (`RandomizedDelaySec=`).

| Member | Does |
|---|---|
| `Every.entries() -> Pairs` | The `[Timer]` directives. |

### `Hardening`

*dataclass*

```python
Hardening(
    protect_system: ProtectSystem = <strict>,
    protect_home: bool = <strict>,
    private_tmp: bool = <strict>,
    private_devices: bool = <strict>,
    devices: Sequence[str] = <strict>,
    no_new_privileges: bool = <strict>,
    capabilities: Sequence[str] | None = <strict>,
    jit: bool = <strict>,
    network: Network = <strict>,
    syscalls: Sequence[str] = <strict>,
    deny_syscalls: Sequence[str] = <strict>,
    kernel: bool = <strict>,
    sandbox: bool = <strict>,
    umask: str | None = <strict>,
)
```

The sandbox a generated unit runs in: one field per knob, and reviewed presets.

| Member | Does |
|---|---|
| `Hardening.entries() -> tuple[tuple[str, str], ...]` | The `[Service]` directives, in the canonical hardening order (design 2.3). |
| `Hardening.none() -> Hardening` | No sandbox at all: renders nothing in a unit (a drop-in turns every knob off). |
| `Hardening.patch_entries() -> tuple[tuple[str, str], ...]` | The directives a drop-in writes to make the `explicit` fields hold over whatever the unit it extends sets: switches render `yes` or `no`, and each list starts with its empty reset (`SystemCallFilter=`, `DeviceAllow=`...), so the patch replaces it instead of adding to it. Unset fields render nothing. |
| `Hardening.preset(name: PresetName, **deviations: Any) -> Hardening` | The sandbox for a runtime class (`PRESETS`), with explicit *deviations*. |
| `Hardening.relaxed(**overrides: Any) -> Hardening` | `ProtectSystem=full`, `PrivateTmp`, `NoNewPrivileges`, the kernel knobs; no syscall filter, no device, capability or address-family limits. |
| `Hardening.strict(**overrides: Any) -> Hardening` | The default, reviewed profile (the field defaults), with *overrides*. |

### `HttpProbe`

*dataclass*

```python
HttpProbe(url: str, *, timeout: str = '90s', interval: str = '500ms')
```

Readiness: the service answers *url* with a 2xx, polled every *interval* until *timeout*, both systemd time spans.

| Member | Does |
|---|---|
| `HttpProbe.command() -> tuple[str, ...]` | The argv of the probe unit. |

### `Listen`

*dataclass*

```python
Listen(
    address: int | str,
    *,
    kind: Literal['stream', 'datagram'] = 'stream',
    accept: bool = False,
    user: str | None = None,
    group: str | None = None,
    mode: int | None = None,
)
```

`Service(socket=Listen(...))`: systemd listens and starts the service on demand.

| Member | Does |
|---|---|
| `Listen.entries() -> Pairs` | The `[Socket]` directives. |
| `Listen.is_path` | Whether the socket is a file in the filesystem. |

### `Notify`

*dataclass*

```python
Notify()
```

Readiness: the program itself reports `READY=1` on `$NOTIFY_SOCKET`.

### `Resources`

*dataclass*

```python
Resources(
    *,
    memory_max: str | None = None,
    memory_high: str | None = None,
    cpu_quota: str | None = None,
    cpu_weight: int | None = None,
    tasks_max: int | None = None,
    io_weight: int | None = None,
)
```

What a service may use: `Resources(memory_max="8G", tasks_max=4096)`.

| Member | Does |
|---|---|
| `Resources.entries() -> tuple[tuple[str, str], ...]` | The `[Service]` directives this value renders. |

### `Shutdown`

*dataclass*

```python
Shutdown(
    *,
    timeout: str | None = None,
    signal: str | None = None,
    kill_mode: Literal['control-group', 'mixed', 'process', 'none'] | None = None,
    final_kill: bool = True,
)
```

How a service is stopped: `Shutdown(timeout="120s", signal="SIGINT")`.

| Member | Does |
|---|---|
| `Shutdown.entries() -> tuple[tuple[str, str], ...]` | The `[Service]` directives beyond `TimeoutStopSec=` and `KillMode=`. |

### `TcpProbe`

*dataclass*

```python
TcpProbe(
    port: int,
    *,
    host: str = '127.0.0.1',
    timeout: str = '90s',
    interval: str = '500ms',
)
```

Readiness: *host* accepts a TCP connection on *port*, polled every *interval* until *timeout*, both systemd time spans.

| Member | Does |
|---|---|
| `TcpProbe.command() -> tuple[str, ...]` | The argv of the probe unit. |

## tundravm.components

Components: the `Component` base class, what a component exports, and the shipped library.

### `Component`

*class*

```python
Component(*args: object, **kwargs: object)
```

A named group of declarations, written as a class, with typed exports.

| Member | Does |
|---|---|
| `Component.compose() -> Iterable[object]` | The declarations and nested components this component stands for. |
| `Component.kernel() -> Sequence[KernelFragment \| KernelPatch]` | What this component needs of a built kernel: `()` (nothing), unless a subclass overrides it. |
| `Component.name` | The component's name, as `show` and its diagnostics print it. |
| `Component.outbound() -> Egress` | Where this component sends, derived from its fields: `Egress()` (nothing), unless a subclass overrides it. |
| `Component.readiness() -> tuple[Readied, ...]` | What `ready` waits for: the readiness of each service it composes that declares one. A component whose readiness is not a service of its own (one that wraps a packaged or internal unit) overrides this. |
| `Component.ready` | "Every service this component declares readiness for is ready". |
| `Component.service(name: str) -> ServiceRef` | The service called *name*, for `requires=` and for a profile's `changes=`. |
| `Component.services() -> tuple[Service, ...]` | Every service this component composes, nested components included. |

### `ComponentReady`

*dataclass*

```python
ComponentReady(component: Component)
```

"That component is up": `component.ready`.

| Member | Does |
|---|---|
| `ComponentReady.handles() -> tuple[Readied, ...]` | What proves the component ready, one handle per unit. |
| `ComponentReady.name` | The component's name. |

### `Container`

*dataclass*

```python
Container(
    name: str,
    argv: Sequence[str],
    *,
    packages: Sequence[str] = (),
    files: Sequence[File] = (),
    state: VolumeDirectory | None = None,
    user: str | None = None,
    expose: Sequence[PortValue] = (),
    egress: Egress | None = Egress(),
)
```

*argv* run in a Debian root of its own: *packages* from the image's snapshot and *files*, built with the image as a read-only root image the payload ships, so the quote covers it, and nothing is fetched at boot.

| Member | Does |
|---|---|
| `Container.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `container-shell`: a production container whose program is a shell. |
| `Container.image` | Where the payload ships its root image. |
| `Container.loader` | The root oneshot that loads `loop` before the unit attaches its root image. |
| `Container.unit` | The container's `.service`. |

### `DevAccess`

*dataclass*

```python
DevAccess(*, ssh_keys: Sequence[str] = (), password: str | None = None)
```

Debug access to a development build: `dev=DevAccess(ssh_keys=["~/.ssh/id.pub"])`.

| Member | Does |
|---|---|
| `DevAccess.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `dev-access-rsa-key`: an RSA key where every current client makes ed25519. |
| `DevAccess.public_keys() -> tuple[str, ...]` | The public key lines, reading every entry that names a `.pub` file. |
| `DevAccess.tools() -> tuple[object, ...]` | The debugging tools and, with *password*, the console login. |

### `Diagnostic`

*dataclass*

```python
Diagnostic(
    code: str,
    severity: Severity,
    message: str,
    subject: str = '',
    profile: str = '',
    location: str = '',
    hint: str | None = None,
    context: Context = (),
)
```

One finding: a lint *code*, its *severity* and a one-line *message*.

| Member | Does |
|---|---|
| `Diagnostic.to_dict() -> dict[str, object]` | The JSON form `check --json` prints: every field, *context* as an object. |

### `Egress`

*dataclass*

```python
Egress(
    ports: Sequence[PortValue] = (),
    hosts: Sequence[str] = (),
    metadata: bool = False,
)
```

Outbound traffic the image, or one service, may send; everything else is dropped.

### `Endpoint`

*dataclass*

```python
Endpoint(
    host: str,
    port: int,
    path: str = '/',
    scheme: str = 'http',
    ready: ServiceReady | None = None,
    provider: str | Component | None = None,
)
```

Where a component answers, with its readiness: an export a caller can wire.

| Member | Does |
|---|---|
| `Endpoint.address` | `host:port`, with an IPv6 host in brackets. |
| `Endpoint.loopback` | Whether only the image itself can reach it (`127.0.0.0/8`, `::1`, `localhost`). |
| `Endpoint.url` | `scheme://host:port/path`. |
| `Endpoint.with_scheme(scheme: str) -> Endpoint` | The same address under *scheme* (`ws` for a JSON-RPC server's WebSocket). |

### `EVIDENCE`

*value*

```python
EVIDENCE = ServiceReady(unit='tdxs.service')
```

### `Firewall`

*dataclass*

```python
Firewall(
    *,
    allow: Sequence[PortValue] = (),
    egress: Egress | None = None,
    modes: Mapping[str, Mode] = FrozenMap({}),
)
```

The inbound (and optionally outbound) packet filter, nftables in the image.

| Member | Does |
|---|---|
| `Firewall.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `firewall-ssh-open`: port 22 opened by hand instead of by `SshAccess`; `egress-unfiltered`: a production profile that sends anywhere; `firewall-modes-kernel`: modes on a kernel whose TDX switches are all refused. |
| `Firewall.lowered_modes() -> tuple[dm.FirewallMode, ...]` | *modes* as the ruleset takes them, production first. |

### `JournalExport`

*dataclass*

```python
JournalExport(url: str | None = None, credential: Secret | None = None)
```

Ship the journal to *url* (a `systemd-journal-remote` receiver) over HTTPS.

| Member | Does |
|---|---|
| `JournalExport.service(proxy: Proxy \| None = None) -> Service` | The `journal-export` service, through *proxy* when given; without *url*, to the in-guest `sink`, never through a proxy. |
| `JournalExport.sink() -> Service` | `journal-sink`: the in-guest receiver, plain HTTP on the loopback. |

### `Login`

*dataclass*

```python
Login(
    name: str,
    keys: Sequence[str] | Secret = (),
    command: str | None = None,
    shell: str | None = None,
    sudo: Sequence[Sudo] = (),
    home: bool = False,
)
```

An account other than root that logs in over SSH into exactly what the image allows: `Login("tenant", keys=[...], command="/usr/bin/app-status")`.

| Member | Does |
|---|---|
| `Login.account_shell` | The login shell: *shell*, else `/bin/sh` to run *command* (sshd runs a forced command through it, so `nologin` would refuse it). |
| `Login.config() -> str` | The `sshd_config.d` file: its own keys only, no forwarding, the command if any. |
| `Login.declarations() -> Iterator[Declaration]` | The account, its sshd block, its keys and its sudo rules. |
| `Login.delivered` | Whether the keys arrive after attestation (a `Secret`) rather than baked in. |
| `Login.key_file` | Where its keys are read: `/etc/ssh/authorized_keys.d/<name>`. |
| `Login.options` | The `authorized_keys` options every baked key carries. |
| `Login.sudoers() -> str` | The `sudoers.d` file: exactly the *sudo* rules. |
| `Login.sudoers_file` | `/etc/sudoers.d/<name>`, or `None` without *sudo*. |

### `Mode`

*dataclass*

```python
Mode(
    ingress: Sequence[PortValue] = (),
    egress: Egress | None = None,
    lasts: str | None = None,
)
```

One mode of `Firewall(modes={...})`: the ports it opens (*ingress*), what it may send beyond the image's `Egress` (*egress*) and how long it lasts once an operator switched to it (*lasts*, a systemd time span; `"30m"` unless given, and never on `production`, which the image boots into and returns to).

| Member | Does |
|---|---|
| `Mode.seconds` | *lasts* in whole seconds, 30 minutes when it is not given. |

### `Network`

*dataclass*

```python
Network(
    *,
    dhcp: bool = True,
    static: StaticAddress | None = None,
    egress: Egress | None = None,
    interfaces: Sequence[str] = ('en*', 'eth*'),
    dns: Sequence[str] = ('1.1.1.1#cloudflare-dns.com', '1.0.0.1#cloudflare-dns.com'),
)
```

How the image reaches the network: `Network()` is DHCP on `en*`/`eth*`.

| Member | Does |
|---|---|
| `Network.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `network-no-route`: a static address without a gateway reaches its subnet only. |
| `Network.online` | "An interface is configured": `systemd-networkd-wait-online`, bounded. |
| `Network.outbound() -> Egress` | DNS over TLS (TCP 853) to each of *dns*. |

### `Observability`

*dataclass*

```python
Observability(
    *,
    logs: JournalExport | None = None,
    metrics: PrometheusRemoteWrite | None = None,
    proxy: str | Proxy | None = None,
)
```

The export path for logs and metrics: `image.observability` takes it.

| Member | Does |
|---|---|
| `Observability.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `observability-plaintext`: an http:// sink sees what the TD logs in the clear; `observability-sink-unset`: a production profile's logs or metrics never leave the VM; `observability-export-unset`: the metrics name no keep-list, an error where a production profile writes them to *url*. |
| `Observability.outbound() -> Egress` | *proxy*, or each sink's `HOST:PORT` without one. |

### `PrometheusRemoteWrite`

*dataclass*

```python
PrometheusRemoteWrite(
    url: str | None = None,
    credential: Secret | None = None,
    interval: str = '30s',
    buffer: str = '1h',
    export: str | Sequence[str] | None = None,
)
```

Send node metrics to *url*, a Prometheus remote-write receiver.

| Member | Does |
|---|---|
| `PrometheusRemoteWrite.config(proxy: Proxy \| None = None) -> dict[str, object]` | The agent's configuration: scrape the node exporter, keep `kept`, write to *url*, through *proxy* when given; without *url*, to the in-guest `sink`. |
| `PrometheusRemoteWrite.identity() -> tuple[dm.Build, dm.Service]` | `image-metrics` from `tools/` and its service: as root without capabilities (the event log and the TDX device are root's), with the TDX device and AF_UNIX alone, rewriting its file in `IMAGE_METRICS_DIRECTORY` every *interval*. |
| `PrometheusRemoteWrite.kept() -> tuple[str, ...]` | The series names that leave: *export* with its presets expanded (`health` without one), then `IDENTITY_SERIES`, in order, once each. |
| `PrometheusRemoteWrite.service(proxy: Proxy \| None = None) -> Service` | The `metrics-export` service: Prometheus in agent mode, through *proxy*; without *url*, to the in-guest `sink`, never through a proxy. |
| `PrometheusRemoteWrite.sink() -> Service` | `metrics-sink`: the in-guest receiver, plain HTTP on the loopback. |

### `Proxy`

*dataclass*

```python
Proxy(address: str, port: int, *, tls_name: str | None = None)
```

The fixed-address proxy both exporters reach their sinks through: `Proxy("10.0.0.9", 3128)`, which `proxy="10.0.0.9:3128"` also spells.

| Member | Does |
|---|---|
| `Proxy.endpoint` | `IP:PORT` (`[IPv6]:PORT`), what the firewall allows. |
| `Proxy.url` | The URL the exporters reach the proxy at: `https://<tls_name>:<port>` or `http://<endpoint>`. |

### `Resolved`

*dataclass*

```python
Resolved(
    profile: str,
    targets: tuple[Target, ...],
    items: tuple[Declaration, ...],
    components: tuple[str, ...],
    production: bool = True,
)
```

One profile of the image after expansion, profile changes and reference checks.

| Member | Does |
|---|---|
| `Resolved.bind(ref: Ref) -> Declaration` | The profile's final declaration *ref* names; `ValidationError` when absent. |

### `Rule`

*dataclass*

```python
Rule(
    code: str,
    severity: Severity | Sequence[Severity],
    summary: str,
    fix: str,
    checks: Sequence[RuleCheck] = (),
    *,
    when: Sequence[str] = (),
)
```

One code `tundravm check` reports: its severity, what it checks and how to fix it.

| Member | Does |
|---|---|
| `Rule.severity_text` | The severity as the lint reference and `--explain` print it: `error (warning under Policy(storage_safety='warn'))`. |

### `ServiceReady`

*dataclass*

```python
ServiceReady(unit: str, probe: Readiness | None = None)
```

"That service is ready": `service.ready`, what `requires=` takes.

### `ServiceRef`

*dataclass*

```python
ServiceRef(unit: str, probe: Readiness | None = None, service: Service | None = None)
```

A service a component composed, named from outside: `node.service("node")`.

| Member | Does |
|---|---|
| `ServiceRef.endpoint(port: int, *, path: str = '/', scheme: str = 'http') -> Endpoint` | Where that service answers on the loopback, with its readiness, as `Service.endpoint` gives it: what a component exports (`self.service("app").endpoint(8080)`). It opens no port. |
| `ServiceRef.ready` | The readiness handle of that service. |
| `ServiceRef.ref` | The reference a profile change targets. |

### `SshAccess`

*dataclass*

```python
SshAccess(
    *,
    keys: Sequence[str] = (),
    from_delivery: bool = False,
    listen_port: int = 22,
    login: Login | Sequence[Login] = (),
)
```

Root logs in over SSH with a public key, and with nothing else.

| Member | Does |
|---|---|
| `SshAccess.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `ssh-keys-baked`: keys baked into a cloud image let in whoever holds them; `ssh-login-shell`: a production login with a shell and no forced command. |
| `SshAccess.logins` | Every `Login`, in the order given. |
| `SshAccess.port` | The port `sshd` listens on. |

### `StaticAddress`

*dataclass*

```python
StaticAddress(address: str, gateway: str | None = None)
```

A fixed address for `Network`: `StaticAddress("10.0.0.5/24", gateway="10.0.0.1")`.

| Member | Does |
|---|---|
| `StaticAddress.version` | 4 or 6. |

### `Sudo`

*dataclass*

```python
Sudo(command: str, nopasswd: bool = True)
```

One command a `Login` runs as root, exactly as written: `Sudo("/usr/bin/systemctl restart app")`.

| Member | Does |
|---|---|
| `Sudo.rule(user: str) -> str` | The `sudoers` line letting *user* run this command as root. |

### `Tdxs`

*dataclass*

```python
Tdxs(
    *,
    source: dm.Git | dm.Local = tools_source(),
    validator: du.TdxsType | None = None,
    expected_measurements: Mapping[str, str] = FrozenMap({}),
    check_revocations: bool = False,
    get_collateral: bool = False,
    verify_imds: bool = False,
    verify_identity_token: bool = False,
)
```

The evidence service: `tdxs` from `tools/` of tundravm's repository, socket-activated on `/var/tdxs.sock`, which produces the TDX quote an attestation is checked against.

| Member | Does |
|---|---|
| `Tdxs.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `tdxs-validator-unchecked`: a validator with nothing to compare a quote to. |
| `Tdxs.from_policy(policy: str \| Path \| Mapping[str, object], *, validator: du.TdxsType = 'tdx', allow_placeholder: bool = False, **kwargs: Any) -> Tdxs` | A verifier whose `expected_measurements` are *policy*'s registers. |
| `Tdxs.group` | The group whose members may ask for quotes. |
| `Tdxs.outbound() -> Egress` | Intel's PCS over HTTPS with *get_collateral* or *check_revocations*, and the root CA's CRL with *check_revocations*; nothing otherwise (quotes are made in the guest). |
| `Tdxs.socket` | Where a service asks for quotes (its `tdx` group may connect). |

### `TeeBase`

*dataclass*

```python
TeeBase(
    *,
    time: bool = True,
    nts_servers: Sequence[str] = ('time.cloudflare.com', 'nts.netnod.se'),
    journal: bool = True,
    journal_max_use: str = '128M',
    sysctl: bool = True,
    dns: bool = True,
    mask: bool = True,
)
```

The confidential-VM baseline: authenticated time, a volatile journal, sysctl hardening, DNS over TLS, no apt timers or console getty, and the CPU mitigations the kernel boots with.

| Member | Does |
|---|---|
| `TeeBase.check(resolved: dm.Resolved) -> tuple[dm.Diagnostic, ...]` | `tee-base-clock`: an image without authenticated time trusts the host's clock. |
| `TeeBase.kernel_cmdline` | `mitigations=auto,nosmt spec_store_bypass_disable=on`: every CPU vulnerability mitigated, SMT off where that needs it, Speculative Store Bypass off for every process (`TEE_KERNEL_CMDLINE` says why each). |
| `TeeBase.outbound() -> Egress` | NTS time (TCP 4460, NTP on UDP 123) with *time*; the `Network` names where DNS goes. |

## tundravm.storage

Persistent storage: encrypted volumes, the directories services keep on them, and how each volume unlocks.

### `BrokerKey`

*dataclass*

```python
BrokerKey(url: str, *, audience: str)
```

A key broker that releases a value after it has verified this image's attestation.

| Member | Does |
|---|---|
| `BrokerKey.manifest() -> dict[str, object]` | The broker as the runtime manifest records it. |

### `OperatorKey`

*dataclass*

```python
OperatorKey()
```

Unlock with a passphrase an operator supplies on every boot (flashbots' model).

| Member | Does |
|---|---|
| `OperatorKey.manifest() -> dict[str, object]` | The unlock as the runtime manifest records it. |

### `TpmSeal`

*dataclass*

```python
TpmSeal(
    *,
    platform: Literal['azure', 'gcp', 'swtpm'],
    pcrs: Sequence[int],
    recovery: Literal['none'],
)
```

Unlock with a random key sealed to a vTPM the operator trusts.

| Member | Does |
|---|---|
| `TpmSeal.manifest() -> dict[str, object]` | The unlock as the runtime manifest records it. |
| `TpmSeal.refusal(targets: Sequence[str]) -> str \| None` | Why *targets* cannot boot a volume sealed this way, or `None` when they can. |

### `Volume`

*dataclass*

```python
Volume(
    name: str,
    *,
    unlock: Unlock,
    mount: str = '/persistent/<name>',
    filesystem: Literal['ext4'] = 'ext4',
)
```

A persistent, encrypted volume: `Volume("data", unlock=...)`.

| Member | Does |
|---|---|
| `Volume.device` | The `/dev/disk/by-id/` glob that finds the attachment on every target. |
| `Volume.directory(name: str, *, mode: int = 0o750, group: str \| None = None) -> VolumeDirectory` | The directory *name* on this volume, for a service's (or component's) state. |
| `Volume.manifest() -> dict[str, object]` | The volume as the runtime manifest records it. |
| `Volume.pipe` | The FIFO an operator (or a broker client) writes the passphrase into. |
| `Volume.ready` | "It is open, mounted, and its directories exist". |
| `Volume.serial` | The attachment name: QEMU's `serial=`, GCP's device name. |
| `Volume.step` | The runtime-init step that prepares the volume's directories once it is open. |

### `VolumeDirectory`

*dataclass*

```python
VolumeDirectory(
    volume: Volume,
    name: str,
    *,
    mode: int = 0o750,
    group: str | None = None,
)
```

A directory on a `Volume`: `data.directory("app")`, bound as a state.

| Member | Does |
|---|---|
| `VolumeDirectory.path` | The absolute path: the volume's mount plus `name`. |
| `VolumeDirectory.ready` | The volume's readiness: it exists once the volume is open. |

## tundravm.secrets

Secrets: values an image needs at run time and never contains.

### `Credential`

*dataclass*

```python
Credential(name: str, source: str)
```

A systemd credential a service reads: `Credential("jwt", "/etc/app/jwt")`.

### `Generated`

*dataclass*

```python
Generated(name: str, *, bytes: int = 32)
```

A secret the image makes at boot instead of having it delivered: `Generated("jwt")`.

| Member | Does |
|---|---|
| `Generated.path` | Where a bound service reads it: `${CREDENTIALS_DIRECTORY}/<name>`, an argv word. |
| `Generated.ready` | "This boot's value exists": what a bound service requires. |
| `Generated.unit` | The oneshot that writes it. |

### `InstanceKey`

*dataclass*

```python
InstanceKey()
```

This boot's instance key, for a service that signs with it: `InstanceKey()`.

| Member | Does |
|---|---|
| `InstanceKey.path` | Where a bound service reads it: `${CREDENTIALS_DIRECTORY}/instance-key`. |
| `InstanceKey.ready` | "This boot's key exists": what a bound service requires. |

### `Secret`

*dataclass*

```python
Secret(name: str, *, required: bool = True, source: BrokerKey | None = None)
```

A secret the image needs at run time and never contains: `Secret("jwt")`.

| Member | Does |
|---|---|
| `Secret.credential` | The `LoadCredential=` binding a service gets. |
| `Secret.location` | Where the delivered value lands: `/run/tundravm/credentials/<name>`. |
| `Secret.path` | Where a bound service reads it: `${CREDENTIALS_DIRECTORY}/<name>`, an argv word. |
| `Secret.ready` | "It has been delivered": what `requires=` takes for a required secret. An optional one has no wait, so nothing can wait for it. |

## tundravm.sources

Software built from source: what `tundravm lock` pins.

### `Build`

*dataclass*

```python
Build(
    name: str,
    source: Git | Http | Local,
    script: str | None = None,
    install: Sequence[Install] = (),
    packages: Sequence[str] = (),
    env: Mapping[str, str] = FrozenMap(),
    cache_namespace: str | None = None,
    using: Go | Cargo | Dotnet | None = None,
    command: Sequence[str] | None = None,
)
```

### `Cargo`

*dataclass*

```python
Cargo(
    *,
    output: str,
    bin: str | None = None,
    package: str | None = None,
    features: Sequence[str] = (),
    profile: str = 'release',
    env: Mapping[str, str] = FrozenMap(),
    packages: Sequence[str] = ('cargo',),
    auditable: bool | None = None,
    reproducible: bool | None = None,
)
```

`cargo fetch && cargo build --frozen`; *bin* or *package* selects the target.

| Member | Does |
|---|---|
| `Cargo.artifact` | `target/<profile>/<bin>`: the built binary, relative to the source tree. |
| `Cargo.command(workdir: str) -> str` | The build's shell command in *workdir*, the checkout's copy inside the sandbox. |

### `CargoBinary`

*dataclass*

```python
CargoBinary(
    name: str,
    source: Git | Http | Local,
    *,
    bin: str | None = None,
    package: str | None = None,
    install: str | None = None,
    features: Sequence[str] = (),
    profile: str = 'release',
    env: Mapping[str, str] = FrozenMap(),
    auditable: bool | None = None,
    cache_namespace: str | None = None,
)
```

A Rust program built from *source* and installed at `executable`.

| Member | Does |
|---|---|
| `CargoBinary.executable` | The absolute path the binary is installed at. |

### `Dotnet`

*dataclass*

```python
Dotnet(
    *,
    project: str,
    output: str,
    configuration: str = 'Release',
    runtime: str = 'linux-x64',
    env: Mapping[str, str] = FrozenMap(),
    packages: Sequence[str] = ('libicu76', 'libssl3t64', 'libstdc++6'),
    restore_args: Sequence[str] = (),
    properties: Mapping[str, str] = FrozenMap(),
)
```

Deterministic self-contained `dotnet publish` of *project* into `publish/`.

| Member | Does |
|---|---|
| `Dotnet.artifact` | `publish/<output>`: the published program, relative to the source tree. |
| `Dotnet.command(workdir: str) -> str` | The build's shell command in *workdir*, the checkout's copy inside the sandbox. |

### `Git`

*dataclass*

```python
Git(
    url: str,
    ref: str,
    subdir: str | None = None,
    submodules: bool = False,
    auth: str | tuple[str, str] | None = None,
)
```

A git repository at *ref* (a tag, a branch, or a 40-hex commit `tundravm lock` pins).

### `Go`

*dataclass*

```python
Go(
    *,
    output: str,
    package: str = '.',
    ldflags: str = '-s -w -buildid=',
    tags: Sequence[str] = (),
    env: Mapping[str, str] = FrozenMap(),
    packages: Sequence[str] = ('golang',),
    output_dir: str = './build',
    mkdir: bool = True,
    reproducible: bool | None = None,
    cgo: bool = False,
)
```

`go build -trimpath` of *package* (the module root, `"."`, by default) into `<output_dir>/<output>`.

| Member | Does |
|---|---|
| `Go.artifact` | `<output_dir>/<output>`: the built binary, relative to the source tree. |
| `Go.command(workdir: str) -> str` | The build's shell command in *workdir*, the checkout's copy inside the sandbox. |

### `GoBinary`

*dataclass*

```python
GoBinary(
    name: str,
    source: Git | Http | Local,
    *,
    package: str = '.',
    install: str | None = None,
    env: Mapping[str, str] = FrozenMap(),
    tags: Sequence[str] = (),
    ldflags: str = '-s -w -buildid=',
    cgo: bool = False,
    cache_namespace: str | None = None,
)
```

A Go program built from *source* and installed at `executable`.

| Member | Does |
|---|---|
| `GoBinary.executable` | The absolute path the binary is installed at. |

### `Http`

*dataclass*

```python
Http(url: str, sha256: str | None = None, auth: str | None = None)
```

A download at *url*, pinned by *sha256* or by `tundravm lock`.

### `Install`

*dataclass*

```python
Install(
    source: str,
    destination: str,
    mode: int | None = 0o755,
    directory: bool = False,
    artifact: bool = False,
)
```

### `Local`

*dataclass*

```python
Local(path: str)
```

A directory of the project: `Local("app")`, the program beside the image file.

## tundravm.errors

Typed SDK error model with stable, machine-readable error codes.

### `ArtifactError`

*exception*

```python
class ArtifactError(TdxError)
```

A built artifact's bytes no longer match the sha256 its build's manifest records. Code `E_ARTIFACT_CHANGED`.

### `AttestationError`

*exception*

```python
class AttestationError(TdxError)
```

`attest` could not complete: a tdxs issuer or validator is unreachable or failed. Code `E_ATTESTATION`.

### `BackendExecutionError`

*exception*

```python
class BackendExecutionError(TdxError)
```

 Code `E_BACKEND_EXECUTION`.

### `DeploymentError`

*exception*

```python
class DeploymentError(TdxError)
```

 Code `E_DEPLOYMENT`.

### `ErrorCode`

*enum*

```python
ErrorCode(*values)
```

Stable error identifiers used across API surfaces.

### `LintError`

*exception*

```python
class LintError(TdxError)
```

`build` refused: the image's checks reported error-level diagnostics. Code `E_LINT`.

### `LockfileError`

*exception*

```python
class LockfileError(TdxError)
```

A lockfile is missing, unreadable or stale, or locking failed. Code `E_LOCKFILE`.

### `MeasurementError`

*exception*

```python
class MeasurementError(TdxError)
```

 Code `E_MEASUREMENT`.

### `PolicyError`

*exception*

```python
class PolicyError(TdxError)
```

 Code `E_POLICY`.

### `ReproducibilityError`

*exception*

```python
class ReproducibilityError(TdxError)
```

 Code `E_REPRODUCIBILITY`.

### `SourceError`

*exception*

```python
class SourceError(TdxError)
```

A source build's source could not be resolved to a pin.

### `StateError`

*exception*

```python
class StateError(TdxError)
```

Persisted state (a build directory, an instance receipt, a checkout) is missing or unreadable. Code `E_STATE`.

### `TdxError`

*exception*

```python
class TdxError(Exception)
```

Base error class: what is wrong (`args[0]`), the fix (`hint`), and context.

### `ValidationError`

*exception*

```python
class ValidationError(TdxError)
```

 Code `E_VALIDATION`.

## tundravm.testing

Helpers for testing images: plans, lint assertions, golden trees, CLI runs.

### `assert_clean`

*function*

```python
assert_clean(
    subject: Lintable,
    /,
    *,
    warnings: Literal['fail', 'allow'] = 'fail',
    allow: Sequence[str] = (),
    profiles: Profiles = None,
) -> Sequence[Diagnostic]
```

Fail on every error and warning finding whose code is not in *allow*.

### `assert_diagnostic`

*function*

```python
assert_diagnostic(
    diagnostics: Lintable,
    code: str,
    /,
    *,
    profiles: Profiles = None,
    severity: Severity | None = None,
    profile: str | None = None,
    subject: str | None = None,
    location: str | None = None,
) -> Diagnostic
```

Return the first diagnostic matching every given field, else fail listing them all.

### `assert_probe`

*function*

```python
assert_probe(console: str | BootResult, expected: Sequence[str] = ()) -> probe.Report
```

`read_probe`, raising `AssertionError` with its table unless the probe finished, nothing failed and every *expected* check printed.

### `assert_tree`

*function*

```python
assert_tree(tree: Tree, golden: str | Path, *, update: bool | None = None) -> None
```

Fail unless *golden* holds exactly *tree*: every path, its bytes, exec bit and symlink.

### `boot`

*function*

```python
boot(
    artifact: Bootable,
    *,
    timeout: float = 300.0,
    memory: str = '2G',
    cpus: int = 2,
    firmware: str | os.PathLike[str] | None = None,
    kvm: bool | Literal['auto'] = 'auto',
    markers: Sequence[str] = ('Reached target minimal.target',),
    expect_units: Sequence[str] = (),
    cmdline: Sequence[str] = ('console=ttyS0', 'systemd.show_status=yes', 'systemd.status_unit_format=name', 'systemd.log_color=no', 'systemd.journald.forward_to_console=1', 'panic=-1'),
    settle: float = 3.0,
    log: str | os.PathLike[str] | None = None,
    network: bool = True,
    qemu: str = 'qemu-system-x86_64',
    extra_args: Sequence[str] = (),
    forward: Mapping[int, int] | None = None,
    wait_http: Sequence[str] = (),
    via: BootPath = 'auto',
) -> BootResult
```

Boot *artifact* under QEMU with OVMF and wait for *markers* and *expect_units*.

### `boot_unavailable`

*function*

```python
boot_unavailable(
    qemu: str = 'qemu-system-x86_64',
    firmware: str | os.PathLike[str] | None = None,
) -> str | None
```

Why `boot` cannot run on this host (no QEMU, no OVMF); `None` when it can.

### `BootResult`

*dataclass*

```python
BootResult(
    console: str,
    log: Path,
    duration: float,
    command: tuple[str, ...],
    started: tuple[str, ...],
    failed: tuple[str, ...],
    fatal: tuple[str, ...],
    missing: tuple[str, ...],
    timed_out: bool,
    returncode: int | None,
    stderr: str = '',
    exited: tuple[str, ...] = (),
    http: tuple[HttpResponse, ...] = (),
)
```

One boot: the console, what it reached, what failed, how long it took.

| Member | Does |
|---|---|
| `BootResult.assert_booted() -> None` | Raise `AssertionError` with `summary` unless the boot is `ok`. |
| `BootResult.http_get(url: str) -> str` | The body *url* (a `wait_http` URL) answered with; `AssertionError` when it never answered with a 2xx. |
| `BootResult.kernel_cmdline` | The command line the kernel printed (`kernel_cmdline`). |
| `BootResult.ok` | Every marker and expected unit seen and still running, no unit failed, nothing fatal, in time, and QEMU did not exit with an error. |
| `BootResult.summary(tail: int = 60) -> str` | A failure report: what went wrong, QEMU's stderr, the console's last lines. |

### `BootUnavailable`

*exception*

```python
class BootUnavailable(RuntimeError)
```

QEMU or OVMF is missing on this host: the boot cannot run here.

### `build_unavailable`

*function*

```python
build_unavailable() -> str | None
```

Why this host cannot run a real build with the Linux backend without a password prompt, or `None` when it can (x86-64 Linux, the pinned mkosi, and user namespaces or non-interactive sudo). Integration tests skip on the reason.

### `ConsoleScan`

*dataclass*

```python
ConsoleScan(
    started: tuple[str, ...],
    failed: tuple[str, ...],
    fatal: tuple[str, ...],
    exited: tuple[str, ...] = (),
)
```

What a console transcript says: units started and failed, fatal lines.

### `FakeCall`

*dataclass*

```python
FakeCall(argv: tuple[str, ...], cwd: Path, log: Path, stdout: bool)
```

One recorded `invoke`: mkosi's *argv*, the directory it ran in and its log.

| Member | Does |
|---|---|
| `FakeCall.option(name: str) -> str \| None` | The value of the last `--name=value` in `argv`. |
| `FakeCall.options(name: str) -> list[str]` | Every `--name=value` value in `argv`, in order. |
| `FakeCall.verb` | The mkosi verb (`build`, `summary`…): the last non-option before `--json`. |

### `FakeEndpoint`

*dataclass*

```python
FakeEndpoint(
    platform: Literal['simulator', 'tdx'] = 'simulator',
    registers: Mapping[str, bytes] = dict(),
    debug: bool = False,
    mode: str = 'simulated',
    credentials: tuple[str, ...] = ('api-token',),
    volumes: tuple[str, ...] = ('data',),
    ssh_key: bool = True,
    listeners: Mapping[int, bytes] = dict(),
    misbehave: Misbehaviour | None = None,
    received: dict[str, bytes] = dict(),
    unlocked: dict[str, bytes] = dict(),
    ssh_keys: list[str] = list(),
    sessions: list[FakeSession] = list(),
    first_nonce: bytes | None = None,
    pins: bool = False,
    owner: str | None = None,
    mismatch: str | None = None,
    confirmed: bool = False,
    signers: list[str] = list(),
    modes: tuple[str, ...] = (),
    mode_events: list[str] = list(),
    clock: int = 412,
    reply: dict[str, object] = dict(),
)
```

The endpoint of one boot: what it accepts, what it received.

| Member | Does |
|---|---|
| `FakeEndpoint.connect(host: str, port: int) -> FakeSession` | A new TLS session (the `connect=` the operator commands take). |
| `FakeEndpoint.evidence(session: FakeSession, nonce: bytes) -> dict[str, object]` |  |
| `FakeEndpoint.presented(host: str, port: int) -> bytes` | The DER certificate the attested TLS listener on *port* presents (the `presented=` that `attest` takes): around its bound key, or around a key the evidence does not bind when misbehaving or when *port* is no listener. |
| `FakeEndpoint.takes_ssh_key` | Whether `POST /ssh-key` installs a key: the first in a `simulated` mode, else only a confirmed owner's. |
| `FakeEndpoint.write(session: FakeSession, path: str, body: Mapping[str, object]) -> int` |  |

### `FakeFetch`

*dataclass*

```python
FakeFetch(url: str, pin: str, path: Path)
```

One recorded checkout: the source's url, the pin and where it landed.

### `FakeMkosi`

*dataclass*

```python
FakeMkosi(
    summary: dict[str, Any] | None = None,
    outputs: Sequence[str] = ('payload.efi', 'qemu.raw'),
    fail: int | None = None,
    fail_verb: str = 'build',
    log: str = '',
    uki_size: int | None = None,
    sudo: bool = False,
    calls: list[FakeCall] = list(),
)
```

Records mkosi verbs instead of running them.

| Member | Does |
|---|---|
| `FakeMkosi.install(setattr: Callable[[object, str, object], None]) -> FakeMkosi` | Put this fake in place of the adapter's `invoke` and `needs_sudo` using *setattr* (pytest's `monkeypatch.setattr`, which undoes it after the test). |
| `FakeMkosi.invoke(argv: Sequence[str], *, cwd: Path, log: Path, on_output: Callable[[str], None] \| None = None, stdout: bool = False, terminal: bool = False) -> MkosiResult` |  |
| `FakeMkosi.needs_sudo() -> bool` |  |
| `FakeMkosi.verbs() -> list[str]` |  |

### `FakeSources`

*dataclass*

```python
FakeSources(fetched: list[FakeFetch] = list())
```

Records source checkouts instead of fetching them.

| Member | Does |
|---|---|
| `FakeSources.fetch_source(source: Source, pin: str, dest: Path) -> None` |  |
| `FakeSources.git_output(checkout: Path, *args: str) -> str \| None` |  |
| `FakeSources.install(setattr: Callable[[object, str, object], None]) -> FakeSources` | Put this fake in place of the fetch boundary using *setattr* (pytest's `monkeypatch.setattr`, which undoes it after the test). |
| `FakeSources.lock_dotnet_sdk(build: SourceBuild, pin: str, checkout: Path) -> LockedToolchain` |  |
| `FakeSources.prefetch_deps(build: SourceBuild, pin: str, checkout: Path, root: Path, **kwargs: object) -> str \| None` |  |

### `HttpResponse`

*dataclass*

```python
HttpResponse(url: str, status: int | None, body: str)
```

What one `wait_http` URL answered while the VM ran: `status` `None` when it never answered with a 2xx before the boot ended (`body` then holds the last error).

| Member | Does |
|---|---|
| `HttpResponse.ok` |  |

### `image_file`

*function*

```python
image_file(tmp_path: Path, source: str, name: str = 'image.py') -> Path
```

Write dedented *source* (an image file) to `tmp_path/name` for CLI tests; return its path.

### `ImageFile`

*dataclass*

```python
ImageFile(
    path: str,
    content: bytes,
    mode: int,
    stage: Stage,
    origin: tuple[Origin, ...] = (),
    symlink: str | None = None,
    owner: str = 'root',
)
```

One file of the image tree: bytes, mode, stage and the declarations that put it there.

| Member | Does |
|---|---|
| `ImageFile.component` | The innermost component that declared the file, `None` for a compiler file. |
| `ImageFile.text` | The content decoded as UTF-8. |

### `InitStep`

*dataclass*

```python
InitStep(
    name: str,
    after: tuple[str, ...] = (),
    script: str = '',
    builtin: bool = False,
)
```

One runtime-init step: a declared one or a built-in (`keys`, `disks`, `secrets`).

### `Lintable`

*value*

```python
Lintable = tundravm._image._image.Image | tundravm.testing._image.PlannedImage | collections.abc.Sequence[tundravm.diagnostics.Diagnostic]
```

### `plan`

*function*

```python
plan(
    subject: PublicImage | Component,
    profile: str | None = None,
    *,
    lock: Lock | None = None,
) -> PlannedImage
```

*profile* of *subject* (default: `production`) as a `PlannedImage`.

### `PlannedImage`

*class*

```python
PlannedImage(
    subject: PublicImage | Component,
    profile: str | None = None,
    *,
    lock: Lock | None = None,
)
```

One profile of an image, planned in memory: what its compiled tree holds, as values.

| Member | Does |
|---|---|
| `PlannedImage.builds` | The declared source builds, by name. |
| `PlannedImage.file(path: str) -> ImageFile` | The file at absolute image *path*; `KeyError` lists its directory's files. |
| `PlannedImage.groups` | The declared groups, by name. |
| `PlannedImage.hooks(stage: str) -> tuple[Hook, ...]` | The build hooks of *stage* (`prepare`, `postinst`, `finalize`...) in run order. |
| `PlannedImage.init_steps` | The runtime-init steps in the order they run (the graph's topological order). |
| `PlannedImage.lint() -> tuple[Diagnostic, ...]` | Every finding for this profile, as `tundravm check` reports it. |
| `PlannedImage.packages` | Every package the image installs (`runtime`) or builds with (`build`), by name. |
| `PlannedImage.service(name: str) -> UnitView` | The `<name>.service` unit; same as `unit` for a service name. |
| `PlannedImage.settings(section: str) -> Mapping[str, tuple[str, ...]]` | The compiled payload's `mkosi.conf` section, `{key: values}` (empty when absent), merged as mkosi reads it: the root `mkosi.conf`, the payload's `mkosi.conf` and `mkosi.conf.d/*.conf`, then the profile's overlay. List settings (`Packages`, scripts, trees) accumulate, the others override. |
| `PlannedImage.unit(name: str) -> UnitView` | The shipped unit *name* (a bare name means `.service`), parsed. |
| `PlannedImage.users` | The declared users, by name. |

### `PROBE_DONE`

*value*

```python
PROBE_DONE = 'tundravm-probe: done '
```

### `ProbeReport`

*dataclass*

```python
ProbeReport(
    results: tuple[Result, ...] = (),
    done: tuple[int, int] | None = None,
    expected: tuple[str, ...] = (),
)
```

What the probe printed: each check's last line, and its `done` counts (`None` until it printed them).

| Member | Does |
|---|---|
| `ProbeReport.failed` |  |
| `ProbeReport.missing` | Expected checks that printed no line. |
| `ProbeReport.ok` | It finished, nothing failed and every expected check printed. |
| `ProbeReport.table() -> str` | The results as a table: check, verdict, detail; then the counts. |
| `ProbeReport.to_dict() -> dict[str, object]` |  |

### `Profiles`

*value*

```python
Profiles = str | collections.abc.Sequence[str] | None
```

### `read_lock`

*function*

```python
read_lock(path: Path) -> Lock
```

The lockfile at *path* (`build/tundravm.lock`), as `tundravm.compile(image, lock=)` and `plan(image, lock=)` take it.

### `read_probe`

*function*

```python
read_probe(console: str | BootResult, expected: Sequence[str] = ()) -> probe.Report
```

What the boot probe (`tundravm-probe.service`, which every development profile carries) printed on *console*: each check's last line and its `done` counts. A production boot's report is empty. *expected* names checks that must have printed.

### `run_cli`

*function*

```python
run_cli(*argv: str | os.PathLike[str]) -> tuple[int, str, str]
```

Run `tundravm.cli.main` in-process; returns `(exit_code, stdout, stderr)`.

### `scan_console`

*function*

```python
scan_console(text: str) -> ConsoleScan
```

Read systemd's status lines and the kernel's fatal messages out of *text*.

### `UnitView`

*class*

```python
UnitView(name: str, path: str, text: str, *, enabled: bool)
```

One compiled systemd unit: its text, parsed sections and whether the image enables it.

| Member | Does |
|---|---|
| `UnitView.after(unit: str) -> bool` | Whether `After=` names *unit*. |
| `UnitView.before(unit: str) -> bool` | Whether `Before=` names *unit*. |
| `UnitView.binds_to(unit: str) -> bool` | Whether `BindsTo=` names *unit*. |
| `UnitView.conflicts(unit: str) -> bool` | Whether `Conflicts=` names *unit*. |
| `UnitView.credentials` | `LoadCredential=` as `{name: path}`. |
| `UnitView.directories` | `{"state": ..., "logs": ..., "runtime": ..., "cache": ...}` for the directory directives the unit sets (`StateDirectory=` ...). |
| `UnitView.dynamic_user` | Whether `[Service] DynamicUser=yes`. |
| `UnitView.environment` | Every `[Service] Environment=` assignment, later ones winning. |
| `UnitView.exec_start` | The `[Service] ExecStart=` command (the last one), `None` for a non-service. |
| `UnitView.get(section: str, key: str) -> str \| None` | The last value of *key* in *section*, the one systemd uses; `None` when unset. |
| `UnitView.hardening` | The sandbox directives the unit sets (`ProtectSystem=` ...), every value in order. |
| `UnitView.part_of(unit: str) -> bool` | Whether `PartOf=` names *unit*. |
| `UnitView.requires(unit: str) -> bool` | Whether `Requires=` names *unit* (a unit or a target). |
| `UnitView.user` | The `[Service] User=`. |
| `UnitView.values(section: str, key: str) -> tuple[str, ...]` | Every value of *key* in *section*, in order (empty when unset). |
| `UnitView.wanted_by` | The `[Install] WantedBy=` targets. |
| `UnitView.wants(unit: str) -> bool` | Whether `Wants=` names *unit*. |

### `UPDATE_GOLDEN_ENV`

*value*

```python
UPDATE_GOLDEN_ENV = 'TUNDRAVM_UPDATE_GOLDEN'
```
