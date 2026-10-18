# API reference

The public API is the declarative one in `tundravm.declarative`. Most names are also exported from `tundravm`; the exceptions are listed under [Imports](#imports). Every record below is a frozen, slotted dataclass: lists passed for tuple fields are frozen into tuples, and `__post_init__` raises `ValidationError` for malformed values.

For the model behind these types see [concepts](concepts.md); for writing reusable fragments see [writing fragments](module-authoring.md).

## Imports

```python
from tundravm import Recipe, Fragment, Variant, Package, File, Service, compile, lint, lock, bake
from tundravm.declarative.utils import Composite, DevTools, Tdxs
from tundravm.declarative import diff, measure, deploy
```

`tundravm` exports the declarations and the `packages()` helper, the build toolchains `Go`, `Cargo` and `Dotnet`, [`Resources`](#resources), the recipe types, the lifecycle functions and result types, `release` and `Release` (from `tundravm.release`; the top-level `tundravm.release` is the function, so import the module as `import tundravm.release` or `from tundravm.release import STAGES`), `attest` and `Attestation` (from `tundravm.attestation`), `Policy`, the [errors](#errors), `load` and `load_recipe`. Only in `tundravm.declarative`: `diff`, `measure` and `deploy` (at the top level those names are the `tundravm.diff`, `tundravm.measure` and `tundravm.deploy` subpackages), `identity`, and the type aliases `Check`, `Pairs`, `Phase` and `Target`. The shipped fragments `Tdxs`, `DevTools`, `EfiStub`, `Backports`, `GoBinary`, `CargoBinary`, `SshAccess` (with `SSH_CONFIG` and `SSH_AUTHORIZED_KEYS_DIR`) and `TeeBase` (with its `TEE_*` defaults), their base class `Composite`, `ConfigFile`, `Export` and `HttpEndpoint` live in `tundravm.declarative.utils`; the `component` decorator is in `tundravm` and `tundravm.declarative`. `tundravm.__version__` is the package version that `tundravm --version` prints.

## Recipe, variants, fragments

### `Recipe`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required |
| `items` | `tuple[Declaration \| Fragment, ...]` | `()`: the recipe's own declarations and fragments, which every variant inheriting `"base"` starts from (provenance level `common`) |
| `variants` | `tuple[Variant, ...]` | `(Variant("default", outputs=("qemu",)),)` |
| `platform` | `Platform` | `Debian("trixie", boot=None)`: the distribution, its archive and what it declares in every variant (see below) |
| `mkosi` | `Mkosi` | `Mkosi()` |
| `policy` | `Policy \| None` | `None` (the default `Policy()`); see [policy](policy.md) |

At least one variant; names are unique. `recipe.variant(name)` returns one or raises `ValidationError`.

### `Platform` and `Debian`

`Recipe(platform=Debian("trixie", snapshot="20251113T083151Z"))` names what the image builds on. `Platform` is the base class; `Debian(release, *, ...)` is the distribution it ships. `platform.name` is `debian/<release>` (mkosi's `Distribution=`/`Release=`).

| Field | Type | Default |
|---|---|---|
| `release` | `str` | required: a release name such as `"trixie"`, not `debian/trixie` |
| `arch` | `"x86_64" \| "aarch64"` | `"x86_64"` |
| `snapshot` | `str \| None` | `None`; a snapshot ID such as `"20251113T083151Z"`, written as `Snapshot=` (with no `mirror`, mkosi reads `https://snapshot.debian.org`). A URL or a value with whitespace raises `ValidationError`: the root goes in `mirror` |
| `mirror` | `str \| None` | `None` (the distribution default); a mirror root written as `Mirror=`, which mkosi completes itself (`<mirror>/debian`, or `<mirror>/archive/debian/<snapshot>` with `snapshot`), so it is never a full archive URL |
| `tools_mirror` | `str \| None` | `None`; the tools tree's mirror root (`ToolsTreeMirror=`), completed the same way |
| `epoch` | `int \| None` | `0`: reproducible (`SOURCE_DATE_EPOCH=0`, stable seed, `IMAGE_VERSION` stripped); `None`: not reproducible; other values set `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` to that value |
| `items` | `tuple[Declaration \| Fragment, ...]` | `()`: declarations and fragments the platform adds to every variant (a kernel, `Backports()`, `Setting` escape hatches) |
| `boot` | `"systemd" \| "uki" \| None` | `"systemd"`: install `kernel_package` and `systemd`, `systemd-sysv`, `udev`, `kmod`, `systemd-boot-efi`; `"uki"`: `kmod`, `systemd`, `systemd-boot-efi` for a UKI whose `/init` the image ships; `None`: no packages, for a `Bootable=no` image |
| `kernel_package` | `str \| None` | `"linux-image-amd64"`; `None` when the image declares its own `Kernel(...)` |
| `clean_package_metadata` | `bool` | `True`: remove apt's and dpkg's package metadata from the finished image (`CleanPackageMetadata=`); `False` keeps `apt`/`dpkg` usable in it |
| `kernel_modules` | `tuple[str, ...] \| "all"` | `()`: each variant on `kernel_package` keeps the curated set of its outputs (`tundravm.kernel_modules`: the TDX guest set, plus Azure's or GCP's, plus the disk set under a `Disk`), written as `KernelModules=` and checked at finalize; names (as `modprobe` takes them, no globs or `.ko`) add to that set; `"all"` keeps every module. A built `Kernel` selects nothing; with `kernel_package=None` a non-empty tuple raises `ValidationError`. See [kernel modules](concepts.md#kernel-modules) |
| `efi_stub` | `str \| None` | `None`; a `systemd-boot-efi` version: the platform adds `EfiStub(snapshot=..., version=efi_stub)` from its own archive (`snapshot`, under `mirror` when both are set, or a `mirror` that is a snapshot archive URL), so the stub and the packages cannot disagree. No archive raises `ValidationError` |

`platform.packages` lists the package names it installs (`kernel_package` and the `boot` set). Every variant gets the platform's declarations, standalone ones (`inherits=None`) included, before `Recipe.items`; their provenance level is `platform` (`declared at FILE:LINE in the platform`, the line that constructs the platform), and a variant edits or removes them by ref like any other. A distribution-specific bundle is a subclass with other defaults, such as `NethermindPlatform` in `examples/nethermind_base.py`.

### `Variant`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required; `"base"` is reserved |
| `inherits` | `str \| None` | `"base"` (`Recipe.items`); `None` is standalone; else a variant name |
| `add` | `Fragment` | empty |
| `changes` | `tuple[Edit \| Remove, ...]` | `()`: edits and removals of inherited declarations, applied before `add` (see [refs and changes](#refs-and-changes)) |
| `outputs` | `tuple[Target, ...]` | `()`: inherited, `qemu` at the root; one disk image per target (`"qemu"`, `"azure"`, `"gcp"`), each listed once; a bare string is refused |
| `release` | `bool` | `True`: `release` ships it by default; `False` (a development variant, such as one adding `DevTools()`) is skipped by `release` unless named with `allow_ineligible=True`. Only `False` enters the recipe digest (the variant's `release` lockfile section) |

The default variant is the one named `default`, else the first that inherits `base` or `None`. It may inherit any variant; the variant it inherits lowers like any other, standalone when it lacks what the default adds. A variant that inherits `base` or the default variant, and which only adds declarations or edits `File`, `User`, `Group`, `Partition`, `Repository` or `Debloat`, lowers as an overlay of the default variant. Every other variant lowers standalone, among them a variant with its own settings or kernel and a variant inheriting `base` that leaves a cloud-targeted default's outputs (see [variants](concepts.md#variants-and-targets)).

### Refs and changes

Every declaration has an identity: its type plus its natural key (the `Identity` column below; `identity(item)` returns it). `Ref(kind, identity)` names one without holding its value:

- `value.ref` is a value's ref: `key.ref` is `Ref("Key", ("k",))`.
- `Type.ref(*key)` builds one from the key fields, bound as the constructor binds them: `Unit.ref("ssh")` (the unit name gets `.service` as identity does), `Package.ref("gcc", role="build")`, `File.ref("/etc/motd", stage="skeleton")`, `Debloat.ref()`. Any other argument raises `ValidationError`.
- `str(ref)` is the identity as diagnostics print it: `Unit(ssh.service)`.

`ref.edit(**fields)` returns an `Edit(target, fields)` and `ref.remove()` a `Remove(target)`, the two kinds of `Variant.changes`. An edit must set at least one field, each a field of the target's type and none of its identity fields (`ValidationError` at construction); the edited value is built with `dataclasses.replace`, so the declaration's own checks run on it (`edit-invalid`). A variant's changes apply in order to what it inherits, before its `add`: changing an identity it does not inherit is `edit-missing` / `remove-missing`, and editing one field twice, or removing an identity the variant also edits, raises `ValidationError` when the `Variant` is built.

```python
key = Key("data", output="/run/key")
disk = Disk("data", mount="/data", key=key)      # holds key.ref
recipe = Recipe(
    "r",
    items=(key, disk, Unit("ssh.service", enabled=True)),
    variants=(
        Variant("default"),
        Variant("small-key", inherits="default", changes=(key.ref.edit(size=32),)),
        Variant("no-ssh", inherits="default", changes=(Unit.ref("ssh.service").remove(),)),
    ),
)
```

`Resolved.bind(ref)` is the resolved variant's declaration for `ref` (`ValidationError` naming the ref and the variant when it declares none); lowering binds `Disk.key` this way, so `small-key`'s disk uses the 32-byte key.

### `Fragment`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required |
| `items` | `tuple[Declaration \| Fragment, ...]` | `()` |
| `requires` | `tuple[str, ...]` | `()`: fragment names that must be in the same variant |
| `checks` | `tuple[Check, ...]` | `()`: `Callable[[Resolved], tuple[Diagnostic, ...]]` |

### `Mkosi`

| Field | Type | Default |
|---|---|---|
| `layout` | `"directories" \| "native"` | `"directories"`; `"native"` writes one root `mkosi.conf` (the default variant) that mkosi applies to every profile, so it supports only variants that extend the default as overlays. Otherwise lowering raises `ValidationError` listing each offending variant with its reason (`'solo' (it is parentless)`, `'big' (it has its own Setting(Content, Locale))`) |
| `dialect` | `"current" \| "nethermind-v1"` | `"current"`, the mkosi 26 emission: source builds and built kernels are locked and [fetched on the host](#fetch), `Backports` and `Repository` sources are written under `mkosi.sandbox/` at compile time, and postoutput scripts name the UKI `${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}`. `"nethermind-v1"` reproduces the historical nethermind-tdx tree |
| `init_script` | `str \| None` | `None`; text written to `mkosi.skeleton/init` (mode 0755) |
| `version_script` | `bool` | `False`; emit `mkosi.version` |
| `cloud_postoutput` | `bool` | `True`; emit the Azure/GCP disk conversion postoutput scripts |
| `strip_os_release` | `bool \| None` | `None`: strip `IMAGE_VERSION` from `os-release` when the platform's `epoch` is set |
| `seed` | `str \| None` | `None`: the compiler's fixed partition UUID seed; a UUID (`Seed=`) only to reproduce a tree built with another one |
| `output_dir` | `str \| None` | `None`: mkosi's default output directory, which `bake` collects from; a directory relative to the tree (`OutputDirectory=`) |
| `package_cache` | `str \| None` | `None`: mkosi's default; a package cache directory relative to the tree (`PackageCacheDirectory=`) |
| `compress` | `"zstd" \| "xz" \| "gz" \| None` | `None`: the image file is not compressed (`CompressOutput=`) |
| `manifest` | `"json" \| "changelog"` | `"json"`: the package manifest written next to the image (`ManifestFormat=`) |
| `build_network` | `bool` | `True`: build scripts and source builds reach the network (`WithNetwork=`) |

The last six are build mechanics: each sets one mkosi key for every variant, and a variant's `Setting` of the same key overrides it there (lint `setting-raw` names the field when a `Setting` repeats it). `examples/nethermind_base.py` sets `Mkosi(dialect="nethermind-v1", seed=..., output_dir="build", package_cache="mkosi.cache")`, the historical tree's spelling.

`nethermind-v1` spells groups and users as postinst `groupadd`/`useradd` lines, omits the `# tundravm build:` declaration line and the `# unpinned:` marker of build hooks, clones sources inside the build sandbox, leaves kernel sources out of the lockfile and generates backports with a sync hook, matching the historical nethermind-tdx tree.

## Declarations

`Declaration` is the union of the types `Fragment.items` holds: `Package`, `File`, `Template`, `FileTree`, `Group`, `User`, `Unit`, `Service`, `Task`, `Mount`, `Hook`, `BootStep`, `Repository`, `Partition`, `Debloat`, `Setting`, `Kernel`, `Key`, `Disk`, `Secrets`, `RuntimeTools` and `Build`.

Every declaration, `Fragment` (components included), `Variant`, `Platform` and `Edit`/`Remove` has `declared_at: str`, the absolute `file:line` of the call that constructed it: the first frame outside tundravm, the standard library and installed packages (a module's or component's declarations point at the line that uses it; `""` when there is none). It is a slot, not a dataclass field: `==`, `hash`, `repr`, `dataclasses.fields`/`replace`, payloads, digests, lockfiles, evidence and `import` never see it, so moving a declaration to another line changes nothing but its location. Resolution keeps an edited declaration's line and records the edit's own in its `Origin.location`; diagnostics, `inspect --why` and `explore` show it relative to the working directory.

Mapping fields (`env`, `limits`, `variables`, `keep_paths_by_variant`, `Tdxs.expected_measurements`) take a `Mapping` and nothing else, and keep an immutable copy in the caller's order (equal to any mapping with the same items; payloads and digests sort it). Lists passed for tuple fields are frozen into tuples.

### Packages, files, accounts, units

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Package` | `name`, `role="runtime"` (`"runtime"` or `"build"`) | name, role |

`packages(*names, role="runtime")` returns a frozen `Fragment` of one `Package` per name, named after the call (`packages(curl, jq)`, `packages(gcc, role=build)`), so it goes in `Recipe.items`, a fragment's `items` or `Variant.add` without an invented fragment name.

| Type | Fields (defaults) | Identity |
|---|---|---|
| `File` | `path` (absolute), `content: str \| bytes \| Path`, `mode: int \| None = 0o644`, `stage="extra"` (`"skeleton"` or `"extra"`), `allow_secret=False`. Spell text `File.text(path, text, *, mode=0o644, stage, allow_secret)` and a host file `File.copy(path, source: Path, *, mode=None, stage, allow_secret)`: each returns the `File` the constructor builds (equal, same digests); the constructor still takes all three for `bytes` and generated values. `mode=None` is for a copied file only: `0755` when the source has an execute bit, else `0644` | stage, path |
| `Template` | `path` (absolute), `template: str \| Path`, `variables: Mapping[str, str \| int \| float] = {}`, `mode=0o644`, `stage="extra"`, `allow_secret=False` | stage, path |
| `FileTree` | `path` (absolute), `source: Path`, `exclude=()`, `mode=None` (keep each file's and empty directory's permission bits; a mode sets every file to it and empty directories to `0755`), `stage="extra"`, `symlinks="preserve"` (ship links as links; `"follow"` ships what they point to), `allow_secret=False` | stage, path |
| `Group` | `name`, `system=True`, `gid=None` | name |
| `User` | `name`, `system=True`, `home=None`, `shell="/usr/sbin/nologin"`, `uid=None`, `primary_group=None`, `groups=()` | name |
| `Unit` | `name`, `content: str \| Path \| None = None`, `enabled=None`, `masked=None`, `needs: Sequence[Ready] = ()`, `allow_secret=False`, `dropin: DropIn \| None = None`, `ports: tuple[Port \| int, ...] = ()` (what the unit's program binds beyond loopback: the variant's `Firewall` opens them, as for a `Service`). `Unit.from_file(name, source: Path, *, enabled=None, masked=None, needs=(), allow_secret=False)` ships the unit file *source* (UTF-8) | unit name |
| `Service` | `name`, `exec_start: str \| tuple[str, ...]`, then keyword-only fields (table below) | unit name |

`Service` fields, each with its default and the directive it writes (a field at its default writes nothing unless the table says otherwise). The defaults are secure and opinionated: a bare `Service("app", "/usr/bin/app")` renders `Type=exec`, `User=app` (a system user declared for you), `Restart=on-failure`, `RestartSec=5s` and the `Hardening.strict()` block; `type=None`, `restart="no"`, `restart_sec=None`, `user="root"` and `hardening=Hardening.none()` opt out (the last two are linted):

| Field | Default | Renders |
|---|---|---|
| `description` | `None` (the name) | `Description=` |
| `type` | `"exec"` | `Type=`: `"simple"`, `"exec"`, `"notify"`, `"oneshot"`, `"forking"`; `None` writes `Type=simple` |
| `remain_after_exit` | `False` | `RemainAfterExit=yes` (for a oneshot; `Task` is the usual spelling) |
| `ports` | `()` | nothing in the unit: the `Port`s (an `int` is TCP) the service binds itself, which a `Firewall` opens and lint `network-missing` counts; a `socket=Listen(port)` that is not loopback-only counts without it |
| `exec_start_pre`, `exec_start_post` | `()` | one `ExecStartPre=`/`ExecStartPost=` per command line |
| `exec_reload` | `None` | `ExecReload=` |
| `user` | `None` (a system user named after the service) | `User=`; `None` declares `User(<name>)` (system, `/usr/sbin/nologin`, no home) unless the variant declares it; `Dynamic()` renders `DynamicUser=yes`; `"root"` renders `User=root` and `Root()` renders no `User=` line (systemd runs it as root: root plumbing such as boot steps and platform glue); both are linted `service-runs-as-root` |
| `group`, `groups` | `None`, `()` | `Group=`, `SupplementaryGroups=` |
| `working_dir` | `None` (`/var/lib/<state>` when `state` is set) | `WorkingDirectory=` |
| `env_file` | `None` | `EnvironmentFile=`, one line per path (a path or a list; a `-` prefix makes it optional) |
| `env` | `{}` | one `Environment=` per key, sorted |
| `credentials` | `{}` | `LoadCredential=<name>:<path>` per name, sorted; a value is a path or a `SecretFile` (its `path`); the program reads `$CREDENTIALS_DIRECTORY/<name>` |
| `state`, `logs`, `runtime`, `cache` | `None` | `StateDirectory=` (`/var/lib/<state>`), `LogsDirectory=` (`/var/log/`), `RuntimeDirectory=` (`/run/`), `CacheDirectory=` (`/var/cache/`): systemd creates them owned by the service user, writable under any `ProtectSystem=` |
| `write_paths` | `()` | `ReadWritePaths=`; a path on a declared `Disk` or `Mount` mount point also renders `RequiresMountsFor=<path>` and `ExecStartPre=+/usr/bin/install -d -o <user> -g <group> -m 0750 <path>` (created on the mounted disk before start; a working directory on it becomes `-<path>`; refused with `user=Dynamic()`); a leading `-` marks an optional path (ignored when missing, never created) |
| `restart` | `"on-failure"` | `Restart=`: `"always"`, `"on-failure"`, `"on-abnormal"`, `"no"` |
| `restart_sec` | `"5s"` | `RestartSec=` when `restart` is set; `None` drops the line |
| `limits` | `{}` | `Limit<NAME>=` per resource (`{"NOFILE": 1048576}`) |
| `kill_mode` | `None` | `KillMode=` |
| `timeout_start`, `timeout_stop` | `None` | `TimeoutStartSec=`, `TimeoutStopSec=` (systemd time spans) |
| `hardening` | `None` (`Hardening.strict()`, unless `security` is set) | the `Hardening` profile's directives (below); `Hardening.none()` is linted `hardening-disabled`, a relaxed profile `hardening-relaxed` |
| `security` | `None` | deprecated, use `hardening`: `"strict"` writes the nine historical sandbox lines, `"default"`/`"none"` mean `Hardening.none()`; refused together with `hardening` |
| `after`, `before`, `part_of`, `conflicts` | `()` | `After=`, `Before=`, `PartOf=`, `Conflicts=`; each entry a unit name or a ref (`Service.ref("db")`, `Unit.ref("tdxs.socket")`) |
| `requires`, `wants`, `binds_to` | `()` | `Requires=`/`Wants=`/`BindsTo=` **and** `After=` the same units (ordering follows the dependency) |
| `needs` | `(RUNTIME_INIT,)` | `After=`/`Requires=` the runtime-init units it waits for, first |
| `enabled` | `True` | `[Install] WantedBy=` plus the build-time enable; `False` renders no `[Install]` |
| `wanted_by` | `None` (`minimal.target`) | `WantedBy=` |
| `conditions` | `()` | `Condition*=` lines in `[Unit]`, in declaration order, from `Condition` constructors: `Condition.virtualization("microsoft")` (`ConditionVirtualization=`; `microsoft` is Azure, `google` is GCP, also `kvm`, `vm`, ...), `Condition.path_exists(path)`, `Condition.kernel_command_line(word)`, `Condition.first_boot()`; each takes `negate=True` for `!`. systemd skips the unit, without failing, where one does not hold: cloud glue gated this way keeps one UKI bootable on every output |
| `extra` | `{}` | the escape hatch, `{"Service": {"OOMScoreAdjust": "-500"}}`: sections `Unit`, `Service`, `Install`; keys must be systemd 257 directives no field renders; a list renders one line per item; keys render sorted after the typed ones; linted `unit-extra` |
| `allow_secret` | `False` | nothing (silences `secret-in-env`) |
| `socket` | `None` | a `Listen(...)` renders and enables `<name>.socket`; the service is started by it (no `[Install]`, not enabled); see [Tasks, sockets and mounts](#tasks-sockets-and-mounts) |

Directives render in one canonical order per section (`docs/design/systemd-units.md` 3.1), so equal declarations render equal bytes. A ref in a relation that no declaration of the variant renders fails to lower (`unit-ref-undefined`); name a packaged unit by its unit name.

`Hardening` is the sandbox profile: `Hardening.strict(**overrides)` (the field defaults), `Hardening.relaxed(**overrides)` and `Hardening.none()`; `Hardening(...)` takes the fields directly. Overrides change one knob where the profile is chosen: `Hardening.strict(jit=True)`.

| Field | `strict()` | `relaxed()` | `none()` | Renders |
|---|---|---|---|---|
| `protect_system` | `"strict"` | `"full"` | `"no"` | `ProtectSystem=` |
| `protect_home` | `True` | `False` | `False` | `ProtectHome=yes` |
| `private_tmp` | `True` | `True` | `False` | `PrivateTmp=yes` |
| `no_new_privileges` | `True` | `True` | `False` | `NoNewPrivileges=yes`, `RestrictSUIDSGID=yes` |
| `kernel` | `True` | `True` | `False` | `ProtectKernelModules=`, `ProtectKernelTunables=`, `ProtectControlGroups=`, `ProtectKernelLogs=`, `ProtectClock=` |
| `jit` | `False` | `True` | `True` | `False` renders `MemoryDenyWriteExecute=yes` (kills .NET, JVM and V8 JITs) |
| `private_devices` | `True` | `False` | `False` | `PrivateDevices=yes` |
| `devices` | `()` | `()` | `()` | `DevicePolicy=closed` + `DeviceAllow=<dev> rw` each, in place of `PrivateDevices=` (`("/dev/tdx_guest",)`) |
| `sandbox` | `True` | `False` | `False` | `ProtectHostname=`, `RestrictRealtime=`, `RestrictNamespaces=`, `LockPersonality=`, `SystemCallArchitectures=native` |
| `network` | `"inet"` | `"any"` | `"any"` | `RestrictAddressFamilies=`: `AF_UNIX AF_INET AF_INET6`, `"unix"`, `"none"`; `"any"` writes nothing |
| `syscalls`, `deny_syscalls` | `("@system-service",)`, `("@privileged", "@resources")` | `()` | `()` | `SystemCallFilter=` and `SystemCallFilter=~...` |
| `capabilities` | `()` | `None` | `None` | `CapabilityBoundingSet=` (empty drops all); names also render `AmbientCapabilities=`; `None` writes nothing |
| `umask` | `"0077"` | `None` | `None` | `UMask=` |

`Unit(name, dropin=DropIn(...))` changes a packaged unit the Debian way: `DropIn(name="20-tundravm", **fields)` renders `/usr/lib/systemd/system/<unit>.d/<name>.conf` next to the `10-tundravm-needs.conf` that `needs` writes. Its fields are `Service`'s relation and `[Service]` fields (`exec_start`, which writes the `ExecStart=` reset line first, `env`, `env_file`, `credentials`, `user`, `group`, `groups`, the directory fields, `write_paths`, `restart`, `restart_sec`, the timeouts, `limits`, `hardening`, `extra`), each defaulting to "unchanged"; `hardening` is opt-in here because hardening a unit you did not write can break it. A drop-in's `hardening` is a patch over the unit's own sandbox: only the `Hardening` fields you pass render (`Hardening(jit=True)` writes `MemoryDenyWriteExecute=no`, `Hardening(devices=("/dev/tdx_guest",))` writes `PrivateDevices=no`, `DevicePolicy=closed`, `DeviceAllow=` and `DeviceAllow=/dev/tdx_guest rw`), while a preset (`Hardening.strict()`, `.relaxed()`, `.none()`) passes every field and replaces the sandbox outright (`Hardening.none()` turns every switch off and resets every list). Switches render `yes` or `no`; lists (`SystemCallFilter=`, `RestrictAddressFamilies=`, `SystemCallArchitectures=`, `DeviceAllow=`, `CapabilityBoundingSet=`, `AmbientCapabilities=`) start with their empty reset, `capabilities=None` renders `CapabilityBoundingSet=~` (every capability) and `umask=None` `UMask=0022`. `Hardening.patch_entries()` is that list and `Hardening.explicit` the fields it covers; a unit's `hardening` renders `entries()` as before, so `Hardening(jit=True) == Hardening.strict(jit=True)`. A drop-in on a unit that ships text is refused (edit the text), as is a `[Service]` field on a non-service unit.

A `Service` command is an argv: a string `exec_start` is split like a shell line (`shlex`), a tuple is taken as is, and each `exec_start_pre` line is split the same way. The unit renders every argument so systemd hands the program exactly that word: an argument with whitespace, a quote or a backslash (or an empty one) is double-quoted, `%` is doubled, and `$` is doubled except in a `${NAME}` reference or a whole-word `$NAME`, which systemd fills from `env=`/`env_file=`. `("/bin/sh", "-c", "echo 100% $HOME")` renders `ExecStart=/bin/sh -c "echo 100%% $$HOME"`.

`Template` renders `template` with `str.format_map(variables)` at lowering time; a placeholder without a value fails to lower. `Service` renders and enables a `.service` unit. `needs` lists the runtime-init steps it waits for (`After=`/`Requires=` on `runtime-init-<step>.service`; `runtime-init.service` for `RUNTIME_INIT` or under `nethermind-v1`). The default waits for every step when the variant has one, and `needs=()` waits for none. `allow_secret=True` exempts a declaration from the `secret-in-file` (`File`, `Template`, every file a `FileTree` imports) or `secret-in-env` (the unit a `Service` renders, a `Unit`'s shipped file) lint, for a value that only looks like a credential; it changes neither the recipe digest nor the tree. `Unit` with `content` ships that text verbatim and needs a type suffix (`app.service`). Without `content` it controls a packaged unit and must set `enabled` or `masked`. `needs=[...]` adds the same `After=`/`Requires=` lines to its shipped text, or, for a packaged unit, in the drop-in `/usr/lib/systemd/system/<unit>.d/10-tundravm-needs.conf`.

### Tasks, sockets and mounts

Three more shapes render units for you; each is a declaration with `.ref`, `.ref.edit()` and `.ref.remove()`, provenance, and a row in `inspect`, `inspect --why unit:<name>` and `explore`. Their units are enabled the way `Service` units are (postinst `systemctl enable` plus the `minimal.target.wants` link).

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Task` | `name`, `command: str \| tuple[str, ...]` (absolute program path), then keyword-only `schedule: Every \| Calendar \| None = None`, `description=None`, `user: str \| Dynamic \| Root \| None = None`, `group=None`, `state=None`, `timeout="5min"`, `after=()`, `requires=()`, `wants=()`, `needs=(RUNTIME_INIT,)`, `hardening=Hardening.strict()`, `wanted_by=None` | unit name |
| `Mount` | `where` (absolute, normalized, not `/`), `what`, then keyword-only `type=None`, `options=()`, `description=None`, `after=()`, `requires=()`, `needs=()`, `enabled=True`, `wanted_by=None` | where |

`Task("migrate", "/usr/bin/app migrate")` is a oneshot that runs once per boot: `Type=oneshot`, `RemainAfterExit=yes`, `TimeoutStartSec=5min` (so a hung task cannot hold boot; `timeout=None` drops it), the `Hardening.strict()` sandbox, `[Install] WantedBy=` the boot target, enabled. `user=None` runs it as a system user named after the task, declared for you unless the variant declares it (`useradd --system`, shell `/usr/sbin/nologin`); `user=Dynamic()` renders `DynamicUser=yes`; `user=Root()` renders no `User=` line. `state="app"` renders `StateDirectory=app` and makes `/var/lib/app` the working directory. `requires`/`wants` also order `After=` the same units. `task.ready` is a `needs=` handle meaning "this boot task has finished": `Service(..., needs=[migrate.ready])` (also on `Task`, `Unit` and `Mount`) renders `After=`/`Requires=migrate.service`; a `BootStep` cannot wait for one, and a scheduled task has no `.ready`.

With `schedule=` the service renders without `[Install]` and is not enabled; `<name>.timer` is, with `[Install] WantedBy=timers.target`. `Every(interval, *, first=None, jitter=None)` renders `OnBootSec=<first or interval>` and `OnUnitActiveSec=<interval>`; `Calendar(spec, *, persistent=True, jitter=None)` renders `OnCalendar=<spec>` (a shorthand such as `daily`, or `*-*-* 03:00:00`) and `Persistent=yes`, which catches up on a run missed while the machine was off (systemd only honors `Persistent=` for calendar timers, so `Every` has no such field); `jitter` renders `RandomizedDelaySec=`. Spans are checked against systemd's time-span syntax and calendar expressions get a basic syntax check, both at construction.

```python
Task("healthcheck", "/usr/bin/curl -fsS -o /dev/null http://127.0.0.1:9090/-/healthy",
     schedule=Every("1min", first="1min"))
```

renders `healthcheck.service` (`Type=oneshot`, `User=healthcheck`, `TimeoutStartSec=5min`, the strict sandbox, no `[Install]`) and `healthcheck.timer` (`OnBootSec=1min`, `OnUnitActiveSec=1min`, `WantedBy=timers.target`).

`Service(..., socket=Listen(address, *, kind="stream", accept=False, user=None, group=None, mode=None))` makes systemd listen and start the service on the first connection: it renders `<name>.socket` (`ListenStream=`/`ListenDatagram=`, `SocketMode=` (`0660` by default), `SocketUser=`, `SocketGroup=`, `Accept=`, `[Install] WantedBy=sockets.target`), enabled, and the service with `After=`/`Requires=<name>.socket`, no `[Install]`, not enabled. `address` is a port (`8080`), `"host:port"` or an absolute socket path; `user`, `group` and `mode` apply to a socket file only. `accept=True` starts one instance per connection, so the service must be a template: `Service("echo@", "/usr/bin/cat", socket=Listen(7777, accept=True))` renders `echo.socket` and `echo@.service`.

`Mount("/var/lib/app/cache", "tmpfs", type="tmpfs", options=("size=256M", "mode=0750"))` renders `var-lib-app-cache.mount` (`What=`, `Where=`, `Type=`, `Options=` joined with commas; the name is `systemd-escape --path` of `where`, so `/var/lib/my-app` is `var-lib-my\x2dapp.mount`), wanted by `local-fs.target`. A mount that `needs` a runtime-init step or a boot task (a bind mount from an encrypted disk) waits for it, is wanted by the boot target instead and gains the `nofail` option, so it is not ordered before `local-fs.target`. `enabled=False` renders no `[Install]`. Encrypted and data disks stay `Disk`.

`Task(..., conditions=(Condition.virtualization("google"),))` takes the same `conditions` as `Service`.

A `Task` and a `Service` cannot share a name. The generated units pass the unit lint's activation rules (`socket-service-unpaired`, `timer-unit-missing`, `timer-service-enabled`).

### Scripts and ordering

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Hook` | `name`, `phase: Phase`, `script`, `env: Mapping[str, str] = {}`, `cwd=None`, `after=()` | name |
| `BootStep` | `name`, `script`, `after: Sequence[Ready] = ()`, `before: Sequence[Ready] = ()`; `.ready` is its handle | name |

`Phase` is one of `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`. `Hook.after` names hooks of the same or an earlier phase. `BootStep`s run at boot in a deterministic topological order of their `after`/`before` edges. When several steps are ready, the built-ins go first, then the declared steps in declaration order. `after`/`before` take readiness handles (a `Ready`, a `BootStep`, or a step name). `Key.ready`, `Disk.ready` and `Secrets.ready` are the built-in steps `keys`, `disks` (after `keys`) and `secrets` (after `disks` when stored on one), which are reserved names. `RUNTIME_INIT` (only in `needs=`) stands for every step. `RuntimeDirectory(path, *, owner=None, group=None, mode=0o755, after=(), name=None)` and `RuntimeFile(path, text, *, owner=None, group=None, mode=0o644, after=(), name=None)` in `tundravm.declarative.utils` return `BootStep`s that create a directory or write a file at boot (default names `dir-<path>`/`file-<path>`).

`Run(command, *, stage="postinst", user=None, name=None, after=())` in `tundravm.declarative.utils` returns the `Hook` that runs *command* inside the image while it is built, so a recipe never writes the `mkosi-chroot` prefix. *command* is a command line (`"update-ca-certificates --fresh"`), a script of several lines, or a sequence of words (`["install", "-d", path]`, each quoted). A line whose words need no quoting runs as it is; any other runs through `bash -euo pipefail -c` inside the image, so a pipe, `&&` or redirection never runs on the build host. `stage` (a `RunStage`, one of `RUN_STAGES`) says when: `"postinst"` (default) after the packages, files and source builds are in the image, where configuration belongs; `"finalize"` last, after everything else, debloat included; `"prepare"` after the packages are installed, before source builds; `"build"` in the build environment, where only what lands in `$DESTDIR` reaches the image. `user` runs it as that account through `runuser` (the account must exist by then: a `User`'s exists from `postinst` on). The hook is named `name`, else `run-<program>`; `after` names hooks of the same stage. `Hook` stays the raw form for a script that mixes host and image steps; lint `hook-covered` names the declaration when a hook or boot step runs what one already does. `Pairs` (`tuple[tuple[str, str], ...]`) types result fields such as `Lock.sections`; declarations take mappings.

### System configuration

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Repository` | `name`, `url`, `suite`, `components=("main",)`, `keyring=None`, `priority=100`, `in_image=True`: the current dialect writes the source (and its keyring) under `mkosi.sandbox/`, where the build's apt reads it, and with `in_image=True` also into the image's `/etc/apt`; `False` keeps it out of the image | name |
| `Partition` | `name`, `size`, `mount`, `filesystem="ext4"` | name |
| `Firewall` | `allow: tuple[Port \| int, ...] = ()`, keyword-only `default="drop"` (or `"reject"`: TCP reset, ICMP unreachable), `ssh=False` (opens 22/tcp), `collect=True` (opens every port a service of the variant declares, and the `Secrets` SSH-key port). Resolution expands it into `/etc/nftables.conf`, `Package("nftables")` and `Unit("nftables.service", enabled=True)`; inbound it accepts established/related traffic, loopback, the RFC 4890 ICMPv6 types, DHCPv4/DHCPv6 replies and the opened ports (one rule each, commented with what listens), drops the rest and all forwarding, and filters no outbound traffic. `Port(number, protocol="tcp")` is one port (`"udp"` too). A variant installing `nftables` keeps the `nf_tables`, `nft_ct` and `nft_reject_inet` kernel modules. See [concepts](concepts.md#networking-and-the-firewall) | one per variant |
| `Debloat` | `enabled=True`, `remove=None` (compiler default list), `extra_remove=()`, `keep_paths=()`, `minimize_systemd=True`, `keep_units=None`, `keep_binaries=None`, `keep_units_extra=()` (kept on top of `keep_units` or its default), `keep_paths_by_variant: Mapping[str, Sequence[str]] = {}` (`{variant: paths}` kept only in that variant), `keep_modules: bool | None = None` (`True` keeps `/usr/lib/modules`; `False` removes it on purpose; `None` removes it with the default list, which lint `debloat-removes-modules` rejects under a modular distribution kernel; with no `Debloat` declared the default keeps it under that kernel) | one per variant |
| `Setting` | `section`, `key`, `values: tuple[str, ...]`; per variant | section, key |
| `Kernel` | `version`, `source: Git \| Http`, `config: Path \| None = None`, `cmdline=""`, `tdx=True`; keyword-only `base: DebianConfig \| Path \| None = None` (the config to compose from), `fragments: tuple[KernelConfig, ...] = ()`, `patches: tuple[KernelPatch, ...] = ()` (applied in order with `patch -p1`), `modules=False` (`False` requests `CONFIG_MODULES=n`; `True` builds and installs loadable modules). `config=` is a whole, final config used as is and excludes the others; fragments and patches need a `base` (see [kernels](concepts.md#built-kernels)) | one per variant |
| `KernelConfig` | `options: Mapping[str, str \| None]` (`CONFIG_*` to `y`/`m`/a value; `None` or `"n"` is off), keyword-only `name=""` (default `config-<sha12>` of the options), `reason=""` | many per variant, by `name`; merged in declaration order into the built kernel, a different value for one option is `kernel-option-conflict` |

No `Debloat` means the compiler default (debloat enabled). `Setting` is the mkosi escape hatch, and settings belong to the variant that declares them. Most keys a recipe needs have a typed spelling: `Output.Seed`, `Output.OutputDirectory`, `Output.CompressOutput`, `Output.ManifestFormat`, `Build.PackageCacheDirectory` and `Build.WithNetwork` are the [`Mkosi`](#mkosi) fields `seed`, `output_dir`, `compress`, `manifest`, `package_cache` and `build_network`, `Content.CleanPackageMetadata` is `Debian(clean_package_metadata=)`, and a `Setting` of one of them reports `setting-raw` (info); the setting still wins for its variant. `Build.Environment` (`NAME=value` or a passed-through `NAME`) and `Build.SandboxTrees` map onto compiler options too; a built `Kernel` with a `config` passes `KERNEL_IMAGE` and `KERNEL_VERSION` through without one. Any other key is written verbatim into that variant's `mkosi.conf`, under its section, one `Key=value` line per value. Keys the compiler writes itself (`Packages`, `BuildPackages`, `Mirror`, `Format`, `ImageId`, `KernelCommandLine`, `ExtraTrees`, the phase script keys such as `BuildScripts`, ...) raise `ValidationError` naming the declaration to use instead (`Package(name)`, `Debian(mirror=...)`, `Hook(name, phase, script)`, ...). `Setting("Build", "BuildSources", ("src[:dest]", ...))` mounts host directories into the variant's build.

`Kernel.source` is a `Git` branch, tag or full commit hash, with `subdir` and `submodules`, or an `Http` tarball, which needs `sha256=` and is checked against it before unpacking. In the current dialect a built kernel's source (a `Kernel` with `config`) is locked and fetched like a source build: its lockfile pin is named `kernel`, or `kernel-<variant>` for a variant whose kernel source differs from the default variant's, `lock --update kernel` re-resolves it, and the kernel build script copies the host checkout without its `.git` (so the kernel version string stays clean). Under `nethermind-v1` the lockfile pins no kernel and the build clones it. A variant whose settings or kernel differ from the default variant's lowers standalone with its own `mkosi.conf` lines, kernel build and config.

## Keys, disks and secrets

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Key` | `name`, `output=None`, `strategy="random"` (`"random"` or `"pipe"`), `persist_in_tpm=True`, `size=64`, `pipe=None` (required with `"pipe"`) | name |
| `Disk` | `name`, `mount`, `device=None` (the largest whole `/dev/sd*` disk, see [Concepts: Disk selection and formatting](concepts.md#disk-selection-and-formatting)), `key: Key \| Ref \| Path \| None = None` (a `Key` is stored as its ref), `mapper=None` (needs a key), `format="never"` (`"always"`, `"on_initialize"`, `"on_fail"`, `"never"`; one that can format needs a stable `device`, lint `disk-device-guessed`), `directories=("ssh", "data", "logs")` | name |
| `Secrets` | `name="secrets"`, `entries: tuple[Secret, ...] = ()`, `store: Disk \| Ref \| None = None` (a `Disk` is stored as its ref), `host="0.0.0.0"`, `port=8080`, `ssh_directory="/root/.ssh"`, `ssh_key_path="/etc/root_key"`, `delivery: ExternalDelivery \| None = None` (`None`: the built-in `tundra-tools` provider, which writes only the SSH key) | name |
| `ExternalDelivery` | `name` (what writes the targets at boot: a cloud secret store, a custom unit; not `"tundra-tools"`), `supports=("file", "env")` (the target kinds it writes: `"file"` for `SecretFile`, `"env"` for `SecretEnv`), `unit=None` (a unit the `delivery-<name>` step starts and waits for) or `ready_path=None` (a path that step polls for), not both. `step` is `delivery-<name>`, `ready` its handle for `needs=` | (inside `Secrets`) |
| `Secret` | `name`, `targets: tuple[SecretFile \| SecretEnv, ...]` (at least one), `required=True`, `schema: Schema \| None = None` | (inside `Secrets`) |
| `SecretFile` | `path`, `mode=0o400`, `owner=None` | |
| `SecretEnv` | `name`, `service=None` (`None`: global environment; `"app"` or `"app.service"`: delivered to `/run/secrets/app.env`, which a generated `app.service.d/tundravm-secrets.conf` drop-in makes the unit read with `EnvironmentFile=`, required when the secret is; the manifest's env target carries `service` and `env_file`) | |
| `Schema` | `kind="string"` (`"string"` or `"json"`), `min_length=None`, `max_length=None`, `pattern=None`, `enum=()` | |
| `RuntimeTools` | `source: Git`, `key_config="/etc/tdx/key-gen.yaml"`, `disk_config="/etc/tdx/disk-setup.yaml"`, `secret_config="/etc/tdx/secrets.yaml"`, `secret_manifest="/etc/tdx/secrets.json"` | one per variant |

`Disk.key` and `Secrets.store` take the declaration or its ref (`Key.ref("k")`), never its name, and hold the ref: resolution checks the variant declares that identity and lowering uses the variant's final value, so a variant that edits the key needs no new disk. Without `RuntimeTools` the tools build from `tundra-tools` `master`.

A variant may declare several `Secrets`. A single one uses the `RuntimeTools` `secret_config` and `secret_manifest` paths. With more than one, each writes `<stem>-<name>` paths instead (`/etc/tdx/secrets-api.yaml`, `/etc/tdx/secrets-api.json` for `Secrets("api")`) and gets its own `secret-delivery setup` runtime-init step; its `SecretEnv(service=)` files are `/run/secrets/<service>-<name>.env`, read through a `tundravm-<name>.conf` drop-in. The `secret-delivery` binary built from `tundra-tools` does not deliver the manifest's targets yet: it receives one SSH public key over HTTP and writes `authorized_keys` (see [Concepts: Secret delivery at boot](concepts.md#secret-delivery-at-boot) for the request and responses); the manifest, env files' paths and drop-ins are what the image declares. Two declarations whose paths overlap (a config path another `Secrets` or a `RuntimeTools` path writes, or a `SecretFile` path another `Secrets` delivers) raise `ValidationError` naming both.

`Secrets.delivery` says who writes the `SecretFile` and `SecretEnv` targets; `BUILTIN_SECRET_CAPABILITIES` in `tundravm._modules.secret_delivery` lists what the built-in `tundra-tools` provider writes (`ssh-key` only). A target its provider does not write is the `secret-target-unsupported` lint error. Without `unit=` or `ready_path=`, `ExternalDelivery` changes neither the tree nor the recipe digest: it records who delivers, and nothing waits for it (`external-delivery-unwired`). With one, it adds the runtime-init step `delivery-<name>` (`systemctl start UNIT`, or a poll until the path exists) that `needs=[delivery.ready]` waits for. The SSH key flow still runs through `tundra-tools`, and `secrets.ready` is that step.

## Sources and builds

| Type | Fields (defaults) |
|---|---|
| `Git` | `url`, `ref`, `subdir=None` (relative), `submodules=False` |
| `Http` | `url`, `sha256=None` (64 lowercase hex) |
| `DebianConfig` | `version` (the Debian `linux` package version, e.g. `"6.12.48-1"`; `6.12.48+deb13` is the ABI it installs), keyword-only `flavor="cloud-amd64"`, `arch="amd64"`, `snapshot=None` (default: the platform's), `sha256=None`: `Kernel(base=)` starts from `config.<arch>_none_<flavor>.xz` of `linux-config-<major.minor>` in that snapshot's pool, locked as `kernel-config` and fetched on the host |
| `KernelPatch` | `source: Path \| Http` (an `Http` patch is locked as `kernel-patch-<name>`), keyword-only `name=None` (default: the file name; a patch that does not apply fails the build naming it) |
| `Install` | `source` (relative to the build's source tree), `destination` (absolute), `mode=0o755`, `directory=False` (requires `mode=None`), `artifact=False` (`source` is the `recipe`'s output path, e.g. `build/<output>` for `Go`: the build installs it as the recipe's artifact, as the builds `Key`/`Disk`/`Secrets` generate do; a file with a mode) |
| `Build` | `name`, `source: Git \| Http`, `script=None`, `install: tuple[Install, ...]` (at least one), `packages=()`, `env: Mapping[str, str] = {}`, `cache_namespace=None` (names the build cache entry, default `name`; the key is fingerprinted: the current dialect caches under `<namespace>-<sha256(source pin, build spec, install spec, arch, toolchain)[:16]>`, so changing any of them rebuilds), `using: Go \| Cargo \| Dotnet \| None = None` (the toolchain; exactly one of `script` and `using`) |
| `Go` | keyword-only: `output`, `package="./..."`, `ldflags="-s -w -buildid="`, `tags=()`, `env: Mapping = {}`, `packages=("golang",)`, `output_dir="./build"`, `mkdir=True` |
| `Cargo` | keyword-only: `output`, `bin=None`, `package=None`, `features=()`, `profile="release"`, `env: Mapping = {}`, `packages=("cargo",)` |
| `Dotnet` | keyword-only: `project`, `output`, `configuration="Release"`, `runtime="linux-x64"`, `env: Mapping = {}`, `packages=("dotnet-sdk-8.0",)`, `restore_args=()`, `properties: Mapping = {}` |

`Go(reproducible=None, cgo=False)` and `Cargo(reproducible=None, auditable=None)` choose the reproducibility flags in the table and `cargo auditable` (which adds the `cargo-auditable` build package and embeds the crate list `tundravm sbom --binaries` reads). `None` means on, except under `Mkosi(dialect="nethermind-v1")`, whose historical hooks render the plain command; `True` or `False` wins in either dialect. `Cargo`'s `rustflags` are `CARGO_RUSTFLAGS` (`-C target-cpu=generic`, `-C link-arg=-Wl,--build-id=none`, `-C symbol-mangling-version=v0`) plus `--remap-path-prefix=<source tree>=/build`, passed with `--config` so they merge with the project's `.cargo/config.toml` `build.rustflags`; a `RUSTFLAGS` in `env` replaces those, so they are prepended to it. See [reproducibility](reproducibility.md#source-builds).

`Go`, `Cargo` and `Dotnet` copy `env` (and `Dotnet.properties`) into an immutable mapping, and their sequence fields into tuples, at construction: changing the dict you passed changes nothing, and the builder stays hashable.

A `Build` sets exactly one of `script` and `recipe`. `script` is a shell script run in the fetched source tree, with `env` exported and `packages` installed as build packages. `recipe` renders the toolchain's command and carries its own `packages` and `env`, so `Build(packages=, env=)` must stay empty. `install` paths are relative to the source tree, and each recipe leaves its output at a fixed path:

| Recipe | Command | Output to `Install` |
|---|---|---|
| `Go` | `SOURCE_DATE_EPOCH=... CGO_ENABLED=0 go build -trimpath -buildvcs=false -ldflags ... -o <output_dir>/<output> <package>` (`cgo=True`, or `CGO_ENABLED` in `env`, drops `CGO_ENABLED=0`) | `build/<output>` (`<output_dir>/<output>`) |
| `Cargo` | `cargo fetch && cargo auditable build --release --frozen --config 'build.rustflags=[...]'` (`--profile <profile>` otherwise), with `--features`, `--package`, `--bin`; one codegen unit, no incremental build, `SOURCE_DATE_EPOCH` | `target/<profile>/<bin or package or output>` |
| `Dotnet` | `dotnet restore`, then a deterministic self-contained `dotnet publish` into `publish/` | `publish/<output>` |

```python
from tundravm import Build, Cargo, Git, Go, Install

Build("app", Git("https://github.com/example/app.git", "v1.0.0"),
      using=Go(output="app", package="./cmd/app", env={"CGO_ENABLED": "0"}),
      install=(Install("build/app", "/usr/bin/app"),))
Build("prover", Git("https://github.com/example/prover.git", "v2.1.0"),
      using=Cargo(output="prover", bin="prover"),
      install=(Install("target/release/prover", "/usr/bin/prover"),))
```

`Build` identity is its name; install destinations of one build need distinct file names. Outside `nethermind-v1` a build caches under `<namespace>-<fingerprint16>`: the namespace is `cache_namespace` (default the build's name) and the fingerprint is the first 16 hex of a sha256 over the source pin (with git `subdir` and `submodules`), the build script or recipe, the install steps, the toolchain (recipe kind and packages) and the distribution it builds against: base, `arch`, mirror, snapshot, the variant's repositories and build packages, and its mkosi `Build` settings (environment, sandbox trees and files, verbatim `Build` keys such as `ToolsTree`; not `PackageCacheDirectory` or `WithNetwork`, which choose where packages come from, not which). Changing any of them rebuilds, so `cache_key` names a family of cache entries, not one. `nethermind-v1` keeps its historical keys: `<name>-<sha256(url)[:12]>-<pin12>`, or `cache_key` with `-<pin12>` appended.

## Resolution

```python
resolve(recipe: Recipe, *, variant: str) -> Resolved
resolve_all(recipe: Recipe) -> tuple[Resolved, ...]
identity(item: Declaration) -> tuple[str, ...]
```

`resolve` raises `ValidationError` on the first error-level diagnostic (the message names its code, its recipe line when it has one, and how many more there are). `Resolved` has `variant: str`, `outputs: tuple[Target, ...]` (own or inherited, never empty), `items: tuple[Declaration, ...]` and `fragments: tuple[str, ...]` (the names of the fragments included).

`Diagnostic(code, severity, message, subject="", variant="", location="", hint=None, context=())` is the one finding type: fragment checks return it, and `lint`, `lock_status` and `doctor` return it (`tundravm.Diagnostic`, also `tundravm.check.Diagnostic` and `tundravm.declarative.model.Diagnostic`; a frozen dataclass). `code` is the lint code; `severity` is `"error"`, `"warning"` or `"info"`; `subject` is what the finding is about (`""` for none); `variant` is the variant the finding is in, `""` for the recipe as a whole (the common fragment, the lockfile, the host); `location` is the recipe line the finding points at, `file:line` with the file relative to the working directory (absolute outside it), `""` when unknown: resolution findings take it from the declaration, fragment or variant change at fault, compiler rules and fragment checks that leave it empty get the declaration their `subject` names in that variant, and lock, lockfile and host findings have none (`lint --format github` annotates that line, text prints it as `at:`, markdown adds a `Location` column; `evidence` bundles record `""`); `hint: str | None` is the fix advice; `context` is sorted `(key, value)` string pairs (a dict is accepted). `to_dict()` gives `{"code", "severity", "message", "subject", "variant", "location", "hint", "context"}`, the shape `lint --format json` prints. A fragment check may leave `variant` empty; resolution fills it in.

### Recipe inputs

```python
from tundravm.declarative.inputs import recipe_inputs

recipe_inputs(recipe: Recipe) -> frozenset[Path]
```

`recipe_inputs` is every host path the recipe's declarations read, in every variant (what `Recipe.platform` contributes, its `items` included, then `Recipe.items`, each variant's `add` and the `Path` values its edits set, nested fragments included): `File` and `Unit` contents and `Template` sources given as a `Path`, `FileTree` sources (the directory; its members are inputs too), `Kernel.config` and the policy file a `Tdxs.from_policy` read (`Tdxs.policy_file`). Paths are absolute (a [`Resources`](#resources) path already is), relative ones taken from the working directory as lowering reads them, and kept when they do not exist. `Build` sources are remote (`Git`, `Http`) and `Disk.key` paths are guest paths, so neither is an input. `tundravm watch` polls these besides the recipe's modules, and every file a `Resources.text()` call read (`tundravm.declarative.resources.resource_reads()`).

### Resources

```python
from tundravm.declarative import Resources

assets = Resources.from_module(__name__)
assets.file("kernel/kernel.config") -> Path      # absolute; the file must exist
assets.directory("app") -> Path                  # absolute; a FileTree source
assets.text("motd.txt", encoding="utf-8") -> str
```

`Resources(module, root)` is the directory of the module named `module`: `from_module(__name__)` takes it from `importlib.resources` (a package's own directory, a module's containing one), or from `__file__` for a script run as `__main__`, so the same paths resolve from a checkout, from any working directory and from an installed (unzipped) package. The module must be imported, as it is while it runs, or importable. Paths are `/`-separated and stay inside `root`. A missing file, a path outside `root` or an unknown module raises `ValidationError` naming the module and the relative path.

### Explaining one object

```python
explain_why(recipe: Recipe, variant: str, subject: str) -> Why
```

`explain_why` answers `tundravm inspect --why`: `subject` is an absolute image path or `unit:NAME`, `package:NAME`, `hook:NAME`, `init:NAME`; an unknown one raises `ValidationError` naming close matches. `Why` has `variant`, `subject`, `declarations` (each with `declaration`, `type`, `key`, `present` and `origins`: `(action, variant, fragments)` steps, `action` one of `declared`, `added`, `edited`, `removed`, and `fields`, the fields an `edited` step set), `fragments` (as `Resolved.fragments`), `files` (compiled-tree paths relative to the variant directory), `generated` (lines the compiler added) and `to_dict()`. The steps come from `tundravm.declarative.resolve.provenance(recipe, *, variant)`.

## Lifecycle

```python
lint(recipe, *, variants=None, lock=None) -> tuple[Diagnostic, ...]
compile(recipe, *, variants=None, lock=None) -> Tree
diff(tree: Tree, against: Tree | Path) -> str
lock(recipe, *, previous=None, update=(), offline=False, resolver=None, variants=None) -> Lock
lock_status(recipe, locked: Lock, *, variants=None, resolver=None) -> tuple[Diagnostic, ...]
read_lock(path: Path) -> Lock
write_lock(locked: Lock, path: Path) -> None
bake(recipe, *, lock: Lock, backend: Backend, build: Path, variants=None, progress=None, fetch=True, offline=False, verify_reproducible=False) -> tuple[Artifact, ...]
read_artifacts(build: Path) -> tuple[Artifact, ...]
verify_artifact(artifact: Artifact) -> None
measure(artifact, *, scheme="qemu", allow_placeholder=False) -> Measurements
deploy(artifact, *, using: Qemu | Azure | Gcp, allow_simulated=False, adapter=None) -> Deployment
attest(issuer: str, policy, *, validator=None, nonce=None, allow_simulated=False, accept_partial=False, transport=None, validator_transport=None, clock=None) -> Attestation
read_attestation(source: Path | bytes | dict, *, policy=None) -> AttestationRecord
sbom(subject: Artifact | lowered, *, lock=None, manifest=None, allow_unbound=False) -> Sbom
evidence(recipe | lowered, *, build: Path, variants=None, lock=None, policy=None, recipe_path=None, runner=None, require=(), allow_unbound=False, attestations=(), max_age=86400, clock=None) -> Evidence
verify_evidence(path: Path, *, build=None, require=(), allowed_signers=None, signer=None, max_age=None, now=None) -> EvidenceVerification
changes(recipe | lowered, *, since: Path, lock=None, variants=None, build=None) -> ChangeReport
explore(recipe | lowered, *, variants=None, lock=None, build=None) -> Exploration
release(recipe, *, version: str, backend: Backend | BuildBackend, variants=None, sign=None, unsigned=False, require=None, since=None, releases=Path("releases"), build=Path("build"), lock=None, restart=False, change_spec=False, allow_ineligible=False, recipe_path=None, progress=None, attestations=None, max_age=None) -> Release
verify_release(path: Path, *, allowed_signers=None, signer=None, max_age=None, now=None) -> ReleaseVerification
witness(recipe, release: Path, *, backend: Backend | BuildBackend, build=None, builder=None, sign=None, output=None, progress=None) -> Witness
verify_witnesses(release_dir: Path, witnesses_dir: Path, *, min_witnesses=1, allowed_signers=None, allow_simulated=False) -> WitnessVerification
doctor(backend: Backend, *, runner=None) -> tuple[Diagnostic, ...]
load(path, *, attribute=None, extra_paths=()) -> Recipe  # load_recipe
import_tree(tree: Path, *, name=None, variants=None, dialect=None, output=None) -> Imported
lower(recipe, *, variants=None)  # internal: the lowered recipe for the recipe
```

```python
from tundravm.declarative.lifecycle import FetchedSource, fetch

fetch(recipe, *, lock: Lock | None, build: Path, variants=None, resolver=None, force=False) -> tuple[FetchedSource, ...]
```

`fetch` and `FetchedSource` are exported from `tundravm` and `tundravm.declarative` as well as `tundravm.declarative.lifecycle`.

`variants=None` means every declared variant; unknown names raise `ValidationError`.

- **`lint`** returns resolution and fragment-check diagnostics first, in resolution order. When none is an error it adds the compiler's rules on the lowered recipe, sorted by variant, severity and code, each with its rule's `hint` (the secrets guidance, for one). With `lock`, its source pins apply before the compiler's rules run (a pinned source never reports `source-unpinned`) and drift is added as `lock-changed`, `lock-added` and `lock-removed` diagnostics whose `subject` is the section.
- **`compile`** returns the tree in memory. With `lock`, its pins apply. Without one no lockfile is consulted, and what an unpinned source compiles to depends on the dialect. A source carries its own pin only when its `Git` ref is a full commit or its `Http` has a `sha256`; any other `Build` or built `Kernel` source is unpinned. In the current dialect, where the build sandbox never fetches, its build hook only fails, with `tundravm: source build NAME is not pinned: run tundravm lock, then tundravm fetch` (`kernel VERSION is not pinned` for a kernel), and `lint` warns `source-unpinned` about it. Under `nethermind-v1` the hook clones the ref inside the build sandbox, as the historical tree does. The supported sequence is the one in the example under [Result and input types](#result-and-input-types): `lock()`, `write_lock()`, then the same `Lock` to `compile()` and `bake()`, which fetches the pinned sources before it builds.
- **`diff`** is a unified diff from `against` (a `Tree` or a directory) to `tree`, empty when they match. Variant directories in `against` that `tree` does not hold are not compared.
- **`lock`** keeps every pin in `previous` whose source is unchanged and resolves the rest, plus the sources named in `update` (unknown names raise). A build whose source differs between variants is pinned once per variant under `<variant>/<name>`; `update` takes that key, or the build's name for every variant's pin. The default lookup runs `git ls-remote` for a git ref and hashes the download for an `Http` source without `sha256`; `resolver`, a function from source to pin, replaces it; `offline=True` fails for any source without a previous pin. Every source is tried before failing: one `LockfileError` lists all of them (`.failures`, name to `SourceError`), with reasons `ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>` or `timed out after 60s` (every network call times out after 60s, and git never prompts for credentials).
- **`lock_status`** is the drift between the recipe and `locked`, one diagnostic per section; empty when current. With `resolver` (as for `lock`) it also reports git refs that moved since the lock, as `sources.<name>` (`sources.<variant>.<name>` for a per-variant pin). A lockfile older than version 5 does not load: `read_lock` raises `LockfileError` (`E_LOCKFILE`) asking to lock again. When `variants` leaves out some declared variant, see [subsets](#locks-and-variant-subsets).
- <a id="fetch"></a>**`fetch`** checks the sources of `variants` out on this host, as the invoking user (git credentials and the SSH agent apply), into `build/.sources/<name>-<pin[:12]>-<id[:8]>/` (`id` hashes the url, subdirectory and submodules): every source build and, outside `nethermind-v1`, every built kernel's source (named `kernel`, or `kernel-<variant>` where a variant's kernel source differs) and every `EfiStub` package (an http source named `efi-stub`, or `efi-stub-<variant>` where a variant's package differs). A git source is checked out at its pinned commit (with submodules when the `Git` asks for them), an http source is downloaded and checked against its sha256. Pins come from `lock`; a source it does not pin (every source, with `lock=None`) is resolved first through `resolver` (default: the network, as `lock` does), which `Policy(mutable_ref_policy="error")` refuses. A complete checkout carries a marker naming its pin (and, for http, a sha256 manifest of its files) and is verified before it is kept, so a second fetch touches nothing: git `HEAD` must be the pin with an empty `git status` (untracked and ignored files included) and initialised submodules when asked for. A modified checkout raises `SourceError` (`E_SOURCE`, `source <name> checkout modified/incomplete: run tundravm fetch --force`); `force=True` checks every source out again and refills the dependency caches. There is one checkout per (source, pin). Each `Go`, `Cargo` and `Dotnet` build's dependencies are then prefetched with the host's `go`, `cargo` or `dotnet` (`go mod download`, `cargo fetch --locked`, `dotnet restore --packages`), run in a scratch copy of the checkout, into `build/.sources/deps/{go,cargo,nuget}`, with a marker `deps/<name>-<pin[:12]>-<id[:8]>.<cache>.json` that records the command and a content list (Go modules from `go.sum`, Cargo crates from `Cargo.lock` with size and sha256, NuGet packages from `project.assets.json`); a later fetch keeps the cache only while every listed entry is still there and unchanged, and prefetches it again otherwise; the current dialect's build hooks copy that cache and point the toolchain at it. A host without the toolchain skips the prefetch and a failing one only warns: the fetch succeeds and the build downloads the dependencies itself unless it bakes offline. Under `Policy(network_mode="offline")` a missing checkout raises `PolicyError` and nothing is prefetched. Returns one `FetchedSource` per source.
- **`bake`** writes `lock` to `build/tundravm.lock` when that file is absent or identical; a different lockfile already there is left alone and the bake reads a scratch copy of `lock`. It bakes frozen into the build directory `build` and writes `build/bake-result.json`, whose `declarative.lockfile` is the path of the lockfile it used (`null` for a scratch copy). It fails before building on lint errors (`LintError`) or drift (`LockfileError`); a `lock` of every variant covers `variants=` naming a [subset](#locks-and-variant-subsets). `progress` receives the CLI's progress lines. Outside `nethermind-v1`, a bake with a real backend first runs `fetch` at the pins it builds; the backend mounts `build/.sources` into the build (`BakeRequest.sources_dir`, at `$SRCDIR/tundravm-sources`, ephemerally) and each source hook (and kernel build script) copies its checkout, so the build sandbox never fetches a source. `fetch=False` builds from the checkouts already in `build/.sources` (copied to an air-gapped host, say) and raises `StateError` (`E_STATE`) before mkosi runs when one is missing. `offline=True` bakes as `Policy(network_mode="offline")` does: the fetch reuses complete checkouts and downloads nothing, every `Go`, `Cargo` and `Dotnet` build must find its prefetched dependencies, complete (else `StateError` before mkosi runs, naming the missing cache entry for an incomplete one), and mkosi runs with `--with-network=no`; a `nethermind-v1` recipe with source builds raises `PolicyError`, since its hooks clone in the sandbox. The in-process backend fetches nothing. `verify_reproducible=True` bakes the same variants a second time into `build/.reproduce`, against the same lockfile and the checkouts of `build/.sources` (hard-linked, nothing is fetched again), compiling and building everything again, and compares every artifact's sha256. The first build's `bake-result.json` is complete before the second build starts, and its `declarative.reproducibility` is rewritten atomically at each step as `{"state", "second_build", "differ", "error"}` with `state` `running`, then `matched`, `different`, `error` (the second build raised; the exception re-raises) or `interrupted` (`KeyboardInterrupt`, re-raised); the second build is removed when all match, and a mismatch keeps it and raises `ReproducibilityError` whose message holds a table per artifact (`variant`, `target`, both sha256 prefixes, `match`/`mismatch`), whose hint names `tundravm diff RECIPE --tree build/.reproduce/mkosi` and the usual causes (timestamps, build ids, packages without a snapshot), and whose context holds `second_build` and `differ` (`variant/target, ...`).
- **`read_artifacts`** reads the `bake-result.json` in the build directory `build`.
- **`verify_artifact`** hashes `artifact.path` and raises `ArtifactError` (`E_ARTIFACT_CHANGED`) when the file is unreadable, has no recorded sha256, or no longer matches the sha256 `bake-result.json` recorded. `measure` and `deploy` call it before anything else.
- **`measure`** derives expected measurements for `scheme` (`"qemu"`, `"gcp"`, `"azure"`, `"portable"`) by replaying the UKI's boot measurements in Python, offline ([policy](policy.md#schemes)); an artifact that is no UKI or disk raises `MeasurementError` unless `allow_placeholder`, which also emits a `PlaceholderMeasurementWarning`. Simulated artifacts are refused unless `allow_placeholder`.
- **`deploy`** deploys with the target's adapter. The `using` type must match `artifact.target`; simulated artifacts are refused unless `allow_simulated`. `adapter` replaces the default adapter (tests). On QEMU every port in `artifact.ports` that `Qemu.forward` does not already forward (by guest port) is forwarded from the same host port. The adapters in `tundravm.deploy` take `runner=` (an argv to `subprocess.CompletedProcess` function) that replaces every `qemu-system-x86_64`, `az` or `gcloud` call, so their command lines can be asserted without the tools.
- **`attest`** asks the `tdxs` issuer at `issuer` (`unix:PATH` or a path, or `tcp://HOST:PORT`; an `http(s)://` endpoint raises `ValidationError`, as tdxs serves no http) for a quote whose full 64-byte `report_data` must be `SHA-256(nonce)` plus 32 zero bytes (`nonce` hex or bytes; default 32 random bytes), reads MRTD and RTMR0..RTMR3 from it (DCAP quote v4 or v5; a `simulator` issuer's come from its `metadata` reply) and compares them with `policy`, a file `measure --export-policy` wrote or its dict. With `validator` (same endpoint forms) the attestation document and nonce go to a `tdxs` validator (`validate`), whose answer decides authenticity: the verdict is `trusted` only when it accepted the document, the nonce and every register in the policy match, and MRTD, RTMR0 (the platform: the scheme's values or the policy's `platform_approval`), RTMR1 and RTMR2 (the image) each matched an accepted value; `untrusted` when it rejected or something mismatches. Without `validator` the signature is `unchecked` and the verdict is `measurements-match` or `measurements-mismatch`, never `trusted`. Nothing disagreeing but one of those four unchecked (a `qemu`/`portable` policy without `platform_approval`) is `partial`, which passes only with `accept_partial=True`. An `azure` policy raises `AttestationError`: its image measurements are vTPM PCRs, and vTPM evidence is not supported yet. A `simulator` platform is at best `simulated`; `allow_simulated=True` lets that pass. `transport` and `validator_transport`, functions from the request envelope (`{"method", "data"}`) to the reply envelope, replace the network; `tundravm.attestation.connect(endpoint)` builds the default one. An unreachable issuer or validator, an issuer `error` reply, or a validator that cannot judge raises `AttestationError`; a reply that is not a TDX attestation or a placeholder policy `MeasurementError`; a bad nonce or policy `ValidationError`. A negative verdict is a value, not an error. `tundravm.attestation.parse_quote(raw) -> Quote` is the quote parser (`version`, `mrtd`, `rtmrs`, `report_data`, `mrseam`, `mrowner`, `xfam`).
- **`read_attestation`** reads a `tundravm.attestation/2` record (a path, its bytes or its dict; `attest --record` / `Attestation.save` write one) and re-derives it offline: the document's quote is parsed again (`parse_quote`; a simulator's registers come from the recorded `metadata` reply), the nonce binding is recomputed from the recorded nonce and user data, each register is compared with its expected value again, the signature status is read from the recorded validator reply and the verdict follows. With `policy` (a policy file, its bytes or its dict) whose sha256 is the one the record names, that policy's registers (with its `platform_approval` values), `artifact` and `scheme` replace the stored ones, so an expectation edited to match a bad quote is caught. A `tundravm.attestation/1` record raises `ValidationError` (its `trusted` did not require the platform approved): attest again. Every stored field that disagrees is listed in `inconsistencies` (`evidence` and `verify` report `attestation-inconsistent`). What this proves: the quote's measurements and that it binds the nonce. What it trusts: the quote's signature, certificate chain and collateral are only as trusted as the recorded validator reply, which the record's producer asserts (the record carries this as its `trust` note). A file that is not a record, or whose document is not an attestation document, raises `ValidationError`.
- **`sbom`** says what is in an image: an `Artifact`, or a lowered recipe with one variant (`lower(recipe, variants=["default"])`, before any bake). Its packages come from mkosi's JSON package manifest, `manifest` or by default `<variant>.manifest` beside the artifact (`<build_dir>/<variant>/output/<variant>.manifest` for a lowered recipe); when that file does not exist it adds a note and lists the packages the recipe declares (the lock's `dependencies` for an artifact), unversioned, and an unreadable manifest or a `manifest_version` other than 1 raises `ArtifactError`. Its sources are `lock`'s pins for the variant: each source build with its install destinations, the built kernel, the `efi-stub` package; for a lowered recipe they are its declared sources, unpinned (ref only) without `lock`. The metadata (base, arch, snapshot, mirror) comes from the lock's `distribution` section for an artifact, else the manifest's `config`. An artifact is hashed first (`verify_artifact`), and its document is bound to the bake: `lock` must hash to `artifact.lock_sha256` and the manifest to `artifact.manifest_sha256`, else `ArtifactError` (`E_ARTIFACT_CHANGED`); `allow_unbound=True` builds it anyway with `bound=False` and a note. The artifact and manifest paths in the document are relative to `artifact.build`, so moving the bake directory changes nothing. See [CLI: SBOM](cli.md#sbom) for the document shapes and package URLs.
- **`evidence`** reads the bake in `build` (the directory holding `bake-result.json`) for `variants` (default: every baked variant; one not baked raises `StateError`) and returns an `Evidence`; it writes nothing. `lock` (a `Lock` or a lockfile path) defaults to the lockfile the bake recorded, else `build/tundravm.lock`; `policy` is a `measure --export-policy` file used for every variant instead of `build/<variant>/policy.json`; `recipe_path` records the recipe file and its sha256; `runner` replaces the `mkosi --version` probe. `require` names checks from `EVIDENCE_REQUIREMENTS` (`"reproducible"`, `"measured"`, `"real"`, `"lock"`, `"installed"`, `"attested"`) whose absence makes the verdict `incomplete` instead of a note; an unknown name raises `ValidationError`. `attestations` are `tundravm.attestation/2` record files, each bundled as `variants/<variant>/attestation-<n>.json` of the variant holding the artifact its policy measured (`ValidationError` when no selected variant holds it) and re-derived against that variant's policy with `read_attestation`; `attested` holds when every artifact a valid policy measures has a record whose re-derived verdict is `trusted` (a `partial` best record is the `incomplete` reason `attestation-partial`), whose policy sha256 is the bundled policy's and whose `observed_at` is within `max_age` (seconds or a `timedelta`, default a day) of `attestation.evaluated_at`, either side: the wall clock (`clock`, a callable returning a `datetime`, for tests), recorded only when records are bundled; never `created`, which honours `SOURCE_DATE_EPOCH`. `attested` implies `measured`: every selected variant needs a verified policy (else `attestation-missing` for it) and every selected artifact must be measured, and each distinct measured subject needs a trusted record. A record that disagrees with its own document is the `fail` reason `attestation-inconsistent`. The lockfile read must hash to the `lock_sha256` the bake recorded and each SBOM must be bound (see `sbom`), else `ArtifactError`; `allow_unbound=True` records them with the `incomplete` reasons `lock-unbound` and `sbom-unbound`. A lowered recipe instead of a `Recipe` leaves out the provenance summary. See [CLI: Evidence](cli.md#evidence) for the index and the members.
- **`verify_evidence`** checks the evidence at `path` (a bundle, the directory `Evidence.write` wrote, or its `evidence.json`) on its own, with no recipe, checkout or bake directory, and returns an `EvidenceVerification`: every member against the sha256 the index lists, that the index carries `schema` `tundravm.evidence/4` and lists every core member (`bake-result.json`, `lint.json`, the lockfile it names, each SBOM, bundled manifest, policy, attestation record and `tree.json`), each bundled attestation record re-derived offline with `read_attestation` against its variant's policy member (`attested` judges freshness at the `evaluated_at` the evidence recorded, with the window it recorded; `max_age`, seconds or a `timedelta`, replaces the window and `now`, a `datetime`, the moment), each fact the verdict uses derived from its member (reproducibility, simulated, artifact sha256s, lint counts, the lockfile sha256, SBOM and policy facts, measurement coverage, `tree_matches`; a field the index states otherwise is `index-inconsistent`) and the stored verdict recomputed from those derived facts, the artifacts in `build` (a build or release directory) re-hashed against the index, `evidence.json.sig` against `allowed_signers` (an OpenSSH allowed-signers file; `signer` is the principal, else the file's principals for the signing key) and `require` as `evidence` takes it. A failed check is a value, not an error: a `path` that is not evidence or an unknown requirement raises `ValidationError`, and `allowed_signers` without `ssh-keygen` on `PATH` raises `StateError`. See [CLI: Verify](cli.md#verify) for the checks and reason codes.
- **`changes`** compares the evidence at `since` with the recipe and lockfile now and returns a `ChangeReport`: the recipe and per-variant tree digests, the lockfile section digests, the source pins, the declared packages (the evidence's `declared_packages` against the recipe's), the installed packages (the evidence's SBOMs from mkosi's inventory against the current bake's), the emitted files of each variant's tree (from the evidence's `tree.json`; a changed file under `mkosi.extra` or `mkosi.skeleton` carries the declarations `explain_why` finds for it, given a `Recipe`) and the measured registers. `lock` (a `Lock` or a path) defaults to the recipe's lockfile when it exists; `variants` to the evidence's variants the recipe still declares (an undeclared name raises `ValidationError`; a declared variant the evidence's lockfile holds but that was not baked is a note, not an added variant); `since` may also be a release directory (`release.json` beside `evidence.tar.gz`). `build` is a bake directory of the current recipe, used only when its recipe digest, tree and lockfile are the current ones: its bound SBOMs give the installed packages and its `<variant>/policy.json`, when it measured one of the variant's artifacts, the current registers. A comparison one side has no data for is `unknown` in `ChangeReport.comparisons`, never a list of removals. It reads no network and writes nothing. See [CLI: Changes](cli.md#changes).
- **`explore`** gathers what a reader needs to understand `variants` of a recipe without reading its Python and returns an `Exploration`: the recipe settings and digest, the lint findings (`lint`'s, with the lock's pins applied), the lock's state (`current`, `drifted` with its drift, or `missing`) with its section digests and pins, the bake in `build` when it holds a `bake-result.json` (artifacts, sha256, reproducibility, simulated), every declaration of every variant with its state there (`present`, `added`, `edited`, `removed`, `absent`), its resolution steps and the tree files it produced, each variant's compiled tree with each file's content (text up to `tundravm.explore.TEXT_LIMIT`, 64 KiB, then truncated) and the declarations behind it, and the hooks in stage order, the runtime-init steps in order and the source builds with their pins. `lock` (a `Lock` or a lockfile path) defaults to the recipe's `build/tundravm.lock` when it exists; a path that does not exist is a `missing` lock, not an error. A lowered recipe has no declarations: the matrix and the annotations are empty, with a note. A recipe that does not compile keeps the overview and variants, with a note. It reads no network and writes nothing. See [CLI: Explore](cli.md#explore).
- **`release`** cuts `releases/<recipe.name>-<version>/` from the locked recipe in the stages of `tundravm.release.STAGES`: `lock-check` (drift against `lock`, default the recipe's `build/tundravm.lock`, which must exist, raises `LockfileError`; the lockfile is never updated), `fetch`, `bake` with `verify_reproducible=True` into `build` with `backend` (a `Backend`, or a build backend instance), `measure` (a policy per variant; skipped for simulated artifacts), `sbom`, `evidence` (signed with the SSH private key `sign`), `verify` (of the bundle just written, with the key's public half), `changes` (against `since`: a release directory or an evidence bundle, default the newest finished release of the recipe in `releases`) and `notes` (`RELEASE.md`), then writes `release.json`, `release-manifest.json` (every file with its sha256 and size) and, with `sign`, `release-manifest.json.sig`. `variants` defaults to the variants with `release=True`; an ineligible one raises `ValidationError` unless `allow_ineligible=True`. `require` is as `evidence` takes it, and so are `attestations` (record paths) and `max_age`, which the `evidence` stage passes on; `progress` receives one line per stage (and a `skip variant` line per variant left out); `recipe_path` is recorded in the evidence. Until it passes, the release directory keeps `release.state.json` with the specification it started with (`variants`, `require`, the signing key, `since`, the backend's `fingerprint()`, the build directory, `allow_ineligible`, and once given the `attestations` with their sha256 and `max_age`): calling `release` again inherits each of them left at its default (`None`, `False`; an inherited attestation record that is gone or changed raises `StateError`), the first call that gives `attestations` or `max_age` adds them without `change_spec`, raises `StateError` for a different one unless `change_spec=True` (recorded in `Release.spec_history`; `unsigned=True` drops the key), keeps every stage whose inputs and files are unchanged and runs the first changed stage and everything after it; `restart=True` discards that state. A finished release raises `StateError`. A failing verdict is a value (`Release.verdict`), not an error. See [CLI: Release](cli.md#release).
- **`verify_release`** checks a release directory on its own and returns a `ReleaseVerification`: every file against `release-manifest.json` (`file-hash-mismatch`, `file-missing`, `file-unlisted`), the manifest signature against `allowed_signers` in the `tundravm-release` namespace (`manifest-signature-invalid`, `manifest-unsigned`), `release.json` against the manifest, the `signer` metadata against the key the manifest signature was made with, and finished (`release-inconsistent`), then the bundle with `verify_evidence(bundle, build=path, require=<the release's>, allowed_signers=..., signer=...)`. A `path` without `release.json` raises `ValidationError`. A build backend's `fingerprint()` (optional on third-party backends: `{"name": ...}` without one) is the JSON the bake stage's inputs record. See [CLI: Release](cli.md#release).
- **`witness`** rebuilds the release directory at `release` from `recipe`, the witness's own checkout, and returns a `Witness`: the recipe's digest must be the release's `recipe_digest` (else `ValidationError`), the lockfile is the one in the release's evidence bundle and must hash to its `lock_sha256` (else `LockfileError`), the released variants are baked with `backend` into `build` (default `build/witness-<release>`) and every artifact's sha256 is compared with the release's. It writes `witness-<builder>.json` to `output` (default `RELEASES/<release>.witnesses/`, never inside the release) and, with `sign` (an SSH private key), its `.sig` in the `tundravm-witness` namespace for principal `builder` (default `<user>@<host>`). A failed rebuild is the verdict `error`, not an exception; an unfinished release raises `StateError`. See [CLI: Witness](cli.md#witness).
- **`verify_witnesses`** applies the witness trust policy to the `*.json` files in `witnesses_dir` and returns a `WitnessVerification`: a witness counts when it names this release and the sha256 of its `release-manifest.json`, its `.sig` verifies against `allowed_signers` for its builder, it is not signed by the publisher, says `matched` for every released artifact and is not simulated unless `allow_simulated`; distinct principals (and keys) count once and the policy passes when at least `min_witnesses` count. A failed policy is a value; a directory without `release.json` raises `ValidationError`.
- **`doctor`** returns one `tool-missing` diagnostic per missing host tool of the backend (a warning when the tool is optional).
- **`import_tree`** reads the mkosi tree at `tree` (one variant directory, or a directory of them as `compile` writes them; `variants` picks some, the first becoming `Recipe.items`) and returns an `Imported`: the generated recipe module, ruff-formatted with `output`'s configuration when given (with a note when ruff is missing), and that module executed. `name` is the recipe name (default: the directory name), `dialect` the dialect the tree was written with (default: guessed). A path that is not a directory, or holds no `mkosi.conf`, raises `ValidationError`. What maps to which declaration is in [CLI: Import](cli.md#import); `tundravm import` is the command.
- **`load`** is `load_recipe`: it imports a recipe file and returns its `Recipe`. `attribute` names the module-level `Recipe` or a zero-argument factory returning one; without it (`attribute=None`, the default) the recipe is discovered as the [CLI does](cli.md#recipe-files). The file's directory and every `extra_paths` entry are importable while it runs.

### Result and input types

| Type | Fields |
|---|---|
| `Imported` | `recipe_source: str` (the module; it binds `recipe`), `recipe: Recipe` (that module executed), `notes: tuple[str, ...]` (what was skipped or not mapped, then the round-trip verdict), `coverage: Mapping[str, int]` (`declared`: first-class declarations; `verbatim`: `File`, `Unit` with content, `Hook` and `Setting`, which keep tree text as is; `lost`: the `lost declaration` notes); `grouped_notes()` maps each reason (`unmapped file`, `lost declaration`, `skipped`, `other`, in that order) to its notes, as `import` prints them; `to_json()` is what `import --format json` prints, besides its `"schema": "tundravm.import/1"` |
| `Tree` | `entries: tuple[Entry, ...]`, `digest: str`, `variants: tuple[str, ...]`, `layout` (`directories`, or `native` for `Mkosi(layout="native")`: the default variant at the root, each variant's additions in `mkosi.profiles/<name>/`; a variant's effective contents, which its tree digest, `tree.json` and `testing.Image` read, are the root plus its overlay); `write(path)` writes it, replacing its variant directories and dropping stale ones |
| `Entry` | `path`, `content: bytes \| None`, `mode: int`, `symlink: str \| None = None` (a directory has neither content nor symlink) |
| `Lock` | `recipe_digest`, `sections: Pairs` (section to digest, see [below](#locks-and-variant-subsets)), `pins: tuple[Pin, ...]`, `compiler_version` (the tundravm version that wrote it, from its `compiler` section); `text()` is the serialized lockfile |
| `Pin` | `identity` (build name, `kernel`/`kernel-<variant>` for a built kernel, `efi-stub`/`efi-stub-<variant>` for an `EfiStub` package, or URL for anonymous fetches), `source: Git \| Http` (the resolved commit or hash), `digest` |
| `FetchedSource` | `name` (a build's lock key: its name or `<variant>/<name>`, `kernel`/`kernel-<variant>`, or `efi-stub`/`efi-stub-<variant>`), `kind` (`"git"`, `"http"`), `url`, `pin` (commit or sha256), `path` (`<build>/.sources/<name>-<pin[:12]>-<id[:8]>`), `cached=False` (an earlier fetch had completed it), `ref=None` (the git ref the pin came from), `deps: str | None = None` (a Go, Cargo or .NET build's dependency prefetch: `go: prefetched`, `go: kept` (`cargo:`, `nuget:`), `go: prefetched (was incomplete: <problem>)` after repairing an incomplete cache, `go: incomplete (<problem>)` when one cannot be prefetched again (no host toolchain, or offline), `skipped (no host go)`, `skipped (offline)` or `failed (<reason>)`; `None` for other sources); `locked()` is the lockfile entry it matches |
| `Backend` | `kind` (`"lima"`, `"nix"`, `"local"`, `"inprocess"`), `cpus=2`, `memory="4GiB"`, `disk="40GiB"` (the last three for Lima) |
| `Artifact` | `path`, `variant`, `target`, `sha256`, `recipe_digest`, `lock_digest`, `tree_digest`, `simulated=False`, `ports=()` (the guest ports the variant's `Secrets` deliveries listen on, as `bake-result.json` records them in the variant's `declarative.ports`), `lock_sha256=None` (the sha256 of the lockfile the bake read), `manifest_sha256=None` (of mkosi's manifest beside the artifact at bake time), `build=None` (the build directory `read_artifacts` read it from) |
| `Measurements` | `scheme` (`"qemu"`, `"gcp"`, `"azure"`, `"portable"`), `values: Pairs` (the registers with one accepted value), `tool` (`"tundravm-measure <version>"` or `"placeholder"`), `artifact_digest`, `artifact_path: str = ""` (the measured file; `measure` sets it), `boot_payload_path`, `boot_payload_digest`, `registers` (each register's accepted values: a hex string, or a mapping with `value` and `firmware`/`label`), `unverifiable: Pairs` (register, reason), `extra` (the scheme's other policy fields: `firmware_table`, `sources`, `vtpm_pcrs`); `to_dict()` is `{"artifact", "artifact_digest", "boot_payload", "registers", "scheme", "tool", "unverifiable", "values"}`; `to_policy(*, allow_placeholder=False, platform_approval=None)` (`platform_approval`: `MRTD`/`RTMR0` to operator-approved values, kept as the policy's `platform_approval`) is exactly the dict `measure --export-policy` writes and `Tdxs.from_policy` and `attest` read (`tundravm.policy/2`, [policy](policy.md#measurement-policies); a `note` for placeholders, which it refuses unless `allow_placeholder`); `verify(expected: Mapping[str, str]) -> tuple[str, ...]` is the sorted registers whose value differs from `expected` (a register only one side has counts), empty when all match |
| `Qemu` | `memory="2G"`, `cpus=2`, `ssh_port=2222`, `tdx=False` (TDVF through `-bios`, `tdx-guest` object, split irqchip), `daemonize=True` (`False`: QEMU runs in the foreground with its console on the terminal), `forward: tuple[tuple[int, int], ...] = ()` (`(host, guest)` TCP ports forwarded besides `ssh_port`), `firmware: str \| None = None` (a TDVF or OVMF image to boot instead of the first one installed), `data_disk_size="16G"` (each data disk created for the image's `/dev/disk/by-id/virtio-SERIAL` disks). `ssh_port` is forwarded only to an image that declares `SshAccess` (the bake records its port) |
| `Azure` | `storage_account`, `resource_group="tdx-vms"`, `location="eastus"`, `vm_size="Standard_DC2es_v5"` (a TDX confidential size: DCesv5/DCedsv5 or ECesv5/ECedsv5), `gallery="tdx_images"` (the Compute Gallery the image version goes to), `secure_boot=False` (on requires `signed`: Azure's Secure Boot firmware boots only signed UKIs, and tundravm does not sign), `signed=False` (the UKI was signed outside tundravm with keys Azure trusts) |
| `Gcp` | `project`, `bucket`, `zone="us-central1-a"`, `machine_type="c3-standard-4"` (TDX runs on the C3 series) |
| `Deployment` | `id`, `target`, `endpoint: str \| None`, `metadata: Pairs` |
| `Attestation` | `issuer`, `validator` (`None` without one), `platform` (`tdx`, `azure-tdx`, `gcp-tdx`, `simulator`), `source` (`"quote"`, or `"metadata"` for the simulator), `quote_version: int \| None`, `nonce`, `report_data` and `expected_report_data` (hex, 64 bytes), `nonce_matches: bool` (the whole `report_data` equals the expected one), `signature_status` (`"valid"`/`"invalid"`/`"unchecked"`), `validator_error` (the rejection, or `None`), `registers: tuple[RegisterCheck, ...]` (`name`, `verdict` `"match"`/`"mismatch"`/`"unchecked"`, `actual`, `expected`; MRTD and RTMR0..RTMR3), `policy` (its path, `None` for a dict); `verdict` (`"trusted"`, `"partial"`, `"untrusted"`, `"measurements-match"`, `"measurements-mismatch"`, `"simulated"`; `trusted` and `measurements-match` need MRTD, RTMR0, RTMR1 and RTMR2 each matched, else nothing disagreeing is `partial`), `covered` (those four are each checked), `reasons` (`validator-rejected`, `nonce-mismatch`, `register-mismatch`, `simulated`, `platform-unverified`, `image-unverified`, `vtpm-unverified`), `trusted` (verdict is `trusted`), `passed` (`trusted` or `measurements-match`, `simulated` with `allow_simulated`, `partial` with `accept_partial`), `policy_scheme` (the policy's `scheme`; `azure` is never trusted), `to_dict()` (what `attest --format json` prints, besides its `"schema": "tundravm.attest/2"`); and what a record keeps: `document` (the hex tdxs document as received), `user_data`, `metadata` (a simulator's `metadata` reply), `validator_reply` (the validator's whole reply envelope), `policy_sha256` (of the policy file, or of a dict policy's sorted JSON), `policy_artifact` (the policy's `artifact`), `observed_at` (ISO 8601 UTC from the wall clock: `SOURCE_DATE_EPOCH` is not honoured, so a record cannot be made to look fresh; tests pass `attest(clock=...)`); `record()` (the `tundravm.attestation/2` record) and `save(path)` (writes it, as `attest --record FILE` does) |
| `AttestationRecord` | What `read_attestation` re-derived from a record: `record` (the JSON as read), `attestation` (an `Attestation` rebuilt from the record's document, nonce, user data, expected registers and recorded validator reply), `inconsistencies` (`field: the record says X, re-derived Y` for each stored field that disagrees), `consistent`, `verdict` (the re-derived one), `observed_at`, `policy_sha256`, `artifact_sha256` (the artifact its policy measured), `policy_matches` (whether the `policy` given is the one the record names; `None` without one), `sha256` (the record's own) |
| `Evidence` | `index` (the `evidence.json` mapping, see [CLI: Evidence](cli.md#evidence)), `members: Mapping[str, bytes]` (relative path to bytes, `evidence.json` excluded), `notes: tuple[str, ...]`, `signature: bytes | None = None` (`evidence.json.sig`); `verdict` (`"pass"`, `"fail"` or `"incomplete"`), `reasons` (the verdict's `{"severity", "code", "subject", "message"}` mappings), `passed`; `index_json()`, `files()` (every file, `evidence.json` included), `write(directory) -> Path`, `bundle(path) -> Path` (a deterministic `tar.gz`), `html() -> str` (one self-contained page), `render(format="text")` (`"text"` or `"json"`), `sign(key: Path) -> Evidence` (a copy with `signature`: `ssh-keygen -Y sign -n tundravm-evidence` over the exact `evidence.json` bytes with the SSH private key `key`; `StateError` without `ssh-keygen`, `ValidationError` when it cannot sign; `write` and `bundle` then include `evidence.json.sig`) |
| `EvidenceVerification` | `path`, `checks: tuple[VerifyCheck, ...]` (each `kind`: `member`, `index`, `artifact`, `signature` or `verdict`; `subject`, `state`: `"pass"`, `"fail"` or `"missing"`, `message`, `code`: the reason code unless it passed), `index` (the evidence's index as read), `required`, `signer` (the principal whose signature verified), `notes` (`{"code", "message"}`: `unsigned`, `signature-unchecked`, `artifacts-unchecked`); `verdict` (`"fail"` when a check failed, `"incomplete"` when one is missing, else `"pass"`), `passed`, `problems` (the checks that did not pass), `counts()`, `to_dict()` (what `verify --format json` prints, with `"schema": "tundravm.verify/1"`), `render(format="text")` (`"text"` or `"json"`), `html() -> str`; `VerifyCheck` is `tundravm.verify.VerifyCheck` |
| `ChangeReport` | `since`, `created` (the evidence's), `recipe`, `variants`, `changes: tuple[Change, ...]` (each `section`: `recipe`, `lockfile`, `pins`, `packages` (declared), `installed`, `files` or `measurements`; `change`: `"added"`, `"removed"` or `"changed"`; `subject`, `variant`, `old`, `new` (full digests, versions or register values), `detail`, `origins`: the declarations behind an emitted file), `notes`, `comparisons: tuple[Comparison, ...]` (per variant and section in `COMPARED`: `packages`, `installed`, `measurements`; `state` `"compared"` or `"unknown"`, and the `reason`); `changed`, `unknown` (the comparisons one side had no data for), `section(name)`, `counts()`, `to_dict()` (what `changes --format json` prints, with `"schema": "tundravm.changes/2"`), `render(format="text")` (`"text"`, `"json"` or `"markdown"`); `Change` and `Comparison` are in `tundravm.changes` |
| `Exploration` | `model` (the JSON document without `schema`: `recipe`, `lint`, `lock`, `bake`, `variants`, `declarations`, `trees`, `scripts`, `notes`; see [CLI: Explore](cli.md#explore)), `variants`; `to_dict()` (what `explore --format json` prints, with `"schema": "tundravm.explore/2"`), `json()`, `html() -> str` (one self-contained page: inline CSS and script, no external assets, every value escaped), `Exploration.from_dict(data)` (the inverse of `to_dict()`; another `schema` raises `ValidationError`) |
| `Release` | `name` (`<recipe>-<version>`), `version`, `path` (the release directory), `recipe`, `recipe_digest`, `lock_sha256`, `variants`, `backend`, `simulated`, `artifacts` (`{"variant", "target", "path", "sha256"}` each, `path` relative to the release), `stages: tuple[ReleaseStage, ...]` (each `name`, `execution` `"completed"`, `"skipped"` or `"failed"`, `verdict` (`evidence`, `verify` and `notes`: `"pass"`, `"fail"` or `"incomplete"`; else `None`), `resumed`, `started`, `finished`, `seconds`, `inputs` and `outputs` sha256, `summary`; `line(number)` is the CLI's stage line), `verdict` (`"pass"` or `"fail"`), `reasons` (the evidence's reasons and the failed verify checks, each with its `source`), `required`, `since`, `signer` (`{"principal", "public_key"}` or `None`), `reproducibility`, `created`, `spec` (the specification, without the private key's path or the build directory), `spec_history` (`{"at", "changes": {KEY: {"old", "new"}}}` each), `skipped` (`{"variant", "reason"}` each), `allow_ineligible`; `passed`, `finished` (it passed: the directory is final), `to_dict()` (`release.json`, with `"schema": "tundravm.release/1"`), `render(format="text")` (`"text"` or `"json"`); `ReleaseStage` is `tundravm.release.ReleaseStage` |
| `ReleaseVerification` | `path`, `checks: tuple[VerifyCheck, ...]` (each `kind`: `file`, `signature` or `release`, with a reason `code` unless it passed), `evidence: EvidenceVerification \| None` (the bundle's verification), `signer` (the principal whose manifest signature verified), `notes` (`unsigned`, `signature-unchecked`), `manifest` (the parsed `release-manifest.json`); `verdict` (`"pass"`, `"fail"` or `"incomplete"`, the evidence's checks included), `passed`, `problems` (both levels), `counts()`, `to_dict()` (`"schema": "tundravm.release-verify/1"`), `render(format="text")` |
| `Witness` | `release`, `recipe`, `version`, `manifest_sha256`, `recipe_digest`, `lock_sha256`, `builder`, `backend` (its fingerprint), `simulated`, `artifacts` (`{"variant", "target", "path", "expected", "actual", "match"}` each), `verdict` (`"matched"`, `"different"` or `"error"`), `created`, `error` (`{"code", "message"}` or `None`), `signer`, `release_path`, `path` (the witness file), `signature` (its `.sig` or `None`), `build`, `kept`, `built`; `matched`, `to_dict()` (the witness file, `"schema": "tundravm.witness/1"`), `json()`, `render(format="text")` |
| `WitnessVerification` | `release`, `witnesses`, `min_witnesses`, `checks: tuple[WitnessCheck, ...]` (each `file`, `builder`, `principal`, `counted`, `code`: a `witness-*` reason unless it counted, `simulated`, `message`), `allow_simulated`, `manifest_sha256`, `notes` (`no-witnesses`, `signatures-unchecked`); `counted` (the distinct principals), `passed`, `verdict` (`"pass"` or `"fail"`), `problems` (`witness-insufficient` last when too few counted), `to_dict()` (`"schema": "tundravm.witness-verify/1"`), `render(format="text")` |
| `Sbom` | `variant`, `components: tuple[Component, ...]` (sorted: packages by name, then builds, kernels, `efi-stub`), `base`, `arch`, `snapshot`, `mirror`, `recipe_digest`, `tree_digest`, `artifact: Path \| None` (relative to the bake directory), `artifact_sha256`, `tundravm_version`, `manifest: Path \| None` (read, or looked for; relative to the bake directory for an artifact), `manifest_found: bool`, `bound: bool` (whether the materials are the bake's: false when `allow_unbound` accepted a lockfile or manifest the bake did not record, or a manifest the bake recorded is gone; the document is incomplete), `missing: tuple[str, ...]` (the materials the bake recorded that are gone: `("manifest",)`), `complete` (property: the installed inventory, mkosi's manifest, is there; false when the packages are the declared ones), `created` (ISO 8601 UTC; `SOURCE_DATE_EPOCH` when set), `notes: tuple[str, ...]` (what it could not include, and why); `packages` and `sources` split `components`; `render(format="spdx-json")` is `"spdx-json"`, `"cyclonedx-json"`, `"text"` or `"markdown"`; `to_spdx()` and `to_cyclonedx()` are the JSON documents as dicts. A `Component` (`tundravm.declarative.bom`) has `kind` (`"package"`, `"build"`, `"kernel"`, `"efi-stub"`), `name`, `version` (a package's version, a source's commit or sha256; empty when unknown), `arch`, `type` (`deb`/`rpm`/`pkg`), `origin` (the repository, when the manifest names one), `url`, `ref`, `sha256`, `installs` (a build's install destinations), `declared` (listed from the recipe, no manifest); `commit` is a git source's pin; `tundravm.declarative.bom.purl(component)` is its package URL |

```python
from pathlib import Path
from tundravm import Backend, bake, compile, lint, lock, read_artifacts, write_lock
from tundravm.declarative import measure

assert not [d for d in lint(recipe) if d.severity == "error"]
locked = lock(recipe)
write_lock(locked, Path("build/tundravm.lock"))
compile(recipe, lock=locked).write(Path("mkosi"))
artifacts = bake(recipe, lock=locked, backend=Backend("inprocess"), build=Path("build"))
default = next(a for a in artifacts if a.variant == "default")
measured = measure(default, allow_placeholder=True)
policy = measured.to_policy(allow_placeholder=True)  # what `measure --export-policy` writes
assert measured.verify(dict(measured.values)) == ()
```

### Serialization round trips

Every file one step writes, the next step reads back with a checking reader, so a hand-off between machines needs no tundravm state beyond the file. With the minimal recipe in `image.py`, baked in process:

```python
import json
from pathlib import Path

from tundravm import Backend, Diagnostic, bake, evidence, load_recipe, lock, verify_evidence
from tundravm.declarative import measure
from tundravm.declarative.utils import Tdxs
from tundravm.measure.policy import read_policy, write_policy

recipe = load_recipe(Path("image.py"))
artifacts = bake(recipe, lock=lock(recipe), backend=Backend("inprocess"), build=Path("build"))

# Measurements -> policy file -> Tdxs: what `measure --export-policy` and `attest --policy` do
measured = measure(artifacts[0], allow_placeholder=True)  # in-process: placeholder values
write_policy(measured.to_policy(allow_placeholder=True), Path("peer.json"))
policy = read_policy(Path("peer.json"))  # checks schema_version, scheme, registers and unverifiable
verifier = Tdxs.from_policy(Path("peer.json"), allow_placeholder=True)
print(sorted(policy["registers"]), policy["tool"])
print(sorted(verifier.expected_measurements))

# Evidence -> directory -> verify_evidence, with no recipe or bake directory
gathered = evidence(recipe, build=Path("build"))
checked = verify_evidence(gathered.write(Path("evidence")))
print(gathered.verdict, checked.verdict, checked.counts())

# Diagnostic -> JSON: the objects `lint --format json` prints
finding = Diagnostic("app-port", "warning", "port 80 is privileged", subject="web",
                     variant="default", hint="Use a port above 1024.", context={"port": 80})
print(json.dumps(finding.to_dict()))
```

```text
['RTMR1', 'RTMR2'] placeholder
['rtmr1', 'rtmr2']
pass pass {'pass': 11, 'fail': 0, 'missing': 0}
{"code": "app-port", "severity": "warning", "message": "port 80 is privileged", "subject": "web", "variant": "default", "location": "", "hint": "Use a port above 1024.", "context": {"port": "80"}}
```

`read_policy` (and `write_policy`, which writes sorted, indented JSON) live in `tundravm.measure.policy`; `read_policy` takes a path or a parsed dict and raises `ValidationError` for a `schema_version` other than 2 (a schema 1 file: export it again), an unknown scheme, a malformed register value, or one of MRTD/RTMR0..RTMR2 neither accepted nor unverifiable ([policy](policy.md#measurement-policies)). `Tdxs.from_policy` refuses a placeholder policy (`tool: "placeholder"`) unless `allow_placeholder=True`. A `Diagnostic`'s `context` values come back as strings. An SDK error under `--format json` is one compact line on stderr, exit status 2:

```console
$ tundravm measure --build nowhere --format json
{"schema":"tundravm.error/1","code":"E_STATE","message":"No bake result found.","hint":"Run bake() / tundravm bake first.","context":{"path":"nowhere/bake-result.json"}}
```

### Locks and variant subsets

`Lock.sections` holds one digest per recipe section: the recipe-wide `distribution` (base, arch, mirror, tools mirror, snapshot, epoch), `compiler` (tundravm version, dialect, mkosi options), `default_profile` and `init_scripts`, and `variants.<variant>.<key>` per variant, for `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `debloat` (the complete debloat configuration), `kernel` (version, source, cmdline, tdx, sha256 of the config bytes), `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets`, `mkosi` for a variant whose mkosi options differ from the default variant's, and `extends` for a variant with a parent. File entries record each path's `kind` (`file`, `symlink` or `directory`), mode and content sha256. Drift diagnostics name these sections in `subject`. The lockfile is version 5 and also records the build toolchain (`Lockfile.toolchain`, a `tundravm.toolchain.Toolchain`; not part of the recipe digest); an older lockfile does not load (`LockfileError`, `E_LOCKFILE`: lock again). The recipe digest covers the `compiler` section, so it changes with the tundravm version.

A lock of every variant covers any subset. When `variants` leaves out a declared variant, `lock_status` and the frozen check in `bake` compare only the selected variants' sections and the recipe-wide ones; the lock's sections for the other variants, and sources pinned only for them, are not reported. The whole-recipe digest is compared only when the selection is every variant the lock holds. A lock written for a subset (`lock(recipe, variants=("default",))`) does not cover a bake of more variants: the missing variants' sections are `lock-added`.

## Shipped fragments

| Class | Fragment name | Declares |
|---|---|---|
| `Tdxs(*, source=TUNDRA_TOOLS, issuer="tdx", validator=None, expected_measurements={}, check_revocations=False, get_collateral=False, verify_imds=False, verify_identity_token=False, needs=(), legacy_units=False)` | `tdxs` | Go build packages, `Build("tdxs")`, `/etc/tdxs/config.yaml`, a typed `Service("tdxs.service", ..., type="notify", socket=Listen("/var/tdxs.sock", group="tdx"))` waiting for `needs` (the socket is enabled, the service started on demand) under `Hardening.strict(network="unix")` (`"inet"` for an `azure`/`gcp` issuer or a validator option that fetches over HTTPS); a quoting issuer (`tdx`, `azure`, `gcp`) adds `devices=("/dev/tdx_guest",)`, `capabilities=("CAP_DAC_OVERRIDE",)` (configfs-tsm report entries are created per quote and root-owned) and `write_paths=("-/sys/kernel/config/tsm/report",)` (unverified on TDX hardware), and the udev rule `/usr/lib/udev/rules.d/65-tundravm-tdx-guest.rules` makes `/dev/tdx_guest` `root:tdx 0660`; `Group("tdx")`, `User("tdxs")`; `legacy_units=True` ships the historical `nethermind-v1` `tdxs.service`/`tdxs.socket` texts instead, without the udev rule |
| `DevTools(*, root_password="tdx", salt=None, reproducible=True)` | `devtools` | Debugging packages, the serial getty on `ttyS0` enabled at build time (`serial-getty@ttyS0.service`, its template kept by debloat), root password login. Never ship it: mark its variant `release=False`. The password is hashed with a fixed salt so a second build matches: `salt` (1 to 16 of `[A-Za-z0-9./]`), else the first 16 hex digits of `sha256("tundravm-devtools:<variant name>")`, which the postinst computes from mkosi's `$IMAGE_ID`. `reproducible=False` keeps the historical random salt and the boot-time `serial-console.service` (the surge `nethermind-v1` tree). |
| `GoBinary(name, source, *, package, install=None, env={}, tags=(), ldflags="-s -w -buildid=", cgo=False, cache_namespace=None)` | *name* | `Build(name, source, using=Go(output=<install file name>, package=..., ...), install=(Install("build/<install file name>", install),))`, equal to that hand-written `Build`. `install` defaults to `/usr/bin/<name>`; `executable` is it, for `Service(name, f"{binary.executable} --flag")` |
| `CargoBinary(name, source, *, bin=None, package=None, install=None, features=(), profile="release", env={}, auditable=None, cache_namespace=None)` | *name* | `Build(name, source, using=Cargo(output=<install file name>, ...), install=(Install("target/<profile>/<bin, package or install file name>", install),))`; `install` and `executable` as for `GoBinary` |
| `TeeBase(*, time=True, nts_servers=TEE_NTS_SERVERS, hypervisor_clocks=TEE_HYPERVISOR_CLOCKS, journal=True, journal_max_use="128M", sysctl=True, dns=True, nameservers=TEE_NAMESERVERS, mask=True, network=True, firewall=True)` | `tee-base` | The confidential-VM baseline the templates use, each piece behind its flag. `time`: `Package("chrony")`, `Package("ca-certificates")`, `/etc/chrony/chrony.conf` syncing over NTS from `nts_servers` (`TEE_NTS_SERVERS`: `time.cloudflare.com`, `nts.netnod.se`), refined by `hypervisor_clocks` (`TEE_HYPERVISOR_CLOCKS`: `169.254.169.254`, `169.254.169.123`, unauthenticated, `maxdistance 1.0`), `Unit("chrony.service", enabled=True)`. `journal`: `ConfigFile.ini("/etc/systemd/journald.conf.d/50-tundravm-tee.conf")` with `Storage=volatile`, `RuntimeMaxUse=<journal_max_use>`, `ForwardToConsole=no`. `sysctl`: `/etc/sysctl.d/50-tundravm-tee.conf` from `TEE_SYSCTL` (`(key, value, reason)`, each line `-`-prefixed so a missing key is skipped). `dns`: `/etc/resolv.conf` naming `nameservers` (`TEE_NAMESERVERS`: `1.1.1.1`, `1.0.0.1`); `gcp-metadata-dns.service` replaces it on Compute Engine. `mask`: `Unit(name, masked=True)` for each of `TEE_MASKED_UNITS` (`(unit, reason)`: the apt and dpkg timers, `getty@tty1.service`). `network`: a nested `Network()`. `firewall`: `Firewall()`, opening the ports the variant's services declare. To configure either, pass `False` and declare your own. See [concepts](concepts.md#the-tee-baseline) |
| `SshAccess(*, port=22, authorized_keys=(), from_secrets=False)` | `ssh-access` | Root logs in over SSH by public key, and by nothing else: `Package("openssh-server")`; `SSH_CONFIG` (`/etc/ssh/sshd_config.d/50-tundravm.conf`: `Port <port>`, `AuthenticationMethods publickey`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin prohibit-password`, `AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/authorized_keys.d/%u`, no X11 or tunnels); `authorized_keys` baked into `SSH_AUTHORIZED_KEYS_DIR/root` (`/etc/ssh/authorized_keys.d`); the `ssh-host-keys` `Task` (root, `ssh-keygen -A`, `ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key`): the image ships no host keys, each instance makes its own at boot; `Unit("ssh.service", enabled=True, needs=(host keys task, + secrets.ready with from_secrets), ports=(port,))`, so the firewall opens *port* and `deploy qemu` forwards `ssh_port` to it (an image without `SshAccess` gets no SSH forward). Root by key only: root is the account tundra-tools delivers a key for and the image has no other login. `from_secrets` accepts the key `Secrets` delivers (`/root/.ssh/authorized_keys`) and makes sshd wait for it; one of the two is required |
| `Network(*, dhcp=True, interfaces=("en*", "eth*"), static=None, ipv6=True, wait_online=True, wait_timeout=30)` | `network` | `NETWORK_FILE` (`/etc/systemd/network/50-tundravm.network`) matching `interfaces`, with `DHCP=yes` (`ipv4` without `ipv6`), `IPv6AcceptRA=`, and `[DHCPv4] UseHostname=no`, `UseDNS=no`, `UseNTP=no`; `static=`, a `StaticAddress(address, gateway=None)` (CIDR; pass `dhcp=False`), renders `Address=`/`Gateway=` instead. Enables the packaged `systemd-networkd.service` and `.socket` (Debian ships them in `systemd`) and, with `wait_online`, `systemd-networkd-wait-online.service` with a `50-tundravm-network` drop-in running it with `--any --timeout=<wait_timeout>`, so `network-online.target` waits for one configured interface but a NIC-less boot goes on. The implicit debloat keeps these units and `/etc/systemd/network`; a declared `Debloat` needs `keep_paths=("/etc/systemd/network",)` |
| `EfiStub(*, snapshot, version)` | `efi-stub` | A postinst hook installing `systemd-boot-efi` `version` from a Debian snapshot: `snapshot` is a snapshot ID (`"20251113T083151Z"`, read from `snapshot.debian.org`) or a snapshot archive URL, and it must carry `version` (`Debian(efi_stub=version)` adds one from the platform's own archive; the templates pair `20251113T083151Z` with `257.8-1~deb13u1`). Outside `nethermind-v1` the package (`systemd-boot-efi_<version>_amd64.deb` in the snapshot's pool) is a source named `efi-stub`: `lock` pins its sha256, `fetch` downloads it on the host, and the current dialect's hook checks the mounted copy against the pin and installs it with `dpkg -i`. Without a pin the hook downloads the package into `$BUILDROOT/` (mkosi-chroot mounts its own `/tmp`) and fails with that advice when the download fails; `nethermind-v1` downloads it with `curl` |
| `KernelHardening(*, allow=())` | `kernel-hardening`: two `KernelConfig` declarations for a composed `Kernel(base=...)`: `kernel-attack-surface` turns off flashbots-images' deny list (`ATTACK_SURFACE`: io_uring, userfaultfd, XFRM/IPsec, SCTP, network filesystems, vhost, Xen, legacy TCP congestion modules...), `kernel-hardening` sets `HARDENING` (lockdown in confidentiality mode, `init_on_alloc`/`init_on_free`, per-syscall stack offset, no `/dev/mem` or `/proc/kcore`, no kexec, unprivileged BPF off, BPF JIT always on, no TIOCSTI/vsyscall/modify_ldt/iopl, no SysRq, slab and usercopy hardening, `IKCONFIG`), each with a one-line reason in `tundravm.declarative._kernel_hardening`. `allow` leaves named options out |
| `Backports(*, archive_url=None, release=None, include_sid=True)` | `backports` | Debian backports and (with `include_sid`, the default) sid apt sources for the build's apt, not the image; `include_sid=False` leaves sid and its pin out, so a package missing from the release and backports fails the build. The current dialect writes `mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources` and `preferences.d/debian-backports.pref` at compile time, pinning backports to 200 and sid to 100 so packages come from the release unless it lacks them. The URI is `archive_url` verbatim, else the platform's `mirror` and `snapshot` completed as mkosi does, else `http://deb.debian.org/debian`; the suite is `release`, else the platform's release. `nethermind-v1` generates the sources with a sync hook and `Setting("Build", "SandboxTrees", ...)`, with no pins |

`Tdxs.from_policy(policy, *, validator="tdx", mrtd=None, firmware=None, label=None, allow_placeholder=False, **fields)` builds a verifier from a policy file `tundravm measure --export-policy` wrote (or its dict): its accepted registers become `expected_measurements` under the keys the tundra-tools validator reads (`mrtd`, `rtmr0`..`rtmr3`, lower-case hex), one value each, because the validator compares a single expected value per register. A register the policy accepts several values for (a gcp policy lists one MRTD per Google firmware and one RTMR0 per firmware and machine type) is narrowed to the values labelled `firmware=` and `label=`; if more than one remains, `MeasurementError` names the choices. `tundravm attest` checks a quote against every listed value instead. The validator checks only the registers it is given, so when the policy lists MRTD as unverifiable pass `mrtd=` to check it too. A placeholder policy raises `MeasurementError` unless `allow_placeholder=True`; an uncovered register or a wrong `schema_version` raises `ValidationError`. `Tdxs.from_measurements(measurements, ...)` does the same for a `measure()` result. Other keyword arguments are `Tdxs` fields. A `Tdxs` read from a file records it in `policy_file` (`None` otherwise; not configuration, so equality and `repr` skip it), which [`recipe_inputs`](#recipe-inputs) reports.

`issuer`/`validator` are a `TdxsType` (`"tdx"`, `"azure"`, `"gcp"` or `"simulator"`) or `None`. `TUNDRA_TOOLS` is `Git("https://github.com/Hyodar/tundra-tools.git", "master")`. `Backports.render_sources(*, mirror, release, snapshot=None)` and `render_preferences(*, release)` return the two files' text (the recipe's mirror root, release and snapshot; the fragment's own fields win). `BACKPORTS_TREE` is the `SandboxTrees` entry `Backports` adds under `nethermind-v1`, `"mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"`. Each class is an instance of `Fragment`, so it goes wherever a `Fragment` does: in `common`, in a variant's `add`, or among another fragment's `items`.

### `component` and `Composite`

`@component` (from `tundravm`) turns an annotated class into a `Composite`, the `Fragment` subclass the shipped fragments are built on: a frozen, slotted dataclass with keyword-only fields whose `compose()` returns or yields its contents.

```python
from collections.abc import Iterator

from tundravm import Package, component
from tundravm.declarative.utils import Composite


@component
class Debugging(Composite):
    gdb: bool = True

    def compose(self) -> Iterator[Package]:
        yield Package("strace")
        if self.gdb:
            yield Package("gdb")
```

- `component(cls)` or `component(*, name=None, requires=())`. Construction freezes list and dict values (and defaults) into tuples and `FrozenMap`s, refuses a `str`, `int`, `float` or `bool` field (or one of those `| None`) holding another type with `ValidationError`, then calls `compose()` once. A class without `compose()` is a `ValidationError` where it is decorated.
- `compose()` returns an iterable of declarations and fragments, or a whole `Fragment` whose `name`, `requires` and `checks` become the instance's (anything else is a `ValidationError`). It may raise `ValidationError` for bad field values.
- The fragment is named `name`, else after an `id` (or `name`) field, else after the class in kebab case (`HealthCheck` is `health-check`). It requires `requires`, plus the `provider` of each `Export` held in a field (directly or in a tuple). A `check(self, resolved)` method returning diagnostics is its check.
- Subclass `Composite` so a type checker sees instances as fragments; a plain class is rebased onto `Composite` at runtime. The configuration fields alone are the instance's `repr` and equality: `Debugging()` prints as `Debugging(gdb=True)`, and two equal configurations are the same fragment, so including both expands once.

`Export(*, provider=None)` is the base of typed values a component publishes in a property for other components to consume; build one with `provider=self`, which stores the fragment name in `provider` and the instance in `origin` (excluded from equality), so resolution reports `export-stale` when a variant holds another configuration of that fragment (error) or edits what it composed (warning). A subclass with its own `__post_init__` calls `Export.__post_init__(self)`. `HttpEndpoint(host, port, path="/", *, scheme="http", provider=None)` is where a service answers HTTP: `address` is `host:port` (an IPv6 host in brackets), `url` is `scheme://address/path`; a bad host, a port outside 1..65535 or a path without a leading `/` is a `ValidationError`.

### `ConfigFile`

`ConfigFile.yaml(path, data, *, mode=0o644, stage="extra", allow_secret=False, sort_keys=True)` and `ConfigFile.toml`, `ConfigFile.json`, `ConfigFile.ini` and `ConfigFile.env`, with the same keywords, render a config file and return `File.text(path, text, mode=..., stage=..., allow_secret=...)`: equal to the hand-written `File` with those bytes, with the same recipe digest and tree. Data is `str`, `int`, `float` (finite), `bool`, `None`, lists or tuples and mappings with `str` keys; anything else (a set, a non-`str` key, `nan`) is a `ValidationError`. Mapping keys are sorted unless `sort_keys=False`, which keeps insertion order; floats are their `repr` (`0.5`, `1.0e-05`), booleans `true`/`false`, and the file ends in a newline.

| Constructor | Data | Output |
|---|---|---|
| `yaml` | a mapping or a list | Block style, two-space indents, sequences indented under their key; a list of scalars is one flow line (`["127.0.0.1:9100"]`). A string is plain unless YAML 1.1 or 1.2 would read it as another type (`"yes"`, `"8080"`, `"1e3"`) or it holds other characters; then it is JSON-quoted |
| `toml` | a mapping | A table's keys, then its sub-tables (`[a.b]`) and arrays of tables (`[[a]]`, a list of mappings); a mapping inside a list is an inline table; `None` is refused (TOML has no null) |
| `json` | a mapping or a list | `json.dumps(data, indent=2, sort_keys=sort_keys, ensure_ascii=False)` |
| `ini` | a mapping of sections to mappings | `[section]` blocks of `key = value`, a blank line apart; a list value repeats the key, `None` leaves it out |
| `env` | a mapping | `KEY=value` lines; a value of letters, digits and `_./:@%+,-` is bare, others double-quoted with `"`, `\`, `$` and backticks escaped |

The examples ship more: `NethermindBase(*, snapshot=PINNED_MIRROR)` in [`examples/nethermind_base.py`](../examples/nethermind_base.py), and `Raiko(*, source)`, `TaikoClient(*, source)`, `Nethermind(*, source)` in [`examples/fragments/`](../examples/fragments/).

## Errors

Every SDK error subclasses `TdxError(message, *, code, hint=None, context=None)`, which has `code`, `hint`, `context` and `to_dict()`. The CLI prints them as `error [CODE]: message`, the hint and the context, and exits 2; under `--format json` it prints one JSON line on stderr instead, `{"schema": "tundravm.error/1", "code", "message", "hint", "context"}` (`hint` is `null` when there is none).

| Class | Code | Raised when |
|---|---|---|
| `ValidationError` | `E_VALIDATION` | A malformed declaration, a resolution error, a recipe that cannot lower, an unknown variant, a bad recipe file |
| `LintError` | `E_LINT` | `bake` refused a recipe with error-level compiler findings |
| `LockfileError` | `E_LOCKFILE` | A missing or unreadable lockfile, a frozen bake of a recipe that drifted from it, or `lock` could not resolve some sources (`.failures`) |
| `SourceError` | `E_SOURCE` | A source could not be resolved; `.source`, `.reason` |
| `ReproducibilityError` | `E_REPRODUCIBILITY` | Two builds that should match did not: `bake(verify_reproducible=True)` (`bake --verify-reproducible`) found an artifact whose sha256 differs between them |
| `BackendExecutionError` | `E_BACKEND_EXECUTION` | The backend failed or its tools are unusable |
| `MeasurementError` | `E_MEASUREMENT` | An artifact `measure` cannot read (no UKI or disk), or a simulated artifact, without `allow_placeholder`; `attest` got a reply that is not a TDX attestation, or a placeholder policy |
| `DeploymentError` | `E_DEPLOYMENT` | A simulated artifact, a target mismatch, or a missing target tool |
| `AttestationError` | `E_ATTESTATION` | `attest` could not reach the tdxs issuer or validator, the issuer replied with an error, or the validator could not judge the document (not configured, no `data.valid`); a rejection is the `untrusted` verdict, not this error |
| `ArtifactError` | `E_ARTIFACT_CHANGED` | `verify_artifact`, and `measure`/`deploy`/`sbom` before they start, found an artifact unreadable or its bytes no longer matching the sha256 `bake-result.json` recorded; `sbom`/`evidence` were given a lockfile or manifest other than the one the bake recorded (without `allow_unbound`) |
| `PolicyError` | `E_POLICY` | A `Policy` setting refused the operation |
| `StateError` | `E_STATE` | `bake-result.json` is missing or unreadable, or has no artifact for the requested variant/target; `bake(fetch=False)` (`bake --no-fetch`) finds a source checkout missing from `build/.sources`; an offline bake (`offline=True`, `bake --offline`, `Policy(network_mode="offline")`) finds a Go, Cargo or .NET build without its prefetched dependencies in `build/.sources/deps` |

## Lint rules

Resolution and references (from `resolve`):

| Code | Severity | Meaning |
|---|---|---|
| `fragment-conflict` | error | Two different fragments share a name |
| `identity-collision` | error | Two different declarations share an identity; edit the inherited one with `changes=` |
| `unit-collision` | error | Two declarations write one unit file (`Service("app")` and `Unit("app.service", text)`, a `Task` and a `Service` with one name); the message names both and where each was declared |
| `edit-missing` | error | A variant edits something it does not inherit |
| `edit-invalid` | error | An edit sets a value its declaration rejects (the declaration's own error follows) |
| `remove-missing` | error | A variant removes something it does not inherit |
| `target-inconsistent` | error | A variant leaves a cloud output of the variant it inherits |
| `disk-key-undefined` | error | A disk's key ref names no key the variant declares |
| `secret-store-undefined` | error | The secrets' store ref names no disk the variant declares |
| `step-undefined` | error | A `BootStep.after`/`before` names a step the variant does not declare |
| `step-cycle` | error | The runtime-init steps' `after`/`before` edges form a cycle (named in the message) |
| `needs-undefined` | error | A `Unit`/`Service` `needs` a step the variant does not declare |
| `hook-after-undefined` | error | A `Hook.after` names no hook |
| `hook-order` | error | A `Hook` runs after a hook of a later phase |
| `needs-without-init` | warning | A `Unit` with `needs` in a variant with no runtime-init step |
| `fragment-requires-missing` | error | A fragment's `requires` names a fragment the variant does not include |
| `export-stale` | error / warning | An export built with `provider=self` comes from another configuration of the provider's fragment than the variant includes (error), or the variant edits or removes a declaration that fragment composed (warning) |
| `external-delivery-unwired` | warning | An `ExternalDelivery` with neither `unit=` nor `ready_path=`: nothing in the image can wait for it |
| `executable-stale` | error | A `Service` command names a path a `Build` installed (`GoBinary(...).executable`) until a variant edited its `install` or removed it |

Compiler rules (on the lowered recipe, when resolution found no error):

| Code | Severity | Meaning |
|---|---|---|
| `user-group-undefined` | error | A user joins a group nothing creates |
| `service-user-missing` | error | A generated service runs as a user nothing creates |
| `file-path-duplicate` | error | One path declared several times with different content |
| `file-path-relative` | error | A path is relative or contains `..` |
| `service-command-not-shipped` | warning | A generated service runs a binary nothing ships |
| `debloat-removes-needed-unit` | warning | Debloat masks a unit a service needs |
| `debloat-removes-declared-file` | warning | A declared `Debloat` deletes a declared file at finalize (the implicit default keeps the paths declared files live under, and the packaged `systemd-*` units the variant enables) |
| `network-missing` | warning | A service listens on a port (`Service(ports=)`, a non-loopback `Listen`), `Secrets` waits for the SSH key over HTTP, or `chrony` syncs time, but the variant brings up no interface: no `.network` file (`Network`), `network-setup.service`, `/etc/network/interfaces` or interface-managing package |
| `port-unreachable` | warning | A service listens on a port the variant's `Firewall` does not allow (`collect=False` without the port in `allow`) |
| `debloat-removes-modules` | error | The variant's debloat removes `/usr/lib/modules` (the default list of a declared `Debloat`, a parent path or a glob) while the variant boots the distribution's modular kernel (no `Kernel`, a `linux-image-*` package): its drivers (virtio, `tdx_guest`, cloud NICs) never load. `Debloat(keep_modules=True)` keeps them, `keep_modules=False` removes them on purpose; a built `Kernel` without loadable modules never triggers it, and `Kernel(modules=True)` is reported only for a declared `Debloat` whose list removes them (the implicit default keeps a built kernel's own modules). The implicit default (no `Debloat` declared) is checked too, and keeps the modules mkosi selected (`Debian(kernel_modules=)`) |
| `source-unpinned` | warning (error / info by policy) | A source build, or in the current dialect a built kernel's source or an `EfiStub` package (`efi-stub`), has no pin in `build/tundravm.lock` |
| `disk-key-path-mismatch` | warning | A disk reads a key file the key does not write |
| `disk-device-guessed` | error (warning with `Policy(storage_safety="warn")`) | A `Disk(device=None)` with `format` other than `"never"`: `disk-setup` picks the largest whole `/dev/sd*` disk, boot disk included, and can format it. Name a stable device (`/dev/disk/by-id/virtio-SERIAL`), or keep `format="never"` (the default) for a disk prepared beforehand |
| `disk-device-unstable` | warning | A formatted `Disk` whose `device` is a SCSI/SATA kernel name (`/dev/sdb`): probe order assigns it, so it can name another disk on the next boot. Use a `/dev/disk/by-id/` or `/dev/disk/by-path/` link |
| `key-pipe-outside-run` | info | A pipe key's path is outside `/run` |
| `variant-empty` | info | A variant declares nothing of its own |
| `kernel-modules-ignored` | warning | `Debian(kernel_modules=...)` selects modules but the variant's built kernel has none to load (`Kernel(modules=False)`, the default, or a whole `config=`): build the drivers in with `KernelConfig`, or pass `Kernel(modules=True)`. A composed `Kernel(modules=True)` whose debloat removes `/usr/lib/modules` (no `keep_modules`) reports `debloat-removes-modules` |
| `kernel-missing` | error | A bootable variant has no `Kernel` and installs no `linux-image-*` package; mkosi cannot build its UKI. `Setting("Content", "Bootable", ("no",))` disables both |
| `secret-env-service-unknown` | error | A `SecretEnv(service=)` names a service the variant neither generates (`Service`), ships (`Unit` with content) nor enables, disables or masks (`Unit(name, enabled=...)`, for a packaged unit) |
| `secret-target-unsupported` | error | A `SecretFile` or `SecretEnv` whose provider does not write it at boot: the built-in `tundra-tools` provider writes only the SSH key, an `ExternalDelivery` only its `supports` kinds. Declare `Secrets(delivery=ExternalDelivery(NAME, supports=...))` naming what writes it, or remove the target and use the SSH key flow |
| `secret-in-file` | error | A `File`, `Template` or `FileTree` file holds what looks like a credential: a PEM private key (header and body), an AWS access key id (`AKIA`/`ASIA`), a GitHub token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), in a config-like file (under `/etc`, or `.conf`, `.ini`, `.env`, `.yaml`, `.toml`, `.json`, ...) a `token`/`secret`/`password`/`api_key = value` assignment with a token-like value, or in a `secrets.yaml` a literal under a sensitive or `value`/`default`/`data` key. Documentation values (`EXAMPLE`, `changeme`, `xxxx`), paths, `$VAR`/`{{ }}` references, commented-out assignments, public keys and certificates do not count; the message names the kind and line, never the value. Deliver it at boot with a `SecretFile` or `SecretEnv` target of a `Secrets` whose `ExternalDelivery` writes it (the hint names the kinds the variant's provider writes), or pass `allow_secret=True` |
| `secret-in-env` | error | A unit file, a verbatim `Unit`'s or the one a `Service` renders (`Environment=` lines, or a key or token anywhere in it: `exec_start`, `exec_start_pre`, `env`...), holds a credential: a key or token as above, or a token-like literal for a `*TOKEN`, `*SECRET`, `*PASSWORD`, `*API_KEY`, `*ACCESS_KEY` variable. A `Service` finding names its field (`in exec_start`), a `Unit`'s the directive and line (`in ExecStart= (line 3)`). Deliver it with `SecretEnv(NAME, service=...)` under an `ExternalDelivery` that writes env targets, or pass `allow_secret=True` |
| `recipe-arch-unsupported` | error | `Debian(arch="aarch64")` with `EfiStub` (it installs the `_amd64.deb` systemd-boot-efi) or a `qemu` target (the QEMU adapter runs `qemu-system-x86_64`) |

Raw spellings (on the recipe's own declarations, not those of the shipped fragments; none under `nethermind-v1`, whose raw spellings reproduce the historical tree). Each reports with variant `""` for the platform and `Recipe.items`, or the variant whose `add` holds it:

| Code | Severity | Meaning |
|---|---|---|
| `setting-raw` | info | A `Setting` spells a key a typed field sets: the hint names it (`Mkosi(seed=...)`, `Debian(clean_package_metadata=...)`, ...), or says a `Build.Environment` of `KERNEL_IMAGE`/`KERNEL_VERSION` repeats what a `Kernel` with a `config` passes through. In a variant's `add` the hint adds that the field is recipe-wide |
| `hook-covered` | info | A `Hook` (or `Run`) runs in the image what a declaration does: `groupadd` (`Group`), `useradd` (`User`), `usermod -a -G` (`User(groups=)`), `systemctl enable`/`disable`/`mask` (`Unit(enabled=)`/`Unit(masked=True)`); or a `BootStep` creates a directory with `mkdir` and `chown` (`RuntimeDirectory`) |

Unit text (every unit and drop-in a variant ships, parsed with systemd 257's directive table: a `Unit`'s text, a `File`/`Template`/`FileTree` file under `/usr/lib/systemd/system/` or `/etc/systemd/system/`, the units modules write and the one each `Service` renders). The style codes (`unit-exec-relative`, `requires-without-after`, `socket-service-started`, `unit-raw`, `unit-as-file`) are off under `nethermind-v1`, whose unit bytes are historical; the others always run:

| Code | Severity | Meaning |
|---|---|---|
| `unit-section-invalid` | error | A section the unit's suffix does not allow (`[Service]` in a `.socket`, a misspelled `[Services]`); only `[Unit]`, `[Install]` and the type's own section are valid, a drop-in following its unit |
| `unit-directive-unknown` | warning | A key systemd 257 does not know in its section (`Restrat=`); systemd ignores it. `X-` keys are allowed |
| `unit-value-invalid` | error | A boolean, enum or time-span value systemd rejects (`Restart=onfailure`, `Type=daemon`, `Accept=maybe`, `TimeoutStartSec=soon`); values with `%` specifiers or `$` variables are not checked |
| `unit-exec-relative` | warning | An `Exec*=` command names its program by a relative name; systemd's search path is not `$PATH` |
| `service-user-missing` | error | Also for unit text: a `User=`, `Group=`, `SocketUser=` or `SocketGroup=` names an account nothing creates (a declared `User`/`Group`, a base account, a `useradd`/`groupadd` command, or a package whose name starts with it); `DynamicUser=yes` units are skipped |
| `socket-service-unpaired` | error (warning when the variant installs packages) | An `x.socket` whose service (`x.service`, `x@.service` with `Accept=yes`, or `Service=`) the variant neither ships, generates nor enables |
| `socket-service-started` | info | A socket-activated service is also enabled with an `[Install]` section, so it starts at boot instead of on the first connection |
| `unit-enable-noop` | warning | A unit enabled (`Unit(enabled=True)`) that has no `[Install]` section: `systemctl enable` does nothing |
| `timer-unit-missing` | error (warning when the variant installs packages) | An `x.timer` whose unit (`x.service` or `Unit=`) the variant neither ships, generates nor enables |
| `timer-service-enabled` | warning | A timer-activated service is also enabled at boot, so it runs once outside its schedule |
| `relation-conflict` | error | A unit pulls in (`Requires=`/`Wants=`) and `Conflicts=` the same unit, is ordered both `Before=` and `After=` it, or names itself |
| `cloud-glue-unconditional` | warning | A unit that only works on one cloud (`azure-complete-provisioning`, `walinuxagent`/`waagent`, `google-guest-agent` and the other Google agents, `gcp-metadata-dns`) ships in a variant whose outputs include another host, with no `ConditionVirtualization=` of its cloud (`microsoft`, `google`) in the unit or a drop-in: it would run, and fail, elsewhere. Add `conditions=(Condition.virtualization(...),)` |
| `requires-without-after` | info | Unit text with `Requires=x` and no `After=x`: both start in parallel |
| `state-dir-mkdir` | info | An `ExecStartPre=` makes a directory under `/var/lib`, `/var/log`, `/run` or `/var/cache` (`mkdir`, `install -d`): `StateDirectory=` and its siblings do it |
| `unit-raw` | info | A `Unit` ships hand-written `.service` text that a `Service(...)` declaration renders (reported on the recipe's own declarations, like the raw spellings above) |
| `unit-as-file` | info | A `File` ships a unit under a systemd directory; `Unit(name, text)` gives it `enabled=`, `needs=` and the unit lint by name |
| `unit-extra` | info | A `Service` or `DropIn` uses the `extra=` escape hatch; the finding names the keys so the hatch stays visible in review (reported under `nethermind-v1` too) |
| `hardening-disabled` | warning | A `Service` or `Task` runs with `hardening=Hardening.none()` (or the deprecated `security="none"`/`"default"`): no sandbox at all |
| `hardening-relaxed` | info | A `Service` or `Task` drops knobs of `Hardening.strict()` (`Hardening.relaxed()`, an override such as `jit=True`, or the deprecated `security="strict"`); the finding lists them |
| `service-runs-as-root` | info | A `Service` or `Task` sets `user="root"` instead of running as its own system user |

Lock drift (from `lint(lock=)` and `lock_status`): `lock-changed`, `lock-added`, `lock-removed` (each with the section as `subject`), and `lock-stale` when every section matches but the whole-recipe digest does not. Host tools (from `doctor`): `tool-missing`.

Toolchain drift (from `tundravm lint --lockfile` and `lock --check`): `toolchain-drift`, one per toolchain field (`subject` `toolchain.<field>`) the running toolchain differs on from the lockfile's; a warning, an error with `--strict` or `strict_toolchain = true`. Witness toolchains (from `verify_witnesses`, `toolchains="same"` by default): `witness-toolchain-different` (matched on another toolchain than the release's; not counted under `"same"`), `witness-toolchain-unknown` (no toolchain recorded), `witness-toolchain-repeated` (under `"distinct"`, a toolchain already counted).

## Policy

`Policy(require_frozen_lock=False, mutable_ref_policy="warn", network_mode="online", storage_safety="error")`. `network_mode="offline"` keeps `lock` and `fetch` off the network and bakes as `bake(offline=True)`; `storage_safety="warn"` demotes `disk-device-guessed` to a warning. See [policy](policy.md).

## Testing helpers

`tundravm.testing` (see [testing](testing.md)); `variants` is a name, a sequence of names, or `None` for every variant:

```python
fragment_plan(*items, include=(), base=None, variant="default", lock=None) -> Image
plan(recipe, variant=None, *, lock=None) -> Image
compile_tree(recipe, *, variants=None, path=None) -> CompiledTree
assert_clean(image_recipe_or_diagnostics, /, *, warnings="fail", allow=(), variants=None) -> diagnostics
assert_diagnostic(image_recipe_or_diagnostics, code, /, *, severity=None, variant=None, subject=None, location=None, variants=None) -> Diagnostic
assert_tree(tree: Tree, golden: str | Path, *, update=None) -> None
assert_tree_matches(recipe, golden_dir, *, variants=None, update=None) -> TreeDiff
fake_bake(tree: Tree, *, variant: str, target: Target, build: str | Path) -> Artifact
bake_in_process(recipe, *, build=None, variants=None, lock=None) -> tuple[Artifact, ...]
fake_fragment(name="fake", *, packages=(), files=None, init=None, after=(), requires=(), checks=()) -> Fragment
recipe_file(tmp_path: Path, source: str, name="recipe.py") -> Path
run_cli(*argv) -> tuple[int, str, str]
boot(artifact, *, timeout=300, memory="2G", cpus=2, firmware=None, kvm="auto", markers=DEFAULT_MARKERS, expect_units=(), cmdline=CONSOLE_CMDLINE, settle=3.0, log=None, network=True, qemu="qemu-system-x86_64", extra_args=()) -> BootResult
boot_unavailable(qemu="qemu-system-x86_64", firmware=None) -> str | None
scan_console(text: str) -> ConsoleScan
```

`BootResult` (one QEMU boot, [booting an image in tests](testing.md#booting-an-image-in-tests)): `console`, `log`, `duration`, `command`, `started`, `failed`, `fatal`, `missing`, `timed_out`, `returncode`, `stderr`, `ok`, `summary(tail=60)`, `assert_booted()`. `boot` raises `BootUnavailable` when QEMU or OVMF is missing; `ConsoleScan` has `started`, `failed`, `fatal`.

Pytest fixtures (plugin `tundravm`, loaded automatically): `recipe`, `compiled`, `run_cli`, `boot` (skips without QEMU or OVMF); marker `boot`.

`Image` (one planned variant): `packages` (`{name: role}`), `file(path)` and `files` (`ImageFile`: `content`, `text`, `mode`, `stage`, `owner`, `origin`, `fragment`), `unit(name)` and `service(name)` (`UnitView`: `get`, `values`, `requires`, `wants`, `after`, `wanted_by`, `exec_start`, `user`, `environment`, `enabled`), `users`, `groups`, `builds`, `init_steps` (`InitStep`: `name`, `after`, `script`, `builtin`, in topological run order), `hooks(stage)`, `settings(section)`, `lint()`, `tree`, `resolved`. `TEST_PLATFORM` is the fragment `fragment_plan` boots on; `Lintable` is what the assertions accept. `CompiledTree` has `root`, `variants`, `default_variant` and `variant(name)`; its readers take `variant=`.
