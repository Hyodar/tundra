# Typed systemd units (design, 2026-10-18)

Goal: nobody writes a systemd unit file by hand. A recipe says *what* runs (a daemon, a boot
task, a scheduled job, a socket-activated server, a mount) in two to four arguments, and the
compiler renders the unit, its drop-ins, its enablement and its hardening. Derived from the
working tree on master `01b53ff` (line numbers as of 2026-10-18): `src/tundravm/declarative/model.py`, `declarative/state.py`, `declarative/_compile.py`,
`compiler/emit_mkosi.py`, the built-in modules, the examples, the templates and the committed
golden tree `examples/surge-tdx-prover/mkosi/`.

## 0. Principles

tundravm is a more accessible way to use mkosi. The SDK is opinionated about mkosi, systemd and
Debian, and this design follows that stance:

1. **A small set of declarations, not systemd's surface.** Five shapes cover what images run:
   `Service` (long-running daemon), `Task` (oneshot at boot, or on a schedule: the timer is
   generated), `Service(socket=Listen(...))` (socket activation), `Mount`, and `Unit(name, ...)`
   for packaged units (enable, mask, typed drop-in). There is no `Path`, `Slice`, `Automount`
   or `Target` type; the escape hatch covers them. A typical service is
   `Service("app", "/usr/bin/app --port 8080", state="app")`.
2. **Secure by default.** Every generated `Service` and `Task` gets the reviewed
   `Hardening.strict()` profile unless the recipe relaxes it by name
   (`hardening=Hardening.relaxed()`, or a per-field opt-out such as
   `Hardening.strict(jit=True)`); `Hardening.none()` exists and is linted. Services run as a
   dedicated system user (declared for you) or `DynamicUser=`, never as root by accident.
   Writable state goes through `StateDirectory=`/`LogsDirectory=`/`RuntimeDirectory=`, not
   `ExecStartPre=mkdir`. Daemons restart on failure; boot tasks time out.
3. **Debian + systemd only.** No init-system or distribution abstraction. Debian paths
   (`/usr/lib/systemd/system`, `/etc/default/<name>`, `/usr/sbin/nologin`), Debian's
   packaged units customized through drop-ins, Debian trixie's systemd (257) as the directive
   reference.
4. **The escape hatch is second-class.** `extra={"Service": {"Key": "value"}}` and raw
   `Unit(name, text)` keep working, are validated (section names, known directives), and every
   use shows up in `tundravm lint` as an `info` finding that names the typed alternative.
5. **Byte fidelity where it is a contract.** The `nethermind-v1` golden tree stays byte for byte;
   typed rendering never silently changes a committed unit (section 3).

The same stance applies to mkosi knobs outside units (section 6): recipes should rarely need an
mkosi section or key name.

## 1. Inventory

### 1.1 Hand-written unit text

"Golden" marks text that lands in `examples/surge-tdx-prover/mkosi/` (checked by
`tests/compiler/test_surge_golden.py` and, for the literals, `tests/compiler/test_surge_contents.py`);
it must stay byte-identical.

