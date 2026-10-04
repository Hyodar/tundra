# Concepts

tundravm turns an immutable Python value, a `Recipe`, into a reproducible mkosi project tree, then into bootable TDX disk images, their expected measurements, and deployments. This page explains the model. The [API reference](api.md) lists every field; the [design record](design/declarative-api.md) explains why it is shaped this way.

## The seven concepts

| Concept | Type | Role |
|---|---|---|
| Recipe | `Recipe` | The whole image: recipe-wide settings, the `common` fragment and the variants |
| Declaration | `Package`, `File`, `Service`, `Unit`, `Key`, `Disk`, `Build`, ... | One typed fact about the image, identified by type plus natural key |
| Fragment | `Fragment` | A named group of declarations and nested fragments, with `requires` and `checks` |
| Variant | `Variant` | An overlay on a parent (`add`, `replace`, `remove`, `target`); one mkosi directory and artifact each |
| Lock | `Lock` | Section digests of the resolved recipe plus one pin per source build |
| Tree | `Tree` | The compiled mkosi project, in memory until written; has a digest |
| Artifact | `Artifact` | A baked disk image of one variant and target, with the digests it came from |

Every one of them is a frozen dataclass. Nothing in a recipe is mutated after construction: a fragment function returns a new `Fragment`, a variant describes a change instead of applying it, and the lifecycle functions take values and return values.

```python
from tundravm import Fragment, Package, Recipe, Variant

recipe = Recipe(
    name="node",
    common=Fragment("node", items=(Package("curl"),)),
    variants=(Variant("default", target="qemu"),),
)
```

`Recipe` fields: `name`, `common`, `variants` (default: one `Variant("default", target="qemu")`), `base` (`debian/trixie`), `arch` (`x86_64` or `aarch64`), `mirror`, `tools_mirror`, `epoch` (`0`), `mkosi` (compiler settings: `Mkosi(layout=, dialect=, ...)`) and `policy` (a [`Policy`](policy.md), or `None` for the defaults).

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

## Lowering and compiling

`compile(recipe)` resolves each variant and lowers it onto the internal compiler, which emits one mkosi directory per variant:

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

Lowering is deterministic and never touches the network; `Path` contents (file sources, unit files, kernel configs) are read at lowering time. The result is a `Tree`: entries (path, bytes, mode, symlink) plus a digest over all of them. `Tree.write(path)` writes it, replacing the variant directories it holds.

The `Mkosi` setting picks the emission: `layout="directories"` and `dialect="current"` by default. `dialect="nethermind-v1"` spells user and group creation as postinst lines and leaves build hooks unmarked, which is what the historical nethermind-tdx tree expects (see [`design/lowering-nethermind-v1.md`](design/lowering-nethermind-v1.md)).

## Runtime init

Some declarations need work at boot, before services start: generating keys, opening and mounting encrypted disks, receiving secrets. tundravm collects that work into one script, `/usr/bin/runtime-init`, run by `runtime-init.service`.

The script is a sequence of steps sorted by priority, lowest first:

| Step | Priority | Present when the variant declares |
|---|---|---|
| `keys` | 10 | a `Key` |
| `disks` | 20 | a `Disk` |
| `secrets` | 30 | a `Secrets` |
| your `Init` | `priority=` (default 100) | that `Init` |

`Init(name, script, priority=100, after=())` adds a step. `after` names other steps, including the built-in `keys`, `disks` and `secrets`, and only orders steps of equal priority; an `after` that points to a step with a higher priority is `init-order`, and one that names a missing step is `init-after-undefined`. The names `keys`, `disks` and `secrets` are reserved.

```python
Init("exporter-directory", "install -d -m 0755 /persistent/exporter\n", priority=25, after=("disks",))
```

## Services and units

Two declarations put systemd units in the image:

