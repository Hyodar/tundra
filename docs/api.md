# API reference

The public API is the declarative one in `tundravm.declarative`. Most names are also exported from `tundravm`; the exceptions are listed under [Imports](#imports). Every record below is a frozen, slotted dataclass: lists passed for tuple fields are frozen into tuples, and `__post_init__` raises `ValidationError` for malformed values.

For the model behind these types see [concepts](concepts.md); for writing reusable fragments see [writing fragments](module-authoring.md).

## Imports

```python
from tundravm import Recipe, Fragment, Variant, Package, File, Service, compile, lint, lock, bake
from tundravm.declarative.utils import Composite, DevTools, Tdxs
from tundravm.declarative import diff, measure, deploy
```

`tundravm` exports the declarations, the build recipes `Go`, `Cargo` and `Dotnet`, the recipe types, the lifecycle functions and result types, `Policy`, the [errors](#errors), `load` and `load_recipe`. Only in `tundravm.declarative`: `diff`, `measure` and `deploy` (at the top level those names are the `tundravm.diff`, `tundravm.measure` and `tundravm.deploy` subpackages), `identity`, and the type aliases `Check`, `Pairs`, `Phase` and `Target`. The shipped fragments `Tdxs`, `DevTools`, `EfiStub` and `Backports`, and their base class `Composite`, live in `tundravm.declarative.utils`.

## Recipe, variants, fragments

### `Recipe`

| Field | Type | Default |
|---|---|---|
| `name` | `str` | required |
| `common` | `Fragment` | required |
| `variants` | `tuple[Variant, ...]` | `(Variant("default", target="qemu"),)` |
| `base` | `str` | `"debian/trixie"` |
| `arch` | `"x86_64" \| "aarch64"` | `"x86_64"` |
| `mirror` | `str \| None` | `None` (the distribution default) |
| `tools_mirror` | `str \| None` | `None` |
| `epoch` | `int \| None` | `0`: reproducible (`SOURCE_DATE_EPOCH=0`, stable seed, `IMAGE_VERSION` stripped); `None`: not reproducible; other values set `SourceDateEpoch=` and `SOURCE_DATE_EPOCH` to that value |
| `mkosi` | `Mkosi` | `Mkosi()` |
| `policy` | `Policy \| None` | `None` (the default `Policy()`); see [policy](policy.md) |

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
| `dialect` | `"current" \| "nethermind-v1"` | `"current"` |
| `init_script` | `str \| None` | `None`; text written to `mkosi.skeleton/init` (mode 0755) |
| `version_script` | `bool` | `False`; emit `mkosi.version` |
| `cloud_postoutput` | `bool` | `True`; emit the Azure/GCP disk conversion postoutput scripts |
| `strip_os_release` | `bool \| None` | `None`: strip `IMAGE_VERSION` from `os-release` when `Recipe.epoch` is set |

`nethermind-v1` spells groups and users as postinst `groupadd`/`useradd` lines and omits the `# unpinned:` build marker, matching the historical nethermind-tdx tree.

## Declarations

### Packages, files, accounts, units

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Package` | `name`, `role="runtime"` (`"runtime"` or `"build"`) | name, role |
| `File` | `path` (absolute), `content: str \| bytes \| Path`, `mode=0o644`, `stage="extra"` (`"skeleton"` or `"extra"`) | stage, path |
| `Template` | `path` (absolute), `template: str \| Path`, `variables: tuple[tuple[str, str \| int \| float], ...] = ()`, `mode=0o644`, `stage="extra"` | stage, path |
| `Directory` | `path` (absolute), `source: Path`, `exclude=()`, `mode=None` (keep), `stage="extra"` | stage, path |
| `Group` | `name`, `system=True`, `gid=None` | name |
| `User` | `name`, `system=True`, `home=None`, `shell="/usr/sbin/nologin"`, `uid=None`, `primary_group=None`, `groups=()` | name |
| `Unit` | `name`, `content: str \| Path \| None = None`, `enabled=None`, `masked=None`, `after_init=False` | unit name |
| `Service` | `name`, `exec_start: str \| tuple[str, ...]`, then keyword-only: `description=None`, `user=None`, `group=None`, `working_dir=None`, `env: Pairs = ()`, `env_file=None`, `exec_start_pre=()`, `after=()`, `requires=()`, `wants=()`, `wanted_by=None` (`minimal.target`), `type=None` (`"simple"`, `"exec"`, `"oneshot"`, `"notify"`, `"forking"`), `restart="no"` (`"always"`, `"on-failure"`, `"no"`), `limits=()` (`(resource, value)` pairs), `kill_mode=None`, `timeout_stop=None`, `security="default"` (`"strict"`, `"default"`, `"none"`), `after_init=True` | unit name |

`Template` renders `template` with `str.format_map(variables)` at lowering time; a placeholder without a value fails to lower. `Service` renders and enables a `.service` unit; `after_init=True` makes it wait for `runtime-init.service` when the variant has a runtime-init step. `Unit` with `content` ships that text verbatim and needs a type suffix (`app.service`). Without `content` it controls a packaged unit and must set `enabled` or `masked`. `after_init=True` adds `After=`/`Requires=runtime-init.service`.

### Scripts and ordering

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Hook` | `name`, `phase: Phase`, `script`, `env: Pairs = ()`, `cwd=None`, `after=()` | name |
| `Init` | `name`, `script`, `priority=100`, `after=()` | name |

`Phase` is one of `sync`, `skeleton`, `prepare`, `build`, `extra`, `postinst`, `finalize`, `postoutput`, `clean`, `repart`, `boot`. `Hook.after` names hooks of the same or an earlier phase. `Init` steps run in `/usr/bin/runtime-init` by ascending priority; `after` orders equal priorities and may name the built-in `keys` (10), `disks` (20) and `secrets` (30), which are reserved names. `Pairs` is `tuple[tuple[str, str], ...]`.

### System configuration

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Repository` | `name`, `url`, `suite`, `components=("main",)`, `keyring=None`, `priority=100` | name |
| `Partition` | `name`, `size`, `mount`, `filesystem="ext4"` | name |
| `Debloat` | `enabled=True`, `remove=None` (compiler default list), `extra_remove=()`, `keep_paths=()`, `minimize_systemd=True`, `keep_units=None`, `keep_binaries=None`, `keep_units_extra=()` (kept on top of `keep_units` or its default), `keep_paths_by_variant=()` (`(variant, paths)` kept only in that variant) | one per variant |
| `Setting` | `section`, `key`, `values: tuple[str, ...]`; per variant | section, key |
| `Kernel` | `version`, `source: Git \| Http`, `config: Path \| None = None`, `cmdline=""`, `tdx=True` | one per variant |

No `Debloat` means the compiler default (debloat enabled). `Setting` is the mkosi escape hatch, and settings belong to the variant that declares them. `Output.Seed`, `Output.OutputDirectory`, `Output.CompressOutput`, `Output.ManifestFormat`, `Build.PackageCacheDirectory`, `Build.WithNetwork`, `Build.Environment`, `Build.SandboxTrees` and `Content.CleanPackageMetadata` map onto compiler options. Any other key is written verbatim into that variant's `mkosi.conf`, under its section, one `Key=value` line per value. Keys the compiler writes itself (`Packages`, `BuildPackages`, `Mirror`, `Format`, `ImageId`, `KernelCommandLine`, `ExtraTrees`, the phase script keys such as `BuildScripts`, ...) raise `ValidationError` naming the declaration to use instead (`Package(name)`, `Recipe.mirror`, `Hook(name, phase, script)`, ...). `Setting("Build", "BuildSources", ("src[:dest]", ...))` mounts host directories into the variant's build.

`Kernel.source` is a `Git` branch, tag or full commit hash, with `subdir` and `submodules`, or an `Http` tarball, which needs `sha256=` and is checked against it before unpacking. The kernel source is not covered by the lockfile (`lock` pins no kernel ref), which is why `Http` carries its digest in the recipe. A variant whose settings or kernel differ from the default variant's lowers standalone with its own `mkosi.conf` lines, kernel build and config.

## Keys, disks and secrets

| Type | Fields (defaults) | Identity |
|---|---|---|
| `Key` | `name`, `output=None`, `strategy="random"` (`"random"` or `"pipe"`), `persist_in_tpm=True`, `size=64`, `pipe=None` (required with `"pipe"`) | name |
| `Disk` | `name`, `mount`, `device=None` (largest unpartitioned disk), `key: Key \| Path \| None = None`, `mapper=None` (needs a key), `format="on_fail"` (`"always"`, `"on_initialize"`, `"on_fail"`, `"never"`), `directories=("ssh", "data", "logs")` | name |
| `Secrets` | `name="secrets"`, `entries: tuple[Secret, ...] = ()`, `store: Disk \| None = None`, `host="0.0.0.0"`, `port=8080`, `ssh_directory="/root/.ssh"`, `ssh_key_path="/etc/root_key"` | name |
| `Secret` | `name`, `targets: tuple[SecretFile \| SecretEnv, ...]` (at least one), `required=True`, `schema: Schema \| None = None` | (inside `Secrets`) |
| `SecretFile` | `path`, `mode=0o400`, `owner=None` | |
| `SecretEnv` | `name`, `service=None` (`None`: global environment) | |
| `Schema` | `kind="string"` (`"string"` or `"json"`), `min_length=None`, `max_length=None`, `pattern=None`, `enum=()` | |
| `RuntimeTools` | `source: Git`, `key_config="/etc/tdx/key-gen.yaml"`, `disk_config="/etc/tdx/disk-setup.yaml"`, `secret_config="/etc/tdx/secrets.yaml"`, `secret_manifest="/etc/tdx/secrets.json"` | one per variant |

`Disk.key` and `Secrets.store` take the declaration object, not its name. Without `RuntimeTools` the tools build from `tundra-tools` `master`.

A variant may declare several `Secrets`. A single one uses the `RuntimeTools` `secret_config` and `secret_manifest` paths. With more than one, each writes `<stem>-<name>` paths instead (`/etc/tdx/secrets-api.yaml`, `/etc/tdx/secrets-api.json` for `Secrets("api")`) and gets its own `secret-delivery setup` runtime-init step. Two declarations whose paths overlap (a config path another `Secrets` or a `RuntimeTools` path writes, or a `SecretFile` path another `Secrets` delivers) raise `ValidationError` naming both.

## Sources and builds

| Type | Fields (defaults) |
|---|---|
| `Git` | `url`, `ref`, `subdir=None` (relative), `submodules=False` |
| `Http` | `url`, `sha256=None` (64 lowercase hex) |
| `Install` | `source` (relative to the build's source tree), `destination` (absolute), `mode=0o755`, `directory=False` (requires `mode=None`) |
| `Build` | `name`, `source: Git \| Http`, `script=None`, `install: tuple[Install, ...]` (at least one), `packages=()`, `env: Pairs = ()`, `cache_key=None`, `recipe: Go \| Cargo \| Dotnet \| None = None` |
| `Go` | keyword-only: `output`, `package="./..."`, `ldflags="-s -w -buildid="`, `tags=()`, `env: Mapping = {}`, `packages=("golang",)`, `output_dir="./build"`, `mkdir=True` |
| `Cargo` | keyword-only: `output`, `bin=None`, `package=None`, `features=()`, `profile="release"`, `env: Mapping = {}`, `packages=("cargo",)` |
| `Dotnet` | keyword-only: `project`, `output`, `configuration="Release"`, `runtime="linux-x64"`, `env: Mapping = {}`, `packages=("dotnet-sdk-8.0",)`, `restore_args=()`, `properties: Mapping = {}` |

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

`Build` identity is its name. `cache_key=None` derives `<name>-<url digest>-<ref>`; install destinations of one build need distinct file names.

## Resolution

```python
resolve(recipe: Recipe, *, variant: str) -> Resolved
resolve_all(recipe: Recipe) -> tuple[Resolved, ...]
identity(item: Declaration) -> tuple[str, ...]
```

`resolve` raises `ValidationError` on the first error-level diagnostic (the message names its code and how many more there are). `Resolved` has `variant: str`, `target: Target` (the first output), `targets: tuple[Target, ...]`, `items: tuple[Declaration, ...]` and `fragments: tuple[str, ...]` (the names of the fragments included).

`Diagnostic` has `code: str`, `message: str`, `level="error"` (`"error"`, `"warning"`, `"info"`), `variant=""` and `subject=""`. A fragment check may leave `variant` empty; resolution fills it in.

## Lifecycle

```python
lint(recipe, *, variants=None, lock=None) -> tuple[Diagnostic, ...]
compile(recipe, *, variants=None, lock=None) -> Tree
diff(tree: Tree, against: Tree | Path) -> str
lock(recipe, *, previous=None, update=(), offline=False, resolver=None, variants=None) -> Lock
lock_status(recipe, locked: Lock, *, variants=None, resolver=None) -> tuple[Diagnostic, ...]
read_lock(path: Path) -> Lock
write_lock(locked: Lock, path: Path) -> None
bake(recipe, *, locked: Lock, backend: Backend, out: Path, variants=None, progress=None) -> tuple[Artifact, ...]
read_artifacts(manifest: Path) -> tuple[Artifact, ...]
measure(artifact, *, scheme="rtmr", allow_placeholder=False) -> Measurements
deploy(artifact, *, using: Qemu | Azure | Gcp, allow_placeholder=False, adapter=None) -> Deployment
doctor(backend: Backend, *, runner=None) -> tuple[Diagnostic, ...]
load(path, *, attribute="recipe", extra_paths=()) -> Recipe
lower(recipe, *, variants=None)  # internal: the compiler's image for the recipe
```

`variants=None` means every declared variant; unknown names raise `ValidationError`.

- **`lint`** returns resolution and fragment-check diagnostics first, in resolution order. When none is an error it adds the compiler's rules on the lowered recipe, sorted by variant, level and code. With `lock`, drift is added as `lock-changed`, `lock-added` and `lock-removed` diagnostics whose `subject` is the section.
- **`compile`** returns the tree in memory. Without `lock` no lockfile is consulted, so source builds use their refs; with one, its pins apply.
- **`diff`** is a unified diff from `against` (a `Tree` or a directory) to `tree`, empty when they match. Variant directories in `against` that `tree` does not hold are not compared.
- **`lock`** keeps every pin in `previous` whose source is unchanged and resolves the rest, plus the sources named in `update` (unknown names raise). The default lookup runs `git ls-remote` for a git ref and hashes the download for an `Http` source without `sha256`; `resolver`, a function from source to pin, replaces it; `offline=True` fails for any source without a previous pin. Every source is tried before failing: one `LockfileError` lists all of them (`.failures`, name to `SourceError`), with reasons `ref '<ref>' not found`, `repository unreachable: <git stderr>`, `HTTP <status>` or `timed out after 60s` (every network call times out after 60s, and git never prompts for credentials).
- **`lock_status`** is the drift between the recipe and `locked`, one diagnostic per section; empty when current. With `resolver` (as for `lock`) it also reports git refs that moved since the lock, as `sources.<name>`. When `variants` leaves out some declared variant, see [subsets](#locks-and-variant-subsets).
- **`bake`** writes `locked` to `out/tundravm.lock` when that file is absent or identical; a different lockfile already there is left alone and the bake reads a scratch copy of `locked`. It bakes frozen into `out` and writes `out/bake-result.json`, whose `declarative.lockfile` is the path of the lockfile it used (`null` for a scratch copy). It fails before building on lint errors (`LintError`) or drift (`LockfileError`); a `locked` of every variant covers `variants=` naming a [subset](#locks-and-variant-subsets). `progress` receives the CLI's progress lines.
- **`read_artifacts`** reads `bake-result.json` (or the directory holding it).
- **`measure`** derives expected measurements with `measured-boot` or `dstack-mr`; without one it raises `MeasurementError` unless `allow_placeholder`, which also emits a `PlaceholderMeasurementWarning`. Simulated artifacts are refused unless `allow_placeholder`.
- **`deploy`** deploys with the target's adapter. The `using` type must match `artifact.target`; simulated artifacts are refused unless `allow_placeholder`. `adapter` replaces the default adapter (tests).
- **`doctor`** returns one `tool-missing` diagnostic per missing host tool of the backend (a warning when the tool is optional).
- **`load`** imports a recipe file and returns its `Recipe`; `attribute` may also name a zero-argument factory, and `attribute=None` discovers it as the [CLI does](cli.md#recipe-files). `load_recipe(path, *, attr=None, extra_paths=())` is the same with CLI discovery by default.

### Result and input types

| Type | Fields |
|---|---|
| `Tree` | `entries: tuple[Entry, ...]`, `digest: str`, `variants: tuple[str, ...]`; `write(path)` writes it, replacing its variant directories and dropping stale ones |
| `Entry` | `path`, `content: bytes \| None`, `mode: int`, `symlink: str \| None = None` (a directory has neither content nor symlink) |
| `Lock` | `recipe_digest`, `sections: Pairs` (section to digest, see [below](#locks-and-variant-subsets)), `pins: tuple[Pin, ...]`, `compiler_version`; `text()` is the serialized lockfile |
| `Pin` | `identity` (build name, or URL for anonymous fetches), `source: Git \| Http` (the resolved commit or hash), `digest` |
| `Backend` | `kind` (`"lima"`, `"nix"`, `"local"`, `"inprocess"`), `cpus=2`, `memory="4GiB"`, `disk="40GiB"` (the last three for Lima) |
| `Artifact` | `path`, `variant`, `target`, `sha256`, `recipe_digest`, `lock_digest`, `tree_digest`, `simulated=False` |
| `Measurements` | `scheme` (`"rtmr"`, `"azure"`, `"gcp"`), `values: Pairs`, `tool` (`"measured-boot <version>"`, `"dstack-mr <version>"` or `"placeholder"`), `artifact_digest`; `to_json(path=None) -> str` is the four fields as JSON (sorted keys, trailing newline), also written to `path` when given; `verify(expected: Mapping[str, str]) -> tuple[str, ...]` is the sorted registers whose value differs from `expected` (a register only one side has counts), empty when all match |
| `Qemu` | `memory="2G"`, `cpus=2`, `ssh_port=2222`, `tdx=False`, `daemonize=True` |
| `Azure` | `storage_account`, `resource_group="tdx-vms"`, `location="eastus"`, `vm_size="Standard_DC2s_v3"` |
| `Gcp` | `project`, `bucket`, `zone="us-central1-a"`, `machine_type="n2d-standard-2"` |
| `Deployment` | `id`, `target`, `endpoint: str \| None`, `metadata: Pairs` |

```python
from pathlib import Path
from tundravm import Backend, bake, compile, lint, lock, read_artifacts, write_lock
from tundravm.declarative import measure

assert not [d for d in lint(recipe) if d.level == "error"]
locked = lock(recipe)
write_lock(locked, Path("build/tundravm.lock"))
compile(recipe, lock=locked).write(Path("mkosi"))
artifacts = bake(recipe, locked=locked, backend=Backend("inprocess"), out=Path("build"))
default = next(a for a in artifacts if a.variant == "default")
measured = measure(default, allow_placeholder=True)
measured.to_json(Path("build/default.measurements.json"))
assert measured.verify(dict(measured.values)) == ()
```

### Locks and variant subsets

`Lock.sections` holds one digest per recipe section: the recipe-wide `base`, `arch`, `default_profile` and `init_scripts`, and `variants.<variant>.<key>` per variant, for `packages`, `build_packages`, `files`, `skeleton_files`, `users`, `services`, `hooks`, `phases`, `debloat`, `partitions`, `repositories`, `secrets`, `templates`, `build_sources`, `source_builds`, `output_targets`, and `extends` for a variant with a parent. Drift diagnostics name these sections in `subject`. The lockfile is version 3; version 2 files, which named the per-variant sections `profiles.<variant>.<key>`, load with the names mapped and the digests unchanged.

A lock of every variant covers any subset. When `variants` leaves out a declared variant, `lock_status` and the frozen check in `bake` compare only the selected variants' sections and the recipe-wide ones; the lock's sections for the other variants, and sources pinned only for them, are not reported. The whole-recipe digest is compared only when the selection is every variant the lock holds. A lock written for a subset (`lock(recipe, variants=("default",))`) does not cover a bake of more variants: the missing variants' sections are `lock-added`.

## Shipped fragments

| Class | Fragment name | Declares |
|---|---|---|
| `Tdxs(*, source=TUNDRA_TOOLS, issuer="tdx", validator=None, expected_measurements=(), check_revocations=False, get_collateral=False, verify_imds=False, verify_identity_token=False, after_init=False)` | `tdxs` | Go build packages, `Build("tdxs")`, `/etc/tdxs/config.yaml`, the socket-activated `tdxs.service`/`tdxs.socket`, `Group("tdx")`, `User("tdxs")` |
| `DevTools(*, root_password="tdx")` | `devtools` | Debugging packages, a serial console unit, root password login. Never ship it. |
| `EfiStub(*, snapshot, version)` | `efi-stub` | A postinst hook installing `systemd-boot-efi` `version` from a Debian snapshot |
| `Backports(*, mirror=None, release=None)` | `backports` | A sync hook generating backports and sid apt sources, plus `Setting("Build", "SandboxTrees", ...)` |

`issuer`/`validator` are `"tdx"`, `"azure"`, `"gcp"`, `"simulator"` or `None`. `TUNDRA_TOOLS` is `Git("https://github.com/Hyodar/tundra-tools.git", "master")`. Each class is an instance of `Fragment`, so it goes wherever a `Fragment` does: in `common`, in a variant's `add`, or among another fragment's `items`.

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

The examples ship more: `NethermindBase(*, snapshot=PINNED_MIRROR)` in [`examples/nethermind_tdx.py`](../examples/nethermind_tdx.py), and `Raiko(*, source)`, `TaikoClient(*, source)`, `Nethermind(*, source)` in [`examples/fragments/`](../examples/fragments/).

## Errors

Every SDK error subclasses `TdxError(message, *, code, hint=None, context=None)`, which has `code`, `hint`, `context` and `to_dict()`. The CLI prints them as `error [CODE]: message`, the hint and the context, and exits 2.

| Class | Code | Raised when |
|---|---|---|
| `ValidationError` | `E_VALIDATION` | A malformed declaration, a resolution error, a recipe that cannot lower, an unknown variant, a bad recipe file |
| `LintError` | `E_LINT` | `bake` refused a recipe with error-level compiler findings |
| `LockfileError` | `E_LOCKFILE` | A missing or unreadable lockfile, a frozen bake of a recipe that drifted from it, or `lock` could not resolve some sources (`.failures`) |
| `SourceError` | `E_SOURCE` | A source could not be resolved; `.source`, `.reason` |
| `ReproducibilityError` | `E_REPRODUCIBILITY` | Two builds that should match did not |
| `BackendExecutionError` | `E_BACKEND_EXECUTION` | The backend failed or its tools are unusable |
| `MeasurementError` | `E_MEASUREMENT` | No measurement tool, or a simulated artifact, without `allow_placeholder` |
| `DeploymentError` | `E_DEPLOYMENT` | A simulated artifact, a target mismatch, or a missing target tool |
| `PolicyError` | `E_POLICY` | A `Policy` setting refused the operation |
| `StateError` | `E_STATE` | `bake-result.json` is missing or unreadable, or has no artifact for the requested variant/target |

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
| `source-unpinned` | warning (error / info by policy) | A source build has no pin in `build/tundravm.lock` |
| `disk-key-path-mismatch` | warning | A disk reads a key file the key does not write |
| `key-pipe-outside-run` | info | A pipe key's path is outside `/run` |
| `variant-empty` | info | A variant declares nothing of its own |

Lock drift (from `lint(lock=)` and `lock_status`): `lock-changed`, `lock-added`, `lock-removed` (each with the section as `subject`), and `lock-stale` when every section matches but the whole-recipe digest does not. Host tools (from `doctor`): `tool-missing`.

## Policy

`Policy(require_frozen_lock=False, mutable_ref_policy="warn", network_mode="online")`. See [policy](policy.md).

## Testing helpers

`tundravm.testing` (see [testing](testing.md)); `variants` is a name, a sequence of names, or `None` for every variant:

```python
compile_tree(recipe, *, variants=None, path=None) -> CompiledTree
assert_clean(diagnostics_or_recipe, /, *, variants=None, allow=(), strict=None) -> diagnostics
assert_diagnostic(diagnostics_or_recipe, code, /, *, level=None, variant=None, subject=None, variants=None) -> Diagnostic
assert_tree(tree: Tree, golden: str | Path, *, update=None) -> None
assert_tree_matches(recipe, golden_dir, *, variants=None, update=None) -> TreeDiff
fake_bake(tree: Tree, *, variant: str, target: Target, out: str | Path) -> Artifact
bake_in_process(recipe, *, out=None, variants=None, locked=None) -> tuple[Artifact, ...]
fake_fragment(name="fake", *, packages=(), files=None, init=None, priority=50, requires=(), checks=()) -> Fragment
recipe_file(tmp_path: Path, source: str, name="recipe.py") -> Path
run_cli(*argv) -> tuple[int, str, str]
```

Pytest fixtures (plugin `tundravm`, loaded automatically): `recipe`, `compiled`, `run_cli`.
