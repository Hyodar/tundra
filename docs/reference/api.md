# API reference

Generated from the `__all__` of `tundravm`, `tundravm.ports`, `tundravm.systemd`, `tundravm.components`, `tundravm.components.tee`, `tundravm.components.network`, `tundravm.components.access`, `tundravm.components.container`, `tundravm.components.observability`, `tundravm.storage`, `tundravm.secrets`, `tundravm.sources`, `tundravm.errors`, `tundravm.testing` by `uv run python -m tundravm._reference docs/reference` (Python 3.12). Do not edit it by hand: `tests/docs/test_reference.py` fails when it is stale.

- [`tundravm`](#tundravm)
- [`tundravm.ports`](#tundravmports)
- [`tundravm.systemd`](#tundravmsystemd)
- [`tundravm.components`](#tundravmcomponents)
- [`tundravm.components.tee`](#tundravmcomponentstee)
- [`tundravm.components.network`](#tundravmcomponentsnetwork)
- [`tundravm.components.access`](#tundravmcomponentsaccess)
- [`tundravm.components.container`](#tundravmcomponentscontainer)
- [`tundravm.components.observability`](#tundravmcomponentsobservability)
- [`tundravm.storage`](#tundravmstorage)
- [`tundravm.secrets`](#tundravmsecrets)
- [`tundravm.sources`](#tundravmsources)
- [`tundravm.errors`](#tundravmerrors)
- [`tundravm.testing`](#tundravmtesting)
- [Type aliases](#type-aliases)

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
) -> tundravm.declarative.lifecycle.Tree
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
    base: Debian = Debian(),
    packages: Sequence[str | Package] = (),
    files: Sequence[File | Tree | Build] = (),
    sysctl: Mapping[str, str | int | bool] = {},
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
    profiles: Mapping[str, Profile] = {},
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
    environment: Mapping[str, str | Binding | Endpoint] = {},
    logs: str | VolumeDirectory | None = None,
    runtime: str | None = None,
    package: str | None = None,
    egress: Egress | None = None,
    description: str | None = None,
)
```

A program the image runs, as its own user, in a tested sandbox.

| Member | Does |
|---|---|
| `Service.binaries() -> tuple[Any, ...]` | The built binary declarations this service's argv pulls in. |
| `Service.bindings` | Every credential this service binds: `credentials=`, then each one its argv or `environment=` names. |
| `Service.command() -> tuple[str, ...]` | The argv as words: a built first element becomes its installed path, an `Endpoint` its URL, a credential `${CREDENTIALS_DIRECTORY}/<name>`. |
| `Service.endpoint(port: int, *, path: str = '/', scheme: str = 'http') -> Endpoint` | Where this service answers on the loopback, with its readiness: what an ingress fronts (`upstream=app.endpoint(8080)`) or a dependent requires. It opens no port and changes no listen address. |
| `Service.metrics` | Its unit's series as PromQL selectors, for a `RecordingRule`: `f"max({app.metrics.active})"`. |
| `Service.packaged(name: str, *, package: str \| None = None, hardening: Sandbox = 'static', requires: Sequence[Requirement] = (), readiness: TcpProbe \| HttpProbe \| None = None, expose: Sequence[PortValue] = (), unit: DropIn \| None = None) -> Service` | The unit *name* the Debian *package* ships (default: the unit's stem), enabled and run under `hardening=`, which replaces the package's own sandbox. |
| `Service.probe_unit` | The bounded probe unit a dependent waits for, when readiness needs one. |
| `Service.ready` | "This service is ready": what another service's `requires=` takes. |
| `Service.ref` | What a `Profile` change targets. |
| `Service.secrets` | The delivered secrets this service binds. |
| `Service.state_directory` | The working directory a `state=` implies. |
| `Service.stem` | The unit name without its suffix: the generated user's name. |
| `Service.unit_name` | The `.service` unit this lowers to. |
| `Service.variables() -> dict[str, str]` | `environment=` as text: a credential becomes `%d/<name>`, systemd's specifier for the directory the service reads it in, an endpoint its URL. |
| `Service.volume_directories` | *state* and *logs*, those on a volume: what the image makes for it at boot. |

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
    owner: str | None = None,
)
```

An account `useradd` creates at build. *owner* names the declaration it belongs to (`Login('ops')`, `Container('web')`): no other declares it again, and no service runs as it by default.

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

| Member | Does |
|---|---|
| `TCP.address` | `0.0.0.0:<port>`: the listen address an argv word names, so the port is typed once (`[app, port.address]` beside `expose=[port]`). |

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

| Member | Does |
|---|---|
| `UDP.address` | `0.0.0.0:<port>`: the listen address an argv word names, so the port is typed once (`[app, port.address]` beside `expose=[port]`). |

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
    env: Mapping[str, str] = {},
    env_file: str | Sequence[str] | None = None,
    credentials: Mapping[str, str | SecretFile] = {},
    state: str | None = None,
    logs: str | None = None,
    runtime: str | None = None,
    cache: str | None = None,
    write_paths: Sequence[str] = (),
    restart: Literal['always', 'on-failure', 'on-abnormal', 'no'] | None = None,
    restart_sec: str | None = None,
    timeout_start: str | None = None,
    timeout_stop: str | None = None,
    limits: Mapping[str, str | int] = {},
    hardening: Hardening | None = None,
    after: Sequence[UnitTarget] = (),
    requires: Sequence[UnitTarget] = (),
    wants: Sequence[UnitTarget] = (),
    before: Sequence[UnitTarget] = (),
    part_of: Sequence[UnitTarget] = (),
    binds_to: Sequence[UnitTarget] = (),
    conflicts: Sequence[UnitTarget] = (),
    extra: Extra = {},
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
    protect_system: ProtectSystem = 'strict',
    protect_home: bool = True,
    private_tmp: bool = True,
    private_devices: bool = True,
    devices: Sequence[str] = (),
    no_new_privileges: bool = True,
    capabilities: Sequence[str] | None = (),
    jit: bool = False,
    network: AddressFamilies = 'inet',
    syscalls: Sequence[str] = ('@system-service',),
    deny_syscalls: Sequence[str] = ('@privileged', '@resources'),
    kernel: bool = True,
    sandbox: bool = True,
    umask: str | None = '0077',
)
```

The sandbox a generated unit runs in: one field per knob, and reviewed presets. Each default is the strict profile's value; a preset (`Service(hardening="jit")`) sets every field, its own changes over them.

| Member | Does |
|---|---|
| `Hardening.entries() -> tuple[tuple[str, str], ...]` | The `[Service]` directives, in the canonical hardening order. |
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

Components: the API for writing one. The shipped library lives in the submodules.

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
| `Component.senders() -> tuple[str, ...]` | The users whose sockets send `outbound`, which the firewall accepts it for alone (`meta skuid`): those of the services it composes, root left out. A component whose traffic a packaged daemon sends overrides it with that daemon's user. |
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
    export: str | None = None,
)
```

Where a component answers, with its readiness: an export a caller can wire.

| Member | Does |
|---|---|
| `Endpoint.address` | `host:port`, with an IPv6 host in brackets. |
| `Endpoint.loopback` | Whether only the image itself can reach it (`127.0.0.0/8`, `::1`, `localhost`). |
| `Endpoint.url` | `scheme://host:port/path`. |
| `Endpoint.with_scheme(scheme: str) -> Endpoint` | The same address under *scheme* (`ws` for a JSON-RPC server's WebSocket). |

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
| `Rule.severity_text` | The severity as the lint reference and `--explain` print it: `error (warning in a development profile)`. |

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

## tundravm.components.tee

The confidential-VM baseline every image implies: `TeeBase` and `Tdxs`, the evidence service, whose readiness is `EVIDENCE`. Passing one in `components=` replaces the default.

### `EVIDENCE`

*value*

```python
EVIDENCE = ServiceReady(unit='tdxs.service')
```

### `Tdxs`

*dataclass*

```python
Tdxs(
    *,
    source: Git | Local = ...,
    validator: TdxsType | None = None,
    expected_measurements: Mapping[str, str] = {},
    check_revocations: bool = False,
    get_collateral: bool = False,
)
```

The evidence service: `tdxs` from `tools/` of tundravm's repository, socket-activated on `/var/tdxs.sock`, which produces the TDX quote an attestation is checked against.

| Member | Does |
|---|---|
| `Tdxs.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `tdxs-validator-unchecked`: a validator with nothing to compare a quote to. |
| `Tdxs.from_policy(policy: str \| Path \| Mapping[str, object], *, validator: TdxsType = 'tdx', allow_placeholder: bool = False, **kwargs: Any) -> Tdxs` | A verifier whose `expected_measurements` are *policy*'s registers. |
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
| `TeeBase.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `tee-base-clock`: an image without authenticated time trusts the host's clock. |
| `TeeBase.kernel_cmdline` | `mitigations=auto,nosmt spec_store_bypass_disable=on`: every CPU vulnerability mitigated, SMT off where that needs it, Speculative Store Bypass off for every process (`TEE_KERNEL_CMDLINE` says why each). |
| `TeeBase.outbound() -> Egress` | With *time*, NTS key exchange (TCP 4460) to each of *nts_servers*. NTP on UDP 123 goes where the key exchange says (RFC 8915 4.1.7), so the firewall accepts it to any address for chrony's user alone (`CHRONY_NTP`). The `Network` names where DNS goes. |
| `TeeBase.senders() -> tuple[str, ...]` | chrony's user, whose daemon makes the key exchange. |

## tundravm.components.network

The network baseline every image implies: `Network` (also `image.network`, configured with `StaticAddress` and `Egress`, whose peers a `Host` pins) and `Firewall` (switchable with `Mode`). Passing one in `components=` replaces the default.

### `Egress`

*dataclass*

```python
Egress(
    ports: Sequence[PortValue] = (),
    hosts: Sequence[str | Host] = (),
    deny: Sequence[str | Host] = (),
    metadata: bool = False,
)
```

Outbound traffic the image, or one service, may send; everything else is dropped.

| Member | Does |
|---|---|
| `Egress.pinned` | The `Host` entries of *hosts*, then of *deny*, in order. |

### `Firewall`

*dataclass*

```python
Firewall(
    *,
    allow: Sequence[PortValue] = (),
    egress: Egress | None = None,
    modes: Mapping[str, Mode] = {},
)
```

The inbound (and optionally outbound) packet filter, nftables in the image.

| Member | Does |
|---|---|
| `Firewall.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `firewall-ssh-open`: port 22 opened by hand instead of by `SshAccess`; `egress-unfiltered`: a production profile that sends anywhere; `firewall-modes-kernel`: modes on a kernel whose TDX switches are all refused. |
| `Firewall.commands` | Its `Login` commands, both run as root and refused without *modes*: `firewall.commands.status` runs `/usr/bin/mode-switch status`, which prints the mode in force, the switches since boot and whether RTMR3 replays them, and `firewall.commands.switch` runs `/usr/bin/mode-switch to {{mode}}`, its `mode` param one of *modes*, which switches to that mode and extends RTMR3 with it. |
| `Firewall.lowered_modes() -> tuple[FirewallMode, ...]` | *modes* as the ruleset takes them, production first. |

### `Host`

*dataclass*

```python
Host(
    name: str,
    addresses: Sequence[str],
    *,
    port: int | None = None,
    aliases: Sequence[str] = (),
)
```

A peer pinned to its addresses, in `Egress(hosts=)`: `Host("api.example", ["203.0.113.7", "203.0.113.8"], port=443)`.

| Member | Does |
|---|---|
| `Host.entries` | One `"ADDRESS"` or `"ADDRESS:PORT"` (`"[IPv6]:PORT"`) per address: what the firewall accepts, as the string entries of `Egress(hosts=)` spell it. |
| `Host.names` | *name*, then *aliases*: what resolves to *addresses*. |

### `Mode`

*dataclass*

```python
Mode(
    ingress: Sequence[PortValue | ContainerSsh | None] = (),
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
| `Network.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `network-no-route`: a static address without a gateway reaches its subnet only. |
| `Network.online` | "An interface is configured": `systemd-networkd-wait-online`, bounded. |
| `Network.outbound() -> Egress` | DNS over TLS (TCP 853) to each of *dns*. |
| `Network.senders() -> tuple[str, ...]` | `systemd-resolved`'s user, which asks the resolvers. |

### `StaticAddress`

*dataclass*

```python
StaticAddress(address: str, gateway: str | None = None)
```

A fixed address for `Network`: `StaticAddress("10.0.0.5/24", gateway="10.0.0.1")`.

| Member | Does |
|---|---|
| `StaticAddress.version` | 4 or 6. |

## tundravm.components.access

The ways in for people: `SshAccess` (root, and each non-root `Login` with its `Command` values, run as the login or as root, each an argv template over the `CommandParam` values the client types, a `ParamType` their domain) and `DevAccess` (`image.dev`).

### `Command`

*dataclass*

```python
Command(
    params: Sequence[CommandParam] = (),
    runs: Sequence[str] | None = None,
    *,
    sudo: bool = False,
    modes: Sequence[str] = (),
    dev: bool = False,
    help: str | None = None,
)
```

A command of a `Login`, as an argparse subparser: `Command(params=[CommandParam("mode", choices=["a", "b"])], runs=["/usr/bin/switch", "{{mode}}"], sudo=True, help="switch the mode")`.

| Member | Does |
|---|---|
| `Command.notes(root: bool) -> list[str]` | What the listing says beside its help: `as root`, `maintenance only`. |
| `Command.payload(name: str, profiles: Sequence[str]) -> dict[str, object]` | What `show --json` says of it, *profiles* the ones it exists in. |
| `Command.shown() -> str` | The argv as the help's `runs:` line prints it: `<lines>` for a value, `[-f]` for a flag's word, `(as root)` after a root command's. |
| `Command.sudoers() -> list[str]` | The sudoers commands admitting every argv it can produce: each flag given and not, one line per choice value, a type's pattern where it has one (then the line is one anchored regular expression over the arguments). |
| `Command.usage(name: str) -> str` | The command as the client types it: `logs [--lines N] [--follow]`. |

### `CommandParam`

*dataclass*

```python
CommandParam(
    name: str,
    type: builtins.type | ParamType = str,
    *,
    choices: Sequence[str] | None = None,
    min: int | None = None,
    max: int | None = None,
    default: object = None,
    help: str | None = None,
)
```

A param of a `Command`, as `add_argument` takes it: `CommandParam("--lines", type=int, min=1, max=10000, default=200, help="lines from the end")`.

| Member | Does |
|---|---|
| `CommandParam.dest` | Its name in `runs` and the program: without the dashes, `-` as `_`. |
| `CommandParam.flag` | Whether it is a flag (`type=bool`). |
| `CommandParam.option` | Whether it is an option (`--name`) rather than positional. |
| `CommandParam.payload() -> dict[str, object]` | What `show --json` says of it. |
| `CommandParam.usage(bracketed: bool) -> str` | The param as the client types it: `<mode>`, `--lines N`, `[--follow]`. |

### `DevAccess`

*dataclass*

```python
DevAccess(*, ssh_keys: Sequence[str] = (), password: str | None = None)
```

Debug access to a development build: `dev=DevAccess(ssh_keys=["~/.ssh/id.pub"])`.

| Member | Does |
|---|---|
| `DevAccess.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `dev-access-rsa-key`: an RSA key where every current client makes ed25519. |
| `DevAccess.public_keys() -> tuple[str, ...]` | The public key lines, reading every entry that names a `.pub` file. |
| `DevAccess.tools() -> tuple[object, ...]` | The debugging tools and, with *password*, the console login. |

### `Login`

*dataclass*

```python
Login(
    name: str,
    keys: Sequence[str] | Secret = (),
    commands: Mapping[str, Command] | None = None,
    shell: str | None = None,
    owner: bool = False,
    home: bool = False,
    help: str | None = None,
)
```

An account other than root that logs in over SSH into its commands: `Login("tenant", keys=[...], help="run the app", commands={"status": Command(runs=["/usr/bin/app-status"])})`.

| Member | Does |
|---|---|
| `Login.account_shell` | The login shell: its program, else *shell*. |
| `Login.built_in(development: Sequence[str]) -> Login` | The login in an image whose *development* profiles compile its `dev=True` commands. |
| `Login.config() -> str` | The `sshd_config.d` file: its own keys only, no forwarding, its program if any, with a terminal only in a development build (`terminal`). |
| `Login.declarations() -> Iterator[Declaration]` | The account, its sshd block, its keys, its program and its sudo lines. |
| `Login.delivered` | Whether the keys arrive after attestation (a `Secret`, or the owner's). |
| `Login.developed` | Whether a development build holds more of it: a command says `dev=True`. |
| `Login.development() -> Login` | The login as a development build holds it: its `dev=True` commands' sudoers lines too, and a terminal for whichever command the client asks one for (`ssh -t`). Its program is the same; the build compiles `dev.go` in. |
| `Login.entries` | The commands this build holds, sorted by name: every command but a `dev=True` one, which `development` holds too. |
| `Login.key_file` | Where its keys are read: `/etc/ssh/authorized_keys.d/<name>`. |
| `Login.listing() -> list[str]` | What its program prints for an empty request, every command of this build. |
| `Login.options` | The `authorized_keys` options every baked key carries: the program's allow a terminal (`pty`) only with `terminal`. |
| `Login.program() -> dict[str, str]` | The files of its program, by name: `main.go`, `dev.go` with `dev=True` commands, `cli.go` (the runtime, `tools/logincli/cli.go` of the tundravm that runs) and `go.mod` (no dependency, the tools' `go` line). |
| `Login.program_path` | `/usr/libexec/tundravm/cli/<name>`, or `None` for a shell login. |
| `Login.sudo_rules() -> tuple[str, ...]` | The `sudoers` lines: every argv each root command of this build can produce. |
| `Login.sudoers() -> str` | The `sudoers.d` file: exactly `sudo_rules`. |
| `Login.sudoers_file` | `/etc/sudoers.d/<name>`, or `None` without a root command. |

### `ParamType`

*class*

```python
ParamType()
```

The domain of a `CommandParam`: how the generated program checks a value, what a root command's sudoers line admits, and how the help spells it.

| Member | Does |
|---|---|
| `ParamType.check() -> str` | A Go expression over the string `value` yielding an `error`: `Match(value, "^[0-9a-f]{64}$")`. |
| `ParamType.pattern() -> str \| None` | The anchored regular expression a root command's sudoers line admits the value with (`^[0-9a-f]{64}$`), or `None` for a type sudo cannot take. |
| `ParamType.usage() -> str` | The domain as the help prints it: `1-10000`, `production\|maintenance`. |

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
| `SshAccess.built_in(development: Sequence[str]) -> SshAccess` | The access in an image whose *development* profiles compile each login's `dev=True` commands into its program. |
| `SshAccess.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `ssh-keys-baked`: keys baked into a cloud image let in whoever holds them; `ssh-login-shell`: a production login with a shell and no commands; `login-mode-undeclared`: a command offered in a mode the firewall lacks. |
| `SshAccess.development() -> SshAccess` | What a development build holds: each login's `dev=True` commands too. |
| `SshAccess.logins` | Every `Login`, in the order given. |
| `SshAccess.port` | The port `sshd` listens on. |

## tundravm.components.container

Containers: `Container`, a program in a Debian root of its own built with the image, its `Bind` mounts, and `ContainerSsh`, its own sshd.

### `Bind`

*dataclass*

```python
Bind(
    path: str | VolumeDirectory,
    *,
    at: str | None = None,
    writable: bool = False,
    file: bool = False,
)
```

A host *path* the container sees at *at* (default: the same path), read-only unless *writable*: `Bind("/var/log/lighthouse")`, `Bind(data.directory("input"), writable=True)`. *file* binds one file rather than a directory (a socket, a config). The path must exist when the container starts; the root gains its mount point. A volume directory is made for it: its service keeps it, the container's group traverses it (`2750`) and writes it when *writable* (`2770`), setgid so what either writes belongs to the group, and the container waits for the volume. A secret reaches a container as `credentials=` on a `Service` does, not as a bind.

| Member | Does |
|---|---|
| `Bind.directive` | `BindPaths=` for a writable bind, else `BindReadOnlyPaths=`. |
| `Bind.target` | Where the container sees it. |

### `Container`

*dataclass*

```python
Container(
    name: str,
    argv: Sequence[str] = (),
    *,
    packages: Sequence[str] = (),
    files: Sequence[File] = (),
    state: VolumeDirectory | None = None,
    user: str | None = None,
    expose: Sequence[PortValue] = (),
    egress: Egress | None = Egress(),
    requires: Sequence[Endpoint] = (),
    modes: Mapping[str, Egress] | None = {},
    serves: Sequence[PortValue] = (),
    probe: TcpProbe | None = None,
    ssh: ContainerSsh | None = None,
    hardening: Sandbox = 'static',
    binds: Sequence[Bind] = (),
)
```

*argv* run in a Debian root of its own: *packages* from the image's snapshot and *files*, built with the image as a read-only root image the payload ships, so the quote covers it, and nothing is fetched at boot.

| Member | Does |
|---|---|
| `Container.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `container-shell`: a production container whose program is a shell. |
| `Container.endpoint(port: int, *, path: str = '/', scheme: str = 'http') -> Endpoint` | Where the container answers the host on *port*, a port of *serves*: its veth address, which nothing outside the host reaches, ready once *probe* connects (`lighthouse = Lighthouse(execution=searcher.endpoint(8551))`). |
| `Container.exposed` | The ports DNATed to it: *expose*'s, and its sshd's. |
| `Container.group` | The group a host service joins to read what the container writes (`Service(..., groups=[searcher.group])`): its account's own, whose gid `uid` pins. The container writes group-readable files (`UMask=0027`), so a service in it reads them without running as the container's uid. |
| `Container.image` | Where the payload ships its root image. |
| `Container.kernel() -> tuple[KernelFragment, ...]` | With a veth, what a built kernel needs for it: veth and the firewall's nat. |
| `Container.loader` | The root oneshot that loads `loop` before the unit attaches its root image, unless `/dev/loop-control` is already there; `None` on a built kernel, which has it built in. |
| `Container.metrics` | Its unit's series as PromQL selectors, for a `RecordingRule`: `f"max({searcher.metrics.active})"`. |
| `Container.namespace` | The `<name>-net.service` that makes its namespace, `None` when it holds `lo` only (`PrivateNetwork=yes`). |
| `Container.network() -> ContainerNetwork \| None` | Its veth: `ve-<name>` (`ve-<first 7>-<4 hex>` past 12 characters) and a /30 of 169.254.0.0/16 the name's sha256 picks, outside third octets 0, 169 and 255. |
| `Container.relay(endpoint: Endpoint) -> str` | The socket in its namespace that relays *endpoint*, named after the export (`bob-nethermind-rpc.socket` for `nethermind.rpc`), else its port. |
| `Container.run_user` | The account the program runs as: *user*, else the container's name. |
| `Container.sshd_unit` | The unit running sshd: the container's own without *argv*, `<name>-ssh` beside it with one; `None` without *ssh*. |
| `Container.unit` | The container's `.service`. |
| `Container.veth` | Whether its namespace has a veth: it exposes a port or sends something. |

### `ContainerSsh`

*dataclass*

```python
ContainerSsh(port: int, keys: Secret | Sequence[str] = (), owner: bool = False)
```

The container's own sshd on *port*, letting its user in by key and with nothing else: `Container("bob", ssh=ContainerSsh(port=10022, owner=True))`.

| Member | Does |
|---|---|
| `ContainerSsh.delivered` | Whether the keys arrive after attestation (a `Secret`, or the owner's) rather than baked in. |

## tundravm.components.observability

The export path (`image.observability`): `Observability` with `JournalExport`, `PrometheusRemoteWrite` (its `RecordingRule` values, a `SigV4` credential) and `Proxy`; the `Selector` values a rule reads through a service's or a container's `UnitMetrics` and a volume's `VolumeMetrics`.

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
| `Observability.check(resolved: Resolved) -> tuple[Diagnostic, ...]` | `observability-plaintext`: an http:// sink sees what the TD logs in the clear; `observability-sink-unset`: a production profile's logs or metrics never leave the VM; `observability-export-unset`: the metrics name no keep-list and no rules, an error where a production profile writes them to *url*. |
| `Observability.development() -> Observability` | What a development profile builds: each sink whose *url* is outside the guest goes to its in-guest receiver, and its credential stays unbound. The proxy goes too when no sink outside the guest is left. A sink on the loopback stays as it is. |
| `Observability.for_image(name: str) -> Observability` | This export as the image *name* builds it: the rules group carries its name. |
| `Observability.outbound() -> Egress` | *proxy*, or each sink's `HOST:PORT` without one; nothing when every sink is in the guest. |

### `PrometheusRemoteWrite`

*dataclass*

```python
PrometheusRemoteWrite(
    url: str | None = None,
    credential: Secret | SigV4 | None = None,
    interval: str = '30s',
    buffer: str = '1h',
    export: str | Sequence[str] | None = None,
    rules: Sequence[RecordingRule] = (),
)
```

Send node metrics to *url*, a Prometheus remote-write receiver.

| Member | Does |
|---|---|
| `PrometheusRemoteWrite.config(proxy: Proxy \| None = None) -> dict[str, object]` | The agent's configuration: scrape the node exporter, keep `kept`, write to *url*, through *proxy* when given; without *url*, to the in-guest `sink`. |
| `PrometheusRemoteWrite.identity() -> tuple[Build, tundravm.declarative.model.Service]` | `image-metrics` from `tools/` and its service: as root without capabilities (the event log and the TDX device are root's), with the TDX device and AF_UNIX alone, rewriting its file in `IMAGE_METRICS_DIRECTORY` every *interval*. |
| `PrometheusRemoteWrite.kept() -> tuple[str, ...]` | The series names that leave, in order, once each: the records of the rules with `export=True`, then *export* with its presets expanded, then `IDENTITY_SERIES`. Without *export*, `health` leaves when there are no rules. A rule's record in *export* is refused: the rule says that itself. |
| `PrometheusRemoteWrite.rules_file(group: str) -> dict[str, object]` | `METRICS_RULES`: *rules* as one group named *group*, the image's name, in their order. |
| `PrometheusRemoteWrite.service(proxy: Proxy \| None = None) -> Service` | The `metrics-export` service: Prometheus in agent mode, in server mode with *rules*, through *proxy*; without *url*, to the in-guest `sink`, never through a proxy. |
| `PrometheusRemoteWrite.sink() -> Service` | `metrics-sink`: the in-guest receiver, plain HTTP on the loopback. |

### `Proxy`

*dataclass*

```python
Proxy(
    address: str,
    port: int,
    *,
    tls_name: str | None = None,
    passthrough: bool = False,
)
```

The fixed-address proxy both exporters reach their sinks through: `Proxy("10.0.0.9", 3128)`, which `proxy="10.0.0.9:3128"` also spells.

| Member | Does |
|---|---|
| `Proxy.check_sink(url: str) -> None` | Refuse a sink *url* a passthrough cannot carry: not `https://`, an address for a host, or another port than *port*. |
| `Proxy.endpoint` | `IP:PORT` (`[IPv6]:PORT`), what the firewall allows. |
| `Proxy.hosts(urls: Sequence[str]) -> tuple[HostsEntry, ...]` | The `/etc/hosts` entries mapping *address* to the TLS name, or to each of the sinks' *urls*' names through a passthrough; none without either. |
| `Proxy.url` | The URL the exporters reach the proxy at: `https://<tls_name>:<port>` or `http://<endpoint>`; `None` for a passthrough, which the exporters do not know of. |

### `RecordingRule`

*dataclass*

```python
RecordingRule(record: str, expr: str, *, export: bool = True)
```

A series the guest computes from others: `RecordingRule("node:up", "min(up)")`.

### `Selector`

*class*

```python
Selector(text: str, *, in_bytes: bool = False)
```

A PromQL selector, as the text a rule's expression holds: `tundravm_unit_active{unit="searcher.service"}`.

| Member | Does |
|---|---|
| `Selector.over(size: str) -> str` | `SELECTOR > bool BYTES`, *size* written `"128Gi"` or `"2048MiB"`. |

### `SigV4`

*dataclass*

```python
SigV4(region: str, secret: Secret)
```

AWS Signature V4 for a remote-write sink such as Amazon Managed Prometheus: `credential=SigV4("us-east-2", Secret("metrics-aws"))`.

### `UnitMetrics`

*dataclass*

```python
UnitMetrics(unit: str)
```

What `image-metrics` writes for one unit, as selectors: a `Service`'s or a `Container`'s `.metrics`. A container's processes are all in its unit's cgroup.

| Member | Does |
|---|---|
| `UnitMetrics.active` | `tundravm_unit_active`: 1 while the unit is active, else 0. |
| `UnitMetrics.cpu_seconds` | `tundravm_unit_cpu_seconds_total`: the CPU time systemd accounts to the unit's cgroup, a counter (`rate(...[5m])` is its CPUs in use). |
| `UnitMetrics.memory_bytes` | `tundravm_unit_memory_bytes`: the memory the unit's cgroup holds now. |
| `UnitMetrics.oom_kills` | `tundravm_unit_oom_kills_total`: the processes the OOM killer took in the unit's cgroup and below it (`memory.events`), a counter. |

### `VolumeMetrics`

*dataclass*

```python
VolumeMetrics(mount: str)
```

What the node exporter reports for one mount, as selectors: a `Volume`'s `.metrics`.

| Member | Does |
|---|---|
| `VolumeMetrics.available` | `node_filesystem_avail_bytes`: what the volume's filesystem has free. |
| `VolumeMetrics.size` | `node_filesystem_size_bytes`: the volume's filesystem's size. |
| `VolumeMetrics.used_ratio` | The share of the filesystem in use, from 0 to 1, in parentheses. |

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
| `Volume.metrics` | Its filesystem's series as PromQL selectors, for a `RecordingRule`: `data.metrics.available.over("128Gi")`. |
| `Volume.pipe` | The FIFO an operator (or a broker client) writes the passphrase into. |
| `Volume.ready` | "It is open, mounted, and its directories exist". |
| `Volume.serial` | The attachment name: QEMU's `serial=`, GCP's device name, Azure's LUN link. |
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

A directory on a `Volume`: `data.directory("app")`, bound as a state. `state / "output.log"` is the absolute path of a file in it.

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
    env: Mapping[str, str] = {},
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
    env: Mapping[str, str] = {},
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
    source: Git | Http | Local,
    *,
    name: str | None = None,
    bin: str | None = None,
    package: str | None = None,
    install: str | None = None,
    features: Sequence[str] = (),
    profile: str = 'release',
    env: Mapping[str, str] = {},
    auditable: bool | None = None,
    cache_namespace: str | None = None,
)
```

A Rust program built from *source* and installed at `executable`, named after *bin*, else *package*, else the directory *source* builds, as `GoBinary` is.

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
    env: Mapping[str, str] = {},
    packages: Sequence[str] = ('libicu76', 'libssl3t64', 'libstdc++6'),
    restore_args: Sequence[str] = (),
    properties: Mapping[str, str] = {},
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
    env: Mapping[str, str] = {},
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
    source: Git | Http | Local,
    *,
    name: str | None = None,
    package: str = '.',
    install: str | None = None,
    env: Mapping[str, str] = {},
    tags: Sequence[str] = (),
    ldflags: str = '-s -w -buildid=',
    cgo: bool = False,
    cache_namespace: str | None = None,
)
```

A Go program built from *source* and installed at `executable`, named after *package*'s last element (`"./cmd/server"` is `server`), else after the directory *source* builds (`Local("signer")` is `signer`; `name=` for a clash).

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
assert_probe(console: str | BootResult, expected: Sequence[str] = ()) -> ProbeReport
```

`read_probe`, raising `AssertionError` with its table unless the probe finished, nothing failed and every *expected* check printed.

### `assert_tree`

*function*

```python
assert_tree(
    tree: tundravm.declarative.lifecycle.Tree,
    golden: str | Path,
    *,
    update: bool | None = None,
) -> None
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
    registers: Mapping[str, bytes] = {},
    debug: bool = False,
    mode: str = 'simulated',
    credentials: tuple[str, ...] = ('api-token',),
    volumes: tuple[str, ...] = ('data',),
    ssh_key: bool = True,
    listeners: Mapping[int, bytes] = {},
    misbehave: Misbehaviour | None = None,
    received: dict[str, bytes] = {},
    unlocked: dict[str, bytes] = {},
    ssh_keys: list[str] = [],
    sessions: list[FakeSession] = [],
    first_nonce: bytes | None = None,
    pins: bool = False,
    owner: str | None = None,
    mismatch: str | None = None,
    confirmed: bool = False,
    signers: list[str] = [],
    modes: tuple[str, ...] = (),
    mode_events: list[str] = [],
    clock: int = 412,
    reply: dict[str, object] = {},
)
```

The endpoint of one boot: what it accepts, what it received.

| Member | Does |
|---|---|
| `FakeEndpoint.connect(host: str, port: int) -> FakeSession` | A new TLS session (the `connect=` the operator commands take). |
| `FakeEndpoint.evidence(session: FakeSession, nonce: bytes) -> dict[str, object]` | The evidence the endpoint answers *nonce* with, bound to *session*'s TLS key unless *misbehave* breaks it. |
| `FakeEndpoint.presented(host: str, port: int) -> bytes` | The DER certificate the attested TLS listener on *port* presents (the `presented=` that `attest` takes): around its bound key, or around a key the evidence does not bind when misbehaving or when *port* is no listener. |
| `FakeEndpoint.takes_ssh_key` | Whether `POST /ssh-key` installs a key: the first in a `simulated` mode, else only a confirmed owner's. |
| `FakeEndpoint.write(session: FakeSession, path: str, body: Mapping[str, object]) -> int` | Apply a delivery to *path* (`/credentials`, `/unlock`, `/owner`...) and return its HTTP status: 403 for a spent nonce or a bad signature. |

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
    calls: list[FakeCall] = [],
)
```

Records mkosi verbs instead of running them.

| Member | Does |
|---|---|
| `FakeMkosi.install(setattr: Callable[[object, str, object], None]) -> FakeMkosi` | Put this fake in place of the adapter's `invoke` and `needs_sudo` using *setattr* (pytest's `monkeypatch.setattr`, which undoes it after the test). |
| `FakeMkosi.invoke(argv: Sequence[str], *, cwd: Path, log: Path, on_output: Callable[[str], None] \| None = None, stdout: bool = False, terminal: bool = False) -> MkosiResult` | Record one mkosi call and answer it as mkosi would, without running anything. |
| `FakeMkosi.needs_sudo() -> bool` | Whether mkosi would run under sudo: *sudo*. |
| `FakeMkosi.verbs() -> list[str]` | The verb of each recorded call, in order: `["summary", "build"]`. |

### `FakeSources`

*dataclass*

```python
FakeSources(fetched: list[FakeFetch] = [])
```

Records source checkouts instead of fetching them.

| Member | Does |
|---|---|
| `FakeSources.fetch_source(source: Source, pin: str, dest: Path) -> None` | Make a checkout of *source* at *pin* in *dest*: one file naming them. |
| `FakeSources.git_output(checkout: Path, *args: str) -> str \| None` | What git prints for *args* in a fake *checkout*: its pin for `rev-parse`, a clean tree otherwise; `None` outside a fake checkout. |
| `FakeSources.install(setattr: Callable[[object, str, object], None]) -> FakeSources` | Put this fake in place of the fetch boundary using *setattr* (pytest's `monkeypatch.setattr`, which undoes it after the test). |
| `FakeSources.lock_dotnet_sdk(build: SourceBuild, pin: str, checkout: Path) -> LockedToolchain` | The .NET SDK a Dotnet build locks to: always `SDK`. |
| `FakeSources.prefetch_deps(build: SourceBuild, pin: str, checkout: Path, root: Path, **kwargs: object) -> str \| None` | Record an empty, complete dependency cache for *build* under *root*; `None` for a build without dependencies to prefetch. |

### `HttpResponse`

*dataclass*

```python
HttpResponse(url: str, status: int | None, body: str)
```

What one `wait_http` URL answered while the VM ran: `status` `None` when it never answered with a 2xx before the boot ended (`body` then holds the last error).

| Member | Does |
|---|---|
| `HttpResponse.ok` | Whether the URL answered with a 2xx. |

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

*alias*

```python
Lintable = Image | PlannedImage | Sequence[Diagnostic]
```

What the assertions check: an image (every profile, or those *profiles* names), one planned profile, or findings.

### `plan`

*function*

```python
plan(
    subject: Image | Component,
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
    subject: Image | Component,
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
    again: bool = False,
)
```

What the probe printed: each check's last line, and its `done` counts (`None` until it printed them).

| Member | Does |
|---|---|
| `ProbeReport.failed` | The checks whose last line was `fail`. |
| `ProbeReport.locked` | A check skipped because its volume waits for its key. |
| `ProbeReport.missing` | Expected checks that printed no line. |
| `ProbeReport.ok` | It finished, nothing failed and every expected check printed. |
| `ProbeReport.table() -> str` | The results as a table: check, verdict, detail; then the counts. |
| `ProbeReport.to_dict() -> dict[str, object]` | The report as JSON: `ok`, the `done` counts, each result, the missing checks. |

### `Profiles`

*alias*

```python
Profiles = str | Sequence[str] | None
```

A profile selection: one name, several names, or `None` for every declared profile.

### `read_lock`

*function*

```python
read_lock(path: Path) -> Lock
```

The lockfile at *path* (`build/tundravm.lock`), as `tundravm.compile(image, lock=)` and `plan(image, lock=)` take it.

### `read_probe`

*function*

```python
read_probe(console: str | BootResult, expected: Sequence[str] = ()) -> ProbeReport
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

## Type aliases

The names the signatures above use for a set of accepted values. None is imported: pass one of the values it stands for.

### `AddressFamilies`

*alias*

```python
AddressFamilies = Literal['inet', 'inet+netlink', 'unix', 'none', 'any']
```

`RestrictAddressFamilies=`: `"inet"` allows IPv4, IPv6 and Unix sockets, `"inet+netlink"` those and netlink, `"unix"` Unix sockets only, `"none"` no socket, `"any"` every family.

### `Argument`

*alias*

```python
Argument = str | int | Endpoint | Binding | Built
```

What an argv element may be: a word (`port.address` and `state / "file"` are words), an `int` rendered in decimal, an `Endpoint` rendered as its URL, a `Secret` (or `Credential`) rendered as the path the service reads it at, which binds it, or a built binary as the first element.

### `Binding`

*alias*

```python
Binding = Credential | Secret
```

What `credentials=` takes: a delivered `Secret`, or a fixed-path credential.

### `Bootable`

*alias*

```python
Bootable = str | os.PathLike[str] | HasPath
```

A UKI (`.efi`) or disk image path, the directory holding one, or an `Artifact` (`path` the disk, `boot_payload` its UKI).

### `BootPath`

*alias*

```python
BootPath = Literal['auto', 'kernel', 'disk']
```

How `boot` starts *artifact*: `"kernel"` passes the UKI to `-kernel`, `"disk"` attaches the disk image and lets OVMF boot its ESP, `"auto"` goes by the path's suffix (an artifact's `path`, a directory's single `.efi`).

### `Change`

*alias*

```python
Change = Edit | Remove
```

What `Profile(changes=)` holds: `value.ref.edit(...)` or `value.ref.remove()` (a `User` or `Group` ref makes the declaration's own).

### `Content`

*alias*

```python
Content = str | Package | File | Tree | Service | Component | User | Group
```

What an image's (or a profile's) content may be.

### `Context`

*alias*

```python
Context = tuple[tuple[str, str], ...]
```

A finding's extra `(key, value)` facts; a mapping passed instead is converted.

### `Declaration`

*alias*

```python
Declaration = (
    tundravm.declarative.model.Package
    | tundravm.declarative.model.File
    | FileTree
    | Group
    | User
    | Unit
    | tundravm.declarative.model.Service
    | Template
    | Hook
    | BootStep
    | Repository
    | Partition
    | Debloat
    | Setting
    | Key
    | Disk
    | Secrets
    | RuntimeTools
    | Build
    | tundravm.declarative.model.Kernel
    | KernelConfig
    | Task
    | Mount
    | tundravm.declarative.model.Firewall
    | ContainerRoot
    | Hosts
)
```

One item of a resolved profile: what the image's values and components lowered to, in the compiler's words; `Resolved.bind(value.ref)` finds the one a value made.

### `Extra`

*alias*

```python
Extra = Mapping[str, Mapping[str, str | tuple[str, ...]]]
```

`extra={"Service": {"OOMScoreAdjust": "-500"}}`: section -> directive -> value(s).

### `HostsEntry`

*alias*

```python
HostsEntry = tuple[str, tuple[str, ...]]
```

One `/etc/hosts` line: an address and the names that resolve to it.

### `Ingress`

*alias*

```python
Ingress = AttestedTLS | TLS
```

A way in: `AttestedTLS` or `TLS`.

### `Misbehaviour`

*alias*

```python
Misbehaviour = Literal['relay', 'replay', 'wrong-key', 'wrong-certificate', 'mode-log', 'service-quote']
```

`relay`: the session's TLS key changes after the evidence (the delivery would land on another session); `replay`: the evidence answers the first nonce it ever saw; `wrong-key`: the evidence binds a TLS key that is not the session's; `wrong-certificate`: an attested TLS listener presents a certificate whose key the evidence does not bind; `mode-log`: the evidence's firewall events leave out the last switch RTMR3 holds; `service-quote`: tdxs issued the quote to a `tdx`-group service (tag `signer`), not to the endpoint.

### `Pairs`

*alias*

```python
Pairs = tuple[tuple[str, str], ...]
```

Unit directives as `(key, value)` pairs, in the order a unit file lists them.

### `PortValue`

*alias*

```python
PortValue = TCP | UDP
```

A port a service opens or a rule names: `TCP(8080)` or `UDP(53)`.

### `PresetName`

*alias*

```python
PresetName = Literal['static', 'dotnet', 'jit', 'node', 'python']
```

The runtime classes `Hardening.preset` knows: `"static"` is the strict profile, `"jit"`, `"node"` and `"python"` allow writable code pages, `"dotnet"` that, the `@resources` system calls and netlink.

### `ProbeRunner`

*alias*

```python
ProbeRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
```

What runs a host probe's command for `doctor`: `subprocess.run` by default, a fake in a test.

### `ProtectSystem`

*alias*

```python
ProtectSystem = Literal['strict', 'full', 'yes', 'no']
```

`ProtectSystem=`: `"strict"` mounts the whole file system read-only but the unit's own directories, `"full"` `/usr`, the boot loader and `/etc`, `"yes"` the first two, `"no"` none.

### `Readied`

*alias*

```python
Readied = ServiceReady | BootReady
```

What proves one thing ready: a service's readiness, or a boot step finishing.

### `Readiness`

*alias*

```python
Readiness = HttpProbe | TcpProbe | Notify
```

What `readiness=` takes: an HTTP or TCP probe dependents wait on, or `Notify()` for a program that calls `sd_notify`.

### `Requirement`

*alias*

```python
Requirement = (
    ServiceReady
    | ComponentReady
    | BootReady
    | Endpoint
    | ServiceRef
    | UnitOutput
)
```

What `requires=` takes: a readiness handle, an export that carries one, or a setup task's `task.output(path)` (start after it, and only if it wrote *path*).

### `RuleCheck`

*alias*

```python
RuleCheck = Callable[['Lowered', str, 'ProfileState'], Iterator[Diagnostic]]
```

A compiler pass: the findings of one profile of the lowered image.

### `Sandbox`

*alias*

```python
Sandbox = Hardening | Literal['static', 'jit', 'node', 'python', 'dotnet', 'relaxed']
```

What `hardening=` takes: a preset by name (`Hardening.preset(name)`, or `Hardening.relaxed()` for `"relaxed"`), or a `Hardening` with deviations.

### `Severity`

*alias*

```python
Severity = Literal['error', 'warning', 'info']
```

How much a finding matters: `"error"` fails `tundravm check` and `build`, `"warning"` and `"info"` are reported only.

### `Source`

*alias*

```python
Source = GitSource | HttpSource | LocalSource
```

A build's source as the lock pins it: a git checkout, an HTTP download or a local directory.

### `Stage`

*alias*

```python
Stage = Literal['skeleton', 'extra']
```

Which tree of the compiled image a file is in: `"skeleton"` before packages install, `"extra"` after them.

### `Target`

*alias*

```python
Target = Literal['qemu', 'azure', 'gcp']
```

Where an image boots: `"qemu"` a local QEMU disk, `"azure"` an Azure confidential VM disk (`.vhd`), `"gcp"` a Google Cloud confidential VM image (`.tar.gz`).

### `TdxsType`

*alias*

```python
TdxsType = Literal['tdx', 'azure', 'gcp', 'simulator']
```

The quote kind `tdxs` issues or validates: `"tdx"` a raw TDX quote, `"azure"` and `"gcp"` that cloud's attestation of one, `"simulator"` the development stand-in.

### `UnitTarget`

*alias*

```python
UnitTarget = str | Ref
```

A relation target: a unit name (`"tdxs.socket"`) or a ref to a unit declaration.

### `Unlock`

*alias*

```python
Unlock = OperatorKey | TpmSeal | BrokerKey
```

How a `Volume`'s LUKS2 key arrives.
