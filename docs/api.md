# API reference

The public API is the declarative one in `tundravm.declarative`. Most names are also exported from `tundravm`; the exceptions are listed under [Imports](#imports). Every record below is a frozen, slotted dataclass: lists passed for tuple fields are frozen into tuples, and `__post_init__` raises `ValidationError` for malformed values.

For the model behind these types see [concepts](concepts.md); for writing reusable fragments see [writing fragments](module-authoring.md).

## Imports

```python
from tundravm import Recipe, Fragment, Variant, Package, File, Service, compile, lint, lock, bake
from tundravm.declarative.utils import Composite, DevTools, Tdxs
from tundravm.declarative import diff, measure, deploy
```

`tundravm` exports the declarations, the build recipes `Go`, `Cargo` and `Dotnet`, the recipe types, the lifecycle functions and result types, `attest` and `Attestation` (from `tundravm.attestation`), `Policy`, the [errors](#errors), `load` and `load_recipe`. Only in `tundravm.declarative`: `diff`, `measure` and `deploy` (at the top level those names are the `tundravm.diff`, `tundravm.measure` and `tundravm.deploy` subpackages), `identity`, and the type aliases `Check`, `Pairs`, `Phase` and `Target`. The shipped fragments `Tdxs`, `DevTools`, `EfiStub` and `Backports`, and their base class `Composite`, live in `tundravm.declarative.utils`. `tundravm.__version__` is the package version that `tundravm --version` prints.

## Recipe, variants, fragments

### `Recipe`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required |
| `common` | `Fragment` | required |
| `variants` | `tuple[Variant, ...]` | `(Variant("default", target="qemu"),)` |
| `base` | `str` | `"debian/trixie"` |
| `arch` | `"x86_64" \| "aarch64"` | `"x86_64"` |
| `mirror` | `str \| None` | `None` (the distribution default); a mirror root written as `Mirror=`, which mkosi completes itself (`<mirror>/debian`, or `<mirror>/archive/debian/<snapshot>` with `snapshot`), so it is never a full archive URL |
| `tools_mirror` | `str \| None` | `None`; the tools tree's mirror root (`ToolsTreeMirror=`), completed the same way |
| `epoch` | `int \| None` | `0`: reproducible (`SOURCE_DATE_EPOCH=0`, stable seed, `IMAGE_VERSION` stripped); `None`: not reproducible; other values set `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` to that value |
| `mkosi` | `Mkosi` | `Mkosi()` |
| `policy` | `Policy \| None` | `None` (the default `Policy()`); see [policy](policy.md) |
| `snapshot` | `str \| None` | `None`; a snapshot ID such as `"20251113T083151Z"`, written as `Snapshot=` (with no `mirror`, mkosi reads `https://snapshot.debian.org`). A URL or a value with whitespace raises `ValidationError`: the root goes in `mirror` |

At least one variant; names are unique. `recipe.variant(name)` returns one or raises `ValidationError`.

### `Variant`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required; `"base"` is reserved |
| `parent` | `str \| None` | `"base"` (`Recipe.common`); `None` is standalone; else a variant name |
| `add` | `Fragment` | empty |
| `replace` | `tuple[Declaration, ...]` | `()` |
| `remove` | `tuple[Declaration, ...]` | `()` |
| `target` | `"qemu" \| "azure" \| "gcp" \| None` | `None`: inherited, `qemu` at the root |
| `targets` | `tuple[Target, ...]` | `()`: several outputs from one variant; `target=X` is the shorthand for `targets=(X,)` and setting both fails |

The default variant is the one named `default`, else the first whose parent is `base` or `None`. It may have any parent; a variant it is chained onto lowers like any other, standalone when it lacks what the default adds. A variant whose parent is `base` or the default variant, and which only adds declarations or replaces `File`, `User`, `Group`, `Partition`, `Repository` or `Debloat`, lowers as an overlay of the default variant. Every other variant lowers standalone, among them a variant with its own settings or kernel and a `base`-parented variant that leaves a cloud-targeted default's targets (see [variants](concepts.md#variants-and-targets)).

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
| `strip_os_release` | `bool \| None` | `None`: strip `IMAGE_VERSION` from `os-release` when `Recipe.epoch` is set |

`nethermind-v1` spells groups and users as postinst `groupadd`/`useradd` lines, omits the `# unpinned:` build marker, clones sources inside the build sandbox, leaves kernel sources out of the lockfile and generates backports with a sync hook, matching the historical nethermind-tdx tree.

## Declarations

`Declaration` is the union of the types `Fragment.items`, `Variant.replace` and `Variant.remove` hold: `Package`, `File`, `Template`, `Directory`, `Group`, `User`, `Unit`, `Service`, `Hook`, `Init`, `Repository`, `Partition`, `Debloat`, `Setting`, `Kernel`, `Key`, `Disk`, `Secrets`, `RuntimeTools` and `Build`.

### Packages, files, accounts, units

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Package` | `name`, `role="runtime"` (`"runtime"` or `"build"`) | name, role |
| `File` | `path` (absolute), `content: str \| bytes \| Path`, `mode=0o644`, `stage="extra"` (`"skeleton"` or `"extra"`), `allow_secret=False` | stage, path |
| `Template` | `path` (absolute), `template: str \| Path`, `variables: tuple[tuple[str, str \| int \| float], ...] = ()`, `mode=0o644`, `stage="extra"`, `allow_secret=False` | stage, path |
| `Directory` | `path` (absolute), `source: Path`, `exclude=()`, `mode=None` (keep each file's and empty directory's permission bits; a mode sets every file to it and empty directories to `0755`), `stage="extra"`, `symlinks="preserve"` (ship links as links; `"follow"` ships what they point to), `allow_secret=False` | stage, path |
| `Group` | `name`, `system=True`, `gid=None` | name |
| `User` | `name`, `system=True`, `home=None`, `shell="/usr/sbin/nologin"`, `uid=None`, `primary_group=None`, `groups=()` | name |
| `Unit` | `name`, `content: str \| Path \| None = None`, `enabled=None`, `masked=None`, `after_init=False`, `allow_secret=False` | unit name |
| `Service` | `name`, `exec_start: str \| tuple[str, ...]`, then keyword-only: `description=None`, `user=None`, `group=None`, `working_dir=None`, `env: Pairs = ()`, `env_file=None`, `exec_start_pre=()`, `after=()`, `requires=()`, `wants=()`, `wanted_by=None` (`minimal.target`), `type=None` (`"simple"`, `"exec"`, `"oneshot"`, `"notify"`, `"forking"`), `restart="no"` (`"always"`, `"on-failure"`, `"no"`), `limits=()` (`(resource, value)` pairs), `kill_mode=None`, `timeout_stop=None`, `security="default"` (`"strict"`, `"default"`, `"none"`), `after_init=True`, `allow_secret=False` | unit name |

`Template` renders `template` with `str.format_map(variables)` at lowering time; a placeholder without a value fails to lower. `Service` renders and enables a `.service` unit; `after_init=True` makes it wait for `runtime-init.service` when the variant has a runtime-init step. `allow_secret=True` exempts a declaration from the `secret-in-file` (`File`, `Template`, every file a `Directory` imports) or `secret-in-env` (`Service.env`, a `Unit`'s shipped file) lint, for a value that only looks like a credential; it changes neither the recipe digest nor the tree. `Unit` with `content` ships that text verbatim and needs a type suffix (`app.service`). Without `content` it controls a packaged unit and must set `enabled` or `masked`. `after_init=True` adds `After=`/`Requires=runtime-init.service`.

### Scripts and ordering

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Hook` | `name`, `phase: Phase`, `script`, `env: Pairs = ()`, `cwd=None`, `after=()` | name |
| `Init` | `name`, `script`, `priority=100`, `after=()` | name |

`Phase` is one of `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`. `Hook.after` names hooks of the same or an earlier phase. `Init` steps run in `/usr/bin/runtime-init` by ascending priority; `after` orders equal priorities and may name the built-in `keys` (10), `disks` (20) and `secrets` (30), which are reserved names. `Pairs` is `tuple[tuple[str, str], ...]`.

### System configuration

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Repository` | `name`, `url`, `suite`, `components=("main",)`, `keyring=None`, `priority=100`, `in_image=True`: the current dialect writes the source (and its keyring) under `mkosi.sandbox/`, where the build's apt reads it, and with `in_image=True` also into the image's `/etc/apt`; `False` keeps it out of the image | name |
| `Partition` | `name`, `size`, `mount`, `filesystem="ext4"` | name |
| `Debloat` | `enabled=True`, `remove=None` (compiler default list), `extra_remove=()`, `keep_paths=()`, `minimize_systemd=True`, `keep_units=None`, `keep_binaries=None`, `keep_units_extra=()` (kept on top of `keep_units` or its default), `keep_paths_by_variant=()` (`(variant, paths)` kept only in that variant) | one per variant |
| `Setting` | `section`, `key`, `values: tuple[str, ...]`; per variant | section, key |
| `Kernel` | `version`, `source: Git \| Http`, `config: Path \| None = None`, `cmdline=""`, `tdx=True` | one per variant |

No `Debloat` means the compiler default (debloat enabled). `Setting` is the mkosi escape hatch, and settings belong to the variant that declares them. `Output.Seed`, `Output.OutputDirectory`, `Output.CompressOutput`, `Output.ManifestFormat`, `Build.PackageCacheDirectory`, `Build.WithNetwork`, `Build.Environment`, `Build.SandboxTrees` and `Content.CleanPackageMetadata` map onto compiler options. Any other key is written verbatim into that variant's `mkosi.conf`, under its section, one `Key=value` line per value. Keys the compiler writes itself (`Packages`, `BuildPackages`, `Mirror`, `Format`, `ImageId`, `KernelCommandLine`, `ExtraTrees`, the phase script keys such as `BuildScripts`, ...) raise `ValidationError` naming the declaration to use instead (`Package(name)`, `Recipe.mirror`, `Hook(name, phase, script)`, ...). `Setting("Build", "BuildSources", ("src[:dest]", ...))` mounts host directories into the variant's build.

`Kernel.source` is a `Git` branch, tag or full commit hash, with `subdir` and `submodules`, or an `Http` tarball, which needs `sha256=` and is checked against it before unpacking. In the current dialect a built kernel's source (a `Kernel` with `config`) is locked and fetched like a source build: its lockfile pin is named `kernel`, or `kernel-<variant>` for a variant whose kernel source differs from the default variant's, `lock --update kernel` re-resolves it, and the kernel build script copies the host checkout without its `.git` (so the kernel version string stays clean). Under `nethermind-v1` the lockfile pins no kernel and the build clones it. A variant whose settings or kernel differ from the default variant's lowers standalone with its own `mkosi.conf` lines, kernel build and config.

## Keys, disks and secrets

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Key` | `name`, `output=None`, `strategy="random"` (`"random"` or `"pipe"`), `persist_in_tpm=True`, `size=64`, `pipe=None` (required with `"pipe"`) | name |
| `Disk` | `name`, `mount`, `device=None` (the largest whole `/dev/sd*` disk, see [Concepts: Disk selection and formatting](concepts.md#disk-selection-and-formatting)), `key: Key \| Path \| None = None`, `mapper=None` (needs a key), `format="on_fail"` (`"always"`, `"on_initialize"`, `"on_fail"`, `"never"`), `directories=("ssh", "data", "logs")` | name |
| `Secrets` | `name="secrets"`, `entries: tuple[Secret, ...] = ()`, `store: Disk \| None = None`, `host="0.0.0.0"`, `port=8080`, `ssh_directory="/root/.ssh"`, `ssh_key_path="/etc/root_key"` | name |
| `Secret` | `name`, `targets: tuple[SecretFile \| SecretEnv, ...]` (at least one), `required=True`, `schema: Schema \| None = None` | (inside `Secrets`) |
| `SecretFile` | `path`, `mode=0o400`, `owner=None` | |
| `SecretEnv` | `name`, `service=None` (`None`: global environment; `"app"` or `"app.service"`: delivered to `/run/secrets/app.env`, which a generated `app.service.d/tundravm-secrets.conf` drop-in makes the unit read with `EnvironmentFile=`, required when the secret is; the manifest's env target carries `service` and `env_file`) | |
| `Schema` | `kind="string"` (`"string"` or `"json"`), `min_length=None`, `max_length=None`, `pattern=None`, `enum=()` | |
| `RuntimeTools` | `source: Git`, `key_config="/etc/tdx/key-gen.yaml"`, `disk_config="/etc/tdx/disk-setup.yaml"`, `secret_config="/etc/tdx/secrets.yaml"`, `secret_manifest="/etc/tdx/secrets.json"` | one per variant |

`Disk.key` and `Secrets.store` take the declaration object, not its name. Without `RuntimeTools` the tools build from `tundra-tools` `master`.

A variant may declare several `Secrets`. A single one uses the `RuntimeTools` `secret_config` and `secret_manifest` paths. With more than one, each writes `<stem>-<name>` paths instead (`/etc/tdx/secrets-api.yaml`, `/etc/tdx/secrets-api.json` for `Secrets("api")`) and gets its own `secret-delivery setup` runtime-init step; its `SecretEnv(service=)` files are `/run/secrets/<service>-<name>.env`, read through a `tundravm-<name>.conf` drop-in. The `secret-delivery` binary built from `tundra-tools` does not deliver the manifest's targets yet: it receives one SSH public key over HTTP and writes `authorized_keys` (see [Concepts: Secret delivery at boot](concepts.md#secret-delivery-at-boot) for the request and responses); the manifest, env files' paths and drop-ins are what the image declares. Two declarations whose paths overlap (a config path another `Secrets` or a `RuntimeTools` path writes, or a `SecretFile` path another `Secrets` delivers) raise `ValidationError` naming both.

## Sources and builds

| Type | Fields (defaults) |
|---|---|
| `Git` | `url`, `ref`, `subdir=None` (relative), `submodules=False` |
| `Http` | `url`, `sha256=None` (64 lowercase hex) |
| `Install` | `source` (relative to the build's source tree), `destination` (absolute), `mode=0o755`, `directory=False` (requires `mode=None`) |
| `Build` | `name`, `source: Git \| Http`, `script=None`, `install: tuple[Install, ...]` (at least one), `packages=()`, `env: Pairs = ()`, `cache_key=None` (the build cache namespace, default `name`: the current dialect caches under `<namespace>-<sha256(source pin, build spec, install spec, arch, toolchain)[:16]>`, so changing any of them rebuilds), `recipe: Go \| Cargo \| Dotnet \| None = None` |
| `Go` | keyword-only: `output`, `package="./..."`, `ldflags="-s -w -buildid="`, `tags=()`, `env: Mapping = {}`, `packages=("golang",)`, `output_dir="./build"`, `mkdir=True` |
| `Cargo` | keyword-only: `output`, `bin=None`, `package=None`, `features=()`, `profile="release"`, `env: Mapping = {}`, `packages=("cargo",)` |
| `Dotnet` | keyword-only: `project`, `output`, `configuration="Release"`, `runtime="linux-x64"`, `env: Mapping = {}`, `packages=("dotnet-sdk-8.0",)`, `restore_args=()`, `properties: Mapping = {}` |

`Go`, `Cargo` and `Dotnet` copy `env` (and `Dotnet.properties`) into an immutable mapping, and their sequence fields into tuples, at construction: changing the dict you passed changes nothing, and the builder stays hashable.

A `Build` sets exactly one of `script` and `recipe`. `script` is a shell script run in the fetched source tree, with `env` exported and `packages` installed as build packages. `recipe` renders the toolchain's command and carries its own `packages` and `env`, so `Build(packages=, env=)` must stay empty. `install` paths are relative to the source tree, and each recipe leaves its output at a fixed path:

| Recipe | Command | Output to `Install` |
|---|---|---|
| `Go` | `go build -trimpath -ldflags ... -o <output_dir>/<output> <package>` | `build/<output>` (`<output_dir>/<output>`) |
| `Cargo` | `cargo fetch && cargo build --release --frozen` (`--profile <profile>` otherwise), with `--features`, `--package`, `--bin` | `target/<profile>/<bin or package or output>` |
| `Dotnet` | `dotnet restore`, then a deterministic self-contained `dotnet publish` into `publish/` | `publish/<output>` |

```python
from tundravm import Build, Cargo, Git, Go, Install

Build("app", Git("https://github.com/example/app.git", "v1.0.0"),
      recipe=Go(output="app", package="./cmd/app", env={"CGO_ENABLED": "0"}),
      install=(Install("build/app", "/usr/bin/app"),))
Build("prover", Git("https://github.com/example/prover.git", "v2.1.0"),
      recipe=Cargo(output="prover", bin="prover"),
      install=(Install("target/release/prover", "/usr/bin/prover"),))
```

`Build` identity is its name; install destinations of one build need distinct file names. Outside `nethermind-v1` a build caches under `<namespace>-<fingerprint16>`: the namespace is `cache_key` (default the build's name) and the fingerprint is the first 16 hex of a sha256 over the source pin (with git `subdir` and `submodules`), the build script or recipe, the install steps, the toolchain (recipe kind and packages) and the distribution it builds against: base, `arch`, mirror, snapshot, the variant's repositories and build packages, and its mkosi `Build` settings (environment, sandbox trees and files, verbatim `Build` keys such as `ToolsTree`; not `PackageCacheDirectory` or `WithNetwork`, which choose where packages come from, not which). Changing any of them rebuilds, so `cache_key` names a family of cache entries, not one. `nethermind-v1` keeps its historical keys: `<name>-<sha256(url)[:12]>-<pin12>`, or `cache_key` with `-<pin12>` appended.

## Resolution

```python
resolve(recipe: Recipe, *, variant: str) -> Resolved
resolve_all(recipe: Recipe) -> tuple[Resolved, ...]
identity(item: Declaration) -> tuple[str, ...]
```

`resolve` raises `ValidationError` on the first error-level diagnostic (the message names its code and how many more there are). `Resolved` has `variant: str`, `target: Target` (the first output), `targets: tuple[Target, ...]`, `items: tuple[Declaration, ...]` and `fragments: tuple[str, ...]` (the names of the fragments included).

`Diagnostic` has `code: str`, `message: str`, `level="error"` (`"error"`, `"warning"`, `"info"`), `variant=""` and `subject=""`. A fragment check may leave `variant` empty; resolution fills it in.

### Explaining one object

```python
explain_why(recipe: Recipe, variant: str, subject: str) -> Why
```

`explain_why` answers `tundravm inspect --why`: `subject` is an absolute image path or `unit:NAME`, `package:NAME`, `hook:NAME`, `init:NAME`; an unknown one raises `ValidationError` naming close matches. `Why` has `variant`, `subject`, `declarations` (each with `declaration`, `type`, `key`, `present` and `origins`: `(action, variant, fragments)` steps, `action` one of `declared`, `added`, `replaced`, `removed`), `fragments` (as `Resolved.fragments`), `files` (compiled-tree paths relative to the variant directory), `generated` (lines the compiler added) and `to_dict()`. The steps come from `tundravm.declarative.resolve.provenance(recipe, *, variant)`.

## Lifecycle

```python
lint(recipe, *, variants=None, lock=None) -> tuple[Diagnostic, ...]
compile(recipe, *, variants=None, lock=None) -> Tree
diff(tree: Tree, against: Tree | Path) -> str
lock(recipe, *, previous=None, update=(), offline=False, resolver=None, variants=None) -> Lock
lock_status(recipe, locked: Lock, *, variants=None, resolver=None) -> tuple[Diagnostic, ...]
read_lock(path: Path) -> Lock
write_lock(locked: Lock, path: Path) -> None
bake(recipe, *, lock: Lock, backend: Backend, out: Path, variants=None, progress=None, fetch=True, offline=False, verify_reproducible=False) -> tuple[Artifact, ...]
read_artifacts(manifest: Path) -> tuple[Artifact, ...]
verify_artifact(artifact: Artifact) -> None
measure(artifact, *, scheme="rtmr", allow_placeholder=False) -> Measurements
deploy(artifact, *, using: Qemu | Azure | Gcp, allow_simulated=False, adapter=None) -> Deployment
attest(endpoint: str, policy, *, nonce=None, transport=None) -> Attestation
sbom(subject: Artifact | lowered, *, lock=None, manifest=None) -> Sbom
evidence(recipe | lowered, *, out: Path, variants=None, lock=None, policy=None, recipe_path=None, runner=None) -> Evidence
doctor(backend: Backend, *, runner=None) -> tuple[Diagnostic, ...]
load(path, *, attribute=None, extra_paths=()) -> Recipe  # load_recipe
lower(recipe, *, variants=None)  # internal: the lowered recipe for the recipe
```

```python
from tundravm.declarative.lifecycle import FetchedSource, fetch

fetch(recipe, *, lock: Lock | None, out: Path, variants=None, resolver=None, force=False) -> tuple[FetchedSource, ...]
```

`fetch` and `FetchedSource` are exported from `tundravm` and `tundravm.declarative` as well as `tundravm.declarative.lifecycle`.

`variants=None` means every declared variant; unknown names raise `ValidationError`.

- **`lint`** returns resolution and fragment-check diagnostics first, in resolution order. When none is an error it adds the compiler's rules on the lowered recipe, sorted by variant, level and code. With `lock`, its source pins apply before the compiler's rules run (a pinned source never reports `source-unpinned`) and drift is added as `lock-changed`, `lock-added` and `lock-removed` diagnostics whose `subject` is the section.
- **`compile`** returns the tree in memory. With `lock`, its pins apply. Without one no lockfile is consulted, and what an unpinned source compiles to depends on the dialect. A source carries its own pin only when its `Git` ref is a full commit or its `Http` has a `sha256`; any other `Build` or built `Kernel` source is unpinned. In the current dialect, where the build sandbox never fetches, its build hook only fails, with `tundravm: source build NAME is not pinned: run tundravm lock, then tundravm fetch` (`kernel VERSION is not pinned` for a kernel), and `lint` warns `source-unpinned` about it. Under `nethermind-v1` the hook clones the ref inside the build sandbox, as the historical tree does. The supported sequence is the one in the example under [Result and input types](#result-and-input-types): `lock()`, `write_lock()`, then the same `Lock` to `compile()` and `bake()`, which fetches the pinned sources before it builds.
- **`diff`** is a unified diff from `against` (a `Tree` or a directory) to `tree`, empty when they match. Variant directories in `against` that `tree` does not hold are not compared.
- **`lock`** keeps every pin in `previous` whose source is unchanged and resolves the rest, plus the sources named in `update` (unknown names raise). A build whose source differs between variants is pinned once per variant under `<variant>/<name>`; `update` takes that key, or the build's name for every variant's pin. The default lookup runs `git ls-remote` for a git ref and hashes the download for an `Http` source without `sha256`; `resolver`, a function from source to pin, replaces it; `offline=True` fails for any source without a previous pin. Every source is tried before failing: one `LockfileError` lists all of them (`.failures`, name to `SourceError`), with reasons `ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>` or `timed out after 60s` (every network call times out after 60s, and git never prompts for credentials).
- **`lock_status`** is the drift between the recipe and `locked`, one diagnostic per section; empty when current. With `resolver` (as for `lock`) it also reports git refs that moved since the lock, as `sources.<name>` (`sources.<variant>.<name>` for a per-variant pin). A version 3 lockfile reports a `lock-changed` diagnostic for `version` and the new sections as added. When `variants` leaves out some declared variant, see [subsets](#locks-and-variant-subsets).
- <a id="fetch"></a>**`fetch`** checks the sources of `variants` out on this host, as the invoking user (git credentials and the SSH agent apply), into `out/.sources/<name>-<pin[:12]>-<id[:8]>/` (`id` hashes the url, subdirectory and submodules): every source build and, outside `nethermind-v1`, every built kernel's source (named `kernel`, or `kernel-<variant>` where a variant's kernel source differs) and every `EfiStub` package (an http source named `efi-stub`, or `efi-stub-<variant>` where a variant's package differs). A git source is checked out at its pinned commit (with submodules when the `Git` asks for them), an http source is downloaded and checked against its sha256. Pins come from `lock`; a source it does not pin (every source, with `lock=None`) is resolved first through `resolver` (default: the network, as `lock` does), which `Policy(mutable_ref_policy="error")` refuses. A complete checkout carries a marker naming its pin (and, for http, a sha256 manifest of its files) and is verified before it is kept, so a second fetch touches nothing: git `HEAD` must be the pin with an empty `git status` (untracked and ignored files included) and initialised submodules when asked for. A modified checkout raises `SourceError` (`E_SOURCE`, `source <name> checkout modified/incomplete: run tundravm fetch --force`); `force=True` checks every source out again and refills the dependency caches. There is one checkout per (source, pin). Each `Go`, `Cargo` and `Dotnet` build's dependencies are then prefetched with the host's `go`, `cargo` or `dotnet` (`go mod download`, `cargo fetch --locked`, `dotnet restore --packages`), run in a scratch copy of the checkout, into `out/.sources/deps/{go,cargo,nuget}`, with a marker `deps/<name>-<pin[:12]>-<id[:8]>.<cache>.json` that records the command and a content list (Go modules from `go.sum`, Cargo crates from `Cargo.lock` with size and sha256, NuGet packages from `project.assets.json`); a later fetch keeps the cache only while every listed entry is still there and unchanged, and prefetches it again otherwise; the current dialect's build hooks copy that cache and point the toolchain at it. A host without the toolchain skips the prefetch and a failing one only warns: the fetch succeeds and the build downloads the dependencies itself unless it bakes offline. Under `Policy(network_mode="offline")` a missing checkout raises `PolicyError` and nothing is prefetched. Returns one `FetchedSource` per source.
- **`bake`** writes `lock` to `out/tundravm.lock` when that file is absent or identical; a different lockfile already there is left alone and the bake reads a scratch copy of `lock`. It bakes frozen into `out` and writes `out/bake-result.json`, whose `declarative.lockfile` is the path of the lockfile it used (`null` for a scratch copy). It fails before building on lint errors (`LintError`) or drift (`LockfileError`); a `lock` of every variant covers `variants=` naming a [subset](#locks-and-variant-subsets). `progress` receives the CLI's progress lines. Outside `nethermind-v1`, a bake with a real backend first runs `fetch` at the pins it builds; the backend mounts `out/.sources` into the build (`BakeRequest.sources_dir`, at `$SRCDIR/tundravm-sources`, ephemerally) and each source hook (and kernel build script) copies its checkout, so the build sandbox never fetches a source. `fetch=False` builds from the checkouts already in `out/.sources` (copied to an air-gapped host, say) and raises `StateError` (`E_STATE`) before mkosi runs when one is missing. `offline=True` bakes as `Policy(network_mode="offline")` does: the fetch reuses complete checkouts and downloads nothing, every `Go`, `Cargo` and `Dotnet` build must find its prefetched dependencies, complete (else `StateError` before mkosi runs, naming the missing cache entry for an incomplete one), and mkosi runs with `--with-network=no`; a `nethermind-v1` recipe with source builds raises `PolicyError`, since its hooks clone in the sandbox. The in-process backend fetches nothing. `verify_reproducible=True` bakes the same variants a second time into `out/.reproduce`, against the same lockfile and the checkouts of `out/.sources` (hard-linked, nothing is fetched again), compiling and building everything again, and compares every artifact's sha256: `bake-result.json` records `declarative.reproducible`, the second build is removed when all match, and a mismatch keeps it and raises `ReproducibilityError` whose message holds a table per artifact (`variant`, `target`, both sha256 prefixes, `match`/`mismatch`), whose hint names `tundravm diff RECIPE --against out/.reproduce/mkosi` and the usual causes (timestamps, build ids, packages without a snapshot), and whose context holds `second_build` and `differ` (`variant/target, ...`).
- **`read_artifacts`** reads `bake-result.json` (or the directory holding it).
- **`verify_artifact`** hashes `artifact.path` and raises `ArtifactError` (`E_ARTIFACT_CHANGED`) when the file is unreadable, has no recorded sha256, or no longer matches the sha256 `bake-result.json` recorded. `measure` and `deploy` call it before anything else.
- **`measure`** derives expected measurements with `measured-boot` or `dstack-mr`; without one it raises `MeasurementError` unless `allow_placeholder`, which also emits a `PlaceholderMeasurementWarning`. Simulated artifacts are refused unless `allow_placeholder`.
- **`deploy`** deploys with the target's adapter. The `using` type must match `artifact.target`; simulated artifacts are refused unless `allow_simulated`. `adapter` replaces the default adapter (tests). On QEMU every port in `artifact.ports` that `Qemu.forward` does not already forward (by guest port) is forwarded from the same host port. The adapters in `tundravm.deploy` take `runner=` (an argv to `subprocess.CompletedProcess` function) that replaces every `qemu-system-x86_64`, `az` or `gcloud` call, so their command lines can be asserted without the tools.
- **`attest`** asks the `tdxs` issuer at `endpoint` (`unix:PATH` or a path, or `tcp://HOST:PORT`; an `http(s)://` endpoint raises `ValidationError`, as tdxs serves no http) for a quote whose `report_data` binds `nonce` (hex or bytes; default 32 random bytes), reads MRTD and RTMR0..RTMR3 from it (DCAP quote v4 or v5; a `simulator` issuer's come from its `metadata` reply) and compares them with `policy`, a file `measure --export-policy` wrote or its dict. It checks measurements only; collateral verification is done by a Tdxs validator. `transport`, a function from the request envelope (`{"method", "data"}`) to the reply envelope, replaces the network; `tundravm.attestation.connect(endpoint)` builds the default one. An unreachable issuer or an `error` reply raises `DeploymentError`, a reply that is not a TDX attestation or a placeholder policy `MeasurementError`, a bad nonce or policy `ValidationError`. An untrusted result is a value, not an error. `tundravm.attestation.parse_quote(raw) -> Quote` is the quote parser (`version`, `mrtd`, `rtmrs`, `report_data`, `mrseam`, `mrowner`, `xfam`).
- **`sbom`** says what is in an image: an `Artifact`, or a lowered recipe with one variant (`lower(recipe, variants=["default"])`, before any bake). Its packages come from mkosi's JSON package manifest, `manifest` or by default `<variant>.manifest` beside the artifact (`<build_dir>/<variant>/output/<variant>.manifest` for a lowered recipe); when that file does not exist it adds a note and lists the packages the recipe declares (the lock's `dependencies` for an artifact), unversioned, and an unreadable manifest or a `manifest_version` other than 1 raises `ArtifactError`. Its sources are `lock`'s pins for the variant: each source build with its install destinations, the built kernel, the `efi-stub` package; for a lowered recipe they are its declared sources, unpinned (ref only) without `lock`. The metadata (base, arch, snapshot, mirror) comes from the lock's `distribution` section for an artifact, else the manifest's `config`. See [CLI: SBOM](cli.md#sbom) for the document shapes and package URLs.
- **`evidence`** reads the bake in `out` (the directory holding `bake-result.json`) for `variants` (default: every baked variant; one not baked raises `StateError`) and returns an `Evidence`; it writes nothing. `lock` (a `Lock` or a lockfile path) defaults to the lockfile the bake recorded, else `out/tundravm.lock`; `policy` is a `measure --export-policy` file used for every variant instead of `out/<variant>/policy.json`; `recipe_path` records the recipe file and its sha256; `runner` replaces the `mkosi --version` probe. A lowered recipe instead of a `Recipe` leaves out the provenance summary. See [CLI: Evidence](cli.md#evidence) for the index and the members.
- **`doctor`** returns one `tool-missing` diagnostic per missing host tool of the backend (a warning when the tool is optional).
- **`load`** is `load_recipe`: it imports a recipe file and returns its `Recipe`. `attribute` names the module-level `Recipe` or a zero-argument factory returning one; without it (`attribute=None`, the default) the recipe is discovered as the [CLI does](cli.md#recipe-files). The file's directory and every `extra_paths` entry are importable while it runs.

### Result and input types

| Type | Fields |
|---|---|
| `Tree` | `entries: tuple[Entry, ...]`, `digest: str`, `variants: tuple[str, ...]`; `write(path)` writes it, replacing its variant directories and dropping stale ones |
| `Entry` | `path`, `content: bytes \| None`, `mode: int`, `symlink: str \| None = None` (a directory has neither content nor symlink) |
| `Lock` | `recipe_digest`, `sections: Pairs` (section to digest, see [below](#locks-and-variant-subsets)), `pins: tuple[Pin, ...]`, `compiler_version` (the tundravm version that wrote it, from its `compiler` section); `text()` is the serialized lockfile |
| `Pin` | `identity` (build name, `kernel`/`kernel-<variant>` for a built kernel, `efi-stub`/`efi-stub-<variant>` for an `EfiStub` package, or URL for anonymous fetches), `source: Git \| Http` (the resolved commit or hash), `digest` |
| `FetchedSource` | `name` (a build's lock key: its name or `<variant>/<name>`, `kernel`/`kernel-<variant>`, or `efi-stub`/`efi-stub-<variant>`), `kind` (`"git"`, `"http"`), `url`, `pin` (commit or sha256), `path` (`<out>/.sources/<name>-<pin[:12]>-<id[:8]>`), `cached=False` (an earlier fetch had completed it), `ref=None` (the git ref the pin came from), `deps: str | None = None` (a Go, Cargo or .NET build's dependency prefetch: `go: prefetched`, `go: kept` (`cargo:`, `nuget:`), `go: prefetched (was incomplete: <problem>)` after repairing an incomplete cache, `go: incomplete (<problem>)` when one cannot be prefetched again (no host toolchain, or offline), `skipped (no host go)`, `skipped (offline)` or `failed (<reason>)`; `None` for other sources); `locked()` is the lockfile entry it matches |
| `Backend` | `kind` (`"lima"`, `"nix"`, `"local"`, `"inprocess"`), `cpus=2`, `memory="4GiB"`, `disk="40GiB"` (the last three for Lima) |
| `Artifact` | `path`, `variant`, `target`, `sha256`, `recipe_digest`, `lock_digest`, `tree_digest`, `simulated=False`, `ports=()` (the guest ports the variant's `Secrets` deliveries listen on, as `bake-result.json` records them under `declarative.ports`) |
| `Measurements` | `scheme` (`"rtmr"`, `"azure"`, `"gcp"`), `values: Pairs`, `tool` (`"measured-boot <version>"`, `"dstack-mr <version>"` or `"placeholder"`), `artifact_digest`; `to_json(path=None) -> str` is the four fields as JSON (sorted keys, trailing newline), also written to `path` when given; `verify(expected: Mapping[str, str]) -> tuple[str, ...]` is the sorted registers whose value differs from `expected` (a register only one side has counts), empty when all match |
| `Qemu` | `memory="2G"`, `cpus=2`, `ssh_port=2222`, `tdx=False` (TDVF through `-bios`, `tdx-guest` object, split irqchip), `daemonize=True` (`False`: QEMU runs in the foreground with its console on the terminal), `forward: tuple[tuple[int, int], ...] = ()` (`(host, guest)` TCP ports forwarded besides `ssh_port`) |
| `Azure` | `storage_account`, `resource_group="tdx-vms"`, `location="eastus"`, `vm_size="Standard_DC2es_v5"` (a TDX confidential size: DCesv5/DCedsv5 or ECesv5/ECedsv5), `gallery="tdx_images"` (the Compute Gallery the image version goes to), `secure_boot=False` (on requires `signed`: Azure's Secure Boot firmware boots only signed UKIs, and tundravm does not sign), `signed=False` (the UKI was signed outside tundravm with keys Azure trusts) |
| `Gcp` | `project`, `bucket`, `zone="us-central1-a"`, `machine_type="c3-standard-4"` (TDX runs on the C3 series) |
| `Deployment` | `id`, `target`, `endpoint: str \| None`, `metadata: Pairs` |
| `Attestation` | `endpoint`, `platform` (`tdx`, `azure-tdx`, `gcp-tdx`, `simulator`), `source` (`"quote"`, or `"metadata"` for the simulator), `quote_version: int \| None`, `nonce` and `report_data` (hex), `nonce_matches: bool`, `registers: tuple[RegisterCheck, ...]` (`name`, `verdict` `"match"`/`"mismatch"`/`"unchecked"`, `actual`, `expected`; MRTD and RTMR0..RTMR3), `policy` (its path, `None` for a dict); `trusted` (the nonce matches and no register mismatches), `verdict` (`"trusted"`/`"untrusted"`), `to_dict()` (what `attest --format json` prints) |
| `Evidence` | `index` (the `evidence.json` mapping, see [CLI: Evidence](cli.md#evidence)), `members: Mapping[str, bytes]` (relative path to bytes, `evidence.json` excluded), `notes: tuple[str, ...]`; `verdict` (`"pass"` or `"fail"`), `passed`; `index_json()`, `files()` (every file, `evidence.json` included), `write(directory) -> Path`, `bundle(path) -> Path` (a deterministic `tar.gz`), `html() -> str` (one self-contained page), `render(format="text")` (`"text"` or `"json"`) |
| `Sbom` | `variant`, `components: tuple[Component, ...]` (sorted: packages by name, then builds, kernels, `efi-stub`), `base`, `arch`, `snapshot`, `mirror`, `recipe_digest`, `tree_digest`, `artifact: Path \| None`, `artifact_sha256`, `tundravm_version`, `manifest: Path \| None` (read, or looked for), `manifest_found: bool`, `created` (ISO 8601 UTC; `SOURCE_DATE_EPOCH` when set), `notes: tuple[str, ...]` (what it could not include, and why); `packages` and `sources` split `components`; `render(format="spdx-json")` is `"spdx-json"`, `"cyclonedx-json"`, `"text"` or `"markdown"`; `to_spdx()` and `to_cyclonedx()` are the JSON documents as dicts. A `Component` (`tundravm.declarative.bom`) has `kind` (`"package"`, `"build"`, `"kernel"`, `"efi-stub"`), `name`, `version` (a package's version, a source's commit or sha256; empty when unknown), `arch`, `type` (`deb`/`rpm`/`pkg`), `origin` (the repository, when the manifest names one), `url`, `ref`, `sha256`, `installs` (a build's install destinations), `declared` (listed from the recipe, no manifest); `commit` is a git source's pin; `tundravm.declarative.bom.purl(component)` is its package URL |

```python
from pathlib import Path
from tundravm import Backend, bake, compile, lint, lock, read_artifacts, write_lock
from tundravm.declarative import measure

assert not [d for d in lint(recipe) if d.level == "error"]
locked = lock(recipe)
write_lock(locked, Path("build/tundravm.lock"))
compile(recipe, lock=locked).write(Path("mkosi"))
artifacts = bake(recipe, lock=locked, backend=Backend("inprocess"), out=Path("build"))
default = next(a for a in artifacts if a.variant == "default")
measured = measure(default, allow_placeholder=True)
measured.to_json(Path("build/default.measurements.json"))
assert measured.verify(dict(measured.values)) == ()
```

### Locks and variant subsets

`Lock.sections` holds one digest per recipe section: the recipe-wide `distribution` (base, arch, mirror, tools mirror, snapshot, epoch), `compiler` (tundravm version, dialect, mkosi options), `default_profile` and `init_scripts`, and `variants.<variant>.<key>` per variant, for `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `debloat` (the complete debloat configuration), `kernel` (version, source, cmdline, tdx, sha256 of the config bytes), `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets`, `mkosi` for a variant whose mkosi options differ from the default variant's, and `extends` for a variant with a parent. File entries record each path's `kind` (`file`, `symlink` or `directory`), mode and content sha256. Drift diagnostics name these sections in `subject`. The lockfile is version 4; a version 3 lockfile (which had `base` and `arch` sections) still loads, drifts with a `version` diagnostic and the new sections as added, and a frozen `bake` refuses it until it is locked again. Version 2 files, which named the per-variant sections `profiles.<variant>.<key>`, load with the names mapped. The recipe digest covers the `compiler` section, so it changes with the tundravm version.

A lock of every variant covers any subset. When `variants` leaves out a declared variant, `lock_status` and the frozen check in `bake` compare only the selected variants' sections and the recipe-wide ones; the lock's sections for the other variants, and sources pinned only for them, are not reported. The whole-recipe digest is compared only when the selection is every variant the lock holds. A lock written for a subset (`lock(recipe, variants=("default",))`) does not cover a bake of more variants: the missing variants' sections are `lock-added`.

## Shipped fragments

| Class | Fragment name | Declares |
|---|---|---|
| `Tdxs(*, source=TUNDRA_TOOLS, issuer="tdx", validator=None, expected_measurements=(), check_revocations=False, get_collateral=False, verify_imds=False, verify_identity_token=False, after_init=False)` | `tdxs` | Go build packages, `Build("tdxs")`, `/etc/tdxs/config.yaml`, the socket-activated `tdxs.service`/`tdxs.socket`, `Group("tdx")`, `User("tdxs")` |
| `DevTools(*, root_password="tdx")` | `devtools` | Debugging packages, a serial console unit, root password login. Never ship it. |
| `EfiStub(*, snapshot, version)` | `efi-stub` | A postinst hook installing `systemd-boot-efi` `version` from a Debian snapshot: `snapshot` is a snapshot ID (`"20251113T083151Z"`, read from `snapshot.debian.org`) or a snapshot archive URL, and it must carry `version` (the templates pair `20251113T083151Z` with `257.8-1~deb13u1`). Outside `nethermind-v1` the package (`systemd-boot-efi_<version>_amd64.deb` in the snapshot's pool) is a source named `efi-stub`: `lock` pins its sha256, `fetch` downloads it on the host, and the current dialect's hook checks the mounted copy against the pin and installs it with `dpkg -i`. Without a pin the hook downloads the package into `$BUILDROOT/` (mkosi-chroot mounts its own `/tmp`) and fails with that advice when the download fails; `nethermind-v1` downloads it with `curl` |
| `Backports(*, archive_url=None, release=None)` | `backports` | Debian backports and sid apt sources for the build's apt, not the image. The current dialect writes `mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources` and `preferences.d/debian-backports.pref` at compile time, pinning backports to 200 and sid to 100 so packages come from the release unless it lacks them. The URI is `archive_url` verbatim, else `Recipe.mirror` and `Recipe.snapshot` completed as mkosi does, else `http://deb.debian.org/debian`; the suite is `release`, else the release of `Recipe.base`. `nethermind-v1` generates the sources with a sync hook and `Setting("Build", "SandboxTrees", ...)`, with no pins |

`Tdxs.from_policy(policy, *, validator="tdx", mrtd=None, allow_placeholder=False, **fields)` builds a verifier from a policy file `tundravm measure --export-policy` wrote (or its dict): its `RTMR0`..`RTMR3` become `expected_measurements` under the keys the tundra-tools validator reads (`rtmr0`..`rtmr3`, lower-case hex). RTMR tools do not report MRTD and the validator checks only the registers it is given, so pass `mrtd=` to check it too. A placeholder policy raises `MeasurementError` unless `allow_placeholder=True`; a missing required register or a wrong `schema_version` raises `ValidationError`. `Tdxs.from_measurements(measurements, ...)` does the same for a `measure()` result. Other keyword arguments are `Tdxs` fields.

`issuer`/`validator` are a `TdxsType` (`"tdx"`, `"azure"`, `"gcp"` or `"simulator"`) or `None`. `TUNDRA_TOOLS` is `Git("https://github.com/Hyodar/tundra-tools.git", "master")`. `Backports.render_sources(*, mirror, release, snapshot=None)` and `render_preferences(*, release)` return the two files' text (the recipe's mirror root, release and snapshot; the fragment's own fields win). `BACKPORTS_TREE` is the `SandboxTrees` entry `Backports` adds under `nethermind-v1`, `"mkosi.builddir/debian-backports.sources:/etc/apt/sources.list.d/debian-backports.sources"`. Each class is an instance of `Fragment`, so it goes wherever a `Fragment` does: in `common`, in a variant's `add`, or among another fragment's `items`.

### `Composite`

`Composite` is the `Fragment` subclass the shipped fragments are built on. A subclass declares its configuration as dataclass fields and returns its contents from `compose()`:

```python
from dataclasses import dataclass

from tundravm import Fragment, Package
from tundravm.declarative.utils import Composite


@dataclass(frozen=True, slots=True, kw_only=True)
class Debugging(Composite):
    gdb: bool = True

    def compose(self) -> Fragment:
        tools = ("strace", "gdb") if self.gdb else ("strace",)
        return Fragment("debugging", items=tuple(Package(name) for name in tools))
```

- Decorate the subclass with `@dataclass(frozen=True, slots=True, kw_only=True)`. Construction calls `compose()` once; its `name`, `items`, `requires` and `checks` become the instance's.
- `compose()` must return a `Fragment` (anything else is a `ValidationError`); it may raise `ValidationError` for bad field values. A subclass without `compose()` raises `NotImplementedError` when constructed.
- The configuration fields alone are the instance's `repr` and equality: `Debugging()` prints as `Debugging(gdb=True)`, and two equal configurations are the same fragment, so including both expands once. Lists passed for fields are frozen into tuples.

The examples ship more: `NethermindBase(*, snapshot=PINNED_MIRROR)` in [`examples/nethermind_base.py`](../examples/nethermind_base.py), and `Raiko(*, source)`, `TaikoClient(*, source)`, `Nethermind(*, source)` in [`examples/fragments/`](../examples/fragments/).

## Errors

Every SDK error subclasses `TdxError(message, *, code, hint=None, context=None)`, which has `code`, `hint`, `context` and `to_dict()`. The CLI prints them as `error [CODE]: message`, the hint and the context, and exits 2.

| Class | Code | Raised when |
|---|---|---|
| `ValidationError` | `E_VALIDATION` | A malformed declaration, a resolution error, a recipe that cannot lower, an unknown variant, a bad recipe file |
| `LintError` | `E_LINT` | `bake` refused a recipe with error-level compiler findings |
| `LockfileError` | `E_LOCKFILE` | A missing or unreadable lockfile, a frozen bake of a recipe that drifted from it, or `lock` could not resolve some sources (`.failures`) |
| `SourceError` | `E_SOURCE` | A source could not be resolved; `.source`, `.reason` |
| `ReproducibilityError` | `E_REPRODUCIBILITY` | Two builds that should match did not: `bake(verify_reproducible=True)` (`bake --verify-reproducible`) found an artifact whose sha256 differs between them |
| `BackendExecutionError` | `E_BACKEND_EXECUTION` | The backend failed or its tools are unusable |
| `MeasurementError` | `E_MEASUREMENT` | No measurement tool, or a simulated artifact, without `allow_placeholder`; `attest` got a reply that is not a TDX attestation, or a placeholder policy |
| `DeploymentError` | `E_DEPLOYMENT` | A simulated artifact, a target mismatch, or a missing target tool; `attest` could not reach the tdxs issuer, or it replied with an error |
| `ArtifactError` | `E_ARTIFACT_CHANGED` | `verify_artifact`, and `measure`/`deploy` before they start, found an artifact unreadable or its bytes no longer matching the sha256 `bake-result.json` recorded |
| `PolicyError` | `E_POLICY` | A `Policy` setting refused the operation |
| `StateError` | `E_STATE` | `bake-result.json` is missing or unreadable, or has no artifact for the requested variant/target; `bake(fetch=False)` (`bake --no-fetch`) finds a source checkout missing from `out/.sources`; an offline bake (`offline=True`, `bake --offline`, `Policy(network_mode="offline")`) finds a Go, Cargo or .NET build without its prefetched dependencies in `out/.sources/deps` |

## Lint rules

Resolution and references (from `resolve`):

| Code | Level | Meaning |
|---|---|---|
| `fragment-conflict` | error | Two different fragments share a name |
| `identity-collision` | error | Two different declarations share an identity; use `replace` |
| `replace-missing` | error | A variant replaces something it does not inherit |
| `remove-missing` | error | A variant removes something it does not inherit |
| `target-inconsistent` | error | A variant leaves the cloud target its parent set |
| `disk-key-undefined` | error | A disk's key is not declared in the variant |
| `disk-key-mismatch` | error | A disk's key differs from the declared key of that name |
| `secret-store-undefined` | error | The secrets' store disk is not declared in the variant |
| `secret-store-mismatch` | error | The store disk differs from the declared disk of that name |
| `init-after-undefined` | error | An `Init.after` names no init step |
| `init-order` | error | An `Init` runs after a step with a higher priority |
| `hook-after-undefined` | error | A `Hook.after` names no hook |
| `hook-order` | error | A `Hook` runs after a hook of a later phase |
| `unit-after-init-without-init` | warning | `Unit(after_init=True)` in a variant with no runtime-init step |
| `fragment-requires-missing` | error | A fragment's `requires` names a fragment the variant does not include |

Compiler rules (on the lowered recipe, when resolution found no error):

| Code | Level | Meaning |
|---|---|---|
| `user-group-undefined` | error | A user joins a group nothing creates |
| `service-user-missing` | error | A generated service runs as a user nothing creates |
| `file-path-duplicate` | error | One path declared several times with different content |
| `file-path-relative` | error | A path is relative or contains `..` |
| `service-command-not-shipped` | warning | A generated service runs a binary nothing ships |
| `init-priority-collision` | warning | Several runtime-init steps share a priority |
| `debloat-removes-needed-unit` | warning | Debloat masks a unit a service needs |
| `debloat-removes-declared-file` | warning | Debloat deletes a declared file at finalize |
| `source-unpinned` | warning (error / info by policy) | A source build, or in the current dialect a built kernel's source or an `EfiStub` package (`efi-stub`), has no pin in `build/tundravm.lock` |
| `disk-key-path-mismatch` | warning | A disk reads a key file the key does not write |
| `disk-auto-format` | warning (error with `Policy(storage_safety="error")`) | A `Disk(device=None)` with `format` other than `"never"`: `disk-setup` picks the largest whole `/dev/sd*` disk, boot disk included, and can format it. Set an explicit `device`, or `format="never"` for a disk prepared beforehand |
| `key-pipe-outside-run` | info | A pipe key's path is outside `/run` |
| `variant-empty` | info | A variant declares nothing of its own |
| `kernel-missing` | error | A bootable variant has no `Kernel` and installs no `linux-image-*` package; mkosi cannot build its UKI. `Setting("Content", "Bootable", ("no",))` disables both |
| `secret-env-service-unknown` | error | A `SecretEnv(service=)` names a service the variant neither generates (`Service`), ships (`Unit` with content) nor enables, disables or masks (`Unit(name, enabled=...)`, for a packaged unit) |
| `secret-in-file` | error | A `File`, `Template` or `Directory` file holds what looks like a credential: a PEM private key (header and body), an AWS access key id (`AKIA`/`ASIA`), a GitHub token (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), in a config-like file (under `/etc`, or `.conf`, `.ini`, `.env`, `.yaml`, `.toml`, `.json`, ...) a `token`/`secret`/`password`/`api_key = value` assignment with a token-like value, or in a `secrets.yaml` a literal under a sensitive or `value`/`default`/`data` key. Documentation values (`EXAMPLE`, `changeme`, `xxxx`), paths, `$VAR`/`{{ }}` references, commented-out assignments, public keys and certificates do not count; the message names the kind and line, never the value. Deliver it at boot with `Secrets` (`SecretFile`, `SecretEnv`), or pass `allow_secret=True` |
| `secret-in-env` | error | A `Service(env=...)` value, or a shipped unit file (`Environment=` lines, or a key or token anywhere in it), holds a credential: a key or token as above, or a token-like literal for a `*TOKEN`, `*SECRET`, `*PASSWORD`, `*API_KEY`, `*ACCESS_KEY` variable. Deliver it with `SecretEnv(NAME, service=...)`, or pass `allow_secret=True` |
| `recipe-arch-unsupported` | error | `Recipe(arch="aarch64")` with `EfiStub` (it installs the `_amd64.deb` systemd-boot-efi) or a `qemu` target (the QEMU adapter runs `qemu-system-x86_64`) |

Lock drift (from `lint(lock=)` and `lock_status`): `lock-changed`, `lock-added`, `lock-removed` (each with the section as `subject`), and `lock-stale` when every section matches but the whole-recipe digest does not. Host tools (from `doctor`): `tool-missing`.

## Policy

`Policy(require_frozen_lock=False, mutable_ref_policy="warn", network_mode="online", storage_safety="warn")`. `network_mode="offline"` keeps `lock` and `fetch` off the network and bakes as `bake(offline=True)`; `storage_safety="error"` makes `disk-auto-format` an error. See [policy](policy.md).

## Testing helpers

`tundravm.testing` (see [testing](testing.md)); `variants` is a name, a sequence of names, or `None` for every variant:

```python
compile_tree(recipe, *, variants=None, path=None) -> CompiledTree
assert_clean(diagnostics_or_recipe, /, *, variants=None, allow=(), strict=None) -> diagnostics
assert_diagnostic(diagnostics_or_recipe, code, /, *, level=None, variant=None, subject=None, variants=None) -> Diagnostic
assert_tree(tree: Tree, golden: str | Path, *, update=None) -> None
assert_tree_matches(recipe, golden_dir, *, variants=None, update=None) -> TreeDiff
fake_bake(tree: Tree, *, variant: str, target: Target, out: str | Path) -> Artifact
bake_in_process(recipe, *, out=None, variants=None, lock=None) -> tuple[Artifact, ...]
fake_fragment(name="fake", *, packages=(), files=None, init=None, priority=50, requires=(), checks=()) -> Fragment
recipe_file(tmp_path: Path, source: str, name="recipe.py") -> Path
run_cli(*argv) -> tuple[int, str, str]
```

Pytest fixtures (plugin `tundravm`, loaded automatically): `recipe`, `compiled`, `run_cli`.