| Where | Unit | Directives | Golden |
|---|---|---|---|
| `examples/fragments/nethermind.py:48` (`NETHERMIND_UNIT`, used at :115 with `needs=[RUNTIME_INIT]`) | `nethermind-surge.service` | Unit: Description. Service: User, Group, Restart, LimitNOFILE, EnvironmentFile, ExecStart (4 continuation lines, no indent). Install: WantedBy=default.target | yes (`needs` injects `After=`/`Requires=runtime-init.service`) |
| `examples/fragments/raiko.py:43` (`RAIKO_UNIT`, :90) | `raiko.service` | Unit: Description, After/Requires=tdxs.service. Service: User, Group, Restart, ExecStart. Install: WantedBy=default.target | yes (injection prepends runtime-init) |
| `examples/fragments/taiko_client.py:36` (`TAIKO_CLIENT_UNIT`, :76) | `taiko-client.service` | as raiko, no relations | yes |
| `src/tundravm/_modules/tdxs.py:102` `render_service_unit` (via `declarative/utils.py:385` `Tdxs`) | `tdxs.service` | Unit: Description, Requires=tdxs.socket (no After). Service: User, Group, WorkingDirectory, Type=notify, ExecStart (indented continuations), Restart=on-failure. Install: WantedBy=default.target | yes (`NethermindBase` composes `Tdxs()`) |
| `src/tundravm/_modules/tdxs.py:124` `render_socket_unit` (`utils.py:391`) | `tdxs.socket` | Unit: Description. Socket: ListenStream, SocketMode, SocketUser, SocketGroup, Accept=false. Install: WantedBy=sockets.target | yes |
| `src/tundravm/_modules/devtools.py:35` `SERIAL_CONSOLE_SERVICE` (`utils.py:447`, enabled by a raw `Hook` at :451) | `serial-console.service` | Unit: Description, After. Service: Type=oneshot, ExecStart=`systemctl enable serial-getty@ttyS0`, ExecStartPost=`systemctl start ...`, RemainAfterExit. Install: WantedBy=minimal.target | yes (devtools variant) |
| `src/tundravm/platforms/azure.py:114` `AZURE_PROVISIONING_SERVICE` (+ `_ONLINE` variant at :132 by string replace; written by `state.py` `_azure`) | `azure-complete-provisioning.service` | Unit: Description, After, Requires (or After/Wants network-online.target). Service: Type=oneshot, ExecStart, RemainAfterExit. Install: WantedBy=minimal.target | yes (azure variant) |
| `src/tundravm/_modules/init.py:213` `_aggregate` (+ `render_service_unit` :174, `_network` :178) | `runtime-init.service` | Unit: Description, After/Requires (or Wants). Service: Type=oneshot, ExecStart, RemainAfterExit. Install: WantedBy=minimal.target | yes (nethermind-v1 form) |
| `src/tundravm/_modules/init.py:199` `_step_unit_text` | `runtime-init-<step>.service` (current dialect) | Unit: Description, After/Requires network + step deps. Service: Type=oneshot, ExecStart, RemainAfterExit. No Install | no |
| `src/tundravm/compiler/emit_mkosi.py:108` `MINIMAL_TARGET_UNIT` | `minimal.target` (skeleton, `/etc/systemd/system`) | Unit: Description, Requires, Conflicts, After, AllowIsolate. Install: WantedBy. No trailing newline | yes |
| `examples/nethermind-base/network-setup.service` (read by `examples/nethermind_base.py:99`, shipped as `File.text(..., stage="skeleton")`) | `network-setup.service` | Unit: Description, DefaultDependencies=no, Before, Wants. Service: Type=oneshot, 4x ExecStart with **relative** binaries (`ip`, `chattr`), RemainAfterExit. Install: WantedBy=sysinit.target. No trailing newline | yes |
| `src/tundravm/declarative/state.py:789` (`_unit`, packaged unit with `needs`) | `<unit>.d/10-tundravm-needs.conf` | Unit: After, Requires | no (surge's packaged units have no `needs`) |
| `src/tundravm/declarative/state.py:875` `inject_after_init` / `_inject_one` | edits any shipped unit's text | prepends to After/Requires, or inserts them after the last `[Unit]` line | yes (produces the golden `After=runtime-init.service ...` lines) |
| `src/tundravm/_modules/secret_delivery.py:122` `service_dropins` | `<service>.d/tundravm-secrets.conf` | Service: EnvironmentFile (`-` prefix when optional) | no |
| `src/tundravm/templates.py:64` `HELLO_UNIT` (`minimal` template, :85) | `hello.service` | Unit: Description. Service: Type=oneshot, ExecStart. Install: WantedBy=minimal.target | no (parity fixture `init-minimal`) |
| `examples/03_fragments.py:43` `TIMER` (used at :118 next to `Service("healthcheck", type="oneshot")` at :117) | `healthcheck.timer` | Timer: OnBootSec, OnUnitActiveSec. Install: WantedBy=timers.target. No `[Unit]` | no (parity fixture) |

The surge `system` fragment (`examples/surge-tdx-prover/image.py:115-121`) and
`examples/03_fragments.py:64,98` only control packaged units (`Unit(name, enabled=..., masked=...)`);
they write no text.

Generated (not hand-written) unit text, for completeness: `compiler/emit_mkosi.py:441`
`_systemd_unit_content` renders every `Service`; everything else above is a string literal or
string concatenation.

### 1.2 What `Service(...)` supports today

`declarative/model.py:948`. Fields: `name`, `exec_start` (`str`, shlex-split at lowering, or an
argv tuple), `description`, `user`, `group`, `working_dir`, `env` (Mapping), `env_file`,
`exec_start_pre`, `after`/`requires`/`wants` (unit-name strings), `wanted_by` (one target),
`type` (`simple|exec|oneshot|notify|forking`), `restart` (`always|on-failure|no`, default `no`),
`limits` (Mapping, validated against `_LIMIT_RESOURCES` in `state.py:115`), `kill_mode`,
`timeout_stop`, `security` (`strict|default|none`, default `default`), `needs` (default
`[RUNTIME_INIT]`), `allow_secret`.

Missing: `ExecStartPost`/`ExecStop`/`ExecReload`, `RestartSec` (hard-coded `5`),
`TimeoutStartSec`, `RemainAfterExit` (so a oneshot `Service` cannot stay active),
`DynamicUser`, `SupplementaryGroups`, `StateDirectory`/`RuntimeDirectory`/`LogsDirectory`/
`CacheDirectory`, `LoadCredential`, `ReadWritePaths`, devices and capabilities, `Before`,
`PartOf`, `Conditions`, sockets, timers, and any way to skip `[Install]`. `ServiceSpec.extra_unit`
(`models.py:180`) is an escape hatch the IR and emitter honor, but lowering always sets it to
`{}` (`state.py:837`): no declaration reaches it.

### 1.3 How lowering renders units today

- **`Unit` (`state.py:760`).** `content` (str or `Path`, read as UTF-8) is written verbatim to
  `/usr/lib/systemd/system/<name>`; `needs` is applied by text surgery (`inject_after_init`,
  `state.py:875`): the wait units are prepended to an existing `After=`/`Requires=` in `[Unit]`,
  or both lines are inserted after the last non-blank `[Unit]` line (`After=` first). A packaged
  unit (`content=None`) with `needs` gets `<unit>.d/10-tundravm-needs.conf` with
  `[Unit] After=... Requires=...`. `enabled=True` calls `enable_unit`; `enabled=False` and
  `masked=True` become `UnitStateSpec`s.
- **`Service` (`state.py:806`).** Lowered to a `ServiceSpec` with `enabled=True` always (there is
  no way to generate a service and not enable it). `_compile.py:133-142` then prepends the wait
  units of `needs` to `after` and `requires` (`runtime-init.service`, or the per-step
  `runtime-init-<step>.service` units outside `nethermind-v1`); `.target` names and runtime-init
  itself are skipped. `emit_mkosi.py:1029-1039` writes the rendered text under
  `mkosi.extra/usr/lib/systemd/system/`.
- **Render order (`emit_mkosi.py:441`).** `[Unit]`: `Description` (defaults to the name),
  `After`, `Requires`, `Wants`, then `extra_unit["Unit"]` sorted. Blank line. `[Service]`: `Type`
  (always written, `simple` when unset), each `ExecStartPre`, `ExecStart` (`' '.join(argv)`),
  `User`, `Group`, `WorkingDirectory`, `EnvironmentFile`, `Environment` (sorted by key, quoted by
  `_systemd_env_assignment`), `Restart` + `RestartSec=5` (when not `no`), `Limit*` (sorted),
  `KillMode`, `TimeoutStopSec`, the 9-line `strict` block (`ProtectSystem=strict` ...
  `MemoryDenyWriteExecute=yes`), `extra_unit["Service"]` sorted. Blank line. `[Install]`:
  `WantedBy` (default `minimal.target`), extras sorted. One trailing newline.
- **Enablement (`emit_mkosi.py:1198`).** For each enabled `ServiceSpec`, in declaration order:
  `mkosi-chroot systemctl enable <unit>` in postinst, then one
  `mkdir -p "$BUILDROOT/etc/systemd/system/minimal.target.wants"` and an
  `ln -sf "/etc/systemd/system/<unit>" ...minimal.target.wants/` per unit (the image boots
  `minimal.target`, so `WantedBy=default.target` alone would not start anything).
  `enable_unit` (`models.py:511`) adds an enablement-only `ServiceSpec` (no command, no file) for
  a shipped or packaged unit. Disable and mask lines follow (`_unit_state_commands`, `:525`).
- **Runtime-init (`_modules/init.py`).** Under `nethermind-v1` one `runtime-init.service` runs
  `/usr/bin/runtime-init`; otherwise each step is `runtime-init-<step>.service` (oneshot,
  `After=`/`Requires=` the network and its direct step deps) and `runtime-init.service` requires
  them all. `_compile.py:143` enables runtime-init where it is generated.
- **Other drop-ins.** Secret delivery writes `<service>.d/tundravm-secrets.conf`
  (`EnvironmentFile=`); all drop-ins live under `/usr/lib/systemd/system/<unit>.d/`.
- **Lint today.** `service-user-missing`, `user-group-undefined`, `service-command-not-shipped`
  and `secret-in-env` (`check.py`) look at `ServiceSpec`s; only `secret-in-env` reads shipped unit
  text. `needs-undefined` and `needs-without-init` live in `resolve.py:455-492`. Raw unit text is
  otherwise never parsed, so a typo such as `Restart=onfailure` ships silently.
- **Import.** `importer.py:829` `_units` / `:1339` `_service_of` turn a shipped unit into
  `Service(...)` only when re-rendering reproduces the bytes exactly; anything else stays
  `Unit(name, text)`. `testing/_units.py:12` `parse_unit` is a second, lossy parser for
  `tundravm.testing`.

### 1.4 Defects found while taking the inventory

1. **`ExecStart` loses quoting.** `emit_mkosi.py:462` joins argv with spaces:
   `Service("a", ("/bin/sh", "-c", "echo hi"))` renders `ExecStart=/bin/sh -c echo hi`, and
   `"/usr/bin/app --name 'a b'"` renders `--name a b`. `%` specifiers are not escaped either
   (`--fmt %h` is expanded by systemd). Same for `ExecStartPre`. Fix in migration step 3.
2. **Unhardened by default.** `security="default"` and `"none"` render the same thing: nothing.
3. **Timer-activated services also start at boot.** `examples/03_fragments.py:117` pairs a
   `Service(type="oneshot")` with `healthcheck.timer`; the service is still enabled with
   `WantedBy=minimal.target`, so it also runs once at boot outside the timer.
4. **`requires` without ordering.** `Service(requires=("x",))` renders `Requires=x` without
   `After=x`, which starts both in parallel; `examples/06_attestation.py:43` has to spell both.
5. **State directories by shell.** `app_template.py:182` creates the state directory with
   `ExecStartPre=+/usr/bin/install -d ...`, the job `StateDirectory=` does.
6. **Boot-time enable hack.** `serial-console.service` runs `systemctl enable` at boot instead of
   enabling `serial-getty@ttyS0.service` at build time (kept for the golden tree; section 7, step 8).

## 2. API design

All new types are frozen, slotted dataclasses deriving `Declared` (so `.ref`, `.ref.edit()`,
`.ref.remove()` work), validate in `__post_init__` with `_fail`, take kv data only as `Mapping`
(frozen with `_freeze_map`), and take sequences frozen to tuples. They live in a new module
`src/tundravm/declarative/systemd.py`, re-exported from `tundravm.declarative`. Helpers that
compose several declarations are PascalCase `@component` classes in `declarative.utils`.

### 2.1 The five shapes

| Declaration | Renders | Enabled by default |
|---|---|---|
| `Service(name, exec_start, ...)` | `<name>.service`, long-running (`Type=exec`) | yes |
| `Service(name, exec_start, socket=Listen(...))` | `<name>.socket` + `<name>.service` (no `[Install]` on the service) | the socket |
| `Task(name, run, ...)` | `<name>.service`, `Type=oneshot`, `RemainAfterExit=yes` at boot | yes |
| `Task(name, run, schedule=Every("1min") \| Calendar("daily"))` | `<name>.service` (no `[Install]`) + `<name>.timer` | the timer |
| `Mount(where, what, type=..., options=...)` | `<escaped where>.mount` | yes |
| `Unit(name, enabled=..., masked=..., dropin=DropIn(...))` | a packaged unit's `<name>.d/20-tundravm.conf` | as declared |
| `Unit(name, text)` / `Unit.from_file(...)` | the text, verbatim (escape hatch, linted `unit-raw`) | as declared |

`Task` is distinct from `BootStep`: a `BootStep` is a script inside the runtime-init chain
(measured, ordered with keys, disks and secrets, runs as root without hardening); a `Task` is an
ordinary oneshot unit an application owns, hardened like a `Service`, that can wait for the chain
(`needs=[secrets.ready]`).

### 2.2 `Service`

```python
@dataclass(frozen=True, slots=True)
class Service(Declared):
    name: str                                    # "app" or "app.service"
    exec_start: str | tuple[str, ...]            # command line (shlex) or argv
    _: KW_ONLY
    description: str | None = None               # default: the name
    user: str | Dynamic | None = None            # None: a system user named after the service
    group: str | None = None                     # default: the user's primary group
    groups: tuple[str, ...] = ()                 # SupplementaryGroups= ("tdx" for /dev/tdx_guest)
    type: Literal["exec", "simple", "notify", "notify-reload", "forking"] | None = "exec"
    exec_start_pre: tuple[str, ...] = ()
    exec_start_post: tuple[str, ...] = ()
    exec_reload: str | None = None
    working_dir: str | None = None               # default: the state directory, if any
    env: Mapping[str, str] = FrozenMap()
    env_file: str | DefaultFile | None = None    # DEFAULT_FILE: "-/etc/default/<name>"
    credentials: Mapping[str, str | SecretFile] = FrozenMap()  # LoadCredential=
    state: str | None = None                     # StateDirectory=   (/var/lib/<state>)
    logs: str | None = None                      # LogsDirectory=    (/var/log/<logs>)
    runtime: str | None = None                   # RuntimeDirectory= (/run/<runtime>)
    cache: str | None = None                     # CacheDirectory=
    write_paths: tuple[str, ...] = ()            # ReadWritePaths= (e.g. "/persistent/app")
    restart: Literal["on-failure", "always", "on-abnormal", "no"] = "on-failure"
    restart_sec: str | None = "5s"              # None omits the line
    timeout_start: str | None = None             # TimeoutStartSec=, systemd's 90s when None
    timeout_stop: str | None = None
    kill_mode: Literal["control-group", "mixed"] | None = None
    limits: Mapping[str, str | int] = FrozenMap()  # {"NOFILE": 1048576}
    socket: Listen | None = None
    hardening: Hardening = Hardening.strict()
    after: tuple[UnitTarget, ...] = ()           # ordering only
    wants: tuple[UnitTarget, ...] = ()           # Wants= + After=
    requires: tuple[UnitTarget, ...] = ()        # Requires= + After=
    before: tuple[UnitTarget, ...] = ()
    part_of: tuple[UnitTarget, ...] = ()         # PartOf= (stop/restart with)
    conflicts: tuple[UnitTarget, ...] = ()
    conditions: tuple[Condition, ...] = ()       # Condition.path_exists("/dev/tdx_guest") ...
    needs: Sequence[Ready] = (RUNTIME_INIT,)
    enabled: bool = True
    wanted_by: str | None = None                 # None: the image's boot target
    extra: Mapping[str, Mapping[str, str | Sequence[str]]] = FrozenMap()
    allow_secret: bool = False
```

`UnitTarget = str | Ref` with `Ref.kind` in the unit kinds (`Unit`, `Service`, `Task`, `Mount`);
`tdxs.socket_ref`-style properties on composites hand out refs. Opinions baked into the fields:

- **Ordering follows dependency.** `requires=`/`wants=` render `After=` too (the bug in 1.4.4);
  `after=` alone is pure ordering. Duplicates across the three collapse in the rendered `After=`.
- **User.** `user=None` declares `User(<name>, system=True, home=None, shell=/usr/sbin/nologin)`
  next to the service unless the variant declares that user already (Debian's `adduser --system`
  convention; a stable uid matters for files on `/persistent`). `user=Dynamic()` renders
  `DynamicUser=yes` for stateless services. `user="root"` is allowed and linted
  (`service-runs-as-root`, info).
- **Directories, not mkdir.** `state="app"` renders `StateDirectory=app` (mode `0750`, owned by
  the service user, writable under `ProtectSystem=strict`) and makes it the default
  `WorkingDirectory=`. For state on an encrypted disk, `write_paths=(f"{disk.mount}/app",)` adds
  `ReadWritePaths=` plus `RequiresMountsFor=`, and the renderer emits the one
  `ExecStartPre=+/usr/bin/install -d -o U -g G -m 0750 PATH` the template writes by hand today.
- **Debian defaults file.** `env_file=DEFAULT_FILE` renders `EnvironmentFile=-/etc/default/<name>`,
  the Debian place for a daemon's options (`ConfigFile.env("/etc/default/app", ...)` writes it).
- **Secrets as credentials.** `credentials={"api-token": "/run/secrets/api-token"}` renders
  `LoadCredential=api-token:/run/secrets/api-token`; the program reads
  `$CREDENTIALS_DIRECTORY/api-token`. Under `ProtectSystem=strict` this is the preferred path over
  `EnvironmentFile=` (and `secret-in-env` already flags secrets in `Environment=`).
- **Commands are quoted.** `exec_start` as a string is shlex-split and re-rendered with systemd
  quoting (`"..."` around words with whitespace or quotes, `%` doubled to `%%`); an argv tuple is
  rendered the same way, so both spellings are lossless. A leading `+`, `-`, `!` or `@` prefix is
  kept as systemd's exec prefix and validated.
- **`security=` stays one release** as a deprecated alias (`"strict"` -> `Hardening.strict()`,
  `"default"`/`"none"` -> `Hardening.none()`), refused together with `hardening=`.

### 2.3 `Hardening`

A frozen value with one field per hardening knob and three named, reviewed presets. Presets are
class methods taking per-field overrides, so a relaxation is visible at the call site and in
`tundravm explain`:

```python
@dataclass(frozen=True, slots=True)
class Hardening:
    protect_system: Literal["strict", "full", "no"] = "strict"
    protect_home: bool = True
    private_tmp: bool = True
    private_devices: bool = True
    devices: tuple[str, ...] = ()          # DeviceAllow=<dev> rw; implies DevicePolicy=closed
    no_new_privileges: bool = True
    capabilities: tuple[str, ...] = ()     # CapabilityBoundingSet= and AmbientCapabilities=
    jit: bool = False                      # True drops MemoryDenyWriteExecute= (.NET, JVM, V8)
    network: Literal["inet", "unix", "none"] = "inet"   # RestrictAddressFamilies=
    syscalls: tuple[str, ...] = ("@system-service",)
    deny_syscalls: tuple[str, ...] = ("@privileged", "@resources")
    kernel: bool = True                    # ProtectKernelTunables/Modules/Logs=, ProtectClock=
    umask: str = "0077"

    @classmethod
    def strict(cls, **overrides) -> Hardening: ...    # the field defaults above
    @classmethod
    def relaxed(cls, **overrides) -> Hardening: ...   # ProtectSystem=full, NoNewPrivileges,
                                                      # PrivateTmp, ProtectKernel*; no syscall
                                                      # filter, no device or address limits
    @classmethod
    def none(cls) -> Hardening: ...                   # renders nothing; linted
```

`Hardening.strict()` renders, in this order: first today's nine `security="strict"` lines in
today's order (`ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
`NoNewPrivileges=yes`, `ProtectKernelModules=yes`, `ProtectKernelTunables=yes`,
`ProtectControlGroups=yes`, `RestrictSUIDSGID=yes`, `MemoryDenyWriteExecute=yes`), then
`PrivateDevices=yes` (or `DevicePolicy=closed` + `DeviceAllow=`), `ProtectKernelLogs=yes`,
`ProtectClock=yes`, `ProtectHostname=yes`, `RestrictRealtime=yes`, `RestrictNamespaces=yes`,
`LockPersonality=yes`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service`,
`SystemCallFilter=~@privileged @resources`, `CapabilityBoundingSet=`, `UMask=0077`. Keeping the
old nine first means the deprecated `security="strict"` maps to an internal `Hardening` value
with exactly those nine knobs and renders today's bytes.
Field opinions with real users in this repo:

- `devices=("/dev/tdx_guest",)` replaces `PrivateDevices=yes` (which hides every device) with
  `DevicePolicy=closed` + `DeviceAllow=/dev/tdx_guest rw`: what tdxs and raiko need.
- `jit=True` for Nethermind (.NET): `MemoryDenyWriteExecute=yes` kills the JIT.
- `capabilities=("CAP_NET_BIND_SERVICE",)` for a port below 1024, instead of running as root
  (the `app-privileged-port` check in `templates.py` becomes a hint pointing here).
- runtime-init step units and `Mount`s are root plumbing and keep `Hardening.none()` explicitly,
  documented in `_modules/init.py`, not by default.

The preset contents are a reviewed contract: a change to `Hardening.strict()` is a breaking
change, pinned by a byte test, called out in the changelog.

### 2.4 Socket activation: `Listen`

```python
Listen(path_or_port, *, mode=0o660, user="root", group=None, accept=False, kind="stream")
```

`Service("tdxs", ..., socket=Listen("/var/tdxs.sock", group="tdx"))` renders `tdxs.socket`
(`ListenStream=`, `SocketMode=`, `SocketUser=`, `SocketGroup=`, `Accept=no`,
`[Install] WantedBy=sockets.target`) and a `tdxs.service` with `Requires=`/`After=tdxs.socket`
and no `[Install]`: systemd starts it on the first connection. The socket is enabled, the service
is not. An integer is a TCP port (`ListenStream=8080`). `accept=True` makes the service a
template (`tdxs@.service`) and is validated against the name. `.socket_ref` on the service value
gives other units a ref to order after.

### 2.5 `Task` and schedules

```python
Task(name, run, *, schedule=None, user=None, timeout="5min", remain=True, persistent=True,
     hardening=Hardening.strict(), needs=(RUNTIME_INIT,), ...)  # plus Service's [Unit] fields