- **`Service`: a generated service.** `Service("app", "/usr/bin/app", user="app", restart="on-failure")` renders `/usr/lib/systemd/system/app.service` from typed fields (`exec_start`, `user`, `group`, `env`, `env_file`, `working_dir`, `exec_start_pre`, `after`/`requires`/`wants`, `wanted_by`, `type`, `restart`, `limits`, `kill_mode`, `timeout_stop`, `security`) and enables it. With `after_init=True`, the default, it also waits for `runtime-init.service` whenever the variant has a runtime-init step.
- **`Unit`: verbatim text, or a packaged unit's state.** `Unit("app.service", content, enabled=True)` ships `content` (a string or a `Path` to read) exactly as written; the name needs its type suffix. `Unit("openntpd.service", enabled=True)` or `Unit("ssh.socket", enabled=False, masked=True)` only changes the state of a unit a package ships; it must enable, disable or mask something.

`Unit(..., after_init=True)` adds `After=runtime-init.service` and `Requires=runtime-init.service` to the shipped text's `[Unit]` section. If the variant has no runtime-init step, `lint` warns with `unit-after-init-without-init`. Apart from that, tundravm never edits `Unit` text. Use `Service` when the fields cover what you need and `Unit` when you want the exact bytes (the shipped `tdxs()` and `devtools()` fragments use `Unit`).

`Template(path, template, variables=())` is a file rendered with `str.format_map(variables)` at lowering time, for config files that differ only in a few values.

## Keys, disks and secrets

These three declarations reference each other by object, so a reference cannot dangle silently:

```python
key = Key("key_persistent")                                    # random 64 bytes, sealed in the TPM
disk = Disk("disk_persistent", mount="/persistent", key=key)   # LUKS2, formatted on first failure to open
secrets = Secrets(store=disk, entries=(
    Secret("api_token", targets=(SecretFile("/run/secrets/api-token"), SecretEnv("API_TOKEN"))),
))
```

- `Key(name, output=None, strategy="random"|"pipe", persist_in_tpm=True, size=64, pipe=None)`.
- `Disk(name, mount, device=None, key=None, mapper=None, format="on_fail", directories=("ssh", "data", "logs"))`. `device=None` picks the largest unpartitioned disk; `key=None` is a plain disk; `key` may also be a `Path` to a key file.
- `Secrets(name="secrets", entries=(), store=None, host="0.0.0.0", port=8080, ssh_directory="/root/.ssh", ssh_key_path="/etc/root_key")` receives secrets over HTTP at boot, validates them against each `Secret`'s `Schema`, and delivers them to files and environment variables.

The variant must declare the very key a disk uses and the very disk secrets are stored on: `disk-key-undefined` / `disk-key-mismatch` and `secret-store-undefined` / `secret-store-mismatch` report the gaps.

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

The source is `Git(url, ref, subdir=None, submodules=False)` or `Http(url, sha256=None)`. `script` runs in the fetched source with `env` exported; `install` copies results into the image (`Install(source, destination, mode=0o755, directory=False)`). `packages` names build-time packages. Built outputs are cached in the build directory under `cache_key` (derived from name, URL and ref when omitted).

The recipe records the symbolic source (`master`), never a commit. `tundravm lock` resolves each `Git` ref to a commit and each `Http` without `sha256` to a hash and stores the pins in the lockfile; compile and bake then fetch exactly those. A build without a pin is the `source-unpinned` warning.

## Variants and targets

A variant produces one mkosi directory and, when baked, one artifact. Its `target` (`qemu`, `azure` or `gcp`, inherited from the parent, `qemu` at the root) decides the disk format and adds the platform integration: an `azure` variant gets the Azure provisioning service and `dmidecode`; a `gcp` variant gets the GCP equivalents.

```python
variants=(
    Variant("default", target="qemu"),
    Variant("azure", parent="default", target="azure"),
    Variant("gcp", parent="default", target="gcp"),
    Variant("devtools", parent="default", add=devtools()),
)
```

`targets=("azure", "gcp")` gives one variant several outputs (one disk image each); `target=X` is shorthand for `targets=(X,)`, and passing both is an error. A child cannot drop a cloud target it inherits (`target-inconsistent`): give it `parent=None` or a qemu parent instead.

