# Concepts

tundravm turns an immutable Python value, a `Recipe`, into a reproducible mkosi project tree, then into bootable TDX disk images, their expected measurements, and deployments. This page explains the model. The [API reference](api.md) lists every field; the [design record](design/declarative-api.md) explains why it is shaped this way.

## The model in one recipe

A recipe says three things. Its **platform** is what every variant builds on: `Debian("trixie", snapshot=..., boot="systemd", efi_stub=...)` is the distribution release and archive, plus the packages a bootable image needs (the kernel and the systemd boot set, `recipe.platform.packages`). Its **items** are the declarations every variant starts from: typed facts (`Package`, `File`, `User`, `Service`, `Build`, `Disk`, ...) and fragments that group them. A reusable fragment is a **component**: a `@component` class whose fields are its configuration and whose `compose()` yields its contents (`GoBinary`, `DevTools` and `Tdxs` in `tundravm.declarative.utils` are components too). Its **variants** each describe one image as a change to the variant it `inherits`: `add` a fragment, `changes` that edit or remove inherited declarations through their refs (`motd.ref.edit(content=...)`, `Unit.ref("ssh.service").remove()`), and the `outputs` it builds (`qemu`, `azure`, `gcp`).

```python
from tundravm import Debian, File, Package, Recipe, Service, User, Variant
from tundravm.declarative.utils import DevTools

motd = File.text("/etc/motd", "node\n")

recipe = Recipe(
    name="node",
    platform=Debian("trixie"),  # the kernel and systemd boot set every variant boots with
    items=(
        Package("curl"),
        motd,
        User("app"),
        Service("app", "/usr/bin/sleep infinity", user="app", wanted_by="minimal.target"),
    ),
    variants=(
        Variant("default", outputs=("qemu",)),
        Variant("azure", inherits="default", outputs=("azure",)),
        Variant("dev", inherits="default", add=DevTools(), release=False),
        Variant("prod", changes=(motd.ref.edit(content="production\n"),)),
    ),
)
```

Resolving a variant flattens platform, items and the variant chain into one ordered tuple of declarations; lowering and compiling turn that into one mkosi directory; baking turns the directory into one disk image per output. Everything on the way is a value with a digest, which the lockfile, the evidence and a release record.

## The concepts

| Concept | Type | Role |
|---|---|---|
| Recipe | `Recipe` | The whole image: its platform, recipe-wide settings, its own `items` and the variants |
| Platform | `Debian` (a `Platform`) | What the image builds on: the distribution release, arch, archive (`snapshot`, `mirror`), `epoch`, and the declarations every variant gets (the `boot` package set, a pinned `efi_stub`, `items`) |
| Declaration | `Package`, `File`, `Service`, `Unit`, `Key`, `Disk`, `Build`, ... | One typed fact about the image, identified by type plus natural key |
| Fragment | `Fragment` | A named group of declarations and nested fragments, with `requires` and `checks`; a `@component` class computes one from its fields |
| Variant | `Variant` | An overlay on the variant it `inherits` (`add`, `changes`, `outputs`); one mkosi directory and artifact each |
| Lock | `Lock` | Section digests of the resolved recipe plus one pin per source build |
| Tree | `Tree` | The compiled mkosi project, in memory until written; has a digest |
| Artifact | `Artifact` | A baked disk image of one variant and target, with the digests it came from |

Every one of them is a frozen dataclass. Nothing in a recipe is mutated after construction: a component computes its contents once, from its fields, a variant describes a change instead of applying it, and the lifecycle functions take values and return values.

`Recipe` fields: `name`, `platform` (default `Debian("trixie", boot=None)`: no kernel, so pass `Debian(release)` for a bootable image), `items` (its own declarations and fragments; `packages("curl", "jq")` bundles packages without a fragment name), `variants` (default: one `Variant("default", outputs=("qemu",))`), `mkosi` (compiler settings: `Mkosi(layout=, dialect=, ...)`) and `policy` (a [`Policy`](policy.md), or `None` for the defaults). The archive and build settings are the platform's: `Debian(release, arch=, snapshot=, mirror=, tools_mirror=, epoch=0, boot="systemd", kernel_package="linux-image-amd64", efi_stub=None, items=())`.

### Mirrors and snapshots

`Debian(mirror=, tools_mirror=)` are mirror roots, written as mkosi's `Mirror=` and `ToolsTreeMirror=`: mkosi appends `debian` (or `archive/debian/<snapshot>`) itself, so a full archive URL there is wrong. `snapshot` is a snapshot ID such as `"20251113T083151Z"`, written as `Snapshot=`; with no `mirror`, mkosi reads it from `https://snapshot.debian.org`, so every package comes from that point in time. A URL in `snapshot` is a `ValidationError` that says to put the root in `mirror`. The `cloud` and `prover` templates set `Debian(release, snapshot="20251113T083151Z", efi_stub="257.8-1~deb13u1")`: the platform pins `EfiStub` to a `systemd-boot-efi` version that snapshot carries; `Backports` derives its sources from the same mirror and snapshot.

Declarations validate themselves at construction: relative paths, empty names, unknown phases, out-of-range modes, a `Disk` whose `key` is a string, or a `Unit` that ships a file without a type suffix raise `ValidationError` right where the value is built.

## Resolution

`resolve(recipe, variant=NAME)` turns one variant into a `Resolved`: a flat, ordered tuple of declarations, the variant's outputs, and the names of the fragments it included. Resolution runs in this order:

1. **Ancestry.** The variant's `inherits` chain is walked to the root. Every variant starts from the platform's declarations (provenance level `platform`); `inherits="base"` (the default) adds `Recipe.items`; `inherits=None` means nothing else is inherited; any other value names a declared variant. Unknown names and cycles raise.
2. **Expansion.** Fragments are flattened depth-first. A fragment name seen twice expands once when both are equal; two different fragments with the same name are `fragment-conflict`.
3. **Overlay.** For each variant from the root down: its `changes` edit or drop inherited declarations, in order, then its `add` fragment is expanded and added.
4. **References.** Disk keys, secret stores, init and hook `after=` names, ordering contradictions and the variant's outputs are checked.
5. **Fragment rules.** Every included fragment's `requires` must name another included fragment, and its `checks` run against the `Resolved` value.

### Identity

Two declarations are "the same" when they have the same identity, the type plus a natural key:

| Declaration | Identity |
|---|---|
| `Package` | name, role (`runtime`/`build`) |
| `File`, `FileTree`, `Template` | stage (`skeleton`/`extra`), normalized path |
| `Unit`, `Service` | unit name (`app` and `app.service` are the same) |
| `Setting` | section, key |
| `Debloat`, `RuntimeTools`, `Kernel` | the type alone (one per variant) |
| everything else | name |

Equal repeats deduplicate silently. Two different declarations with the same identity are `identity-collision`, whether they meet inside one level or between a variant's `add` and what it inherits. There is no "last one wins": a variant that wants to change an inherited declaration says so in `changes`.

A change targets an identity through a ref: `value.ref` for a value you hold, `Type.ref(*key)` for one you do not (`Unit.ref("ssh.service")`, `Package.ref("jq")`). `ref.edit(**fields)` sets fields of the inherited declaration and `ref.remove()` drops it:

```python
key = Key("data", output="/run/key")
disk = Disk("data", mount="/data", key=key)     # the disk holds key.ref, not the value
secrets = Secrets(store=disk)                   # likewise disk.ref
recipe = Recipe(
    "r",
    items=(key, disk, secrets, Unit("ssh.service", enabled=True)),
    variants=(
        Variant("default"),
        Variant("small-key", inherits="default", changes=(key.ref.edit(size=32),)),
        Variant("no-ssh", inherits="default", changes=(Unit.ref("ssh.service").remove(),)),
    ),
)
```