```

`run` is a command line or a sequence of command lines (one `ExecStart=` each, in order: what
`network-setup.service` does with four). Without `schedule` the task runs once per boot
(`Type=oneshot`, `RemainAfterExit=yes`, enabled, `TimeoutStartSec=5min` so a hung task cannot
hold boot forever, unlike systemd's infinite oneshot default). With
`schedule=Every("1min", first="1min")` it renders `OnBootSec=1min` + `OnUnitActiveSec=1min`;
`schedule=Calendar("daily")` renders `OnCalendar=daily` + `Persistent=yes`. A scheduled task's
service has no `[Install]` and is not enabled; the timer is (`WantedBy=timers.target`).
For a packaged unit that needs a timer, `Timer(unit=Unit.ref("x.service"), schedule=...)` is the
one lower-level type (Debian already ships timers for logrotate and apt, so it is rare).

### 2.6 `Mount`

`Mount("/var/lib/app/cache", "tmpfs", type="tmpfs", options=("size=256M", "mode=0750"))` renders
`var-lib-app-cache.mount` (the name is `systemd-escape --path` of `where`, computed, never typed).
`after`/`requires`/`needs` work as on `Service`; `enabled=True` means `WantedBy=local-fs.target`.
Encrypted and data disks stay `Disk`/`Partition`: `Mount` is for tmpfs, bind and network mounts.

### 2.7 Packaged units: `Unit(..., dropin=DropIn(...))`

Debian ships units; the Debian way to change one is a drop-in, never a copy of the unit file.
`Unit(name, content=None)` already controls packaged units; it gains one field:

```python
Unit("prometheus.service", enabled=True,
     dropin=DropIn(env_file=DEFAULT_FILE, limits={"NOFILE": 65536}, restart="always"))