How a variant lowers depends on what it does:

- A variant whose parent is `base` or the default variant, and which only adds declarations or replaces `File`, `User`, `Group`, `Partition`, `Repository` or `Debloat`, lowers as an overlay of the default variant's directory. This keeps committed trees such as surge's byte-identical.
- Every other variant (chained onto another variant, parentless, removing anything, replacing other kinds, changing keys, disks or secrets, or mounting its own `BuildSources`) is lowered standalone from its own resolved declarations.

These still raise `ValidationError` when you compile, lint or bake:

- a variant that declares or removes a `Setting` (other than `Build.BuildSources`) or the `Kernel`: both are recipe-wide, so declare them in `common`;
- a standalone-lowered variant under `Mkosi(layout="native")`, because mkosi applies the root configuration to every profile;
- more than one `Secrets` per variant;
- a default variant whose parent is another variant;
- a `Kernel` from `Http` or a git ref other than `v<version>`.

## Lifecycle

```
Recipe ──compile──▶ Tree ──write──▶ mkosi/            (committed, compile --check)
   │
   └──lock──▶ Lock ──write_lock──▶ build/tundravm.lock  (committed, lock --check)
                │
Recipe + Lock + Backend ──bake──▶ Artifact(s) + build/bake-result.json
                                     │
                                     ├──measure──▶ Measurements
                                     └──deploy───▶ Deployment
```

| Step | Function | CLI | Needs |
|---|---|---|---|
| Inspect | `resolve`, `resolve_all` | `inspect` | the recipe |
| Lint | `lint(recipe, lock=None)` | `lint` | the recipe (and a lock for drift) |
| Compile | `compile(recipe, lock=None) -> Tree` | `compile` | the recipe; a lock applies its pins |
| Lock | `lock(recipe, previous=None) -> Lock` | `lock` | the network, unless every source is already pinned |
| Bake | `bake(recipe, locked=, backend=, out=) -> tuple[Artifact, ...]` | `bake` | a backend |
| Measure | `measure(artifact, scheme="rtmr") -> Measurements` | `measure` | `measured-boot` or `dstack-mr`, or `allow_placeholder` |
| Deploy | `deploy(artifact, using=Qemu()/Azure(...)/Gcp(...)) -> Deployment` | `deploy` | the target's tool (`qemu-system-x86_64`, `az`, `gcloud`) |

`bake` writes `out/bake-result.json`, the manifest `read_artifacts()`, `tundravm measure` and `tundravm deploy` read, so measuring and deploying work in a later process. Each `Artifact` carries the recipe digest, the lockfile digest and the tree digest it was built from.

The in-process backend writes simulated artifacts (`Artifact.simulated`). `measure` and `deploy` refuse them unless you pass `allow_placeholder=True` (`--allow-placeholder`), and placeholder measurements say so (`tool="placeholder"`).

## Backends

| Backend | `Backend(kind)` | Runs mkosi |
|---|---|---|
| `LimaMkosiBackend` | `lima` | inside a Lima VM with Nix (macOS and Linux) |
| `NixMkosiBackend` | `nix` | through `nix develop` on a Linux host |
| `LocalLinuxBackend` | `local` | from `PATH` on Linux (mkosi v25+, `sudo` or `unshare`) |
| `InProcessBackend` | `inprocess` | not at all: writes simulated artifacts for tests |

A recipe file names its backend in a module-level `backend` variable; the Python `bake()` takes a `Backend` value. Nothing before `bake` needs a backend.

## Measurements

`measure(artifact, scheme=)` derives the values a verifier should expect:

- `rtmr` uses `measured-boot` or `dstack-mr` on `PATH` and returns RTMR values; `Measurements.tool` names the tool and version.
- `azure` and `gcp` have no local tool yet; they return placeholders only with `allow_placeholder=True`.

Placeholder values are derived from the artifact digest. They are never real measurements and must never go into an attestation policy.
