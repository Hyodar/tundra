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
- `examples/surge-tdx-prover` is a declarative recipe that compiles byte-for-byte to the committed nethermind-tdx tree for `default`, `azure`, `gcp` and `devtools`; `examples/fragments` holds `Raiko`, `TaikoClient` and `Nethermind`, and `examples/nethermind_tdx.py` holds `NethermindBase`.

### Changed

- Lockfile version 1 → 3. Per-variant sections are named `variants.<variant>.<section>` (version 2 said `profiles.`). Version 2 files still load with their sections renamed and digests unchanged; version 1 files load too, and `lock --check` reports every section as added until you re-lock.
- User-facing output says variant: `inspect` prints `variant=NAME`, `Parent:` and `Fragments:` (JSON keys `variant`, `parent`, `fragments`; Markdown heading `## Variant`), the bake summary has a `variant` column and ends `baked N variants`, and `lint --format json` and `bake --json-logs` use `variant`. The lint code `profile-empty` is now `variant-empty`.
- `Recipe.epoch` other than `0` or `None` emits `SourceDateEpoch=<epoch>` as well as `SOURCE_DATE_EPOCH`.
- Diagnostic hints name the declarative API and current commands (frozen-lock policy, missing backend, `source-unpinned`, `init-priority-collision`, `disk-key-undefined`, `secret-store-undefined`).
- Recipe files bind a `Recipe` to `recipe` (or `RECIPE`, or a `build()` factory). A module-level `backend` is the bake backend. `load_recipe()` returns the `Recipe`.
- `Recipe.base` defaults to `debian/trixie`.
- `compile()` recreates each variant directory, so stale files are removed.
- The `BuildBackend` protocol has `requirements()`, which `doctor` probes.

### Removed

- The fluent `Image`, `Profile` and `Module` API (`img.install()`, `img.service()`, `img.profile()`, `img.apply()`, `Module` subclasses, `MkosiOptions`, `SourceBuild`/`GitSource`/`HttpSource`/`GoBuild`/`CargoBuild`/`DotnetBuild`/`ScriptBuild`). It remains as internal lowering machinery (`tundravm._image`, `tundravm._modules`, ...), not as API. Use `Recipe`, `Fragment` and the declarations.
- `tundravm.modules` and its module classes: `KeyGeneration`, `DiskEncryption` and `SecretDelivery` (use the `Key`, `Disk` and `Secrets` declarations), `AzurePlatform` and `GcpPlatform` (use `Variant(target="azure"|"gcp")`). `Tdxs` and `DevTools` are now fragments in `tundravm.declarative.utils`, next to `EfiStub` and `Backports`.
- CLI verbs `explain` (use `inspect`), `check` (use `lint`), `digest` (use `inspect --json`, key `digest`) and `new` (use `init`), with no aliases.
- `--profile`/`-p` and `--all-profiles` (use repeatable `--variant`; omit it for every variant), `bake --lock`/`--frozen`/`--force`, `measure --backend` (use `--scheme`), `deploy --memory`/`--cpus` (use `--param`), `measure --out`/`deploy --out` (pass the bake directory).
- The lint rules `output-target-platform-mismatch`, `platform-target-missing` and `secret-undelivered`, which a declarative recipe cannot trigger, and `backend-missing`: a recipe holds no backend, and `bake` reports a missing one.
- `tundravm.builders` and `tundravm.fetch`. Builds are `Build(script=...)` or `Build(recipe=Go(...) | Cargo(...) | Dotnet(...))`, and `tundravm lock` resolves pins itself (`git ls-remote` for a git ref, the sha256 of the download for http).
- The `cbor2` dependency, which nothing used: tundravm has no runtime dependencies.
- `tundravm.testing.FakeModule` (use `fake_fragment`) and the `image`/`inprocess_image` fixtures (use `recipe`).
- `SPEC.md`, which described the fluent API. The design record is `docs/design/declarative-api.md`.

### Fixed

- `runtime-init.service` is registered once per variant, so compiling one variant and then all of them no longer fails with a duplicate service.
- A file removed from the recipe no longer lingers in the compiled tree or in `compile --check`.
- `tundravm ci` no longer reports false lock drift on recipes with runtime-init steps: its compile step works on a copy of the lowered recipe instead of changing what the lock step compares.
- A frozen bake of some variants against a lock of every variant no longer fails at `verify lockfile`.
