# Concepts

tundravm turns an immutable Python value, a `Recipe`, into a reproducible mkosi project tree, then into bootable TDX disk images, their expected measurements, and deployments. This page explains the model. The [API reference](api.md) lists every field; the [design record](design/declarative-api.md) explains why it is shaped this way.

## The seven concepts

| Concept | Type | Role |
|---|---|---|
| Recipe | `Recipe` | The whole image: recipe-wide settings, the `common` fragment and the variants |
| Declaration | `Package`, `File`, `Service`, `Unit`, `Key`, `Disk`, `Build`, ... | One typed fact about the image, identified by type plus natural key |
| Fragment | `Fragment` | A named group of declarations and nested fragments, with `requires` and `checks`; a `Composite` subclass computes one from its fields |
| Variant | `Variant` | An overlay on a parent (`add`, `replace`, `remove`, `target`); one mkosi directory and artifact each |
| Lock | `Lock` | Section digests of the resolved recipe plus one pin per source build |
| Tree | `Tree` | The compiled mkosi project, in memory until written; has a digest |
| Artifact | `Artifact` | A baked disk image of one variant and target, with the digests it came from |

Every one of them is a frozen dataclass. Nothing in a recipe is mutated after construction: a `Composite` fragment computes its contents once, from its fields, a variant describes a change instead of applying it, and the lifecycle functions take values and return values.

```python
from tundravm import Fragment, Package, Recipe, Variant

recipe = Recipe(
    name="node",
    common=Fragment("node", items=(Package("curl"),)),
    variants=(Variant("default", target="qemu"),),
)
```

`Recipe` fields: `name`, `common`, `variants` (default: one `Variant("default", target="qemu")`), `base` (`debian/trixie`), `arch` (`x86_64` or `aarch64`), `mirror`, `tools_mirror`, `snapshot`, `epoch` (`0`), `mkosi` (compiler settings: `Mkosi(layout=, dialect=, ...)`) and `policy` (a [`Policy`](policy.md), or `None` for the defaults).

### Mirrors and snapshots

`mirror` and `tools_mirror` are mirror roots, written as mkosi's `Mirror=` and `ToolsTreeMirror=`: mkosi appends `debian` (or `archive/debian/<snapshot>`) itself, so a full archive URL there is wrong. `snapshot` is a snapshot ID such as `"20251113T083151Z"`, written as `Snapshot=`; with no `mirror`, mkosi reads it from `https://snapshot.debian.org`, so every package comes from that point in time. A URL in `snapshot` is a `ValidationError` that says to put the root in `mirror`. The `cloud` and `prover` templates set `snapshot="20251113T083151Z"` and pin `EfiStub(snapshot=..., version="257.8-1~deb13u1")`, a `systemd-boot-efi` version that snapshot carries; `Backports` derives its sources from the same mirror and snapshot.

Declarations validate themselves at construction: relative paths, empty names, unknown phases, out-of-range modes, a `Disk` whose `key` is a string, or a `Unit` that ships a file without a type suffix raise `ValidationError` right where the value is built.

## Resolution

`resolve(recipe, variant=NAME)` turns one variant into a `Resolved`: a flat, ordered tuple of declarations, the variant's target, and the names of the fragments it included. Resolution runs in this order:

1. **Ancestry.** The variant's parent chain is walked to the root. `parent="base"` (the default) means `Recipe.common`; `parent=None` means nothing is inherited; any other value names a declared variant. Unknown parents and cycles raise.
2. **Expansion.** Fragments are flattened depth-first. A fragment name seen twice expands once when both are equal; two different fragments with the same name are `fragment-conflict`.
3. **Overlay.** For each variant from the root down: its `replace` declarations swap inherited ones, its `remove` declarations drop them, then its `add` fragment is expanded and added.
4. **References.** Disk keys, secret stores, init and hook `after=` names, ordering contradictions and the variant's target are checked.
5. **Fragment rules.** Every included fragment's `requires` must name another included fragment, and its `checks` run against the `Resolved` value.

### Identity

Two declarations are "the same" when they have the same identity, the type plus a natural key:

| Declaration | Identity |
|---|---|
| `Package` | name, role (`runtime`/`build`) |
| `File`, `Directory`, `Template` | stage (`skeleton`/`extra`), normalized path |
| `Unit`, `Service` | unit name (`app` and `app.service` are the same) |
| `Setting` | section, key |
| `Debloat`, `RuntimeTools`, `Kernel` | the type alone (one per variant) |
| everything else | name |

Equal repeats deduplicate silently. Two different declarations with the same identity are `identity-collision`, whether they meet inside one level or between a variant's `add` and what it inherits. There is no "last one wins": a variant that wants to change an inherited declaration says so with `replace`.