```

`DropIn` takes the `[Unit]` relation fields, `conditions`, and the `[Service]` fields of
`Service` that make sense on someone else's unit (`exec_start`, which renders the `ExecStart=`
reset line first, `env`, `env_file`, `credentials`, `user`, `groups`, the directory fields,
`write_paths`, `restart`, `restart_sec`, timeouts, `limits`, `hardening`, `extra`). Every field
defaults to "unchanged"; `hardening=None` by default here, because hardening a unit we did not
write can break it, so it is opt-in (`dropin=DropIn(hardening=Hardening.relaxed())`). It renders
`/usr/lib/systemd/system/<unit>.d/20-tundravm.conf`, next to the existing
`10-tundravm-needs.conf` and `tundravm-secrets.conf`; `/etc/systemd/system` stays the
administrator's. `DropIn` on a `Unit` that ships text is refused (edit the text, or convert it).

Rejected: `Unit.ref("nginx.service").override(...)`. A `Ref` is an identity; `ref.edit()` and
`ref.remove()` already are the change mechanism (`Variant(changes=...)`), and a third verb on
refs would make two ways to change a packaged unit. A variant adds a drop-in with
`Unit.ref("nginx.service").edit(dropin=DropIn(...))`.

### 2.8 Relations, conditions, install

- `after`/`wants`/`requires`/`before`/`part_of`/`conflicts` take unit names or refs:
  `requires=[tdxs.socket_ref]`, `after=[Mount.ref("/var/lib/app/cache")]`,
  `wants=["network-online.target"]`. A ref resolves to its unit name at lowering
  (`unit_name`, `systemd-escape` for mounts); one that does not resolve in the variant is
  `unit-ref-undefined` (error). `needs=[x.ready]` keeps meaning "wait for runtime-init steps" and
  still renders the wait units first, as `_compile.py` does today.
- `conditions=(Condition.path_exists("/dev/tdx_guest"), Condition.kernel_cmdline("debug"))`:
  a closed set of constructors over `Condition*=`, with `Condition.raw(key, value)` as the
  linted escape.
- `enabled=True` writes `[Install] WantedBy=` the image's boot target (`minimal.target` today)
  and keeps the postinst `systemctl enable` + `minimal.target.wants` link; `wanted_by=` names
  another target. `enabled=False` generates the unit without `[Install]` (a unit only something
  else starts). Socket- and timer-activated services get `enabled=False` implicitly.

### 2.9 The escape hatch

`extra={"Service": {"OOMScoreAdjust": "-500"}, "Unit": {"StartLimitBurst": "3"}}` on every typed
declaration and on `DropIn`. Validation: the section must be `Unit`, `Install` or the type's own
section (`Service`, `Socket`, `Timer`, `Mount`), else `unit-section-invalid` (error); a key a
typed field owns is refused at construction with the field's name in the hint
(`extra-shadows-field`); a key missing from the vendored directive table for that section is
`unit-directive-unknown` (warning); a sequence value renders one line per item. Keys render
sorted after the section's typed directives (as `extra_unit` does today), so equal declarations
render equal bytes. Every use is reported as `unit-extra` (info) naming the keys, so the hatch
stays visible in review. Raw `Unit(name, text)` is the bigger hatch: kept, parsed for lint
(section 4), and reported as `unit-raw` (info) with the `tundravm units convert` command that
would replace it.

### 2.10 Variants and identity

`Service`, `Task`, `Mount` and `Unit` are keyed by unit name (`_key_part` already normalizes
`Unit`/`Service` through `unit_name`; `Task` joins it, `Mount` keys by escaped `where`). A
`Service` and a `Task` of the same name collide (`unit-duplicate`). Because fields are typed, a
variant changes one aspect instead of replacing a whole text:
`Service.ref("app").edit(hardening=Hardening.relaxed(), env={"LOG": "debug"})` in a dev variant,
which a text `Unit` cannot express today.

### 2.11 Before and after: three real units

**Raiko (surge prover service).** Before, `examples/fragments/raiko.py:43` plus `:90`:

```python
RAIKO_UNIT = """\
[Unit]
Description=Raiko
After=tdxs.service
Requires=tdxs.service

[Service]
User=raiko
Group=tdx
Restart=on-failure
ExecStart=/usr/bin/raiko

[Install]
WantedBy=default.target
"""
Unit("raiko.service", RAIKO_UNIT, enabled=True, needs=[RUNTIME_INIT])
```

After (current dialect; `needs=[RUNTIME_INIT]` and `user`'s `User` are defaults, the user is
already declared by the fragment):

```python
Service("raiko", "/usr/bin/raiko", user="raiko", group="tdx", requires=[Service.ref("tdxs")],
        hardening=Hardening.strict(devices=("/dev/tdx_guest",)))