`small-key` edits one field: the disk and the secrets still name the key and the disk by identity, so they need no new values, and lowering binds the disk to the 32-byte key. Each change must match something inherited (`edit-missing`, `remove-missing`); an edit names fields of its type, never an identity field, and an edited value is checked like a declared one (`edit-invalid`). Editing one field twice in a variant, or editing and removing the same identity, is refused when the `Variant` is built. A variant that wants a different identity removes the old declaration and adds the new one.

### Diagnostics

`resolve()` raises `ValidationError` on the first error. `lint(recipe)` returns every problem as a `Diagnostic(code, severity, message, subject, variant, location, hint, context)` instead: resolution problems, fragment checks, and (when nothing is an error so far) the compiler's own rules on the lowered image, with their hints. `variant` is the variant a finding is in, or `""` for the recipe as a whole (the common fragment, the lockfile, the host). The [API reference](api.md#lint-rules) lists every code.

### Provenance

`tundravm.declarative.resolve.provenance(recipe, variant=NAME)` records how resolution reached its result: for every identity the variant touched, the steps that touched it, in order, each an `Origin(action, variant, fragments, fields, location)`. The action is `declared` (in `Recipe.items`, the level `common`), `added` (by a variant's `add`), `edited` (by an edit in a variant's `changes`; `fields` names the fields it set) or `removed` (by a removal); `fragments` is the chain of fragments the declaration came through, empty for changes, which name declarations by ref, and for a declaration placed directly in `Recipe.items`. `location` is where the step was written: every declaration, `Fragment`, `Variant`, platform and `Edit`/`Remove` records the `file:line` of the call that constructed it (`declared_at`: the first caller outside tundravm, so what a module or component declares points at the recipe line that uses it, and what a platform declares at the platform). It is not part of the value: two declarations written on different lines are equal, hash the same and give the same recipe digest, lockfile and compiled tree, and evidence records no lines. A declared or added step carries the declaration's line, an edit or removal its own (an edited declaration keeps its original line). `inspect --why` and `explore` print the line relative to the working directory: `declared at node.py:53 in common via app (App)`, `size edited at recipe.py:30 in variant small-key`; lint findings use the same lines (see [CLI: Lint](cli.md#lint)). A removed identity stays in the result with `removed` as its last step, and resolution errors are not raised here (`resolve` and `lint` report them).

