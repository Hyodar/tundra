# Changelog

## Unreleased

This release replaces the SDK's public API. An image is now an immutable value, a `Recipe`, and every lifecycle step is a function over it. The fluent `Image`/`Profile`/`Module` builder is gone from the public surface.

### Added

- **Declarative API** (`tundravm`, `tundravm.declarative`). A `Recipe` holds recipe-wide settings (`base`, `arch`, `mirror`, `tools_mirror`, `epoch`, `Mkosi`, `Policy`), a `common` `Fragment` and its `Variant`s. Declarations are frozen dataclasses that validate at construction: `Package`, `File`, `Directory`, `Template`, `Group`, `User`, `Service` (a generated unit), `Unit` (verbatim text or a packaged unit's state), `Hook`, `Init`, `Repository`, `Partition`, `Debloat`, `Setting`, `Kernel`, `Build` (`Git`/`Http` source, a `script` or a `Go`/`Cargo`/`Dotnet` recipe, `Install` map, `cache_key`), and the `Key`, `Disk` and `Secrets` trio (`Secret`, `SecretFile`, `SecretEnv`, `Schema`, `RuntimeTools`), which reference each other by object. Fragments group declarations and carry `requires` and `checks` (functions of the `Resolved` variant). Variants overlay a parent (`base`, another variant, or `None`) with `add`, `replace` and `remove`, and pick their output with `target` or `targets`. `resolve`/`resolve_all` expand fragments, apply ancestry and check references; identity is type plus natural key, so collisions are errors and changes are explicit. `tundravm.declarative.utils` ships the `Tdxs`, `DevTools`, `EfiStub` and `Backports` fragments.
- **Lifecycle functions** with explicit inputs and results: `lint(recipe, lock=)`, `compile(recipe, lock=) -> Tree`, `diff(tree, against)`, `lock(recipe, previous=, update=, offline=) -> Lock`, `lock_status`, `read_lock`/`write_lock`, `bake(recipe, locked=, backend=Backend(kind), out=) -> tuple[Artifact, ...]`, `read_artifacts`, `measure(artifact) -> Measurements`, `deploy(artifact, using=Qemu()/Azure()/Gcp()) -> Deployment`, `doctor(backend)`, `load(path)`. Artifacts record the recipe, lockfile and tree digests; simulated (in-process) artifacts are refused by `measure`/`deploy` unless `allow_placeholder=True`.
- **Build recipes**: `Build(name, source, script=None, install=(), packages=(), env=(), cache_key=None, recipe=None)` takes exactly one of `script` and `recipe`. `Go`, `Cargo` and `Dotnet` (exported from `tundravm`) render the toolchain's build command and carry their own `packages` and `env`; their output is at `build/<output>` (`Go`), `target/<profile>/<bin>` (`Cargo`) or `publish/<output>` (`Dotnet`) for `Install` to copy.
- **`Composite`** (`tundravm.declarative.utils`): a `Fragment` subclass whose configuration is its own dataclass fields and whose contents come from `compose()`. The shipped fragments are `Composite`s, and so are the examples'.
- **CLI grammar**: `init`, `inspect`, `lint`, `compile`, `diff`, `lock`, `bake`, `measure`, `deploy`, `doctor`, `ci`. Every recipe command takes a repeatable `--variant` and runs on every variant without it. `--format auto|text|json|github|markdown` for review output (`auto` picks GitHub annotations under `GITHUB_ACTIONS=true`). `lint` reports resolution, fragment-check and compiler diagnostics together. `bake` is frozen whenever `build/tundravm.lock` exists, takes `--backend lima|nix|local|inprocess`, and reports progress (`-v`, `-q`, `--json-logs`). `measure` and `deploy` read `bake-result.json` (`--scheme`, `--target`, `--param KEY=VALUE`, `--allow-placeholder`). `ci` runs `lint --strict`, `compile --check` and `lock --check`. `init --ci github` writes a workflow that calls it.
- **Testing**: `tundravm.testing` works on lifecycle values: `assert_clean`/`assert_diagnostic` take `lint()` diagnostics, `assert_tree(tree, golden)` compares every path, byte, exec bit and symlink (`TUNDRAVM_UPDATE_GOLDEN=1` rewrites), `compile_tree`, `fake_bake`, `bake_in_process`, `fake_fragment`, `recipe_file`, `run_cli`. Pytest fixtures `recipe`, `compiled`, `run_cli`.
- Lockfile section digests (`variants.<variant>.<section>`) and source pins (`fetches`); `lock --check` names the drifted sections, `lock --update NAME` re-resolves one source, `lock --offline` reuses pins.
- A lock written for every variant covers a subset: a frozen `bake --variant X`, `lock --check --variant X` and `lock_status(recipe, locked, variants=("X",))` compare only the selected variants' sections and the recipe-wide ones, and the whole-recipe digest only when the selection is every variant the lock holds.
- `lock_status(recipe, locked, *, variants=None, resolver=None)`: with a `resolver`, git refs that moved since the lock are reported too.
- `Measurements.to_json(path=None)` returns the measurements as JSON (and writes them to `path`); `Measurements.verify(expected)` returns the registers that differ from `expected`, empty when all match.
- Measurement provenance: `Measurements.tool` names `measured-boot`, `dstack-mr` or `placeholder`; placeholders print a banner and emit `PlaceholderMeasurementWarning`.
- `StateError` (`E_STATE`) for a missing or unreadable `bake-result.json`, `LintError` (`E_LINT`) for a bake refused by the linter.
- `Kernel` sources beyond tag `v<version>`: any git branch, tag or full commit hash, with `subdir` and `submodules`, or an `Http` tarball checked against its `sha256` (required, since the lockfile does not pin kernel sources). An `Http` kernel adds `curl` to the build packages.
- Per-variant settings and kernels: a variant whose `Setting`s or `Kernel` differ from the default variant's lowers standalone with its own `mkosi.conf` lines, kernel build and kernel config.
- A `Setting` without a compiler mapping is written verbatim into its variant's `mkosi.conf`, under its section. Keys the compiler writes itself (`Packages`, `Mirror`, `Format`, the phase script keys, ...) raise and name the declaration to use.
- Several `Secrets` per variant. With more than one, each writes `<stem>-<name>` config and manifest paths (`/etc/tdx/secrets-api.yaml`) and gets its own runtime-init step; overlapping paths raise naming both declarations.
- The default variant may have any parent; the parent lowers like any other variant.
- A `base`-parented variant may leave a cloud-targeted default variant's targets; it lowers standalone.
- `examples/surge-tdx-prover` is a declarative recipe that compiles byte-for-byte to the committed nethermind-tdx tree for `default`, `azure`, `gcp` and `devtools`; `examples/fragments` holds `Raiko`, `TaikoClient` and `Nethermind`, and `examples/nethermind_base.py` holds `NethermindBase`.

- `tundravm completion bash|zsh|fish` prints a completion script generated from the parser; bare `tundravm` prints help plus a quickstart; unknown verbs and flags exit 2 with a "did you mean" suggestion; `init` ends with a `doctor` probe of the chosen backend (`--no-doctor` skips it); `inspect --diff-variants A B` lists declarations that differ between two variants; `main(runner=)` for tests.
- Every error raised by the package carries a hint; `tests/lint/test_error_hints.py` enforces it.
- `tundravm lock` tries every source and reports all failures in one `E_LOCKFILE` error (`<name>: git <url> @ <ref>: <reason>` lines, `K of M sources resolved; nothing written.`); `LockfileError.failures` maps names to the new `SourceError` (`E_SOURCE`, `.source`, `.reason`). `lock --format github` adds an `::error title=E_SOURCE::` line per failure, and `lock --offline` lists every unpinned source at once.
- `init --template minimal|service|cloud|prover` and `init --list-templates`. `init` scaffolds a project: the recipe, `tests/test_NAME.py` (`--with-tests`/`--no-tests`), and `pyproject.toml` and `README.md` when absent.

- `kernel-missing` lint error: a bootable variant must declare `Kernel(...)` or install a `linux-image-*` package; `Setting("Content", "Bootable", ("no",))` builds a non-bootable disk instead. `init` templates install the Debian kernel (`linux-image-amd64`, `systemd-sysv`, `udev`, `kmod`, `systemd-boot-efi`) so they bake a bootable UKI.
- Examples consolidated into six numbered teaching recipes (`01_minimal` … `06_attestation`) plus `nethermind_base.py`, `fragments/` and the flagship; `examples/*.py` are loaded, linted and compiled by the test suite.

- `tundravm fetch RECIPE [--lockfile] [--out] [--variant]` checks every source build out on the host as the invoking user (git: pinned commit; http: sha256-verified download) into `build/.sources/<name>-<pin12>/`, idempotently; `bake` runs it first (`--no-fetch` to skip) and mounts `.sources` ephemerally into the build, so the sandbox never fetches sources and private repositories work. `lifecycle.fetch()` / `FetchedSource`; `bake(..., fetch=True)`; `BakeRequest.sources_dir`.

### Changed

- Current-dialect source hooks copy host-fetched checkouts instead of cloning; unpinned hooks fail with a pointer to `tundravm lock` and `tundravm fetch`; `$BUILDDIR` falls back to `$BUILDROOT/build` in source and kernel scripts (mkosi 26 sets it only with `BuildDirectory=`); existing locks for current-dialect recipes with source builds need re-locking.

- Lockfile version 1 → 3. Per-variant sections are named `variants.<variant>.<section>` (version 2 said `profiles.`). Version 2 files still load with their sections renamed and digests unchanged; version 1 files load too, and `lock --check` reports every section as added until you re-lock.
- User-facing output says variant: `inspect` prints `variant=NAME`, `Parent:` and `Fragments:` (JSON keys `variant`, `parent`, `fragments`; Markdown heading `## Variant`), the bake summary has a `variant` column and ends `baked N variants`, and `lint --format json` and `bake --json-logs` use `variant`. The lint code `profile-empty` is now `variant-empty`.
- `Recipe.epoch` other than `0` or `None` emits `SourceDateEpoch=<epoch>` as well as `SOURCE_DATE_EPOCH`.
- Diagnostic hints name the declarative API and current commands (frozen-lock policy, missing backend, `source-unpinned`, `init-priority-collision`, `disk-key-undefined`, `secret-store-undefined`).
- Recipe files bind a `Recipe` to `recipe` (or `RECIPE`, or a `build()` factory). A module-level `backend` is the bake backend. `load_recipe()` returns the `Recipe`.
- `Recipe.base` defaults to `debian/trixie`.
- `compile()` recreates each variant directory, so stale files are removed.
- The `BuildBackend` protocol has `requirements()`, which `doctor` probes.
- `Mkosi(layout="native")` refuses a recipe with standalone variants in one error that lists each offending variant with its reason (`'solo' (it is parentless)`).
- `inspect --format json`: the kernel's `source_repo` key is replaced by a `source` object (`repo`, `ref`, `subdir`, `submodules` for git; `url`, `sha256` for a tarball).
- The hint for a `Build` that passes `packages`/`env` beside a `recipe` names `Go`, `Cargo` or `Dotnet`, not the internal build class.
- `default_resolver` raises `SourceError` (was `ValidationError`), never prompts for git credentials, and times out network calls after 60s. An unpinned source drifts as `+ sources.<name>: source <name> is not pinned`.
- `init` starts from the `service` template by default; it prints the lint summary, then a numbered `next:` list, then the backend probe. The `uv sync` note is replaced by a note when `pyproject.toml` already exists (`uv add tundravm`, `uv add --dev pytest`).

### Removed

- The fluent `Image`, `Profile` and `Module` API (`img.install()`, `img.service()`, `img.profile()`, `img.apply()`, `Module` subclasses, `MkosiOptions`, `SourceBuild`/`GitSource`/`HttpSource`/`GoBuild`/`CargoBuild`/`DotnetBuild`/`ScriptBuild`). It remains as internal lowering machinery (`tundravm._image`, `tundravm._modules`, ...), not as API. Use `Recipe`, `Fragment` and the declarations.
- `tundravm.modules` and its module classes: `KeyGeneration`, `DiskEncryption` and `SecretDelivery` (use the `Key`, `Disk` and `Secrets` declarations), `AzurePlatform` and `GcpPlatform` (use `Variant(target="azure"|"gcp")`). `Tdxs` and `DevTools` are now fragments in `tundravm.declarative.utils`, next to `EfiStub` and `Backports`.
- CLI verbs `explain` (use `inspect`), `check` (use `lint`), `digest` (use `inspect --json`, key `digest`) and `new` (use `init`), with no aliases.
- `--profile`/`-p` and `--all-profiles` (use repeatable `--variant`; omit it for every variant), `bake --lock`/`--frozen`/`--force`, `measure --backend` (use `--scheme`), `deploy --memory`/`--cpus` (use `--param`), `measure --out`/`deploy --out` (pass the bake directory).
- The lint rules `output-target-platform-mismatch`, `platform-target-missing` and `secret-undelivered`, which a declarative recipe cannot trigger, and `backend-missing`: a recipe holds no backend, and `bake` reports a missing one.
- `tundravm.builders` and `tundravm.fetch`. Builds are `Build(script=...)` or `Build(recipe=Go(...) | Cargo(...) | Dotnet(...))`, and `tundravm lock` resolves pins itself (`git ls-remote` for a git ref, the sha256 of the download for http).
- The `cbor2` dependency, which nothing used: tundravm has no runtime dependencies.
- `tundravm.testing.FakeModule` (use `fake_fragment`) and the `image`/`inprocess_image` fixtures (use `recipe`).
- `Policy.require_integrity`, inert since `tundravm.fetch` went away. `Policy` has `require_frozen_lock`, `mutable_ref_policy` and `network_mode`.
- `SPEC.md`, which described the fluent API. The design record is `docs/design/declarative-api.md`.

### Fixed
- Debian snapshots on mkosi 26: `Recipe.snapshot` (a snapshot ID such as `20251113T083151Z`) lowers to mkosi's `Snapshot=`; `mirror`/`tools_mirror` are mirror roots that mkosi completes itself. Templates use a (snapshot, `systemd-boot-efi` version) pair that exists.
- `EfiStub` in the current dialect installs the package from `$BUILDROOT/` because `mkosi-chroot` mounts its own `/tmp`.
- `Backports()` pins backports to 200 and sid to 100 through `preferences.d`, so images no longer drift to sid.
- `Repository()` declarations reach the build's apt (written under `mkosi.sandbox/` in the current dialect) and, with `in_image=True` (default), the image's `/etc/apt`.
- The Azure provisioning unit depends on `network-online.target` unless the variant ships `network-setup.service`.
- `Backports()` works on mkosi 26: the current dialect writes `mkosi.sandbox/etc/apt/sources.list.d/debian-backports.sources` at compile time (mirror from the fragment, the recipe or deb.debian.org; release from the recipe base) instead of a sync hook that mkosi 26 cannot run.
- Azure/GCP conversions run inside mkosi's tools tree when one is used: the local backend adds `--tools-tree-package=qemu-utils,gdisk,parted`, reuses a cached tree only when it has those tools, and skips the host pre-check in that case (azure VHD and gcp tar.gz bake for real on a host without qemu-img).
- Current-dialect postoutput scripts name the UKI `${IMAGE_ID}${IMAGE_VERSION:+_$IMAGE_VERSION}`, so recipes without a version no longer die under `set -u`.
- `runtime-init.service` requires `network-setup.service` only when the variant ships that unit; otherwise it orders after `network-online.target`.
- `doctor` probes `pefile` under mkosi's own Python; the local backend adds the tools tree when the host lacks pefile for a bootable build.
- Images that declare users or groups install `passwd`, so `useradd` in postinst no longer exits 127 (the service template bakes for real).
- `bake` with the local backend fails before mkosi when an azure/gcp variant's disk tool (`qemu-img`/`sgdisk`) is missing on the host; `doctor` lists those tools for cloud variants.

- Local bakes: artifacts landed inside the mkosi tree and `bake-result.json` listed none, because mkosi received a relative `--output-dir`; all backends now pass absolute paths. Lima ignored the per-variant directory.
- A tools-tree bake left root-owned `mkosi.tools` in the compiled tree and the post-build tree read crashed with `PermissionError`; mkosi state now lives under `OUT/.mkosi/`, sudo output is chowned back, unreadable leftovers are warnings.
- `‣ Could not find ukify`: the local backend adds `--tools-tree=default` when the host lacks `ukify` and the recipe sets no `ToolsTree`; `doctor` checks `ukify`, `systemd-repart` and `apt`.
- A recipe file with a syntax, import or runtime error printed a Python traceback (exit 1); it is now `E_VALIDATION` (exit 2) with `location` and `error`, and `--traceback` re-raises.
- Recipes are compiled from source on every load, so a same-size edit within the same second is never hidden by stale `__pycache__`.

- `runtime-init.service` is registered once per variant, so compiling one variant and then all of them no longer fails with a duplicate service.
- A file removed from the recipe no longer lingers in the compiled tree or in `compile --check`.
- `tundravm ci` no longer reports false lock drift on recipes with runtime-init steps: its compile step works on a copy of the lowered recipe instead of changing what the lock step compares.
- A frozen bake of some variants against a lock of every variant no longer fails at `verify lockfile`.
- `bake --lockfile PATH` no longer overwrites a different `<out>/tundravm.lock`: it bakes against `PATH` and leaves that file alone. `bake-result.json` records the lockfile used as `declarative.lockfile`.