```

(`Service.ref("tdxs")` names the unit `Tdxs` declares; the fragment already lists
`requires=("tdxs",)`, so the ref always resolves.)

renders `Description=raiko`, `After=runtime-init.service tdxs.service`,
`Requires=runtime-init.service tdxs.service`, `Type=exec`, `ExecStart=/usr/bin/raiko`,
`User=raiko`, `Group=tdx`, `Restart=on-failure`, `RestartSec=5s`, the strict block with
`DevicePolicy=closed`/`DeviceAllow=/dev/tdx_guest rw` in place of `PrivateDevices=yes`, and
`WantedBy=minimal.target`. The golden tree keeps the text (section 3); its typed twin, used only
by the semantic test, is
`Service("raiko", "/usr/bin/raiko", description="Raiko", user="raiko", group="tdx", type=None,
restart_sec=None, requires=["tdxs.service"], wanted_by="default.target",
hardening=Hardening.none())` (`type=None` and `restart_sec=None` omit the line, allowed so that
any existing unit has a twin).

**tdxs (socket + service).** Before, `_modules/tdxs.py:102-140` builds both files from line
lists, and `Tdxs.compose` (`declarative/utils.py:385-396`) ships them as two text `Unit`s with
`enabled=True`. After, inside `Tdxs.compose`:

```python
Service(
    "tdxs",
    ("/usr/bin/tdxs", "--config", spec.config_path, "--log-level", spec.log_level),
    user=spec.user, group=spec.group, type="notify", working_dir=f"/home/{spec.user}",
    socket=Listen(spec.socket_path, mode=0o660, user=spec.socket_user, group=spec.group),
    hardening=Hardening.strict(devices=("/dev/tdx_guest",)),
    needs=self.needs,
)
```

(`network="unix"` would be tighter, but a validator with `get_collateral` or
`check_revocations` fetches collateral over HTTPS, so `Tdxs` picks it only when neither is set.)

One declaration replaces two renderers and two `Unit`s; the service loses its `[Install]`
(socket activation starts it; today it is both socket-activated and started at boot) and gains
`After=tdxs.socket`, which the golden text lacks. `Tdxs.service_ref`/`.socket_ref` give Raiko
and `examples/06_attestation.py` something typed to require. Open question for the hardware
run: tdxs writes configfs-tsm reports under `/sys/kernel/config/tsm`, which
`ProtectKernelTunables=yes` makes read-only; the preset override is either
`kernel=False` or `write_paths=("/sys/kernel/config/tsm",)`, decided on a real TDX guest before
step 8 lands. Step 8 shipped `write_paths=("-/sys/kernel/config/tsm/report",)` (optional, so a
guest without configfs-tsm still starts) with `kernel=True`, from tundra-tools' source:
go-tdx-guest v0.3.1 uses configfs-tsm when `/sys/kernel/config/tsm/report` exists and falls back
to `/dev/tdx_guest` otherwise (no fallback when the configfs request fails).
go-configfs-tsm v0.2.2 `mkdir`s a fresh report entry per quote, and the kernel creates the
directory and the entry's attributes `root:root` (`inblob` 0200), so a root `ExecStartPre=+`
that re-owns a pre-created entry cannot help. The quoting issuers (`tdx`, `azure`, `gcp`)
therefore get `CAP_DAC_OVERRIDE` as their only capability (`AmbientCapabilities=` and the whole
`CapabilityBoundingSet=`), bounded by the sandbox: everything outside the optional report
directory stays read-only or hidden (DAC override does not lift a read-only mount; remounting
needs `CAP_SYS_ADMIN`), and `DevicePolicy=closed` admits only the guest device. It does widen
reads to every file the namespace shows. `/dev/tdx_guest` gets `root:tdx 0660` from
`/usr/lib/udev/rules.d/65-tundravm-tdx-guest.rules`, which the typed units ship; `Group=tdx`
already makes `tdx` the service's primary group. Validator-only and `simulator` services keep
`PrivateDevices=yes`, an empty bounding set and no write path. Offline
`systemd-analyze security` (systemd 255) moves from 1.3 to 1.6 (ambient capability 0.1,
`CAP_DAC_*` 0.2). Unverified on TDX hardware: a real quote request decides it.

**Health check (timer) and hello (boot oneshot).** Before, `examples/03_fragments.py:43,117-118`:

```python
TIMER = "[Timer]\nOnBootSec=1min\nOnUnitActiveSec=1min\n\n[Install]\nWantedBy=timers.target\n"
...
yield Service("healthcheck", probe, description="Probe the service", type="oneshot")
yield Unit("healthcheck.timer", TIMER, enabled=True)
```

After:

```python
yield Task("healthcheck", probe, description="Probe the service",
           schedule=Every("1min", first="1min"))