`tundravm inspect --variant NAME --why SUBJECT` and `explain_why(recipe, variant, subject)` build on it: for one emitted object (an image path, `unit:NAME`, `package:NAME`, `hook:NAME` or `init:NAME`) they print the declarations behind it with these steps, the compiled files that hold it and the lines the compiler generated for it (see [CLI: Explaining one object](cli.md#explaining-one-object)). `tundravm explore` (`explore(recipe)`) puts these steps for every declaration and every emitted file on one page, cross-linked, so a reviewer can follow a file back to its fragment and overlay without reading the recipe (see [CLI: Explore](cli.md#explore)).

## Lowering and compiling

`compile(recipe)` resolves each variant and lowers it onto the internal compiler, which emits one mkosi directory per variant:

Lowering writes each variant's compiler state directly from its resolved declarations, in declaration order (hooks per phase, `BootStep`s in graph order, runtime tools after the declarations they belong to), and the compiler emits that state.
Internally `lower()` returns a frozen `Lowered` value (`declarative/_lowered.py`); `declarative/_compile.py` emits it and `declarative/_bake.py` builds it.

```
mkosi/
  default/
    mkosi.conf
    mkosi.skeleton/...        # File(stage="skeleton"), minimal.target
    mkosi.extra/...           # File, FileTree, Unit content, runtime-init
    scripts/04-build.sh       # Build declarations, build-phase hooks
    scripts/06-postinst.sh    # accounts, unit enablement, postinst hooks
    scripts/07-finalize.sh    # debloat, IMAGE_VERSION strip, finalize hooks
  azure/
    ...
```

Script names follow the mkosi phases a `Hook` can run in: `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`. Inside a phase, hooks keep their declaration order except where `after=` moves one behind another hook of the same phase. A hook that runs `after` a hook of a later phase is `hook-order`.

A command that only needs to run inside the image while it is built is a `Run("update-ca-certificates --fresh")` (from `tundravm.declarative.utils`): it renders the `Hook` with the `mkosi-chroot` prefix, quotes a sequence of words, runs anything with shell syntax through `bash -euo pipefail -c` inside the image, and takes a `stage` in plain words (`"postinst"` by default; `"finalize"`, `"prepare"`, `"build"`). Accounts, unit enablement and state directories are declarations, not commands: lint `hook-covered` names `Group`, `User(groups=)`, `Unit(enabled=)` or `RuntimeDirectory` when a hook or boot step spells one by hand.

Lowering is deterministic and never touches the network; `Path` contents (file sources, unit files, templates, `FileTree` sources, kernel configs) are read at lowering time. A `FileTree` keeps what it reads: without `mode` each file's and empty directory's permission bits, symlinks as links (`symlinks="follow"` ships what they point to instead) and empty directories; the lockfile records each entry's `kind` (`file`, `symlink`, `directory`). The result is a `Tree`: entries (path, bytes, mode, symlink) plus a digest over all of them. `Tree.write(path)` writes it, replacing the variant directories it holds.

The `Mkosi` setting picks the emission: `layout="directories"` and `dialect="current"` by default. The current dialect targets mkosi 26: apt sources that the build needs (`Repository`, `Backports`) are written at compile time under `mkosi.sandbox/`, where mkosi's apt reads them, and postoutput scripts name the UKI `${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}`, so a recipe without a version works under `set -u`. `dialect="nethermind-v1"` spells user and group creation as postinst lines, leaves build hooks unmarked and clones sources inside the build, which is what the historical nethermind-tdx tree expects (see [`design/lowering-nethermind-v1.md`](design/lowering-nethermind-v1.md)).

The rest of mkosi's build mechanics have opinionated defaults a recipe rarely touches, so it never needs an mkosi section or key name: the compiler's fixed partition UUID seed, mkosi's own output and package cache directories, an uncompressed image with a JSON manifest, network for build scripts, and package metadata cleaned from the image. `Mkosi(seed=, output_dir=, package_cache=, compress=, manifest=, build_network=)` and `Debian(clean_package_metadata=)` change them for every variant. `Setting(section, key, values)` stays the escape hatch for the long tail of mkosi keys; a `Setting` that repeats a typed field reports `setting-raw` (info) naming it.

### Paths and assets

A relative `Path` in a declaration (`File.copy(path, Path(...))`, `Unit.from_file(name, Path(...))`, `Template(path, Path(...))`, `FileTree(path, source)`, `Kernel(config=...)`) resolves against the working directory of the process that compiles, not against the recipe file. `tundravm compile image.py` inside the project works and `tundravm compile proj/image.py` from its parent fails with `E_VALIDATION` (`cannot read motd.txt`). The same holds for a policy file passed to `Tdxs.from_policy()`, which is read when the recipe loads. Name host files through `Resources.from_module(__name__)` instead: it resolves paths against the directory of the module that declares them, so they work from any working directory, after the recipe moves, and from an installed package (`importlib.resources`). The surge example's base layer (`examples/nethermind_base.py`) and image read their kernel config and system files this way:

```python
from tundravm import File, FileTree, Kernel, Resources, Unit

assets = Resources.from_module(__name__)

File.copy("/etc/app/app.conf", assets.file("files/app.conf"))   # read when the recipe lowers
File.text("/etc/motd", assets.text("motd.txt"))                  # read now, shipped as text
Unit.from_file("app.service", assets.file("units/app.service"))
FileTree("/opt/app", assets.directory("app"))
Kernel(KERNEL_VERSION, KERNEL_SOURCE, config=assets.file("kernel/kernel.config"))
```

`file()` and `directory()` return absolute paths and fail at once, naming the module and the relative path, when the file is missing. `File.text` and `File.copy` say which kind of content a `File` holds; both build the same `File` the constructor does, so the digests do not change. `File.copy(..., mode=None)`, its default, ships `0755` when the source has an execute bit and `0644` otherwise: the bits git records, whatever the checkout's umask. `tundravm watch` notices edits to every file these return or read.

### Bootable images

By default every variant compiles to `Format=uki`, a bootable unified kernel image, and mkosi can only build one from an image that holds a kernel. A bootable variant therefore declares a `Kernel(...)` built from source or installs a distribution kernel package (the platform's `kernel_package`, `"linux-image-amd64"` by default, any `linux-image-*`, or Ubuntu's `linux-generic`, `linux-kvm` and `linux-virtual`). Without either, lint reports `kernel-missing`, an error, so `bake` stops at its lint step before mkosi runs. A disk that is never booted declares `Setting("Content", "Bootable", ("no",))` on `Debian(boot=None)` instead: its variant compiles to `Format=disk` and `Bootable=no` and needs no kernel. `Debian(release)` (`boot="systemd"`) installs the Debian kernel together with `systemd`, `systemd-sysv`, `udev`, `kmod` and `systemd-boot-efi`, as the `init` templates do.

## Runtime init

Some declarations need work at boot, before services start: generating keys, opening and mounting encrypted disks, receiving secrets. tundravm collects that work into runtime-init *steps*. The steps form a graph, not a priority list:

| Step | Handle | Runs after | Present when the variant declares |
|---|---|---|---|
| `keys` | `key.ready` | nothing | a `Key` |
| `disks` | `disk.ready` | `keys` | a `Disk` |
| `secrets` | `secrets.ready` | `disks`, when a `Secrets` has `store=` | a `Secrets` (tundra-tools' SSH-key setup) |
| `delivery-<name>` | `delivery.ready` | as `secrets` | an `ExternalDelivery` with `unit=` or `ready_path=` |
| yours | `step.ready` | its `after=`, and before its `before=` | that `BootStep` |

`BootStep(name, script, after=[...], before=[...])` adds a step. `after` and `before` take readiness handles: `key.ready`, `disk.ready`, `secrets.ready`, another step's `.ready`, or a step name. The steps run in a deterministic topological order. When several steps are ready, the built-ins go first (`keys`, `disks`, `secrets`), then yours in declaration order, so existing recipes keep their order. A cycle is `step-cycle` and names the steps in it. A reference to a step the variant does not declare is `step-undefined`. Both are errors. The names `keys`, `disks` and `secrets` are reserved. `RuntimeDirectory(path, owner=, group=, mode=, after=)` and `RuntimeFile(path, text, owner=, group=, mode=, after=)` in `tundravm.declarative.utils` are `BootStep`s that create a directory or write a file:

```python
directory = RuntimeDirectory("/persistent/exporter", after=[disk.ready])
marker = RuntimeFile("/persistent/exporter/boot.prom", "up 1\n", after=[directory.ready, secrets.ready])
Unit("prometheus-node-exporter.service", EXPORTER_UNIT, enabled=True, needs=[marker.ready])
```

In the current dialect each step is a oneshot unit, `runtime-init-<step>.service`, running `/usr/lib/runtime-init/<step>`. Its `After=`/`Requires=` name the steps it runs after. `runtime-init.service` requires every step and is enabled in `minimal.target`. Steps that do not depend on each other can run in parallel. Under `nethermind-v1`, one script, `/usr/bin/runtime-init`, runs every step in the topological order from `runtime-init.service`. Every step unit, like that script's unit, requires and orders after `network-setup.service` only when the variant ships that unit. Otherwise it orders after `network-online.target` (`Wants=`), and so does an `azure` variant's provisioning unit. The lockfile records the steps in run order (each step's name and script digest), so reordering steps drifts the lock (see [Reproducibility](reproducibility.md)).

## Services and units

Two declarations put systemd units in the image:

- **`Service`: a generated service.** `Service("app", "/usr/bin/app", user="app", restart="on-failure")` renders `/usr/lib/systemd/system/app.service` from typed fields (`exec_start`, `user`, `group`, `env`, `env_file`, `working_dir`, `exec_start_pre`, `after`/`requires`/`wants`, `wanted_by`, `type`, `restart`, `limits`, `kill_mode`, `timeout_stop`, `security`) and enables it. `needs=[...]` lists the runtime-init steps it waits for. The default, `needs=[RUNTIME_INIT]`, waits for every step (`runtime-init.service`) whenever the variant has one. `needs=()` starts it without waiting.
- **`Unit`: verbatim text, or a packaged unit's state.** `Unit("app.service", content, enabled=True)` ships `content` (a string or a `Path` to read) exactly as written; the name needs its type suffix. `Unit("openntpd.service", enabled=True)` or `Unit("ssh.socket", enabled=False, masked=True)` only changes the state of a unit a package ships; it must enable, disable or mask something.

`Unit(..., needs=[disk.ready])` adds `After=runtime-init-disks.service` and `Requires=runtime-init-disks.service` to the shipped text's `[Unit]` section, one unit per needed step. `needs=[RUNTIME_INIT]` adds `runtime-init.service`, and so does any `needs` under `nethermind-v1`. A `needs` naming a step the variant does not declare is `needs-undefined`, an error. A `Unit` with `needs` in a variant with no runtime-init step gets a `needs-without-init` warning from `lint`. Apart from that, tundravm never edits `Unit` text. Use `Service` when the fields cover what you need and `Unit` when you want the exact bytes (the shipped `Tdxs()` and `DevTools()` fragments use `Unit`).

`Template(path, template, variables={})` is a file rendered with `str.format_map(variables)` at lowering time, for config files that differ only in a few values.

## Keys, disks and secrets

These three declarations reference each other by object, so a reference cannot dangle silently:

```python
key = Key("key_persistent")                                    # random 64 bytes, sealed in the TPM
disk = Disk("disk_persistent", mount="/persistent", key=key)   # LUKS2; formatted when the device is not LUKS yet
secrets = Secrets(store=disk, entries=(
    Secret("api_token", targets=(SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN"))),
), delivery=ExternalDelivery("vault-agent", unit="vault-agent.service"))  # what writes the targets at boot
```

- `Key(name, output=None, strategy="random"|"pipe", persist_in_tpm=True, size=64, pipe=None)`.
- `Disk(name, mount, device=None, key=None, mapper=None, format="on_fail", directories=("ssh", "data", "logs"))`. `key=None` is a plain disk; `key` may also be a `Path` to a key file. See [Disk selection and formatting](#disk-selection-and-formatting) for what `device=None` and `format` do at boot.
- `Secrets(name="secrets", entries=(), store=None, host="0.0.0.0", port=8080, ssh_directory="/root/.ssh", ssh_key_path="/etc/root_key", delivery=None)` declares the secrets a variant expects, where each goes and who writes them (`delivery`: `None` for the built-in `tundra-tools` provider, or an `ExternalDelivery`; see [Secret delivery at boot](#secret-delivery-at-boot)). Lowering writes the `secret-delivery` config (`/etc/tdx/secrets.yaml`: listen address, SSH directory, key path, store disk), the secrets manifest (`/etc/tdx/secrets.json`: each `Secret` with `required`, its `Schema` and its targets) and the `secrets` runtime-init step, `secret-delivery setup /etc/tdx/secrets.yaml`. What the binary does with them today is narrower than the declaration: see [Secret delivery at boot](#secret-delivery-at-boot). `SecretEnv(name)` targets the global environment; `SecretEnv(name, service="app")` targets one service: its values go to `/run/secrets/app.env`, and a generated drop-in, `/usr/lib/systemd/system/app.service.d/tundravm-secrets.conf`, makes the unit read that file with `EnvironmentFile=` (a hard requirement when the secret is required, `-` optional otherwise). The secrets manifest (`/etc/tdx/secrets.json`) records `service` and `env_file` on that env target. A service the variant neither generates (`Service`), ships (`Unit` with content) nor enables, disables or masks is the `secret-env-service-unknown` lint error.

`Disk.key` and `Secrets.store` hold refs (a `Key` or `Disk` passed in is stored as its ref): the variant must declare a key and a disk with those identities, whatever their values (`disk-key-undefined` and `secret-store-undefined` name the ref and the variant), and lowering uses the variant's final values.

### Disk selection and formatting

The `disks` step runs `disk-setup setup /etc/tdx/disk-setup.yaml` (from `tundra-tools`), which first finds each disk's device:

- `device=None` writes `strategy: "largest"`: the largest whole SCSI disk (`/dev/sd*`, partitions excluded) that `/proc/partitions` lists. It neither looks at partition tables nor skips the boot disk, so where the boot disk is the largest `sd*` disk, that is the one it picks; where the VM has no `sd*` disk (virtio disks are `/dev/vd*`, and `deploy qemu` attaches none) it finds nothing and the step fails.
- `device="/dev/..."` writes `strategy: "pathglob"`: the first block device matching that path (a glob pattern) under `/dev/`, skipping `/dev/sda`.

Then `format` decides whether the device is formatted before it is opened and mounted at `mount`:

| `format` | Encrypted disk (`key=` set) | Plain disk |
|---|---|---|
| `"on_fail"` (default) | formatted when the device is not a LUKS device yet; a LUKS device the key cannot open fails the step, it is not reformatted | formatted when mounting it fails, whatever the reason |
| `"on_initialize"` | formatted until `disk-setup`'s init token is on it (a LUKS device without the token included) | as `on_fail` |
| `"always"` | formatted on every boot: nothing on it survives a reboot | formatted on every boot |
| `"never"` | never formatted: a device that is not LUKS fails the step | never formatted: a device without a filesystem fails to mount |

After formatting, `disk-setup` creates `directories` under the mount point. A failing step fails its unit (`set -euo pipefail`), so every step after it, `runtime-init.service` and every unit that needs it (`Service(needs=[disk.ready])`, the default `needs=[RUNTIME_INIT]`) stay down. The defaults suit a throwaway VM; a production recipe names the device it means (`device="/dev/disk/by-id/..."` or the platform's data-disk path) and picks `format` for the data it must keep. Because the defaults can format the boot disk, lint reports every `Disk(device=None)` whose `format` is not `"never"` as `disk-auto-format`, a warning that `Policy(storage_safety="error")` makes an error (`storage_safety="warn"` is the default).

### Secret delivery at boot

The `secrets` step runs the `secret-delivery` binary (`tundra-tools` `cmd/secret-delivery`, `pkg/secrets`). Today it delivers one thing, an SSH public key, over this contract:

- **Readiness.** It listens on the config's `host:port` (`0.0.0.0:8080` unless `Secrets(host=, port=)` say otherwise) once the `keys` and `disks` steps have finished. There is no status endpoint: an open port is the signal that the VM waits for its key.
- **Request.** `POST` to any path; the body is the 68-character base64 field of an ed25519 public key (the `AAAAC3Nza...` part of `ssh-ed25519 AAAAC3Nza... comment`, without the type or comment), read up to 256 bytes with surrounding whitespace trimmed:

  ```console
  $ curl -X POST --data-binary "$(cut -d' ' -f2 ~/.ssh/id_ed25519.pub)" http://VM:8080/
  key accepted
  ```

- **Responses.** `200 key accepted`; `400 invalid key: must be a base64-encoded ed25519 public key (68 characters)` for any other body; `405 method not allowed` for any other method. After a `400` or `405` it keeps waiting.
- **Delivery.** On the first valid key it stops listening and writes `ssh_directory/authorized_keys` (directory `0700`, file `0600`, replacing its contents) with the one line `no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 KEY`, and the bare key to `ssh_key_path` (`0600`) when that is set. The step exits 0 and runtime-init carries on. The restriction makes sshd refuse every forward with that key, `ssh -L` to a unix socket included, but not commands: the host reaches the image's tdxs socket through a command-channel relay as `root` (`socat UNIX-LISTEN:./tdxs.sock,fork EXEC:'ssh -p 2222 root@localhost socat - UNIX-CONNECT\:/var/tdxs.sock'`, with `socat` in the image), or through a second account the recipe declares with its own `authorized_keys` file (see [CLI: Real TDX](cli.md#real-tdx)). Since the file is replaced, a key shipped in `ssh_directory` does not survive delivery.
- **Waiting and failure.** Until a valid key arrives the step blocks with no timeout of its own (`runtime-init.service` is a oneshot unit, which systemd starts without a timeout by default), and every unit ordered after `runtime-init.service` waits with it. If it cannot listen (the port is taken) it exits 1, runtime-init fails and those units do not start.

What is delivered, and by whom: each `Secrets` names a provider for its targets with `delivery=`. Without it the provider is the built-in `tundra-tools` one, whose capabilities are listed once, in `BUILTIN_SECRET_CAPABILITIES` (`tundravm._modules.secret_delivery`):

| Target | Built-in `tundra-tools` | `ExternalDelivery(name, supports=...)` |
|---|---|---|
| SSH key (`ssh_directory/authorized_keys`, `ssh_key_path`) | written, as above | still written by `tundra-tools` |
| `SecretFile(path)` | not written | written by `name` when `"file"` is in `supports` |
| `SecretEnv(name, service=)` | not written | written by `name` when `"env"` is in `supports`, to `/run/secrets/<service>.env` for a service target |

A target its provider does not write is the `secret-target-unsupported` lint error. That lint checks the declared capabilities, not that a provider exists in the image or delivers successfully. The two honest fixes: declare `ExternalDelivery("NAME", supports=(...))` for what really writes it (a cloud secret store agent, a unit of your own), or drop the target and reach the VM through the SSH key. You provide the provider itself: ship or package it.

Say how the image can tell it is done, and make consumers wait with `needs=[delivery.ready]`. `ExternalDelivery(name, unit="vault-agent.service")` adds the runtime-init step `delivery-<name>`, which runs `systemctl start vault-agent.service` and so finishes when the unit's start job does (a `Type=oneshot` unit when it exits, `Type=notify` when it reports ready; the unit must not order itself after runtime-init). `ExternalDelivery(name, ready_path="/run/secrets/.ready")` makes that step poll until the path exists (with no timeout of its own). `secrets.ready` still means the built-in SSH-key step, not the external provider. An `ExternalDelivery` with neither is the `external-delivery-unwired` warning: nothing waits for it, and services start without their secrets. Without `unit=`/`ready_path=` an `ExternalDelivery` is only a declaration of responsibility: the image, the tree and the recipe digest are the same with or without it. The evidence index records each variant's targets under `secrets` (kind, location, provider, `supported`), so a reviewer sees what the image expects at boot.

What the binary does not do yet:

- It never reads the manifest, so `Schema`s are not checked and `SecretFile` and `SecretEnv` targets, global or per service, are not written. The image carries the manifest, the `/run/secrets/<service>.env` paths and the `EnvironmentFile=` drop-ins; a drop-in for a required secret (no `-` prefix) makes its service fail to start while that file is missing; order it after the provider with `needs=[delivery.ready]`, since systemd does not retry on its own.
- `store` is written to the config as `store_at`, but `secret-delivery setup` runs without a disk store, so the key is not kept on the disk: every boot waits for a key again.

### Secrets never belong in the image

An image is copied to every host, registry and bucket that holds it, and its measurements are public, so a credential baked into it is a credential published. Two lint errors catch the common cases. `secret-in-file` scans every file a `File`, `Template` or `FileTree` ships for a PEM private key (header and body), an AWS access key id, a GitHub token, a `token`/`secret`/`password`/`api_key = value` assignment with a token-like value in a config-like file (under `/etc`, or a `.conf`, `.env`, `.yaml`, `.json`, ... file), and a literal value in a `secrets.yaml`. `secret-in-env` scans every unit file the image ships, a verbatim `Unit`'s and the one a `Service` renders alike (`Environment=` lines, or a key or token anywhere in the unit, `exec_start` and `exec_start_pre` included), for the same keys and tokens and for a token-like literal under a `*TOKEN`, `*SECRET`, `*PASSWORD`, `*API_KEY` or `*ACCESS_KEY` variable. The guards keep them quiet on what is not a credential: public keys, certificates and a PEM header without a key body do not match; documentation values (`EXAMPLE`, `changeme`, `xxxx`, a string of few distinct characters), paths, `$VAR` and `{{ template }}` references and commented-out assignments are skipped; a SOPS-encrypted `secrets.yaml` is left alone. A finding names the kind of credential and its line (for a `Service`, the field it is in), never the value. For a value that only looks like a credential, `allow_secret=True` on the `File`, `Template`, `FileTree`, `Unit` or `Service` exempts that declaration; it changes neither the recipe digest nor the tree. A real credential goes in `Secrets` instead, delivered at boot to a `SecretFile` or a `SecretEnv` target by an `ExternalDelivery` that writes it, so the image carries only where it goes (see [Secret delivery at boot](#secret-delivery-at-boot): the built-in delivery writes only the SSH key). The hint names the target kinds the variant's `ExternalDelivery` writes, or `ExternalDelivery` itself when the variant declares none.

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

The source is `Git(url, ref, subdir=None, submodules=False)` or `Http(url, sha256=None)`. `script` runs in a copy of the source checkout with `env` exported; `install` copies results into the image (`Install(source, destination, mode=0o755, directory=False)`). `packages` names build-time packages. With `using=Go(...)`, `Cargo(...)` or `Dotnet(...)` in place of `script` the toolchain renders the build command. For one Go or Rust program, `GoBinary(name, source, package=...)` and `CargoBinary(name, source, bin=...)` (in `tundravm.declarative.utils`) are that `Build` with its `Install` at `/usr/bin/<name>` (or `install=`), and their `executable` is the installed path, for `Service(name, f"{binary.executable} --flag")`. Built outputs are cached in the build directory under `<namespace>-<fingerprint16>`: `cache_namespace` (default: the build's name) is the namespace, and the fingerprint hashes the source pin, the build script or toolchain, the install steps, the architecture and the toolchain, so changing any of them rebuilds. `nethermind-v1` keeps its historical keys.

The recipe records the symbolic source (`master`), never a commit. `tundravm lock` resolves each `Git` ref to a commit and each `Http` without `sha256` to a hash and stores the pins in the lockfile; compile and bake then build exactly those. A build without a pin is the `source-unpinned` warning.

### Fetched on the host

In the current dialect the build sandbox never fetches a source. `tundravm fetch` (and `bake`, which runs it first) checks each pinned source out on the host, as the invoking user, into `build/.sources/<name>-<pin12>-<id8>/` (`id8` hashes the url, subdirectory and submodules): a git source at its pinned commit, an http source verified against its sha256. A JSON marker records each completed checkout, and a checkout is verified before reuse (git `HEAD` at the pin with a clean tree and initialised submodules, http files matching the recorded manifest); a modified one fails with `E_SOURCE` until `tundravm fetch --force` replaces it. A build whose source differs between variants is pinned per variant, as `<variant>/<name>`. Your git credentials and SSH agent apply, so private repositories work, and one checkout serves every variant that builds that (source, pin). The backend mounts `build/.sources` into the build at `$SRCDIR/tundravm-sources` (ephemerally: nothing a script writes there reaches the host), and each build hook copies its checkout into `$BUILDROOT/build/<name>` before running the script. A hook whose source has no pin fails with a pointer to `tundravm lock` and `tundravm fetch`.

A built kernel (a `Kernel` with `config`) is a source too: its lockfile pin is named `kernel`, or `kernel-<variant>` when a variant's kernel source differs from the default variant's; `inspect` shows `pinned=`; `source-unpinned` covers it; and the kernel build script copies the checkout without `.git`, so the kernel version string stays clean. `nethermind-v1` keeps cloning sources and kernels inside the sandbox, as the historical tree does. See [reproducibility](reproducibility.md#sources-fetched-on-the-host).

### Hermetic builds

A checkout on the host still leaves a `Go`, `Cargo` or `Dotnet` build downloading its dependencies inside the sandbox, so `fetch` also prefetches them with the host's own toolchain, run as you in a scratch copy of the checkout (the checkout stays clean): `go mod download` into `BUILD/.sources/deps/go` (a `GOMODCACHE`), `cargo fetch --locked` into `BUILD/.sources/deps/cargo` (a `CARGO_HOME`) and `dotnet restore --runtime RID --packages` into `BUILD/.sources/deps/nuget` (a NuGet global packages folder). A marker, `deps/<name>-<pin12>-<id8>.<cache>.json`, records each prefetch with the command and environment it ran, so the next fetch keeps it (`FetchedSource.deps` goes from `cargo: prefetched` to `cargo: kept`). A host without the toolchain on `PATH` skips the prefetch with a notice (`skipped (no host go)`) and a failing one is a warning (`failed (<reason>)`); either way the fetch succeeds and that build downloads its dependencies in the sandbox, as before. Script builds and kernels have no dependency cache.

In the current dialect each language build's hook copies the mounted cache, when there is one, to `$BUILDROOT/build/.tundravm-deps/<cache>` (the mount stays read-only) and, inside mkosi-chroot, points the toolchain at that copy: `GOMODCACHE` and `GOFLAGS=-mod=mod` for Go, `CARGO_HOME` for Cargo, `NUGET_PACKAGES` for .NET. When mkosi runs the script without network (`$WITH_NETWORK` is `0`) it also sets `GOPROXY=off` or `CARGO_NET_OFFLINE=true`, or, for .NET, `RestoreSources` at the copied packages folder (an explicit `--source` in `restore_args` or a project `RestoreSources` overrides it).

`bake --offline`, or `Policy(network_mode="offline")` in the recipe, makes the host the only place anything is downloaded: mkosi gets `--with-network=no`, so build and postinst scripts reach no network (mkosi itself still installs the distribution packages from the configured mirror). The fetch step reuses complete checkouts and downloads nothing (a missing checkout fails it with `E_POLICY`; prefetches show `deps: skipped (offline)`), and the bake fails with `E_STATE` before mkosi runs when a Go, Cargo or .NET build's prefetched dependencies are missing (or, with `--no-fetch`, a checkout is), naming the `tundravm fetch` to run on a host with that toolchain. A `nethermind-v1` recipe with source builds is refused offline with `E_POLICY`, because its hooks clone inside the sandbox.

`EfiStub` takes the same path. Outside `nethermind-v1` its package (`systemd-boot-efi_<version>_amd64.deb` in the snapshot's pool) is an http source named `efi-stub` (`efi-stub-<variant>` where a variant's package differs from the default variant's): `lock` pins its sha256, `fetch` downloads it on the host, and the postinst hook checks the mounted copy against the pin and installs it with `dpkg -i`. Compiled without a pin, the hook downloads the package in the sandbox as before; `nethermind-v1` keeps its `curl`.

The caches are filled by the host's toolchain and read by the image's, so they have to agree:

- **Cargo.** Cargo 1.85 changed the names of the registry cache's directories, so the host's and the image's cargo must both be 1.85 or newer; otherwise the build does not find the cache and, offline, fails.
- **.NET.** The host's .NET SDK must match the image's: the restore picks package versions (runtime packs included) for the SDK that ran it, and the image's `dotnet` looks for its own.
- **Go.** A `go.mod` whose `go` or `toolchain` line is newer than the image's Go makes it download that toolchain, which an offline bake cannot do; the image's Go has to satisfy `go.mod`.

See [reproducibility](reproducibility.md#hermetic-builds).

## Variants and targets

A variant produces one mkosi directory and, when baked, one artifact. Its `outputs` (`qemu`, `azure` or `gcp`, inherited from the variant it inherits, `qemu` at the root) decides the disk format and adds the platform integration: an `azure` variant gets the Azure provisioning service and `dmidecode` and converts its disk to a fixed VHD (`parted`, `qemu-img`) in a postoutput script; a `gcp` variant gets the GCP equivalents and a `tar.gz` disk (`sgdisk`). Those scripts run wherever mkosi runs its tools: on the host, or inside mkosi's tools tree when the build uses one, in which case the local backend installs `qemu-utils`, `gdisk` and `parted` into it.

```python
variants=(
    Variant("default", outputs=("qemu",)),
    Variant("azure", inherits="default", outputs=("azure",)),
    Variant("gcp", inherits="default", outputs=("gcp",)),
    Variant("devtools", inherits="default", add=DevTools()),
)
```

`outputs=("azure", "gcp")` gives one variant several outputs (one disk image each); an empty `outputs` inherits them (`qemu` at the root). A child cannot drop a cloud output it inherits (`target-inconsistent`): give it `inherits=None` or have it inherit a qemu variant instead.

How a variant lowers depends on what it does:

- A variant that inherits `base` or the default variant, and which only adds declarations or edits `File`, `User`, `Group`, `Partition`, `Repository` or `Debloat`, lowers as an overlay of the default variant's directory. This keeps committed trees such as surge's byte-identical.
- Every other variant (inheriting another variant, standalone, removing anything, replacing other kinds, changing keys, disks or secrets, mounting its own `BuildSources`, having its own settings or kernel, or leaving a cloud-targeted default variant's targets) is lowered standalone from its own resolved declarations, with its own `mkosi.conf` lines and kernel.

The default variant (the one named `default`, else the first root variant) may itself inherit another variant; that variant then lowers like any other.

These still raise `ValidationError` when you compile, lint or bake:

- a standalone-lowered variant under `Mkosi(layout="native")`, because mkosi applies the root configuration to every profile; the error lists each such variant with its reason;
- in the current dialect, a variant under `Mkosi(layout="native")` whose build distribution (repositories, build packages, Build settings) differs from the default variant's while the root builds a kernel or source builds, because an overlay cannot suppress the root hook; match the default's distribution or use the per-directory layout;
- unreadable files, `Unit` or `Template` sources that are not UTF-8, and missing or empty `FileTree` sources;
- a `Template` placeholder without a value, and a malformed `Setting` value (a non-boolean `Build.WithNetwork`, several values for a single-valued key);
- a `Setting` for a key the compiler writes itself (`Packages`, `Mirror`, `Format`, ...), naming the declaration to use instead;
- a `Kernel` from `Http` without `sha256`;
- two `Secrets` whose config, manifest or delivered file paths overlap;
- variant `inherits` cycles and `BootStep`/`Hook` ordering cycles.

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
                                     ├──measure──▶ Measurements ──export──▶ peer.json (policy)
                                     ├──sbom─────▶ Sbom
                                     └──deploy───▶ Deployment ──attest(peer.json)──▶ Attestation
```

| Step | Function | CLI | Needs |
|---|---|---|---|
| Inspect | `resolve`, `resolve_all`, `explain_why`, `explore` | `inspect`, `inspect --why`, `explore` | the recipe |
| Lint | `lint(recipe, lock=None)` | `lint` | the recipe; a lock applies its pins and adds drift |
| Compile | `compile(recipe, lock=None) -> Tree` | `compile` | the recipe; a lock applies its pins |
| Watch | `lint` and `compile --check` again on each change (`tundravm.watch.Watch`) | `watch` | the recipe; writes the tree only with `--write` |
| Lock | `lock(recipe, previous=None) -> Lock` | `lock` | the network, unless every source is already pinned |
| Fetch | `fetch(recipe, lock=, build=, force=False) -> tuple[FetchedSource, ...]` | `fetch` | the network (as you), unless the checkouts are already there; `go`, `cargo` or `dotnet` on `PATH` to prefetch dependencies |
| Bake | `bake(recipe, lock=, backend=, build=, fetch=True, offline=False) -> tuple[Artifact, ...]` | `bake` | a backend; fetches first unless `fetch=False` (`--no-fetch`); `offline=True` (`--offline`) needs every checkout and dependency cache fetched |
| Measure | `measure(artifact, scheme="rtmr") -> Measurements` | `measure` | `measured-boot` or `dstack-mr`, or `allow_placeholder` |
| Deploy | `deploy(artifact, using=Qemu()/Azure(...)/Gcp(...), allow_simulated=False) -> Deployment` | `deploy` | the target's tool (`qemu-system-x86_64`, `az`, `gcloud`) |
| Attest | `attest(issuer, policy, validator=None, nonce=None) -> Attestation` | `attest` | the running image's `tdxs` issuer (a unix socket, `unix:PATH`, or `tcp://HOST:PORT`), a `tdxs` validator for a `trusted` verdict, and a policy from `measure --export-policy`, not a placeholder |
| SBOM | `sbom(artifact, lock=None, manifest=None) -> Sbom` | `sbom` | a real bake (its mkosi package manifest); a lock for the source pins |
| Evidence | `evidence(recipe, build=, variants=None, lock=None, policy=None) -> Evidence` | `evidence` | a bake in `build` (its `bake-result.json`); the lockfile it recorded |

A lock of every variant covers a bake or `lock --check` of any subset of them: only the selected variants' sections and the recipe-wide ones are compared. `bake` fetches the locked sources into `build/.sources` before it builds, so `tundravm fetch` on its own is for checking sources out ahead of time (or for a host that bakes offline). `tundravm status RECIPE` checks each of these steps without writing anything or touching the network and names the next command to run.

`tundravm init` writes a `[tool.tundravm]` table (`recipe`, `tree`, `build`, `lockfile`, `backend`; an optional `require` list names the evidence checks `tundravm status` judges release readiness against and `tundravm evidence` and `tundravm release` require without `--require`; default `reproducible`, `lock`) into the project's `pyproject.toml`, and every command reads it, so in a scaffolded project `RECIPE` is optional everywhere and every step agrees on the paths: `tundravm status`, `tundravm bake` and `tundravm ci` need no arguments, and `measure`, `deploy` and `sbom` find the bake in the table's `build`. A flag overrides the table's value, and `tundravm config` prints the values the commands resolve, each with its origin (`flag`, `pyproject`, `default`, or `recipe` for the backend the recipe file binds), and marks a recipe or lockfile that does not exist (see [CLI: Project configuration](cli.md#project-configuration)).

`bake` writes `BUILD/bake-result.json`, the manifest `read_artifacts(build)`, `tundravm measure` and `tundravm deploy` read, so measuring and deploying work in a later process. Each `Artifact` carries the recipe digest, the lockfile digest and the tree digest it was built from. `measure` and `deploy` first hash the artifact (`verify_artifact`) and refuse one whose bytes changed since the bake with `ArtifactError` (`E_ARTIFACT_CHANGED`); `tundravm status --verify` reports the same check.

`deploy` hands the artifact to the target's adapter. On QEMU the VM detaches by default, with its serial console in `BUILD/<variant>/qemu-serial.log`, a monitor socket `qemu.monitor` and a pid file `qemu.pid` beside it (`deploy --attach` keeps it in the foreground on the terminal instead); every port the variant's `Secrets` listen on, which the bake records in `bake-result.json`, is forwarded from the same host port, besides `ssh_port` and `Qemu(forward=...)`; and `tdx=True` boots TDVF through `-bios` with a `tdx-guest` object and a split irqchip. On Azure the VHD is published as an image version in a Compute Gallery (`Azure(gallery=)`, default `tdx_images`) whose definition supports confidential VMs, and the VM is a TDX confidential VM (`Standard_DC2es_v5` by default) with Secure Boot off: Azure's Secure Boot firmware boots only signed images and tundravm does not sign the UKI, so `Azure(secure_boot=True)` also needs `signed=True`, your statement that the UKI was signed outside tundravm with keys Azure trusts. On GCP the image is created `TDX_CAPABLE` and the instance requests `--confidential-compute-type=TDX` on the C3 series (`c3-standard-4` by default). See [CLI: Deploying to each target](cli.md#deploying-to-each-target).

The in-process backend writes simulated artifacts (`Artifact.simulated`). `measure` refuses them unless you pass `allow_placeholder=True` (`--allow-placeholder`), and `deploy` unless you pass `allow_simulated=True` (`--allow-simulated-artifact`); placeholder measurements say so (`tool="placeholder"`).

### Day-to-day loop

1. Edit the recipe, or a module beside it, with `tundravm watch` running in another terminal.
2. Each save prints one line, `lint N errors N warnings; tree up to date`, `tree stale (N files)` or `tree missing (N files to write)`, or the error that stopped the recipe from loading.
3. Once the line says `tree stale`, `tundravm compile --tree mkosi` rewrites the committed tree (`watch --write` does that on every change).
4. `uv run pytest tests` runs the generated tests, which lint the recipe strictly and compare `mkosi/` with what it compiles to.
5. `tundravm lock` records the changed sections, and `tundravm ci` runs `lint --strict`, `compile --check` and `lock --check`, as the `check` job of the `init --ci github` workflow does on every push to `main` and pull request.
6. The workflow's `bake` job is opt-in: once the repository variable `TUNDRAVM_BAKE_BACKEND` names a backend, it bakes twice with it after `check` passes (`bake --verify-reproducible`).
7. It then runs `evidence --bundle --html` and uploads the bundle, its HTML report and the built images as the `tundravm-bake` artifact, the record a reviewer checks without your machine.

### Supply chain

`sbom(artifact, lock=locked)` answers "what is in this image" from what the build already pins and records, without opening the image: mkosi's package manifest beside the artifact (every installed distribution package and version, from the snapshot the recipe names), the lockfile's pins (each source build's URL, ref and commit and the paths it installs, the built kernel's source, the `EfiStub` package's URL and sha256) and the recipe metadata (base, arch, snapshot, mirror, the recipe, tree and artifact digests, the tundravm version). `Sbom.render()` writes SPDX 2.3 or CycloneDX 1.5 JSON with a package URL per component, or text and Markdown tables (`tundravm sbom`; see [CLI: SBOM](cli.md#sbom)). Everything in it is sorted and its ids derive from its content, so with a snapshot, a lockfile and `SOURCE_DATE_EPOCH` the same bake gives the same document, one more artifact to publish next to the measurements. A simulated bake has no manifest: the document says so and lists the packages the recipe declares, unversioned.

`evidence(recipe, build=BUILD)` (`tundravm evidence`) puts what an auditor asks about one bake into a single record, without rebuilding anything: the recipe digest now and the one the bake recorded, the lockfile verbatim with its drift against the recipe, every artifact re-hashed against the sha256 in `bake-result.json`, the tree digest, the reproducibility outcome `bake --verify-reproducible` recorded, the measurements policy (`BUILD/<variant>/policy.json` or `policy=`, marked when it is a placeholder or exported from another artifact), an SPDX SBOM per artifact, the lint summary, a provenance summary per variant and the tool versions. `evidence.json` indexes these files with each one's sha256 and size. The verdict is `fail` when the record contradicts itself or the recipe: an artifact no longer hashes to its recorded sha256 or is gone, the recipe, tree or lockfile drifted since the bake, lint has errors, the second build differed, or the policy is invalid, a placeholder or measured another artifact. It is `incomplete` when a check `require=` (`evidence --require reproducible,measured,real,lock`) demands is missing, and otherwise `pass` (a bake that never ran the reproducibility check passes as `not checked` unless `reproducible` is required); `Evidence.reasons` says why, and the command exits 1 unless the verdict is `pass`. What could not be gathered and was not required (no lockfile, no policy, no mkosi manifest) is a note in the index, never a silent gap. `Evidence.write()` lays the files out as a directory, `Evidence.bundle()` packs them into a `tar.gz` and `Evidence.html()` renders one self-contained page with a pass/fail banner. The bundle is deterministic: members sorted, owned by `0:0` with mode `0644`, and every timestamp, the index's `created` included, taken from `SOURCE_DATE_EPOCH` (without it the archive's are 0 and `created` is the current time), so with `SOURCE_DATE_EPOCH` set two runs over the same bake give the same bytes and a reviewer can check the bundle by its sha256 (see [CLI: Evidence](cli.md#evidence)).

## Backends

| Backend | `Backend(kind)` | Runs mkosi |
|---|---|---|
| `LimaMkosiBackend` | `lima` | inside a Lima VM with Nix (macOS and Linux) |
| `NixMkosiBackend` | `nix` | through `nix develop` on a Linux host |
| `LocalLinuxBackend` | `local` | from `PATH` on Linux (mkosi v25+, `sudo` or `unshare`) |
| `InProcessBackend` | `inprocess` | not at all: writes simulated artifacts for tests |

A recipe file names its backend in a module-level `backend` variable; the Python `bake()` takes a `Backend` value. Nothing before `bake` needs a backend.

`bake` compiles the tree into `BUILD/mkosi/` and every mkosi backend gives mkosi absolute paths: `--directory` is the variant's directory of that tree (or the tree root plus `--profile` for native profiles) and `--output-dir` is `BUILD/<variant>/output/`, so artifacts land there whatever the working directory.

- `LocalLinuxBackend` keeps mkosi's workspace, cache and tools tree in `BUILD/.mkosi/`. After a `sudo` run it chowns what mkosi wrote back to the invoking user, warning with the `sudo chown` to run if that fails. When the recipe sets no `ToolsTree` and the host lacks a tool the build needs (`ukify` for a UKI; `pefile`, under the Python that runs mkosi, for any build that is not `Bootable=no`), it adds `--tools-tree=default` to the mkosi command, and a later bake into the same `BUILD` (also one whose recipe sets `ToolsTree=default`) reuses `BUILD/.mkosi/mkosi.tools`. An `azure` or `gcp` variant built in the default tools tree gets `--tools-tree-package=qemu-utils,gdisk,parted` and reuses a cached tree only when it holds those tools; only a build on host tools needs `qemu-img` or `sgdisk` on the host, which `bake` checks before mkosi runs. See [CLI: Local backend](cli.md#local-backend) for the measured template bakes.
- `NixMkosiBackend` runs mkosi inside `nix develop path:BUILD/mkosi` (an absolute path), with the tools the generated flake provides; inside a Nix shell already, it runs mkosi directly.
- `LimaMkosiBackend` mounts `BUILD` in the VM at `/home/debian/mnt` and translates each host path under it to the VM path, so `BUILD/mkosi/<variant>` is `/home/debian/mnt/mkosi/<variant>`; a tree or output path outside `BUILD` is an `E_BACKEND_EXECUTION` error. mkosi writes to `/home/debian/mkosi-output` in the VM, with its cache in `/home/debian/mkosi-cache`, and the backend moves the output to `BUILD/<variant>/output/`.

A backend gets one `BakeRequest` per variant (`profile`, `build_dir`, `emit_dir`, `output_targets`, and `sources_dir`, the host checkouts `BUILD/.sources` that it mounts at `$SRCDIR/tundravm-sources`, or `None` when the build fetches nothing on the host). It streams mkosi's output lines to `on_output(line)` and reports what it decided on the user's behalf, such as adding a tools tree, to `on_notice(level, message)` with `level` `"info"` or `"warning"`; `request.notice(level, message)` calls it when it is set. `bake` prints an `info` notice as a `note` line and a `warning` as a `warning` line, both hidden by `-q`; with `--json-logs` they are a `log` event whose `extra.source` is `notice` and a `warning` event.

## Measurements

`measure(artifact, scheme=)` derives the values a verifier should expect:

- `rtmr` uses `measured-boot` or `dstack-mr` on `PATH` and returns RTMR values; `Measurements.tool` names the tool and version.
- `azure` and `gcp` have no local tool yet; they return placeholders only with `allow_placeholder=True`.

`Measurements.to_dict()` gives the values (`artifact`, `artifact_digest`, `scheme`, `tool`, `values`), and `Measurements.verify(expected)` returns the registers that differ from an expected set (empty when all match). `Measurements.to_policy()` is the verifier policy that `tundravm measure --export-policy FILE` writes (it refuses placeholder values unless `allow_placeholder=True`), which `Tdxs.from_policy(FILE)` (or `Tdxs.from_measurements(measurements)` in-process) turns into a validator's `expected_measurements`; RTMR tools do not report MRTD, so pass `mrtd=` to have it checked too (see [API: Shipped fragments](api.md#shipped-fragments)).

Placeholder values are derived from the artifact digest. They are never real measurements and must never go into an attestation policy.

### Attesting a running image

A policy from `measure --export-policy` can be checked two ways. `tundravm attest --issuer unix:./tdxs.sock --validator ENDPOINT --policy peer.json` (or `attest()`) asks the running image's `tdxs` issuer for a quote bound to a fresh nonce, has a `tdxs` validator check the quote's signature, certificate chain and collateral, reads MRTD and RTMR0..RTMR3 from the quote and reports `match`, `mismatch` or `unchecked` per register (see [CLI: Attest](cli.md#attest)). `trusted` now means three things held at once: the validator accepted the attestation document, the quote's whole 64-byte `report_data` binds the nonce, and every register the policy holds matched; the platform is not the simulator. A validator rejection, or a mismatch with a validator, is `untrusted`. Without a validator nothing vouches for the quote's origin, so the verdict is only `measurements-match` or `measurements-mismatch` (`signature_status: unchecked`), a quick operator check rather than trust; a simulator issuer is `simulated` at best. `tdxs` speaks JSON lines on the unix socket `/var/tdxs.sock` in the image, so the issuer is that socket relayed to the host (over an SSH command channel, since the delivered key forbids `ssh -L`; see [Secret delivery at boot](#secret-delivery-at-boot)), then `unix:./tdxs.sock`, or a TCP port relayed to it (`tcp://HOST:PORT`); it serves no http, and an `http://` or `https://` endpoint is a `ValidationError`. A `Tdxs` validator built with `Tdxs.from_policy("peer.json")` verifies the collateral as well and is what a relying service should run. `bake --verify-reproducible` is the build-side counterpart: it bakes twice and proves the artifacts are identical.

### Reviewing a release

Evidence is what a release leaves behind for review. `tundravm evidence --sign KEY --bundle vN.tar.gz` packs the bake's record: digests, the lockfile, SBOMs, the measurements policy, and each variant's emitted-file manifest in `tree.json`. It signs the index, and the index holds every member's sha256, so the one signature covers the whole bundle. Anyone with the bundle and an allowed-signers file can then run `tundravm verify vN.tar.gz --allowed-signers FILE --build DIR` without the recipe or the repository. It confirms nothing was swapped or edited, that the bundle agrees with itself, and that the artifacts they hold are the ones it describes. For the next release, `tundravm changes --since vN.tar.gz --format markdown` compares that record with the recipe and lockfile as they are now. It lists which lockfile sections, pins, packages, emitted files and registers moved, and which declarations produced each changed file, as a table ready for the pull request.

### Cutting a release

`tundravm release --version X` turns a locked recipe into one directory a reviewer can trust: `releases/<recipe>-<X>/`. It checks the lockfile without ever updating it. It bakes twice and compares the artifacts, measures them and writes the SBOMs. It gathers and optionally signs the evidence, then verifies the bundle it just wrote. Finally it lists what changed since the previous release and writes all of it into `RELEASE.md`. Each stage records a hash of its inputs, so a run that failed or was interrupted resumes where its inputs last changed, and a long bake isn't repeated for a typo in `--require`. The release also records what it was started with (variants, requirements, signing key, backend settings): a resumed run inherits all of it, and loosening any of it takes an explicit `--change-spec` that the release records. Variants marked `release=False`, like the scaffolds' `dev`, are left out unless named. Once a release passes it is final: cutting the same version again is refused, so a version number always names one set of bytes, and `release-manifest.json` (signed with the release's key) lists every file in it, so `tundravm verify RELEASE_DIR` can check the directory anywhere. With the in-process backend the release is marked simulated, and `--require real` fails it.

### Independent rebuilds

A release is its publisher's claim about which bytes a recipe and a lockfile build. `tundravm witness RELEASE_DIR` lets someone else check that claim: from their own checkout, with the release's own lockfile, they rebuild the released variants and sign whether every artifact's sha256 matches. `tundravm verify RELEASE_DIR --min-witnesses 2` then accepts the release only when two distinct principals, neither of them the publisher, signed matching witnesses of this very manifest. A witness proves reproducibility across builders, not that the recipe or its sources are safe, so pick witnesses whose build environments really differ.

### Adopting an existing mkosi tree

`tundravm import mkosi --output image.py` turns a tree you already have into a recipe. Configuration keys, packages, files and units become declarations, and what tundravm generated itself (accounts, enablement, debloat, runtime-init, cloud glue, backports, the EFI stub) becomes the declaration that generates it again. What has no first-class declaration stays verbatim as `File`, `Unit`, `Hook` or `Setting`, and the report counts both. The last note says whether the recipe compiles back to the tree. Commit the generated module, check that `tundravm diff image.py --tree mkosi` is empty, then refactor it into fragments at your own pace, re-running `diff` after each step.
