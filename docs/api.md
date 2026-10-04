# API reference

The public API is the declarative one in `tundravm.declarative`. Most names are also exported from `tundravm`; the exceptions are listed under [Imports](#imports). Every record below is a frozen, slotted dataclass: lists passed for tuple fields are frozen into tuples, and `__post_init__` raises `ValidationError` for malformed values.

For the model behind these types see [concepts](concepts.md); for writing reusable fragments see [writing fragments](module-authoring.md).

## Imports

```python
from tundravm import Recipe, Fragment, Variant, Package, File, Service, compile, lint, lock, bake
from tundravm.declarative.utils import DevTools, Tdxs
from tundravm.declarative import diff, measure, deploy
```

`tundravm` exports the declarations, the recipe types, the lifecycle functions and result types, `Policy`, the [errors](#errors), `load` and `load_recipe`. Only in `tundravm.declarative`: `diff`, `measure` and `deploy` (at the top level those names are the `tundravm.diff`, `tundravm.measure` and `tundravm.deploy` subpackages), `identity`, and the type aliases `Check`, `Pairs`, `Phase` and `Target`. The shipped fragments `Tdxs`, `DevTools`, `EfiStub` and `Backports` live in `tundravm.declarative.utils`.

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
| `epoch` | `int \| None` | `0`: reproducible (`SOURCE_DATE_EPOCH=0`, stable seed, `IMAGE_VERSION` stripped); `None`: not reproducible; other values set `SOURCE_DATE_EPOCH` |
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
| `layout` | `"directories" \| "native"` | `"directories"` |
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
| `Setting` | `section`, `key`, `values: tuple[str, ...]` | section, key |
| `Kernel` | `version`, `source: Git \| Http`, `config: Path \| None = None`, `cmdline=""`, `tdx=True` | one per variant |

No `Debloat` means the compiler default (debloat enabled). `Setting` is the mkosi escape hatch; supported keys are `Output.Seed`, `Output.OutputDirectory`, `Output.CompressOutput`, `Output.ManifestFormat`, `Build.PackageCacheDirectory`, `Build.WithNetwork`, `Build.Environment`, `Build.SandboxTrees` and `Content.CleanPackageMetadata`; others fail to lower. Settings are recipe-wide, except `Setting("Build", "BuildSources", ("src[:dest]", ...))`, which mounts host directories into one variant's build. A `Kernel` is recipe-wide too, and must come from a git repository tagged `v<version>`.

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

## Sources and builds

| Type | Fields (defaults) |
|---|---|
| `Git` | `url`, `ref`, `subdir=None` (relative), `submodules=False` |
| `Http` | `url`, `sha256=None` (64 lowercase hex) |
| `Install` | `source` (relative to the build directory), `destination` (absolute), `mode=0o755`, `directory=False` (requires `mode=None`) |
| `Build` | `name`, `source: Git \| Http`, `script`, `install: tuple[Install, ...]` (at least one), `packages=()`, `env: Pairs = ()`, `cache_key=None` |

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
lock_status(recipe, locked: Lock, *, variants=None) -> tuple[Diagnostic, ...]
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
- **`lock`** keeps every pin in `previous` whose source is unchanged and resolves the rest, plus the sources named in `update` (unknown names raise). `resolver` replaces the network lookup; `offline=True` fails for any source without a previous pin.
- **`lock_status`** is the drift between the recipe and `locked`, one diagnostic per section; empty when current.
- **`bake`** writes `locked` to `out/tundravm.lock`, bakes frozen into `out` and writes `out/bake-result.json`. It fails before building on lint errors (`LintError`) or drift (`LockfileError`). `progress` receives the CLI's progress lines.
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
| `Lock` | `recipe_digest`, `sections: Pairs` (section to digest), `pins: tuple[Pin, ...]`, `compiler_version`; `text()` is the serialized lockfile |
| `Pin` | `identity` (build name, or URL for anonymous fetches), `source: Git \| Http` (the resolved commit or hash), `digest` |
| `Backend` | `kind` (`"lima"`, `"nix"`, `"local"`, `"inprocess"`), `cpus=2`, `memory="4GiB"`, `disk="40GiB"` (the last three for Lima) |
| `Artifact` | `path`, `variant`, `target`, `sha256`, `recipe_digest`, `lock_digest`, `tree_digest`, `simulated=False` |
| `Measurements` | `scheme` (`"rtmr"`, `"azure"`, `"gcp"`), `values: Pairs`, `tool` (`"measured-boot <version>"`, `"dstack-mr <version>"` or `"placeholder"`), `artifact_digest` |
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
print(measure(default, allow_placeholder=True).values)
```

## Shipped fragments

| Class | Fragment name | Declares |
|---|---|---|
| `Tdxs(*, source=TUNDRA_TOOLS, issuer="tdx", validator=None, expected_measurements=(), check_revocations=False, get_collateral=False, verify_imds=False, verify_identity_token=False, after_init=False)` | `tdxs` | Go build packages, `Build("tdxs")`, `/etc/tdxs/config.yaml`, the socket-activated `tdxs.service`/`tdxs.socket`, `Group("tdx")`, `User("tdxs")` |
| `DevTools(*, root_password="tdx")` | `devtools` | Debugging packages, a serial console unit, root password login. Never ship it. |
| `EfiStub(*, snapshot, version)` | `efi-stub` | A postinst hook installing `systemd-boot-efi` `version` from a Debian snapshot |
| `Backports(*, mirror=None, release=None)` | `backports` | A sync hook generating backports and sid apt sources, plus `Setting("Build", "SandboxTrees", ...)` |

`issuer`/`validator` are `"tdx"`, `"azure"`, `"gcp"`, `"simulator"` or `None`. `TUNDRA_TOOLS` is `Git("https://github.com/Hyodar/tundra-tools.git", "master")`. Each class is a `Composite`, a `Fragment` subclass: the keyword arguments are its dataclass fields (its `repr` and equality), and `name`/`items` come from its `compose()`.

The examples ship more: `NethermindBase(*, snapshot=PINNED_MIRROR)` in [`examples/nethermind_tdx.py`](../examples/nethermind_tdx.py), and `Raiko(*, source)`, `TaikoClient(*, source)`, `Nethermind(*, source)` in [`examples/fragments/`](../examples/fragments/).

## Errors

Every SDK error subclasses `TdxError(message, *, code, hint=None, context=None)`, which has `code`, `hint`, `context` and `to_dict()`. The CLI prints them as `error [CODE]: message`, the hint and the context, and exits 2.

| Class | Code | Raised when |
|---|---|---|
| `ValidationError` | `E_VALIDATION` | A malformed declaration, a resolution error, a recipe that cannot lower, an unknown variant, a bad recipe file |
| `LintError` | `E_LINT` | `bake` refused a recipe with error-level compiler findings |
| `LockfileError` | `E_LOCKFILE` | A missing or unreadable lockfile, or a frozen bake of a recipe that drifted from it |
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
| `output-target-platform-mismatch` | warning | A cloud output without its platform integration |
| `platform-target-missing` | warning | Platform integration without its output target |
| `init-priority-collision` | warning | Several runtime-init steps share a priority |
| `debloat-removes-needed-unit` | warning | Debloat masks a unit a service needs |
| `debloat-removes-declared-file` | warning | Debloat deletes a declared file at finalize |
| `secret-undelivered` | warning | A secret has no delivery target, or nothing delivers secrets at boot |
| `source-unpinned` | warning (error / info by policy) | A source build has no pin in `build/tundravm.lock` |
| `disk-key-path-mismatch` | warning | A disk reads a key file the key does not write |
| `key-pipe-outside-run` | info | A pipe key's path is outside `/run` |
| `profile-empty` | info | A variant declares nothing of its own |

Lock drift (from `lint(lock=)` and `lock_status`): `lock-changed`, `lock-added`, `lock-removed`, `lock-stale`. Host tools (from `doctor`): `tool-missing`.

## Policy

`Policy(require_frozen_lock=False, mutable_ref_policy="warn", require_integrity=True, network_mode="online")`. See [policy](policy.md).

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