```

renders `healthcheck.service` (`Type=oneshot`, `ExecStart=/usr/bin/curl -fsS -o /dev/null URL`,
`User=healthcheck`, `TimeoutStartSec=5min`, strict hardening, no `[Install]`, not enabled: the
1.4.3 defect is gone) and `healthcheck.timer` (`OnBootSec=1min`, `OnUnitActiveSec=1min`,
`WantedBy=timers.target`, enabled). The `minimal` template's `HELLO_UNIT`
(`templates.py:64-85`) becomes `Task("hello", "/usr/bin/cat /etc/motd")`, and the Azure
provisioning unit becomes `Task("azure-complete-provisioning",
"/usr/bin/azure-complete-provisioning", user="root", wants=["network-online.target"],
hardening=Hardening.relaxed())` outside `nethermind-v1`.

## 3. Rendering and fidelity

### 3.1 One canonical renderer

Every generated unit and drop-in goes through one renderer, `src/tundravm/_units/render.py`,
over a small text model, `UnitText` (ordered sections of ordered `(key, value)` entries, plus a
trailing-newline flag). Typed declarations lower to it through a per-type field table; module
code that must keep exact historical bytes builds a `UnitText` explicitly (no string
concatenation either way).

- **Sections.** `[Unit]`, then the type's section (`[Service]`, `[Socket]`, `[Timer]`,
  `[Mount]`), then `[Install]` (the order of systemd.unit(5) and systemd.service(5)); empty
  sections are omitted; one blank line between sections; one trailing newline; no comments; no
  continuation lines.
- **Keys.** A fixed canonical order per section, chosen as a superset of today's
  `_systemd_unit_content` order, so a `Service` that uses only today's fields renders today's
  bytes (new keys slot in between, old keys never move relative to each other):
  - `[Unit]`: Description, Documentation, DefaultDependencies, After, Requires, Wants, Before,
    PartOf, BindsTo, Conflicts, RequiresMountsFor, Condition* (declaration order),
    StartLimit*, then `extra` sorted.
  - `[Service]`: Type, RemainAfterExit, ExecStartPre*, ExecStart*, ExecStartPost*, ExecReload,
    User, Group, DynamicUser, SupplementaryGroups, WorkingDirectory, EnvironmentFile,
    Environment* (sorted by name), LoadCredential* (sorted), StateDirectory, LogsDirectory,
    RuntimeDirectory, CacheDirectory, ReadWritePaths, Restart, RestartSec, Limit* (sorted),
    KillMode, TimeoutStartSec, TimeoutStopSec, the hardening block (2.3 order), `extra` sorted.
  - `[Socket]`: ListenStream/ListenDatagram, SocketMode, SocketUser, SocketGroup, Accept.
    `[Timer]`: OnBootSec, OnUnitActiveSec, OnCalendar, Persistent, RandomizedDelaySec, Unit.
    `[Mount]`: What, Where, Type, Options. `[Install]`: WantedBy, RequiredBy, Also.
- **Values.** Booleans render `yes`/`no`; relation lists join on one line; `ExecStart`,
  `Environment`, `LoadCredential`, `DeviceAllow` and `SystemCallFilter` repeat one line per item;
  commands are quoted per systemd.service(5) "Command lines" (1.4.1 fixed). Mapping fields are
  sorted, sequence fields keep author order with duplicates dropped.
- **`needs`.** For typed units the wait units are prepended to the typed `After`/`Requires`
  before rendering (what `_compile.py:133` does to `ServiceSpec` today), so there is no text
  surgery; `inject_after_init` remains only for raw `Unit` text.

### 3.2 The golden tree cannot come from a canonical order

The committed `nethermind-v1` units disagree with each other, so no renderer order, canonical or
dialect-specific, reproduces them all:

- `nethermind-surge.service`: `User, Group, Restart, LimitNOFILE, EnvironmentFile, ExecStart`;
  `tdxs.service`: `User, Group, WorkingDirectory, Type, ExecStart, Restart`. `Restart` comes
  before `ExecStart` in one and after it in the other.
- Continuations: unindented in `nethermind-surge.service`, four spaces in `tdxs.service`.
- `network-setup.service` and `minimal.target` have no trailing newline; `tdxs.socket` spells
  `Accept=false`; `tdxs.service` has `Requires=tdxs.socket` without `After=`; no surge service
  writes `Type=` or `RestartSec=`.

Options considered:

| Option | Golden bytes | API cost | Verdict |
|---|---|---|---|
| A. Golden units stay text (`Unit`, `Unit.from_file`, `File` for the skeleton); each gets a typed twin checked semantically by a test | exact | none | **recommended** |
| B. Renderer honors a declared order, continuation style and trailing newline (`layout=`) | exact | formatting fields on every declaration | rejected |
| C. `Unit.parse` captures a hidden layout that the renderer replays | exact | invisible state: equal declarations render different bytes, which breaks the recipe digest | rejected |
| D. A `nethermind-v1` key order in the renderer | impossible (above) | none | rejected |
| E. Typed declaration plus `pin=<file>`: bytes from the file, lowering fails when `parse(file)` is not semantically equal to the declaration | exact | one field, two sources of truth | possible follow-up |

**Recommendation: A.** The golden texts are a historical artifact whose bytes are the contract;
the typed API's job is to *express* them, not to *reproduce their formatting*. A costs no API,
keeps the renderer a single fixed table, and the twin tests prove coverage: for every unit in
`examples/surge-tdx-prover/mkosi/*/`, `tests/compiler/test_surge_unit_twins.py` asserts
`semantic(render(twin)) == semantic(golden)`, where `semantic` parses both with `Unit.parse`,
normalizes booleans (`false` = `no`), drops directives equal to systemd's defaults (`Type=simple`,
`Accept=no`), and compares sections as key -> ordered values. The trade-off is that the flagship
example keeps showing unit text; the typed examples (03-06) and the `init` templates carry the
idiom, and the raw-unit lint (section 4) gives surge the validation without converting it. E
stays available if surge later needs typed semantics (for example a hardening report in
`tundravm explain`) on golden units.

Module-owned golden texts (tdxs, the devtools serial console, Azure provisioning, the
`nethermind-v1` runtime-init unit, `minimal.target`) move into one module,
`src/tundravm/_units/v1.py`, built as explicit `UnitText` values and each pinned by a byte test.
They are selected the way `historical` already selects them (`state.py` `_azure`, `_compile.py`
`_per_step`) and, for composites that cannot see the dialect, by an explicit field following the
`DevTools(reproducible=False)` precedent: `Tdxs(legacy_units=True)` set by `NethermindBase`.
Outside `nethermind-v1` the same modules emit typed declarations.

## 4. Validation and lint

Two layers, as elsewhere in the SDK: construction refuses what can never work
(`ValidationError` with a hint), and `tundravm lint` reports what depends on the variant. The
lint layer covers *all* unit text in a variant, not only typed units: every raw `Unit` text,
every `File`/`FileTree` entry under `/usr/lib/systemd/system/` or `/etc/systemd/system/`
(`is_unit_path` already exists in `_secret_scan.py:95`), and every generated unit are parsed with
`Unit.parse` (section 5) and checked by the same rules, so surge and other text-based recipes get
the safety net before anyone converts anything.

The directive reference is a vendored table, `src/tundravm/_units/directives.py`: every
directive per section for Debian trixie's systemd 257 (generated once from systemd's
`load-fragment-gperf.gperf.in`, committed, with the generator's input version in the header),
plus hand-written value types (enum, boolean, time span, absolute path) for the ~40 directives the
typed API and the lints care about.

### 4.1 At construction

- Unit name vs type: `Service`/`Task` names are bare or end in `.service`; `Mount` names are
  computed; `Listen(accept=True)` needs a template service; raw `Unit` text needs a suffix
  (already enforced, `model.py:918`).
- Enum fields are `Literal`s checked with `_require_choice` (`type`, `restart`, `kill_mode`,
  `Listen.kind`, `Hardening.protect_system`, `Hardening.network`).
- `exec_start`, `exec_start_pre`/`post`, `run`: the first word after an optional systemd prefix
  (`+ - ! @ :`) must be an absolute path (Debian convention; systemd's own search path differs
  from `$PATH` and is a frequent surprise). Time fields must parse as systemd time spans.
- `extra`: section names and `extra-shadows-field` (2.9); values may not contain newlines.
- `user=Dynamic()` with `groups=` that name a group the image creates is allowed; with a `File`
  owned by that name it is refused at resolution (`dynamic-user-owns-file`).

### 4.2 Lint codes

| Code | Severity | Fires when | In the repo today |
|---|---|---|---|
| `unit-section-invalid` | error | a section is not `Unit`, `Install` or the suffix's own (`[Service]` in a `.socket`, `[Services]`) | none |
| `unit-directive-unknown` | warning | a key is not in the directive table for its section (`Restrat=`) | none |
| `unit-value-invalid` | error | an enum, boolean or time value is invalid (`Type=daemon`, `Restart=onfailure`, `Accept=maybe`) | none |
| `unit-exec-relative` | warning | an `Exec*=` command is not an absolute path | `network-setup.service` (`ip`, `chattr`) |
| `service-user-missing` | error | extended from `ServiceSpec` to `User=`, `Group=`, `SocketUser=`, `SocketGroup=` of any unit text and `DropIn.user` | none |
| `socket-service-unpaired` | error | `x.socket` without `x.service` (or `Service=`) shipped, generated or enabled; `Accept=yes` without `x@.service` | none |
| `socket-service-started` | info | a socket-activated service is also `WantedBy=` a boot target, so it starts eagerly | `tdxs.service` |
| `notify-through-shell` | warning | `Type=notify` and `ExecStart=` runs a shell or wrapper without `NotifyAccess=all`: the notification comes from a child systemd ignores | none |
| `unit-enable-noop` | warning | `enabled=True` on a unit without `[Install]`: `systemctl enable` does nothing | none |
| `timer-unit-missing` | error | `x.timer` without `x.service` or `Unit=` | none |
| `timer-service-enabled` | warning | a timer-activated service is also enabled at boot | `examples/03_fragments.py` (1.4.3) |
| `relation-conflict` | error | a unit both requires/wants and conflicts with another; orders both before and after it; or names itself | none |
| `requires-without-after` | info | raw text with `Requires=x` and no `After=x` (typed units cannot) | `tdxs.service` |
| `unit-ref-undefined` | error | a relation ref does not resolve in the variant | n/a |
| `unit-duplicate` | error | two declarations render the same unit name (generalizes `state.py`'s duplicate-service error) | none |
| `hardening-disabled` | warning | a typed `Service`/`Task` with `Hardening.none()` | n/a |
| `hardening-relaxed` | info | `Hardening.relaxed()` or an override that drops a strict knob, listing the knobs | n/a |
| `service-runs-as-root` | info | a typed unit with `user="root"` | n/a |
| `state-dir-mkdir` | info | `ExecStartPre=` creates a directory under `/var/lib`, `/var/log`, `/run` or `/var/cache`: use `state=`/`logs=`/`runtime=`/`cache=` | `app_template.py:182` |
| `unit-extra` | info | the `extra=` escape hatch is used (names the keys) | n/a |
| `unit-raw` | info | a raw `Unit` text that `tundravm units convert` turns into a typed declaration (exact or semantic) | every golden unit |
| `unit-as-file` | info | a unit shipped as a `File` instead of a `Unit` | `network-setup.service` |

The style codes (`unit-exec-relative`, `requires-without-after`, `socket-service-started`,
`unit-raw`, `unit-as-file`) are not reported for a variant whose dialect is `nethermind-v1`: that
tree is historical by definition, and its bytes are pinned. Correctness codes (`unit-value-invalid`,
`socket-service-unpaired`, `timer-unit-missing`, `service-user-missing`, ...) always fire. Each
code gets a row in the lint table of `docs/cli.md` and an `explain` entry like the existing ones.

## 5. Import: `Unit.parse` and `tundravm units convert`

Migration must be mechanical: nobody should translate unit text into fields by hand either.

- **`UnitText.parse(text)`** (internal, `_units/text.py`) is lossless: it keeps comments, blank
  lines, continuation lines and the trailing-newline flag, so `UnitText.parse(t).text == t` for
  every input. Lint and the exact check use it; `testing/_units.py` `parse_unit` becomes a view
  over it (one parser in the package instead of two).
- **`Unit.parse(text, name, *, mode="exact" | "semantic") -> Service | Task | Mount | Unit`**
  maps sections and keys back to fields through the *same* field table the renderer uses, so the
  two directions cannot drift. Shape detection: `.service` with `Type=oneshot` is a `Task`
  (`remain=` from `RemainAfterExit=`); `.mount` is a `Mount`; given several files,
  `x.socket` + `x.service` pair into `Service(socket=Listen(...))` and `x.timer` + oneshot
  `x.service` into `Task(schedule=...)`. Wait units that `needs=` would inject
  (`runtime-init.service` first in `After=` and `Requires=`) come back as `needs=` (what
  `importer.py:1401-1406` does today for `Service`).
- **Defaults are explicit in the output.** Because typed defaults are opinionated, a faithful
  conversion spells every opt-out the text implies: `hardening=Hardening.none()`,
  `restart="no"`, `type=None`, `restart_sec=None`, `enabled=False` when there is no `[Install]`.
  `--adopt-defaults` drops them and prints the semantic diff the defaults add (the hardening
  lines, `Restart=on-failure`, `Type=exec`, the implied `After=`) for review, which is the
  intended migration path for non-golden recipes.
- **Guarantees.** `exact`: returns a typed declaration only when `render(decl) == text` byte for
  byte, else `Unit(name, text)`; `semantic`: always returns a typed declaration (leftover keys in
  `extra`, reported) with `semantic(render(decl)) == semantic(text)` (the 3.2 comparison), and
  lists what it dropped (comments, continuation formatting). Property tests: every canonical
  rendering parses back to an equal declaration (`Unit.parse(render(d), mode="exact") == d`), and
  every unit text in the repository converts semantically.
- **CLI.** `tundravm units convert FILE... [--name NAME] [--exact | --semantic]
  [--adopt-defaults] [--diff] [--check] [--json]` prints the Python declaration(s), using the
  source printer `tundravm import` already has; `--diff` shows rendered vs original; `--check`
  exits non-zero unless the conversion is exact (for CI). `tundravm units render RECIPE
  [--variant V] UNIT` prints the unit a recipe generates, so users can see the text without
  compiling a tree. The `unit-raw` lint hint quotes the convert command for the unit at hand.
  Nothing rewrites recipe source in place.
- **`tundravm import`.** `_service_of` (`importer.py:1339`) is replaced by
  `Unit.parse(mode="exact")`, so an imported tree yields `Task`s, `Mount`s and socket pairs too,
  still byte-identical by construction. `--typed-units=semantic` opts into semantic conversion
  and reports every unit whose bytes would change.

## 6. Opinionated defaults beyond units

Same stance where recipes touch mkosi directly: a user should rarely need an mkosi section or key
name, or the `mkosi-chroot` prefix. What recipes still spell raw today:

| Where | Raw knob | Typed replacement or default |
|---|---|---|
| `examples/nethermind_base.py:123` | `Setting("Output", "Seed", (SEED,))` | `Mkosi(seed=SEED)`. The seed is a reproducibility detail of the tree, not image content; recipes that do not reproduce a historical tree keep the compiler's fixed `DEFAULT_SEED` and never see it |
| `examples/nethermind_base.py:124-125` | `Setting("Output", "OutputDirectory", ("build",))`, `Setting("Build", "PackageCacheDirectory", ("mkosi.cache",))` | build-host layout, owned by `tundravm bake`; `Mkosi(output_dir=, package_cache=)` only for trees that must spell them (the `nethermind-v1` dialect could set both itself) |
| `examples/nethermind_base.py:126` | `Setting("Build", "Environment", ("KERNEL_IMAGE", "KERNEL_VERSION"))` | likely redundant: `emit_mkosi.py:1391` already passes both through when a `Kernel` has a config file. Delete it if `test_surge_golden.py` still passes; otherwise make the auto-add produce the golden order |
| `declarative/lower.py:87-96` | `_SINGLE_SETTINGS`/`_BOOL_SETTINGS`: `Seed`, `OutputDirectory`, `CompressOutput`, `ManifestFormat`, `PackageCacheDirectory`, `WithNetwork`, `CleanPackageMetadata` reach typed config only through `Setting` | promote each to a typed field (`Mkosi(compress=, manifest=, build_network=)`, `Debian(clean_package_metadata=)`); `Setting` stays for the long tail and is linted `setting-raw` (info) naming the typed field when one exists |
| `declarative/lower.py:116` | `Setting("Content", "Bootable", ("no",))` | already expressible as `Debian(boot=None)`; `setting-raw` says so |
| `examples/surge-tdx-prover/image.py:102` | `Hook("eth-group", "postinst", "mkosi-chroot groupadd -r eth")` | `Group("eth")` (renders `groupadd --system`, same meaning; golden keeps the hook for its bytes) |
| `examples/surge-tdx-prover/image.py:107` | `Hook(..., "mkosi-chroot usermod -a -G tdx nethermind-surge")` | `User("nethermind-surge", groups=("eth", "tdx"))` (golden keeps the hook) |
| `examples/03_fragments.py:56` | `Hook("ca-certificates", "postinst", "mkosi-chroot update-ca-certificates --fresh")` | Debian's `ca-certificates` postinst already runs it; drop the hook, or a `Run(...)` (below) if `--fresh` matters |
| `declarative/utils.py:451` | `DevTools` enables its unit with a raw `Hook("... systemctl enable serial-console.service")` | `Unit("serial-getty@ttyS0.service", enabled=True)` in the current dialect (section 7, step 8) |

Proposals that follow from the table:

- **`Run(command, *, after=())`**: a build-time command inside the image. It renders
  `mkosi-chroot <command>` in postinst; users never learn mkosi phases or the chroot prefix.
  `Hook(name, phase, script)` remains the raw hatch, and `hook-chroot-covered` (info) recognizes
  `mkosi-chroot groupadd|useradd|usermod -a -G|systemctl enable|disable|mask` and names the
  declaration that already does it (`Group`, `User(groups=)`, `Unit(enabled=/masked=)`).
- **No boot-target knowledge.** The image boots `minimal.target`; `enabled=True` is all a unit
  needs, and `wanted_by=` is for the rare unit that belongs to another target. Users never write
  `minimal.target.wants` links (the compiler already does).
- **Debian layout is the default, not a choice.** Units go to `/usr/lib/systemd/system`, drop-ins
  next to them, daemon options to `/etc/default/<name>` (`env_file=DEFAULT_FILE`,
  `ConfigFile.env`), state to `/var/lib/<name>`, system users get `/usr/sbin/nologin`.
- **`Setting` and raw `Unit` text are documented together** in `docs/api.md` as "escape hatches",
  after the typed declarations, each with the lint code that reports it.

## 7. Migration plan

Each step lands on master on its own, keeps CI green, and says which digests may move. Three
guards run on every step: `tests/compiler/test_surge_golden.py` (the golden tree, byte for byte,
whole and per variant), `tests/compiler/test_lowering_parity.py` (tree and recipe digests of
every example, surge and every `init` template, from `fixtures/lowering_parity.json`), and
`tests/compiler/test_surge_contents.py` (the literal unit constants). A step that is meant to be
digest-neutral must pass them untouched; a step that moves digests re-records only the named
fixture entries, in the same commit, with a test that shows *which* unit files changed.

1. **Unit text model and directive table** (no output change). New `src/tundravm/_units/`
   (`text.py` lossless `UnitText`, `directives.py` vendored systemd 257 table);
   `testing/_units.py` delegates to it. Tests: `tests/unit/test_unit_text.py`, a corpus test that
   parses every unit text in the repository (golden files, module constants, example constants,
   every rendered `Service` of every example) and asserts `parse(t).text == t`.
2. **Lint over all unit text.** The section 4.2 rules that need no typed API (`unit-section-*`,
   `unit-directive-unknown`, `unit-value-invalid`, `unit-exec-relative`, extended
   `service-user-missing`, `socket-service-unpaired`, `socket-service-started`,
   `unit-enable-noop`, `timer-*`, `relation-conflict`, `requires-without-after`, `unit-raw`,
   `unit-as-file`, `state-dir-mkdir`) in `check.py`; rows in `docs/cli.md`; `explain` entries.
   Tests: `tests/lint/test_unit_lint.py`, one minimal recipe per code, plus the expected findings
   for each example (03 gets `timer-service-enabled` until step 9). Digest-neutral.
3. **One renderer, quoting fixed.** `_systemd_unit_content` moves from `compiler/emit_mkosi.py`
   to `_units/render.py` with the 3.1 table; `_modules/init.py`, `_modules/tdxs.py`,
   `_modules/devtools.py`, `platforms/azure.py`, `MINIMAL_TARGET_UNIT`, the needs drop-in in
   `declarative/state.py` and `secret_delivery.py`'s drop-ins build `UnitText` values; the
   historical ones move to `_units/v1.py`. Tests: per-unit byte tests
   (`tests/compiler/test_unit_render.py`) and the three guards untouched. The quoting fix only
   changes commands with quoted words or `%`; none exist in the parity corpus (checked in the
   step), so it stays digest-neutral there.
4. **IR room.** `ServiceSpec` (`models.py:171`) gains a `kind` and the new optional fields;
   `extras()` only emits fields that are set, so recipe digests hold; `_lowered.py`/`explain.py`
   serialize them; `_compile.py`'s `needs` patching handles every kind. Digest-neutral.
5. **Typed `Service` growth, today's defaults.** `declarative/systemd.py` (`Hardening`,
   `Dynamic`, `Condition`, `DEFAULT_FILE`, `DropIn`), the new `Service` fields with defaults that
   render today's bytes (`hardening` from `security`, `restart="no"`, `type=None`, no implied
   `After=`), `extra=`, refs in relations, `Unit(dropin=)`; lowering in `state.py`; exports in
   `declarative/__init__.py`; `docs/api.md`. Tests: model validation per field, render per field,
   guards untouched.
6. **New shapes.** `Task`, `Every`/`Calendar`, `Timer`, `Listen`, `Mount`: added to
   `DECLARATION_TYPES`, `Declaration`, `_KEY_FIELDS` and the importer's declaration `match`
   (`importer.py:1753`). Tests: byte tests per shape, pairing lints, guards untouched (nothing uses
   them yet).
7. **Flip the defaults (breaking, opinionated).** `Service`: `Hardening.strict()`, `type="exec"`,
   `restart="on-failure"`, `restart_sec="5s"`, dedicated system user when `user=None`,
   `requires`/`wants` imply `After=`; `security=` deprecated; `hardening-*` and
   `service-runs-as-root` lints. Files: `declarative/model.py`, `declarative/systemd.py`,
   `declarative/state.py`, `check.py`, `CHANGELOG.md` (Breaking), `docs/api.md`,
   `docs/tutorial.md`, `docs/concepts.md`. Re-records the parity entries that use `Service`:
   `03_fragments`, `04_storage_and_secrets`, `05_source_builds`, `06_attestation`,
   `init-service`, `init-cloud`, `init-prover`; `surge`, `nethermind_base`, `01_minimal`,
   `02_variants` and `init-minimal` must not move. `tests/compiler/test_service_defaults.py`
   snapshots each changed unit so the review sees the exact added lines. Gate: bake and boot the
   `init` templates on the Lima backend before landing.
8. **Modules.** `Tdxs` emits the typed socket pair outside `nethermind-v1` and keeps the text
   with `Tdxs(legacy_units=True)`, which `examples/nethermind_base.py` sets; `DevTools` enables
   `serial-getty@ttyS0.service` at build time, `reproducible=False` keeps the legacy unit and hook;
   Azure provisioning becomes a `Task` outside `historical`; current-dialect runtime-init step
   units render through the `Task` table with explicit `Hardening.none()`. Files:
   `_modules/tdxs.py`, `_modules/devtools.py`, `declarative/utils.py`, `platforms/azure.py`,
   `declarative/state.py`, `_modules/init.py`, `_units/v1.py`, `examples/nethermind_base.py`.
   Re-records `02_variants` (Azure output), `06_attestation` (`Tdxs`), `init-service`
   (`DevTools`), `init-cloud` (Azure, `DevTools`) and `init-prover` (all three); the golden tree
   and `nethermind_base` must not move. Gate: tdxs on a real TDX
   guest (the configfs-tsm question in 2.11).
9. **Examples, templates, docs.** `examples/03_fragments.py` (`Task(schedule=)`),
   `04_storage_and_secrets.py` (`write_paths=`, `credentials=`), `05_source_builds.py`,
   `06_attestation.py` (`requires=[Service.ref("tdxs")]`); `templates.py` (`HELLO_UNIT` ->
   `Task`, the service template) and `app_template.py` (`state=`/`write_paths=` replace
   `MAKE_STATE_DIR`); the surge fragments keep their text and gain
   `tests/compiler/test_surge_unit_twins.py` (3.2). Docs: `tutorial.md`, `api.md`,
   `module-authoring.md`, `README.md`. Re-records the touched example and `init-*` entries.
10. **Import and convert.** `Unit.parse` (typed, sharing the render table), `tundravm units
    convert` and `units render` in `cli.py`, `docs/cli.md`; `importer.py` `_service_of` replaced
    by `Unit.parse(mode="exact")`, `--typed-units=semantic`. Tests: corpus round trips (exact is
    typed only when bytes match; semantic is sections-equal), re-importing the surge tree yields
    the same tree digest, CLI tests.
11. **Beyond units** (section 6), independent after step 2: typed `Mkosi` fields, `Run`,
    `setting-raw` and `hook-chroot-covered`, the redundant `Setting` in `nethermind_base.py`.

### Risks

- **Hardening breaks programs.** `MemoryDenyWriteExecute` kills JITs, `PrivateDevices` hides
  `/dev/tdx_guest`, `ProtectSystem=strict` refuses writes outside declared directories. Mitigated
  by the boot gates in steps 7 and 8, `Hardening.relaxed()` as a one-word fix, lint hints that
  name the knob, and the preset being a pinned, reviewed contract.
- **Packaged units are invisible to lint.** `socket-service-unpaired` and `unit-ref-undefined`
  cannot know what a Debian package ships; they are errors only when the variant installs no
  package that could provide the unit, warnings otherwise.
- **Digest churn.** Steps 7 to 9 move parity digests by design; each re-record is limited to the
  named entries and paired with per-unit snapshots, and the golden tree never moves. Users with
  locked recipes see new tree digests after upgrading; the changelog says so.
- **Two parsers drifting.** Avoided by construction: one lossless text model, one field table
  for render and parse.
- **API growth.** The field list is the ceiling, not a start; anything else goes through `extra=`
  (linted) until real recipes show it is common.