`replace` and `remove` match by identity, not by object, so `remove=(Package("jq"),)` removes the inherited `jq` package. Each must match something inherited (`replace-missing`, `remove-missing`).

### Diagnostics

`resolve()` raises `ValidationError` on the first error. `lint(recipe)` returns every problem as a `Diagnostic(code, message, level, variant, subject)` instead: resolution problems, fragment checks, and (when nothing is an error so far) the compiler's own rules on the lowered image. The [API reference](api.md#lint-rules) lists every code.

### Provenance

`tundravm.declarative.resolve.provenance(recipe, variant=NAME)` records how resolution reached its result: for every identity the variant touched, the steps that touched it, in order, each an `Origin(action, variant, fragments)`. The action is `declared` (in `common`), `added` (by a variant's `add`), `replaced` (by a variant's `replace`) or `removed` (by a variant's `remove`); `fragments` is the chain of fragments the declaration came through, empty for `replace` and `remove`, which name declarations directly. A removed identity stays in the result with `removed` as its last step, and resolution errors are not raised here (`resolve` and `lint` report them).

`tundravm inspect --variant NAME --why SUBJECT` and `explain_why(recipe, variant, subject)` build on it: for one emitted object (an image path, `unit:NAME`, `package:NAME`, `hook:NAME` or `init:NAME`) they print the declarations behind it with these steps, the compiled files that hold it and the lines the compiler generated for it (see [CLI: Explaining one object](cli.md#explaining-one-object)).

## Lowering and compiling

`compile(recipe)` resolves each variant and lowers it onto the internal compiler, which emits one mkosi directory per variant:

Lowering writes each variant's compiler state directly from its resolved declarations, in declaration order (hooks per phase, `Init` steps by priority, runtime tools after the declarations they belong to), and the compiler emits that state.
Internally `lower()` returns a frozen `Lowered` value (`declarative/_lowered.py`); `declarative/_compile.py` emits it and `declarative/_bake.py` builds it.

```
mkosi/
  default/
    mkosi.conf
    mkosi.skeleton/...        # File(stage="skeleton"), minimal.target
    mkosi.extra/...           # File, Directory, Unit content, runtime-init
    scripts/04-build.sh       # Build declarations, build-phase hooks
    scripts/06-postinst.sh    # accounts, unit enablement, postinst hooks
    scripts/07-finalize.sh    # debloat, IMAGE_VERSION strip, finalize hooks
  azure/
    ...
```

Script names follow the mkosi phases a `Hook` can run in: `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`. Inside a phase, hooks keep their declaration order except where `after=` moves one behind another hook of the same phase. A hook that runs `after` a hook of a later phase is `hook-order`.

Lowering is deterministic and never touches the network; `Path` contents (file sources, unit files, templates, `Directory` sources, kernel configs) are read at lowering time. A `Directory` keeps what it reads: without `mode` each file's and empty directory's permission bits, symlinks as links (`symlinks="follow"` ships what they point to instead) and empty directories; the lockfile records each entry's `kind` (`file`, `symlink`, `directory`). The result is a `Tree`: entries (path, bytes, mode, symlink) plus a digest over all of them. `Tree.write(path)` writes it, replacing the variant directories it holds.

A relative `Path` in a declaration (`File(path, Path(...))`, `Unit(name, Path(...))`, `Template(path, Path(...))`, `Directory(path, source)`, `Kernel(config=...)`) resolves against the working directory of the process that compiles, not against the recipe file. `tundravm compile image.py` inside the project works and `tundravm compile proj/image.py` from its parent fails with `E_VALIDATION` (`cannot read motd.txt`). The same holds for a policy file passed to `Tdxs.from_policy()`, which is read when the recipe loads. Anchor every input to the recipe file instead, as the surge example's base layer (`examples/nethermind_base.py`) does:

```python
from pathlib import Path

ROOT = Path(__file__).resolve().parent

File("/etc/app/app.conf", ROOT / "files" / "app.conf")
Directory("/opt/app", ROOT / "app")
Kernel(KERNEL_VERSION, KERNEL_SOURCE, config=ROOT / "kernel" / "kernel.config")
```

The `Mkosi` setting picks the emission: `layout="directories"` and `dialect="current"` by default. The current dialect targets mkosi 26: apt sources that the build needs (`Repository`, `Backports`) are written at compile time under `mkosi.sandbox/`, where mkosi's apt reads them, and postoutput scripts name the UKI `${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}`, so a recipe without a version works under `set -u`. `dialect="nethermind-v1"` spells user and group creation as postinst lines, leaves build hooks unmarked and clones sources inside the build, which is what the historical nethermind-tdx tree expects (see [`design/lowering-nethermind-v1.md`](design/lowering-nethermind-v1.md)).

### Bootable images

By default every variant compiles to `Format=uki`, a bootable unified kernel image, and mkosi can only build one from an image that holds a kernel. A bootable variant therefore declares a `Kernel(...)` built from source or installs a distribution kernel package (`Package("linux-image-amd64")`, any `linux-image-*`, or Ubuntu's `linux-generic`, `linux-kvm` and `linux-virtual`). Without either, lint reports `kernel-missing`, an error, so `bake` stops at its lint step before mkosi runs. A disk that is never booted declares `Setting("Content", "Bootable", ("no",))` instead: its variant compiles to `Format=disk` and `Bootable=no` and needs no kernel. The `init` templates install the Debian kernel together with `systemd-sysv`, `udev`, `kmod` and `systemd-boot-efi`.

## Runtime init

Some declarations need work at boot, before services start: generating keys, opening and mounting encrypted disks, receiving secrets. tundravm collects that work into one script, `/usr/bin/runtime-init`, run by `runtime-init.service`. The unit requires and orders after `network-setup.service` only when the variant ships that unit; otherwise it orders after `network-online.target` (`Wants=`), and so does an `azure` variant's provisioning unit.

The script is a sequence of steps sorted by priority, lowest first:

| Step | Priority | Present when the variant declares |
|---|---|---|
| `keys` | 10 | a `Key` |
| `disks` | 20 | a `Disk` |
| `secrets` | 30 | a `Secrets` |
| your `Init` | `priority=` (default 100) | that `Init` |

`Init(name, script, priority=100, after=())` adds a step. `after` names other steps, including the built-in `keys`, `disks` and `secrets`, and only orders steps of equal priority; an `after` that points to a step with a higher priority is `init-order`, and one that names a missing step is `init-after-undefined`. The names `keys`, `disks` and `secrets` are reserved. The lockfile records the order `runtime-init` runs the steps in (each step's priority and script digest), so reordering steps of equal priority drifts the lock (see [Reproducibility](reproducibility.md)).

```python
Init("exporter-directory", "install -d -m 0755 /persistent/exporter\n", priority=25, after=("disks",))
```

## Services and units

Two declarations put systemd units in the image:

- **`Service`: a generated service.** `Service("app", "/usr/bin/app", user="app", restart="on-failure")` renders `/usr/lib/systemd/system/app.service` from typed fields (`exec_start`, `user`, `group`, `env`, `env_file`, `working_dir`, `exec_start_pre`, `after`/`requires`/`wants`, `wanted_by`, `type`, `restart`, `limits`, `kill_mode`, `timeout_stop`, `security`) and enables it. With `after_init=True`, the default, it also waits for `runtime-init.service` whenever the variant has a runtime-init step.
- **`Unit`: verbatim text, or a packaged unit's state.** `Unit("app.service", content, enabled=True)` ships `content` (a string or a `Path` to read) exactly as written; the name needs its type suffix. `Unit("openntpd.service", enabled=True)` or `Unit("ssh.socket", enabled=False, masked=True)` only changes the state of a unit a package ships; it must enable, disable or mask something.

`Unit(..., after_init=True)` adds `After=runtime-init.service` and `Requires=runtime-init.service` to the shipped text's `[Unit]` section. If the variant has no runtime-init step, `lint` warns with `unit-after-init-without-init`. Apart from that, tundravm never edits `Unit` text. Use `Service` when the fields cover what you need and `Unit` when you want the exact bytes (the shipped `Tdxs()` and `DevTools()` fragments use `Unit`).

`Template(path, template, variables=())` is a file rendered with `str.format_map(variables)` at lowering time, for config files that differ only in a few values.

## Keys, disks and secrets

These three declarations reference each other by object, so a reference cannot dangle silently:

```python
key = Key("key_persistent")                                    # random 64 bytes, sealed in the TPM
disk = Disk("disk_persistent", mount="/persistent", key=key)   # LUKS2; formatted when the device is not LUKS yet
secrets = Secrets(store=disk, entries=(
    Secret("api_token", targets=(SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN"))),
))
```

- `Key(name, output=None, strategy="random"|"pipe", persist_in_tpm=True, size=64, pipe=None)`.
- `Disk(name, mount, device=None, key=None, mapper=None, format="on_fail", directories=("ssh", "data", "logs"))`. `key=None` is a plain disk; `key` may also be a `Path` to a key file. See [Disk selection and formatting](#disk-selection-and-formatting) for what `device=None` and `format` do at boot.
- `Secrets(name="secrets", entries=(), store=None, host="0.0.0.0", port=8080, ssh_directory="/root/.ssh", ssh_key_path="/etc/root_key")` declares the secrets a variant expects and where each goes. Lowering writes the `secret-delivery` config (`/etc/tdx/secrets.yaml`: listen address, SSH directory, key path, store disk), the secrets manifest (`/etc/tdx/secrets.json`: each `Secret` with `required`, its `Schema` and its targets) and the `secrets` runtime-init step, `secret-delivery setup /etc/tdx/secrets.yaml`. What the binary does with them today is narrower than the declaration: see [Secret delivery at boot](#secret-delivery-at-boot). `SecretEnv(name)` targets the global environment; `SecretEnv(name, service="app")` targets one service: its values go to `/run/secrets/app.env`, and a generated drop-in, `/usr/lib/systemd/system/app.service.d/tundravm-secrets.conf`, makes the unit read that file with `EnvironmentFile=` (a hard requirement when the secret is required, `-` optional otherwise). The secrets manifest (`/etc/tdx/secrets.json`) records `service` and `env_file` on that env target. A service the variant neither generates (`Service`), ships (`Unit` with content) nor enables, disables or masks is the `secret-env-service-unknown` lint error.

The variant must declare the very key a disk uses and the very disk secrets are stored on: `disk-key-undefined` / `disk-key-mismatch` and `secret-store-undefined` / `secret-store-mismatch` report the gaps.

### Disk selection and formatting

The `disks` step runs `disk-setup setup /etc/tdx/disk-setup.yaml` (from `tundra-tools`), which first finds each disk's device:

- `device=None` writes `strategy: "largest"`: the largest whole SCSI disk (`/dev/sd*`, partitions excluded) that `/proc/partitions` lists. It neither looks at partition tables nor skips the boot disk, so where the boot disk is the largest `sd*` disk, that is the one it picks; where the VM has no `sd*` disk (virtio disks are `/dev/vd*`, and `deploy --target qemu` attaches none) it finds nothing and the step fails.
- `device="/dev/..."` writes `strategy: "pathglob"`: the first block device matching that path (a glob pattern) under `/dev/`, skipping `/dev/sda`.

Then `format` decides whether the device is formatted before it is opened and mounted at `mount`:

| `format` | Encrypted disk (`key=` set) | Plain disk |
|---|---|---|
| `"on_fail"` (default) | formatted when the device is not a LUKS device yet; a LUKS device the key cannot open fails the step, it is not reformatted | formatted when mounting it fails, whatever the reason |
| `"on_initialize"` | formatted until `disk-setup`'s init token is on it (a LUKS device without the token included) | as `on_fail` |
| `"always"` | formatted on every boot: nothing on it survives a reboot | formatted on every boot |
| `"never"` | never formatted: a device that is not LUKS fails the step | never formatted: a device without a filesystem fails to mount |

After formatting, `disk-setup` creates `directories` under the mount point. A failing step stops `runtime-init` (`set -euo pipefail`), so `runtime-init.service` fails and every unit that requires it (`Service(after_init=True)`, `Unit(after_init=True)`) stays down. The defaults suit a throwaway VM; a production recipe names the device it means (`device="/dev/disk/by-id/..."` or the platform's data-disk path) and picks `format` for the data it must keep. Because the defaults can format the boot disk, lint reports every `Disk(device=None)` whose `format` is not `"never"` as `disk-auto-format`, a warning that `Policy(storage_safety="error")` makes an error (`storage_safety="warn"` is the default).

### Secret delivery at boot

The `secrets` step runs the `secret-delivery` binary (`tundra-tools` `cmd/secret-delivery`, `pkg/secrets`). Today it delivers one thing, an SSH public key, over this contract:

- **Readiness.** It listens on the config's `host:port` (`0.0.0.0:8080` unless `Secrets(host=, port=)` say otherwise) once the `keys` and `disks` steps have finished. There is no status endpoint: an open port is the signal that the VM waits for its key.
- **Request.** `POST` to any path; the body is the 68-character base64 field of an ed25519 public key (the `AAAAC3Nza...` part of `ssh-ed25519 AAAAC3Nza... comment`, without the type or comment), read up to 256 bytes with surrounding whitespace trimmed:

  ```console
  $ curl -X POST --data-binary "$(cut -d' ' -f2 ~/.ssh/id_ed25519.pub)" http://VM:8080/
  key accepted
  ```

- **Responses.** `200 key accepted`; `400 invalid key: must be a base64-encoded ed25519 public key (68 characters)` for any other body; `405 method not allowed` for any other method. After a `400` or `405` it keeps waiting.
- **Delivery.** On the first valid key it stops listening and writes `ssh_directory/authorized_keys` (directory `0700`, file `0600`, replacing its contents) with the one line `no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 KEY`, and the bare key to `ssh_key_path` (`0600`) when that is set. The step exits 0 and runtime-init carries on.
- **Waiting and failure.** Until a valid key arrives the step blocks with no timeout of its own (`runtime-init.service` is a oneshot unit, which systemd starts without a timeout by default), and every unit ordered after `runtime-init.service` waits with it. If it cannot listen (the port is taken) it exits 1, runtime-init fails and those units do not start.

What the binary does not do yet:

- It never reads the manifest, so `Schema`s are not checked and `SecretFile` and `SecretEnv` targets, global or per service, are not written. The image carries the manifest, the `/run/secrets/<service>.env` paths and the `EnvironmentFile=` drop-ins; a drop-in for a required secret (no `-` prefix) keeps its service from starting until something else writes that file.
- `store` is written to the config as `store_at`, but `secret-delivery setup` runs without a disk store, so the key is not kept on the disk: every boot waits for a key again.

### Runtime tools

The boot-time tools come from the `tundra-tools` repository and are built from source like any other `Build`. `RuntimeTools(source=Git(...), key_config=..., disk_config=..., secret_config=..., secret_manifest=...)` overrides where they come from and where their configs live; without it the defaults (`tundra-tools` `master`, `/etc/tdx/*.yaml`) apply.

## Builds and pinning

`Build` compiles something from source during the mkosi build phase:

```python
Build(
    "tdxs",
    Git("https://github.com/Hyodar/tundra-tools.git", "master"),
    script='go build -trimpath -o ./build/tdxs ./cmd/tdxs',
    install=(Install("build/tdxs", "/usr/bin/tdxs"),),
)
```

The source is `Git(url, ref, subdir=None, submodules=False)` or `Http(url, sha256=None)`. `script` runs in a copy of the source checkout with `env` exported; `install` copies results into the image (`Install(source, destination, mode=0o755, directory=False)`). `packages` names build-time packages. Built outputs are cached in the build directory under `<namespace>-<fingerprint16>`: `cache_key` (default: the build's name) is the namespace, and the fingerprint hashes the source pin, the build script or recipe, the install steps, the architecture and the toolchain, so changing any of them rebuilds. `nethermind-v1` keeps its historical keys.

The recipe records the symbolic source (`master`), never a commit. `tundravm lock` resolves each `Git` ref to a commit and each `Http` without `sha256` to a hash and stores the pins in the lockfile; compile and bake then build exactly those. A build without a pin is the `source-unpinned` warning.

### Fetched on the host

In the current dialect the build sandbox never fetches a source. `tundravm fetch` (and `bake`, which runs it first) checks each pinned source out on the host, as the invoking user, into `build/.sources/<name>-<pin12>-<id8>/` (`id8` hashes the url, subdirectory and submodules): a git source at its pinned commit, an http source verified against its sha256. A JSON marker records each completed checkout, and a checkout is verified before reuse (git `HEAD` at the pin with a clean tree and initialised submodules, http files matching the recorded manifest); a modified one fails with `E_SOURCE` until `tundravm fetch --force` replaces it. A build whose source differs between variants is pinned per variant, as `<variant>/<name>`. Your git credentials and SSH agent apply, so private repositories work, and one checkout serves every variant that builds that (source, pin). The backend mounts `build/.sources` into the build at `$SRCDIR/tundravm-sources` (ephemerally: nothing a script writes there reaches the host), and each build hook copies its checkout into `$BUILDROOT/build/<name>` before running the script. A hook whose source has no pin fails with a pointer to `tundravm lock` and `tundravm fetch`.

A built kernel (a `Kernel` with `config`) is a source too: its lockfile pin is named `kernel`, or `kernel-<variant>` when a variant's kernel source differs from the default variant's; `inspect` shows `pinned=`; `source-unpinned` covers it; and the kernel build script copies the checkout without `.git`, so the kernel version string stays clean. `nethermind-v1` keeps cloning sources and kernels inside the sandbox, as the historical tree does. See [reproducibility](reproducibility.md#sources-fetched-on-the-host).

### Hermetic builds

A checkout on the host still leaves a `Go`, `Cargo` or `Dotnet` build downloading its dependencies inside the sandbox, so `fetch` also prefetches them with the host's own toolchain, run as you in a scratch copy of the checkout (the checkout stays clean): `go mod download` into `OUT/.sources/deps/go` (a `GOMODCACHE`), `cargo fetch --locked` into `OUT/.sources/deps/cargo` (a `CARGO_HOME`) and `dotnet restore --runtime RID --packages` into `OUT/.sources/deps/nuget` (a NuGet global packages folder). A marker, `deps/<name>-<pin12>-<id8>.<cache>.json`, records each prefetch with the command and environment it ran, so the next fetch keeps it (`FetchedSource.deps` goes from `cargo: prefetched` to `cargo: kept`). A host without the toolchain on `PATH` skips the prefetch with a notice (`skipped (no host go)`) and a failing one is a warning (`failed (<reason>)`); either way the fetch succeeds and that build downloads its dependencies in the sandbox, as before. Script builds and kernels have no dependency cache.

In the current dialect each language build's hook copies the mounted cache, when there is one, to `$BUILDROOT/build/.tundravm-deps/<cache>` (the mount stays read-only) and, inside mkosi-chroot, points the toolchain at that copy: `GOMODCACHE` and `GOFLAGS=-mod=mod` for Go, `CARGO_HOME` for Cargo, `NUGET_PACKAGES` for .NET. When mkosi runs the script without network (`$WITH_NETWORK` is `0`) it also sets `GOPROXY=off` or `CARGO_NET_OFFLINE=true`, or, for .NET, `RestoreSources` at the copied packages folder (an explicit `--source` in `restore_args` or a project `RestoreSources` overrides it).

`bake --offline`, or `Policy(network_mode="offline")` in the recipe, makes the host the only place anything is downloaded: mkosi gets `--with-network=no`, so build and postinst scripts reach no network (mkosi itself still installs the distribution packages from the configured mirror). The fetch step reuses complete checkouts and downloads nothing (a missing checkout fails it with `E_POLICY`; prefetches show `deps: skipped (offline)`), and the bake fails with `E_STATE` before mkosi runs when a Go, Cargo or .NET build's prefetched dependencies are missing (or, with `--no-fetch`, a checkout is), naming the `tundravm fetch` to run on a host with that toolchain. A `nethermind-v1` recipe with source builds is refused offline with `E_POLICY`, because its hooks clone inside the sandbox.

`EfiStub` takes the same path. Outside `nethermind-v1` its package (`systemd-boot-efi_<version>_amd64.deb` in the snapshot's pool) is an http source named `efi-stub` (`efi-stub-<variant>` where a variant's package differs from the default variant's): `lock` pins its sha256, `fetch` downloads it on the host, and the postinst hook checks the mounted copy against the pin and installs it with `dpkg -i`. Compiled without a pin, the hook downloads the package in the sandbox as before; `nethermind-v1` keeps its `curl`.

The caches are filled by the host's toolchain and read by the image's, so they have to agree:

- **Cargo.** Cargo 1.85 changed the names of the registry cache's directories, so the host's and the image's cargo must both be 1.85 or newer; otherwise the build does not find the cache and, offline, fails.
- **.NET.** The host's .NET SDK must match the image's: the restore picks package versions (runtime packs included) for the SDK that ran it, and the image's `dotnet` looks for its own.
- **Go.** A `go.mod` whose `go` or `toolchain` line is newer than the image's Go makes it download that toolchain, which an offline bake cannot do; the image's Go has to satisfy `go.mod`.

See [reproducibility](reproducibility.md#hermetic-builds).

## Variants and targets

A variant produces one mkosi directory and, when baked, one artifact. Its `target` (`qemu`, `azure` or `gcp`, inherited from the parent, `qemu` at the root) decides the disk format and adds the platform integration: an `azure` variant gets the Azure provisioning service and `dmidecode` and converts its disk to a fixed VHD (`parted`, `qemu-img`) in a postoutput script; a `gcp` variant gets the GCP equivalents and a `tar.gz` disk (`sgdisk`). Those scripts run wherever mkosi runs its tools: on the host, or inside mkosi's tools tree when the build uses one, in which case the local backend installs `qemu-utils`, `gdisk` and `parted` into it.

```python
variants=(
    Variant("default", target="qemu"),
    Variant("azure", parent="default", target="azure"),
    Variant("gcp", parent="default", target="gcp"),
    Variant("devtools", parent="default", add=DevTools()),
)
```

`targets=("azure", "gcp")` gives one variant several outputs (one disk image each); `target=X` is shorthand for `targets=(X,)`, and passing both is an error. A child cannot drop a cloud target it inherits (`target-inconsistent`): give it `parent=None` or a qemu parent instead.

How a variant lowers depends on what it does:

- A variant whose parent is `base` or the default variant, and which only adds declarations or replaces `File`, `User`, `Group`, `Partition`, `Repository` or `Debloat`, lowers as an overlay of the default variant's directory. This keeps committed trees such as surge's byte-identical.
- Every other variant (chained onto another variant, parentless, removing anything, replacing other kinds, changing keys, disks or secrets, mounting its own `BuildSources`, having its own settings or kernel, or leaving a cloud-targeted default variant's targets) is lowered standalone from its own resolved declarations, with its own `mkosi.conf` lines and kernel.

The default variant (the one named `default`, else the first root variant) may itself have a parent; that parent then lowers like any other variant.

These still raise `ValidationError` when you compile, lint or bake:

- a standalone-lowered variant under `Mkosi(layout="native")`, because mkosi applies the root configuration to every profile; the error lists each such variant with its reason;
- unreadable files, `Unit` or `Template` sources that are not UTF-8, and missing or empty `Directory` sources;
- a `Template` placeholder without a value, and a malformed `Setting` value (a non-boolean `Build.WithNetwork`, several values for a single-valued key);
- a `Setting` for a key the compiler writes itself (`Packages`, `Mirror`, `Format`, ...), naming the declaration to use instead;
- a `Kernel` from `Http` without `sha256`;
- two `Secrets` whose config, manifest or delivered file paths overlap;
- variant parent cycles and `Init`/`Hook` ordering cycles.

## Lifecycle

```
Recipe ──compile──▶ Tree ──write──▶ mkosi/            (committed, compile --check)
   │
   └──lock──▶ Lock ──write_lock──▶ build/tundravm.lock  (committed, lock --check)
                │
                └──fetch──▶ build/.sources/<name>-<pin12>-<id8>/  (host checkouts)
                                     │
Recipe + Lock + Backend ──bake──▶ Artifact(s) + build/bake-result.json
                                     │
                                     ├──measure──▶ Measurements
                                     └──deploy───▶ Deployment
```

| Step | Function | CLI | Needs |
|---|---|---|---|
| Inspect | `resolve`, `resolve_all`, `explain_why` | `inspect`, `inspect --why` | the recipe |
| Lint | `lint(recipe, lock=None)` | `lint` | the recipe; a lock applies its pins and adds drift |
| Compile | `compile(recipe, lock=None) -> Tree` | `compile` | the recipe; a lock applies its pins |
| Lock | `lock(recipe, previous=None) -> Lock` | `lock` | the network, unless every source is already pinned |
| Fetch | `fetch(recipe, lock=, out=, force=False) -> tuple[FetchedSource, ...]` | `fetch` | the network (as you), unless the checkouts are already there; `go`, `cargo` or `dotnet` on `PATH` to prefetch dependencies |
| Bake | `bake(recipe, lock=, backend=, out=, fetch=True, offline=False) -> tuple[Artifact, ...]` | `bake` | a backend; fetches first unless `fetch=False` (`--no-fetch`); `offline=True` (`--offline`) needs every checkout and dependency cache fetched |
| Measure | `measure(artifact, scheme="rtmr") -> Measurements` | `measure` | `measured-boot` or `dstack-mr`, or `allow_placeholder` |
| Deploy | `deploy(artifact, using=Qemu()/Azure(...)/Gcp(...), allow_simulated=False) -> Deployment` | `deploy` | the target's tool (`qemu-system-x86_64`, `az`, `gcloud`) |

A lock of every variant covers a bake or `lock --check` of any subset of them: only the selected variants' sections and the recipe-wide ones are compared. `bake` fetches the locked sources into `build/.sources` before it builds, so `tundravm fetch` on its own is for checking sources out ahead of time (or for a host that bakes offline). `tundravm status RECIPE` checks each of these steps without writing anything or touching the network and names the next command to run.

`tundravm init` writes a `[tool.tundravm]` table (`recipe`, `out`, `tree`, `lockfile`, `backend`) into the project's `pyproject.toml`, and every command reads it, so in a scaffolded project `RECIPE` is optional everywhere and every step agrees on the paths: `tundravm status`, `tundravm bake` and `tundravm ci` need no arguments. A flag overrides the table's value, and `tundravm config` prints the values the commands resolve, each with its origin (`flag`, `pyproject`, `default`, or `recipe` for the backend the recipe file binds), and marks a recipe or lockfile that does not exist (see [CLI: Project configuration](cli.md#project-configuration)).

`bake` writes `out/bake-result.json`, the manifest `read_artifacts()`, `tundravm measure` and `tundravm deploy` read, so measuring and deploying work in a later process. Each `Artifact` carries the recipe digest, the lockfile digest and the tree digest it was built from. `measure` and `deploy` first hash the artifact (`verify_artifact`) and refuse one whose bytes changed since the bake with `ArtifactError` (`E_ARTIFACT_CHANGED`); `tundravm status --verify` reports the same check.

`deploy` hands the artifact to the target's adapter. On QEMU the VM detaches by default, with its serial console in `OUT/<variant>/qemu-serial.log`, a monitor socket `qemu.monitor` and a pid file `qemu.pid` beside it (`deploy --attach` keeps it in the foreground on the terminal instead); every port the variant's `Secrets` listen on, which the bake records in `bake-result.json`, is forwarded from the same host port, besides `ssh_port` and `Qemu(forward=...)`; and `tdx=True` boots TDVF through `-bios` with a `tdx-guest` object and a split irqchip. On Azure the VHD is published as an image version in a Compute Gallery (`Azure(gallery=)`, default `tdx_images`) whose definition supports confidential VMs, and the VM is a TDX confidential VM (`Standard_DC2es_v5` by default) with Secure Boot off: Azure's Secure Boot firmware boots only signed images and tundravm does not sign the UKI, so `Azure(secure_boot=True)` also needs `signed=True`, your statement that the UKI was signed outside tundravm with keys Azure trusts. On GCP the image is created `TDX_CAPABLE` and the instance requests `--confidential-compute-type=TDX` on the C3 series (`c3-standard-4` by default). See [CLI: Deploying to each target](cli.md#deploying-to-each-target).

The in-process backend writes simulated artifacts (`Artifact.simulated`). `measure` refuses them unless you pass `allow_placeholder=True` (`--allow-placeholder`), and `deploy` unless you pass `allow_simulated=True` (`--allow-simulated-artifact`); placeholder measurements say so (`tool="placeholder"`).

## Backends

| Backend | `Backend(kind)` | Runs mkosi |
|---|---|---|
| `LimaMkosiBackend` | `lima` | inside a Lima VM with Nix (macOS and Linux) |
| `NixMkosiBackend` | `nix` | through `nix develop` on a Linux host |
| `LocalLinuxBackend` | `local` | from `PATH` on Linux (mkosi v25+, `sudo` or `unshare`) |
| `InProcessBackend` | `inprocess` | not at all: writes simulated artifacts for tests |

A recipe file names its backend in a module-level `backend` variable; the Python `bake()` takes a `Backend` value. Nothing before `bake` needs a backend.

`bake` compiles the tree into `OUT/mkosi/` and every mkosi backend gives mkosi absolute paths: `--directory` is the variant's directory of that tree (or the tree root plus `--profile` for native profiles) and `--output-dir` is `OUT/<variant>/output/`, so artifacts land there whatever the working directory.

- `LocalLinuxBackend` keeps mkosi's workspace, cache and tools tree in `OUT/.mkosi/`. After a `sudo` run it chowns what mkosi wrote back to the invoking user, warning with the `sudo chown` to run if that fails. When the recipe sets no `ToolsTree` and the host lacks a tool the build needs (`ukify` for a UKI; `pefile`, under the Python that runs mkosi, for any build that is not `Bootable=no`), it adds `--tools-tree=default` to the mkosi command, and a later bake into the same `OUT` (also one whose recipe sets `ToolsTree=default`) reuses `OUT/.mkosi/mkosi.tools`. An `azure` or `gcp` variant built in the default tools tree gets `--tools-tree-package=qemu-utils,gdisk,parted` and reuses a cached tree only when it holds those tools; only a build on host tools needs `qemu-img` or `sgdisk` on the host, which `bake` checks before mkosi runs. See [CLI: Local backend](cli.md#local-backend) for the measured template bakes.
- `NixMkosiBackend` runs mkosi inside `nix develop path:OUT/mkosi` (an absolute path), with the tools the generated flake provides; inside a Nix shell already, it runs mkosi directly.
- `LimaMkosiBackend` mounts `OUT` in the VM at `/home/debian/mnt` and translates each host path under it to the VM path, so `OUT/mkosi/<variant>` is `/home/debian/mnt/mkosi/<variant>`; a tree or output path outside `OUT` is an `E_BACKEND_EXECUTION` error. mkosi writes to `/home/debian/mkosi-output` in the VM, with its cache in `/home/debian/mkosi-cache`, and the backend moves the output to `OUT/<variant>/output/`.

A backend gets one `BakeRequest` per variant (`profile`, `build_dir`, `emit_dir`, `output_targets`, and `sources_dir`, the host checkouts `OUT/.sources` that it mounts at `$SRCDIR/tundravm-sources`, or `None` when the build fetches nothing on the host). It streams mkosi's output lines to `on_output(line)` and reports what it decided on the user's behalf, such as adding a tools tree, to `on_notice(level, message)` with `level` `"info"` or `"warning"`; `request.notice(level, message)` calls it when it is set. `bake` prints an `info` notice as a `note` line and a `warning` as a `warning` line, both hidden by `-q`; with `--json-logs` they are a `log` event whose `extra.source` is `notice` and a `warning` event.

## Measurements

`measure(artifact, scheme=)` derives the values a verifier should expect:

- `rtmr` uses `measured-boot` or `dstack-mr` on `PATH` and returns RTMR values; `Measurements.tool` names the tool and version.
- `azure` and `gcp` have no local tool yet; they return placeholders only with `allow_placeholder=True`.

`Measurements.to_json(path)` writes the values for a verifier, and `Measurements.verify(expected)` returns the registers that differ from an expected set (empty when all match). `tundravm measure --export-policy FILE` writes them as a verifier policy, which `Tdxs.from_policy(FILE)` (or `Tdxs.from_measurements(measurements)` in-process) turns into a validator's `expected_measurements`; RTMR tools do not report MRTD, so pass `mrtd=` to have it checked too (see [API: Shipped fragments](api.md#shipped-fragments)).

Placeholder values are derived from the artifact digest. They are never real measurements and must never go into an attestation policy.
