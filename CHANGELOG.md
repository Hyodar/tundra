# Changelog

## Unreleased

This release replaces the SDK's public API. An image is now an immutable value, a `Recipe`, and every lifecycle step is a function over it. The fluent `Image`/`Profile`/`Module` builder is gone from the public surface.

### Added

- **Declarative API** (`tundravm`, `tundravm.declarative`). A `Recipe` holds recipe-wide settings (`base`, `arch`, `mirror`, `tools_mirror`, `epoch`, `Mkosi`, `Policy`), a `common` `Fragment` and its `Variant`s. Declarations are frozen dataclasses that validate at construction: `Package`, `File`, `Directory`, `Template`, `Group`, `User`, `Service` (a generated unit), `Unit` (verbatim text or a packaged unit's state), `Hook`, `Init`, `Repository`, `Partition`, `Debloat`, `Setting`, `Kernel`, `Build` (`Git`/`Http` source, `Install` map, `cache_key`), and the `Key`, `Disk` and `Secrets` trio (`Secret`, `SecretFile`, `SecretEnv`, `Schema`, `RuntimeTools`), which reference each other by object. Fragments group declarations and carry `requires` and `checks` (functions of the `Resolved` variant). Variants overlay a parent (`base`, another variant, or `None`) with `add`, `replace` and `remove`, and pick their output with `target` or `targets`. `resolve`/`resolve_all` expand fragments, apply ancestry and check references; identity is type plus natural key, so collisions are errors and changes are explicit. `tundravm.modules` ships `tdxs()`, `devtools()`, `efi_stub()` and `backports()` as fragment functions.
- **Lifecycle functions** with explicit inputs and results: `lint(recipe, lock=)`, `compile(recipe, lock=) -> Tree`, `diff(tree, against)`, `lock(recipe, previous=, update=, offline=) -> Lock`, `lock_status`, `read_lock`/`write_lock`, `bake(recipe, locked=, backend=Backend(kind), out=) -> tuple[Artifact, ...]`, `read_artifacts`, `measure(artifact) -> Measurements`, `deploy(artifact, using=Qemu()/Azure()/Gcp()) -> Deployment`, `doctor(backend)`, `load(path)`. Artifacts record the recipe, lockfile and tree digests; simulated (in-process) artifacts are refused by `measure`/`deploy` unless `allow_placeholder=True`.
- **CLI grammar**: `init`, `inspect`, `lint`, `compile`, `diff`, `lock`, `bake`, `measure`, `deploy`, `doctor`, `ci`. Every recipe command takes a repeatable `--variant` and runs on every variant without it. `--format auto|text|json|github|markdown` for review output (`auto` picks GitHub annotations under `GITHUB_ACTIONS=true`). `lint` reports resolution, fragment-check and compiler diagnostics together. `bake` is frozen whenever `build/tundravm.lock` exists, takes `--backend lima|nix|local|inprocess`, and reports progress (`-v`, `-q`, `--json-logs`). `measure` and `deploy` read `bake-result.json` (`--scheme`, `--target`, `--param KEY=VALUE`, `--allow-placeholder`). `ci` runs `lint --strict`, `compile --check` and `lock --check`. `init --ci github` writes a workflow that calls it.
- **Testing**: `tundravm.testing` works on lifecycle values: `assert_clean`/`assert_diagnostic` take `lint()` diagnostics, `assert_tree(tree, golden)` compares every path, byte, exec bit and symlink (`TUNDRAVM_UPDATE_GOLDEN=1` rewrites), `compile_tree`, `fake_bake`, `bake_in_process`, `fake_fragment`, `recipe_file`, `run_cli`. Pytest fixtures `recipe`, `compiled`, `run_cli`.
- Lockfile section digests (`profiles.<variant>.<section>`) and source pins (`fetches`); `lock --check` names the drifted sections, `lock --update NAME` re-resolves one source, `lock --offline` reuses pins.
- Measurement provenance: `Measurements.tool` names `measured-boot`, `dstack-mr` or `placeholder`; placeholders print a banner and emit `PlaceholderMeasurementWarning`.
- `StateError` (`E_STATE`) for a missing or unreadable `bake-result.json`, `LintError` (`E_LINT`) for a bake refused by the linter.
- `examples/surge-tdx-prover` is a declarative recipe that compiles byte-for-byte to the committed nethermind-tdx tree for `default`, `azure`, `gcp` and `devtools`; `examples/modules` holds `raiko()`, `taiko_client()` and `nethermind()`.

### Changed

- Lockfile version 1 → 2. Version 1 files still load; `lock --check` reports every section as added until you re-lock.
- Recipe files bind a `Recipe` to `recipe` (or `RECIPE`, or a `build()` factory). A module-level `backend` is the bake backend. `load_recipe()` returns the `Recipe`.
- `Recipe.base` defaults to `debian/trixie`.
- `compile()` recreates each variant directory, so stale files are removed.
- The `BuildBackend` protocol has `requirements()`, which `doctor` probes.

### Removed

- The fluent `Image`, `Profile` and `Module` API (`img.install()`, `img.service()`, `img.profile()`, `img.apply()`, `Module` subclasses, `MkosiOptions`, `SourceBuild`/`GitSource`/`HttpSource`/`GoBuild`/`CargoBuild`/`DotnetBuild`/`ScriptBuild`). It remains as internal lowering machinery (`tundravm._image`, `tundravm._modules`, ...), not as API. Use `Recipe`, `Fragment` and the declarations.
- The module classes `KeyGeneration`, `DiskEncryption`, `SecretDelivery`, `Tdxs`, `DevTools`, `AzurePlatform`, `GcpPlatform`: use the `Key`, `Disk` and `Secrets` declarations, the `tdxs()` and `devtools()` fragments, and `Variant(target="azure"|"gcp")`.
- CLI verbs `explain` (use `inspect`), `check` (use `lint`), `digest` (use `inspect --json`, key `digest`) and `new` (use `init`), with no aliases.
- `--profile`/`-p` and `--all-profiles` (use repeatable `--variant`; omit it for every variant), `bake --lock`/`--frozen`/`--force`, `measure --backend` (use `--scheme`), `deploy --memory`/`--cpus` (use `--param`), `measure --out`/`deploy --out` (pass the bake directory).
- `tundravm.testing.FakeModule` (use `fake_fragment`) and the `image`/`inprocess_image` fixtures (use `recipe`).
- `SPEC.md`, which described the fluent API. The design record is `docs/design/declarative-api.md`.

### Fixed

- `runtime-init.service` is registered once per variant, so compiling one variant and then all of them no longer fails with a duplicate service.
- A file removed from the recipe no longer lingers in the compiled tree or in `compile --check`.
